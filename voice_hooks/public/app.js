class VoiceHooksClient {
    constructor() {
        this.baseUrl = window.location.origin;
        this.debug = localStorage.getItem('voiceHooksDebug') === 'true';

        // Core UI
        this.refreshBtn = document.getElementById('refreshBtn');
        this.clearAllBtn = document.getElementById('clearAllBtn');
        this.utterancesList = document.getElementById('utterancesList');
        this.infoMessage = document.getElementById('infoMessage');
        this.listenBtn = document.getElementById('listenBtn');
        this.listenBtnText = document.getElementById('listenBtnText');
        this.listeningIndicator = document.getElementById('listeningIndicator');
        this.interimText = document.getElementById('interimText');

        // Confirmation UI
        this.pendingDraftText = document.getElementById('pendingDraftText');
        this.pendingDraftMeta = document.getElementById('pendingDraftMeta');
        this.confirmDraftBtn = document.getElementById('confirmDraftBtn');
        this.discardDraftBtn = document.getElementById('discardDraftBtn');

        // Network UI
        this.networkLinks = document.getElementById('networkLinks');
        this.copyPhoneUrlBtn = document.getElementById('copyPhoneUrlBtn');

        // Voice controls
        this.languageSelect = document.getElementById('languageSelect');
        this.voiceSelect = document.getElementById('voiceSelect');
        this.speechRateSlider = document.getElementById('speechRate');
        this.speechRateInput = document.getElementById('speechRateInput');
        this.testTTSBtn = document.getElementById('testTTSBtn');
        this.voiceResponsesToggle = document.getElementById('voiceResponsesToggle');
        this.voiceOptions = document.getElementById('voiceOptions');
        this.localVoicesGroup = document.getElementById('localVoicesGroup');
        this.cloudVoicesGroup = document.getElementById('cloudVoicesGroup');
        this.rateWarning = document.getElementById('rateWarning');
        this.systemVoiceInfo = document.getElementById('systemVoiceInfo');

        // Speech capture state
        this.isListening = false;
        this.mediaStream = null;
        this.mediaRecorder = null;
        this.mediaRecorderMimeType = null;
        this.recordingChunks = [];
        this.segmentTimeout = null;
        this.vadInterval = null;
        this.audioContext = null;
        this.analyser = null;
        this.analyserBuffer = null;
        this.segmentStartedAt = 0;
        this.lastSpeechAt = 0;
        this.segmentHasSpeech = false;
        this.isTranscribing = false;
        this.lastTranscription = { text: '', timestamp: 0 };
        this.capturePausedForSpeech = false;
        this.discardNextSegment = false;

        // Tunables for VAD/segmentation
        this.maxSegmentMs = 8000;
        this.minSegmentMs = 900;
        this.silenceMs = 750;
        this.minBlobBytes = 1800;
        this.vadThreshold = 0.015;

        // Draft confirmation state
        this.pendingDraft = '';

        // Speech synthesis state
        this.voices = [];
        this.selectedVoice = 'system';
        this.speechRate = 1.0;
        this.speechPitch = 1.0;
        this.ttsQueue = [];
        this.isSpeaking = false;

        this.hideLegacySendModeControls();
        this.initializeSpeechSynthesis();
        this.initializeTTSEvents();
        this.loadPreferences();
        this.setupEventListeners();
        this.updatePendingDraftUI('No draft command yet.', 'Speak a command in English to create a draft.');

        this.loadData();
        this.loadNetworkInfo();

        setInterval(() => this.loadData(), 2000);
    }

    hideLegacySendModeControls() {
        const legacyControls = document.querySelector('.send-mode-controls');
        if (legacyControls) {
            legacyControls.style.display = 'none';
        }
    }

    setupEventListeners() {
        if (this.refreshBtn) {
            this.refreshBtn.addEventListener('click', () => this.loadData());
        }

        if (this.clearAllBtn) {
            this.clearAllBtn.addEventListener('click', () => this.clearAllUtterances());
        }

        if (this.listenBtn) {
            this.listenBtn.addEventListener('click', () => this.toggleListening());
        }

        if (this.confirmDraftBtn) {
            this.confirmDraftBtn.addEventListener('click', () => this.confirmPendingDraft());
        }

        if (this.discardDraftBtn) {
            this.discardDraftBtn.addEventListener('click', () => this.discardPendingDraft(true));
        }

        if (this.copyPhoneUrlBtn) {
            this.copyPhoneUrlBtn.addEventListener('click', () => this.copyPrimaryPhoneUrl());
        }

        if (this.languageSelect) {
            this.languageSelect.addEventListener('change', () => {
                localStorage.setItem('selectedLanguage', this.languageSelect.value);
                this.populateVoiceList();
            });
        }

        if (this.voiceSelect) {
            this.voiceSelect.addEventListener('change', (event) => {
                this.selectedVoice = event.target.value;
                localStorage.setItem('selectedVoice', this.selectedVoice);
                this.updateVoiceWarnings();
            });
        }

        if (this.speechRateSlider && this.speechRateInput) {
            this.speechRateSlider.addEventListener('input', (event) => {
                this.speechRate = parseFloat(event.target.value);
                this.speechRateInput.value = this.speechRate.toFixed(1);
                localStorage.setItem('speechRate', this.speechRate.toString());
            });

            this.speechRateInput.addEventListener('input', (event) => {
                const parsed = parseFloat(event.target.value);
                if (Number.isNaN(parsed)) {
                    return;
                }

                this.speechRate = Math.max(0.5, Math.min(5, parsed));
                this.speechRateInput.value = this.speechRate.toFixed(1);
                this.speechRateSlider.value = this.speechRate.toString();
                localStorage.setItem('speechRate', this.speechRate.toString());
            });
        }

        if (this.testTTSBtn) {
            this.testTTSBtn.addEventListener('click', () => {
                this.enqueueSpeech('Voice check. Your assistant is ready.', {
                    interrupt: true,
                    force: true,
                });
            });
        }

        if (this.voiceResponsesToggle) {
            this.voiceResponsesToggle.addEventListener('change', (event) => {
                const enabled = event.target.checked;
                localStorage.setItem('voiceResponsesEnabled', String(enabled));
                this.updateVoiceOptionsVisibility();
                this.updateVoicePreferences();
            });
        }
    }

    async loadData() {
        try {
            const response = await fetch(`${this.baseUrl}/api/utterances?limit=30`);
            if (!response.ok) {
                return;
            }

            const data = await response.json();
            this.updateUtterancesList(data.utterances || []);
        } catch (error) {
            this.debugLog('Failed to load utterances', error);
        }
    }

    async loadNetworkInfo() {
        if (!this.networkLinks) {
            return;
        }

        try {
            const response = await fetch(`${this.baseUrl}/api/network-info`);
            if (!response.ok) {
                return;
            }

            const data = await response.json();
            const urls = Array.isArray(data.urls) ? data.urls : [];
            const phoneUrls = urls.filter((url) => !url.includes('localhost'));

            if (phoneUrls.length === 0) {
                this.networkLinks.innerHTML = `<div class="empty-state">No LAN IP detected. Use ${this.escapeHtml(this.baseUrl)} on this machine.</div>`;
                if (this.copyPhoneUrlBtn) {
                    this.copyPhoneUrlBtn.style.display = 'none';
                }
                return;
            }

            this.primaryPhoneUrl = data.phoneUrl || phoneUrls[0];
            this.networkLinks.innerHTML = phoneUrls
                .map((url) => `<div><a href="${this.escapeHtml(url)}" target="_blank" rel="noreferrer">${this.escapeHtml(url)}</a></div>`)
                .join('');

            if (this.copyPhoneUrlBtn) {
                this.copyPhoneUrlBtn.style.display = '';
            }
        } catch (error) {
            this.debugLog('Failed to load network info', error);
        }
    }

    async copyPrimaryPhoneUrl() {
        if (!this.primaryPhoneUrl || !navigator.clipboard) {
            return;
        }

        try {
            await navigator.clipboard.writeText(this.primaryPhoneUrl);
            if (this.copyPhoneUrlBtn) {
                this.copyPhoneUrlBtn.textContent = 'Copied';
                setTimeout(() => {
                    this.copyPhoneUrlBtn.textContent = 'Copy';
                }, 1500);
            }
        } catch (error) {
            this.debugLog('Failed to copy phone URL', error);
        }
    }

    updateUtterancesList(utterances) {
        if (!this.utterancesList || !this.infoMessage) {
            return;
        }

        if (utterances.length === 0) {
            this.utterancesList.innerHTML = '<div class="empty-state">Nothing yet.</div>';
            this.infoMessage.style.display = 'none';
            return;
        }

        const allPending = utterances.every((utterance) => utterance.status === 'pending');
        this.infoMessage.style.display = allPending ? 'block' : 'none';

        this.utterancesList.innerHTML = utterances
            .map((utterance) => {
                const safeText = this.escapeHtml(utterance.text);
                const status = this.escapeHtml(utterance.status || 'pending');
                const timestamp = this.escapeHtml(this.formatTimestamp(utterance.timestamp));
                const deleteButton = utterance.status === 'pending'
                    ? `<button class="delete-btn" data-id="${utterance.id}" title="Delete">&times;</button>`
                    : '';

                return `
                    <div class="utterance-item">
                        <div class="utterance-text">${safeText}</div>
                        <div class="utterance-meta">
                            <div>${timestamp}</div>
                            <div class="utterance-status status-${status}">${status.toUpperCase()}</div>
                            ${deleteButton}
                        </div>
                    </div>
                `;
            })
            .join('');

        this.utterancesList.querySelectorAll('.delete-btn').forEach((button) => {
            button.addEventListener('click', (event) => {
                const id = event.currentTarget.dataset.id;
                if (id) {
                    this.deleteUtterance(id);
                }
            });
        });
    }

    formatTimestamp(timestamp) {
        if (!timestamp) {
            return '';
        }

        const date = new Date(timestamp);
        return date.toLocaleTimeString();
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = String(text ?? '');
        return div.innerHTML;
    }

    toggleListening() {
        if (this.isListening) {
            this.stopListening();
        } else {
            this.startListening();
        }
    }

    async startListening() {
        if (this.isListening) {
            return;
        }

        if (!window.MediaRecorder || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            alert('This browser does not support microphone recording with MediaRecorder.');
            return;
        }

        try {
            await this.ensureMediaStream();
            this.isListening = true;
            this.setListeningUI(true);
            this.setInterimText('Listening for English voice commands...');
            await this.updateVoiceInputState(true);
            this.startRecordingSegment();
        } catch (error) {
            console.error('Failed to start listening:', error);
            alert('Microphone access failed. Please allow mic permission and try again.');
            this.cleanupMediaResources();
            this.setListeningUI(false);
        }
    }

    async stopListening() {
        if (!this.isListening) {
            return;
        }

        this.isListening = false;
        this.capturePausedForSpeech = false;
        this.discardNextSegment = false;
        this.clearSegmentTimers();

        if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
            this.mediaRecorder.stop();
        }

        this.cleanupMediaResources();
        this.setListeningUI(false);
        this.setInterimText('Listening stopped. Click Start Listening to resume.');
        await this.updateVoiceInputState(false);
    }

    async ensureMediaStream() {
        if (this.mediaStream) {
            return;
        }

        this.mediaStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
                channelCount: 1,
            },
        });

        this.mediaRecorderMimeType = this.pickRecorderMimeType();

        const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
        if (AudioContextCtor) {
            this.audioContext = new AudioContextCtor();
            const source = this.audioContext.createMediaStreamSource(this.mediaStream);
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 2048;
            this.analyserBuffer = new Uint8Array(this.analyser.fftSize);
            source.connect(this.analyser);
        }
    }

    pickRecorderMimeType() {
        const candidates = [
            'audio/webm;codecs=opus',
            'audio/webm',
            'audio/mp4',
            'audio/ogg;codecs=opus',
        ];

        for (const candidate of candidates) {
            if (MediaRecorder.isTypeSupported(candidate)) {
                return candidate;
            }
        }

        return '';
    }

    startRecordingSegment() {
        if (!this.isListening || this.isTranscribing) {
            return;
        }

        if (!this.mediaStream) {
            return;
        }

        if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
            return;
        }

        const options = this.mediaRecorderMimeType ? { mimeType: this.mediaRecorderMimeType } : undefined;
        this.mediaRecorder = new MediaRecorder(this.mediaStream, options);
        this.recordingChunks = [];
        this.segmentStartedAt = Date.now();
        this.lastSpeechAt = 0;
        this.segmentHasSpeech = false;
        if (!this.analyser) {
            // Fallback: if VAD is unavailable, always transcribe recorded segments.
            this.segmentHasSpeech = true;
        }

        this.mediaRecorder.ondataavailable = (event) => {
            if (event.data && event.data.size > 0) {
                this.recordingChunks.push(event.data);
            }
        };

        this.mediaRecorder.onerror = (event) => {
            console.error('MediaRecorder error:', event.error);
            this.setInterimText('Recording error. Restarting...');
            this.clearSegmentTimers();
            if (this.isListening) {
                setTimeout(() => this.startRecordingSegment(), 500);
            }
        };

        this.mediaRecorder.onstop = async () => {
            const blob = new Blob(this.recordingChunks, {
                type: this.mediaRecorderMimeType || 'audio/webm',
            });

            this.recordingChunks = [];
            this.clearSegmentTimers();

            await this.handleRecordedSegment(blob);

            if (this.isListening && !this.capturePausedForSpeech) {
                this.startRecordingSegment();
            }
        };

        this.mediaRecorder.start(250);

        this.segmentTimeout = setTimeout(() => {
            if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
                this.mediaRecorder.stop();
            }
        }, this.maxSegmentMs);

        this.vadInterval = setInterval(() => {
            this.checkVoiceActivity();
        }, 100);
    }

    checkVoiceActivity() {
        if (!this.analyser || !this.analyserBuffer || !this.mediaRecorder || this.mediaRecorder.state === 'inactive') {
            return;
        }

        this.analyser.getByteTimeDomainData(this.analyserBuffer);

        let sum = 0;
        for (let i = 0; i < this.analyserBuffer.length; i += 1) {
            const centered = (this.analyserBuffer[i] - 128) / 128;
            sum += centered * centered;
        }

        const rms = Math.sqrt(sum / this.analyserBuffer.length);
        const now = Date.now();

        if (rms > this.vadThreshold) {
            this.segmentHasSpeech = true;
            this.lastSpeechAt = now;
        }

        const elapsed = now - this.segmentStartedAt;
        const shouldStopForSilence =
            this.segmentHasSpeech &&
            elapsed > this.minSegmentMs &&
            this.lastSpeechAt > 0 &&
            now - this.lastSpeechAt > this.silenceMs;

        if (shouldStopForSilence && this.mediaRecorder.state !== 'inactive') {
            this.mediaRecorder.stop();
        }
    }

    clearSegmentTimers() {
        if (this.segmentTimeout) {
            clearTimeout(this.segmentTimeout);
            this.segmentTimeout = null;
        }

        if (this.vadInterval) {
            clearInterval(this.vadInterval);
            this.vadInterval = null;
        }
    }

    cleanupMediaResources() {
        if (this.mediaStream) {
            this.mediaStream.getTracks().forEach((track) => track.stop());
            this.mediaStream = null;
        }

        if (this.audioContext) {
            this.audioContext.close().catch(() => {});
            this.audioContext = null;
        }

        this.analyser = null;
        this.analyserBuffer = null;
        this.mediaRecorder = null;
    }

    async handleRecordedSegment(blob) {
        if (this.discardNextSegment) {
            this.discardNextSegment = false;
            return;
        }

        if (!blob || blob.size < this.minBlobBytes || !this.segmentHasSpeech) {
            return;
        }

        if (this.isTranscribing) {
            return;
        }

        this.isTranscribing = true;
        this.setInterimText('Transcribing...');

        try {
            const text = await this.transcribeBlob(blob);
            if (!text) {
                this.setInterimText('No clear speech detected.');
                return;
            }

            const now = Date.now();
            if (this.lastTranscription.text === text && now - this.lastTranscription.timestamp < 2000) {
                this.debugLog('Skipping duplicate transcription:', text);
                return;
            }

            this.lastTranscription = { text, timestamp: now };
            this.setInterimText(`Heard: ${text}`);
            await this.handleRecognizedText(text);
        } catch (error) {
            console.error('Transcription failed:', error);
            this.setInterimText('Transcription failed. Try speaking again.');
        } finally {
            this.isTranscribing = false;
        }
    }

    async transcribeBlob(blob) {
        const formData = new FormData();
        const extension = this.getAudioExtension(this.mediaRecorderMimeType || blob.type);
        const filename = `command.${extension}`;
        formData.append('audio', blob, filename);
        formData.append('hints', this.pendingDraft || '');

        const response = await fetch(`${this.baseUrl}/api/transcribe`, {
            method: 'POST',
            body: formData,
        });

        const data = await response.json();

        if (!response.ok) {
            const message = data?.error || 'Transcription request failed';
            throw new Error(message);
        }

        return (data.text || '').trim();
    }

    getAudioExtension(mimeType) {
        if (!mimeType) {
            return 'webm';
        }

        if (mimeType.includes('ogg')) {
            return 'ogg';
        }

        if (mimeType.includes('mp4')) {
            return 'm4a';
        }

        return 'webm';
    }

    async handleRecognizedText(text) {
        const normalized = this.normalizeTranscript(text);
        if (!normalized) {
            return;
        }

        if (!this.pendingDraft) {
            this.pendingDraft = normalized;
            this.updatePendingDraftUI(
                this.pendingDraft,
                'Say "confirm and execute" to run, or say "change to ..." to edit.'
            );
            this.enqueueSpeech(
                `I heard: ${this.pendingDraft}. Say confirm and execute to run it, or say change to followed by your correction.`,
                { interrupt: true, force: true }
            );
            return;
        }

        if (this.isConfirmPhrase(normalized)) {
            await this.confirmPendingDraft();
            return;
        }

        if (this.isCancelPhrase(normalized)) {
            this.discardPendingDraft(false);
            this.enqueueSpeech('Draft canceled. Please say your next command.', {
                interrupt: true,
                force: true,
            });
            return;
        }

        const correction = this.extractCorrection(normalized);
        if (correction) {
            this.pendingDraft = correction;
            this.updatePendingDraftUI(
                this.pendingDraft,
                'Draft updated. Say "confirm and execute" when ready.'
            );
            this.enqueueSpeech(`Updated draft: ${this.pendingDraft}. Say confirm and execute when ready.`, {
                interrupt: true,
                force: true,
            });
            return;
        }

        // If user says a new sentence while draft exists, treat it as a replacement.
        this.pendingDraft = normalized;
        this.updatePendingDraftUI(
            this.pendingDraft,
            'Draft replaced. Say "confirm and execute" to run.'
        );
        this.enqueueSpeech(`I will use: ${this.pendingDraft}. Say confirm and execute when ready.`, {
            interrupt: true,
            force: true,
        });
    }

    normalizeTranscript(text) {
        if (!text) {
            return '';
        }

        return text
            .replace(/\s+/g, ' ')
            .trim();
    }

    isConfirmPhrase(text) {
        const normalized = text.toLowerCase();
        const confirmPatterns = [
            /\bconfirm( it)?\b/,
            /\bconfirm and execute\b/,
            /\bexecute( now)?\b/,
            /\brun( it| this)?\b/,
            /\bgo ahead\b/,
            /\byes\b/,
            /\bok(?:ay)?\b/,
            /\bthat'?s (right|correct)\b/,
            /\bcorrect\b/,
            /\bdo it\b/,
        ];

        return confirmPatterns.some((pattern) => pattern.test(normalized));
    }

    isCancelPhrase(text) {
        const normalized = text.toLowerCase();
        return /\b(cancel|discard|never mind|start over|drop it)\b/.test(normalized);
    }

    extractCorrection(text) {
        const normalized = text.trim();
        const patterns = [
            /^change(?: it)? to\s+(.+)$/i,
            /^update(?: it)? to\s+(.+)$/i,
            /^replace(?: it)? with\s+(.+)$/i,
            /^correction[:\s]+(.+)$/i,
            /^actually\s+(.+)$/i,
            /^no[,\s]+use\s+(.+)$/i,
        ];

        for (const pattern of patterns) {
            const match = normalized.match(pattern);
            if (match && match[1]) {
                return this.normalizeTranscript(match[1]);
            }
        }

        return '';
    }

    async confirmPendingDraft() {
        if (!this.pendingDraft) {
            this.updatePendingDraftUI('No draft command yet.', 'Speak a command in English to create a draft.');
            return;
        }

        const command = this.pendingDraft;
        const success = await this.sendVoiceUtterance(command);

        if (!success) {
            this.enqueueSpeech('I could not send the command. Please try again.', {
                interrupt: true,
                force: true,
            });
            return;
        }

        this.pendingDraft = '';
        this.updatePendingDraftUI('No draft command yet.', `Sent: ${command}`);
        this.enqueueSpeech(`Executing: ${command}`, {
            interrupt: true,
            force: true,
        });
    }

    discardPendingDraft(withVoiceFeedback) {
        this.pendingDraft = '';
        this.updatePendingDraftUI('No draft command yet.', 'Draft cleared. Speak a new command.');

        if (withVoiceFeedback) {
            this.enqueueSpeech('Draft cleared. Please speak your next command.', {
                interrupt: true,
                force: true,
            });
        }
    }

    updatePendingDraftUI(title, subtitle) {
        if (this.pendingDraftText) {
            this.pendingDraftText.textContent = title;
        }

        if (this.pendingDraftMeta) {
            this.pendingDraftMeta.textContent = subtitle;
        }

        const hasDraft = Boolean(this.pendingDraft);
        if (this.confirmDraftBtn) {
            this.confirmDraftBtn.disabled = !hasDraft;
        }

        if (this.discardDraftBtn) {
            this.discardDraftBtn.disabled = !hasDraft;
        }
    }

    async sendVoiceUtterance(text) {
        const trimmed = text.trim();
        if (!trimmed) {
            return false;
        }

        try {
            const response = await fetch(`${this.baseUrl}/api/potential-utterances`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    text: trimmed,
                    timestamp: new Date().toISOString(),
                }),
            });

            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                this.debugLog('Failed to queue utterance', data);
                return false;
            }

            this.loadData();
            return true;
        } catch (error) {
            this.debugLog('Failed to queue utterance', error);
            return false;
        }
    }

    async clearAllUtterances() {
        if (!this.clearAllBtn) {
            return;
        }

        this.clearAllBtn.disabled = true;
        this.clearAllBtn.textContent = 'Clearing...';

        try {
            const response = await fetch(`${this.baseUrl}/api/utterances`, {
                method: 'DELETE',
            });

            if (response.ok) {
                this.loadData();
            }
        } catch (error) {
            this.debugLog('Failed to clear utterances', error);
        } finally {
            this.clearAllBtn.disabled = false;
            this.clearAllBtn.textContent = 'Clear All';
        }
    }

    async deleteUtterance(id) {
        try {
            const response = await fetch(`${this.baseUrl}/api/utterances/${id}`, {
                method: 'DELETE',
            });

            if (response.ok) {
                this.loadData();
            }
        } catch (error) {
            this.debugLog('Failed to delete utterance', error);
        }
    }

    setListeningUI(active) {
        if (!this.listenBtn || !this.listenBtnText || !this.listeningIndicator) {
            return;
        }

        if (active) {
            this.listenBtn.classList.add('listening');
            this.listenBtnText.textContent = 'Stop Listening';
            this.listeningIndicator.classList.add('active');
        } else {
            this.listenBtn.classList.remove('listening');
            this.listenBtnText.textContent = 'Start Listening';
            this.listeningIndicator.classList.remove('active');
        }
    }

    setInterimText(text) {
        if (!this.interimText) {
            return;
        }

        this.interimText.textContent = text;
        this.interimText.classList.toggle('active', Boolean(text));
    }

    initializeTTSEvents() {
        this.eventSource = new EventSource(`${this.baseUrl}/api/tts-events`);

        this.eventSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === 'speak' && data.text) {
                    this.enqueueSpeech(data.text, {
                        interrupt: true,
                        force: false,
                    });
                } else if (data.type === 'waitStatus') {
                    this.handleWaitStatus(Boolean(data.isWaiting));
                }
            } catch (error) {
                this.debugLog('Failed to parse SSE message', error);
            }
        };

        this.eventSource.onopen = () => {
            this.syncStateWithServer();
        };

        this.eventSource.onerror = (error) => {
            this.debugLog('SSE connection error', error);
        };
    }

    handleWaitStatus(isWaiting) {
        if (!this.listeningIndicator) {
            return;
        }

        const textNode = this.listeningIndicator.querySelector('span');
        if (!textNode) {
            return;
        }

        if (isWaiting) {
            this.listeningIndicator.classList.add('waiting-mode');
            textNode.textContent = 'Gemini is waiting for your next voice command';
        } else {
            this.listeningIndicator.classList.remove('waiting-mode');
            textNode.textContent = 'Listening...';
        }
    }

    initializeSpeechSynthesis() {
        if (!window.speechSynthesis) {
            this.debugLog('speechSynthesis is unavailable in this browser');
            return;
        }

        const loadVoices = () => {
            const available = window.speechSynthesis.getVoices();
            const deduplicated = [];
            const seen = new Set();

            available.forEach((voice) => {
                const key = `${voice.name}-${voice.lang}-${voice.voiceURI}`;
                if (!seen.has(key)) {
                    seen.add(key);
                    deduplicated.push(voice);
                }
            });

            this.voices = deduplicated;
            this.populateVoiceList();
        };

        loadVoices();
        setTimeout(loadVoices, 120);

        if (window.speechSynthesis.onvoiceschanged !== undefined) {
            window.speechSynthesis.onvoiceschanged = loadVoices;
        }
    }

    populateVoiceList() {
        if (!this.voiceSelect || !this.localVoicesGroup || !this.cloudVoicesGroup || !this.languageSelect) {
            return;
        }

        const selectedLanguage = this.languageSelect.value || 'en-US';
        this.localVoicesGroup.innerHTML = '';
        this.cloudVoicesGroup.innerHTML = '';

        this.populateLanguageFilter();

        this.voices.forEach((voice, index) => {
            const isEnglish = voice.lang.toLowerCase().startsWith('en');
            if (!isEnglish) {
                return;
            }

            if (selectedLanguage !== 'all' && voice.lang !== selectedLanguage) {
                return;
            }

            const option = document.createElement('option');
            option.value = `browser:${index}`;
            option.textContent = `${voice.name} (${voice.lang})`;

            if (voice.localService) {
                this.localVoicesGroup.appendChild(option);
            } else {
                this.cloudVoicesGroup.appendChild(option);
            }
        });

        const savedVoice = localStorage.getItem('selectedVoice');
        if (savedVoice) {
            this.selectedVoice = savedVoice;
            this.voiceSelect.value = savedVoice;
        } else {
            this.selectedVoice = this.findDefaultBrowserVoice();
            this.voiceSelect.value = this.selectedVoice;
        }

        this.updateVoiceWarnings();
    }

    findDefaultBrowserVoice() {
        const preferredIndex = this.voices.findIndex((voice) => {
            const name = voice.name.toLowerCase();
            return (
                name.includes('google') &&
                name.includes('english') &&
                voice.lang.toLowerCase().startsWith('en')
            );
        });

        if (preferredIndex >= 0) {
            return `browser:${preferredIndex}`;
        }

        const fallbackIndex = this.voices.findIndex((voice) => voice.lang.toLowerCase().startsWith('en'));
        if (fallbackIndex >= 0) {
            return `browser:${fallbackIndex}`;
        }

        return 'system';
    }

    populateLanguageFilter() {
        if (!this.languageSelect) {
            return;
        }

        const saved = localStorage.getItem('selectedLanguage') || 'en-US';
        const languages = new Set(['all', 'en-US']);

        this.voices.forEach((voice) => {
            if (voice.lang.toLowerCase().startsWith('en')) {
                languages.add(voice.lang);
            }
        });

        this.languageSelect.innerHTML = '';
        Array.from(languages)
            .sort()
            .forEach((lang) => {
                const option = document.createElement('option');
                option.value = lang;
                option.textContent = lang === 'all' ? 'All English Voices' : lang;
                this.languageSelect.appendChild(option);
            });

        this.languageSelect.value = saved;
        if (this.languageSelect.value !== saved) {
            this.languageSelect.value = 'en-US';
        }
    }

    updateVoiceWarnings() {
        if (!this.rateWarning || !this.systemVoiceInfo) {
            return;
        }

        if (this.selectedVoice === 'system') {
            this.systemVoiceInfo.style.display = 'flex';
            this.rateWarning.style.display = 'none';
            return;
        }

        if (this.selectedVoice.startsWith('browser:')) {
            const index = parseInt(this.selectedVoice.substring(8), 10);
            const voice = this.voices[index];

            if (!voice) {
                this.rateWarning.style.display = 'none';
                this.systemVoiceInfo.style.display = 'none';
                return;
            }

            this.rateWarning.style.display = voice.name.toLowerCase().includes('google') ? 'flex' : 'none';
            this.systemVoiceInfo.style.display = voice.localService ? 'flex' : 'none';
            return;
        }

        this.rateWarning.style.display = 'none';
        this.systemVoiceInfo.style.display = 'none';
    }

    loadPreferences() {
        const storedVoiceResponses = localStorage.getItem('voiceResponsesEnabled');
        const voiceResponsesEnabled = storedVoiceResponses === null ? true : storedVoiceResponses === 'true';

        if (this.voiceResponsesToggle) {
            this.voiceResponsesToggle.checked = voiceResponsesEnabled;
        }

        if (storedVoiceResponses === null) {
            localStorage.setItem('voiceResponsesEnabled', 'true');
        }

        const storedRate = localStorage.getItem('speechRate');
        if (storedRate && this.speechRateInput && this.speechRateSlider) {
            this.speechRate = Math.max(0.5, Math.min(5, parseFloat(storedRate)));
            this.speechRateInput.value = this.speechRate.toFixed(1);
            this.speechRateSlider.value = this.speechRate.toString();
        }

        const storedLanguage = localStorage.getItem('selectedLanguage');
        if (storedLanguage && this.languageSelect) {
            this.languageSelect.value = storedLanguage;
        }

        const storedVoice = localStorage.getItem('selectedVoice');
        if (storedVoice) {
            this.selectedVoice = storedVoice;
        }

        this.updateVoiceOptionsVisibility();
        this.updateVoicePreferences();
    }

    updateVoiceOptionsVisibility() {
        if (!this.voiceOptions || !this.voiceResponsesToggle) {
            return;
        }

        this.voiceOptions.style.display = this.voiceResponsesToggle.checked ? 'flex' : 'none';
    }

    async updateVoicePreferences() {
        if (!this.voiceResponsesToggle) {
            return;
        }

        try {
            await fetch(`${this.baseUrl}/api/voice-preferences`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    voiceResponsesEnabled: this.voiceResponsesToggle.checked,
                }),
            });
        } catch (error) {
            this.debugLog('Failed to update voice preferences', error);
        }
    }

    async updateVoiceInputState(active) {
        try {
            await fetch(`${this.baseUrl}/api/voice-input-state`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ active }),
            });
        } catch (error) {
            this.debugLog('Failed to update voice input state', error);
        }
    }

    async syncStateWithServer() {
        await this.updateVoicePreferences();

        if (this.isListening) {
            await this.updateVoiceInputState(true);
        }
    }

    enqueueSpeech(text, options = {}) {
        const { interrupt = false, force = false } = options;

        if (!text || !text.trim()) {
            return;
        }

        if (!force && this.voiceResponsesToggle && !this.voiceResponsesToggle.checked) {
            return;
        }

        const sanitized = this.sanitizeSpeechText(text);
        const chunks = this.splitSpeechChunks(sanitized, 220);
        if (chunks.length === 0) {
            return;
        }

        if (interrupt) {
            this.ttsQueue = [];
            this.cancelCurrentSpeech();
        }

        if (this.isListening && !this.capturePausedForSpeech) {
            this.capturePausedForSpeech = true;
            if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
                this.discardNextSegment = true;
                this.mediaRecorder.stop();
            }
        }

        this.ttsQueue.push(...chunks);

        if (!this.isSpeaking) {
            this.processSpeechQueue();
        }
    }

    cancelCurrentSpeech() {
        if (window.speechSynthesis) {
            window.speechSynthesis.cancel();
        }
        this.isSpeaking = false;
    }

    async processSpeechQueue() {
        if (this.isSpeaking) {
            return;
        }

        while (this.ttsQueue.length > 0) {
            const chunk = this.ttsQueue.shift();
            this.isSpeaking = true;

            try {
                if (this.selectedVoice === 'system') {
                    await this.speakSystemChunk(chunk);
                } else {
                    await this.speakBrowserChunk(chunk);
                }
            } catch (error) {
                this.debugLog('Speech playback failed', error);
            }

            this.isSpeaking = false;
        }

        if (this.capturePausedForSpeech && this.isListening && !this.isTranscribing) {
            this.capturePausedForSpeech = false;
            this.startRecordingSegment();
        } else if (!this.isListening) {
            this.capturePausedForSpeech = false;
        }
    }

    async speakSystemChunk(text) {
        const response = await fetch(`${this.baseUrl}/api/speak-system`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                text,
                rate: Math.round(this.speechRate * 150),
            }),
        });

        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            throw new Error(payload.error || 'System voice failed');
        }
    }

    speakBrowserChunk(text) {
        return new Promise((resolve) => {
            if (!window.speechSynthesis) {
                resolve();
                return;
            }

            const utterance = new SpeechSynthesisUtterance(text);
            utterance.rate = this.speechRate;
            utterance.pitch = this.speechPitch;

            if (this.selectedVoice && this.selectedVoice.startsWith('browser:')) {
                const index = parseInt(this.selectedVoice.substring(8), 10);
                if (this.voices[index]) {
                    utterance.voice = this.voices[index];
                }
            }

            utterance.onend = () => resolve();
            utterance.onerror = () => resolve();
            window.speechSynthesis.speak(utterance);
        });
    }

    sanitizeSpeechText(text) {
        let cleaned = String(text);

        cleaned = cleaned.replace(/```[\s\S]*?```/g, '');
        cleaned = cleaned.replace(/`([^`]+)`/g, '$1');
        cleaned = cleaned.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1');
        cleaned = cleaned.replace(/^\s*[#>*-]+\s*/gm, '');
        cleaned = cleaned.replace(/\s+/g, ' ').trim();

        return cleaned;
    }

    splitSpeechChunks(text, maxLength) {
        if (!text) {
            return [];
        }

        const sentences = text.match(/[^.!?]+[.!?]?/g) || [text];
        const chunks = [];
        let current = '';

        sentences.forEach((sentence) => {
            if (!sentence) {
                return;
            }

            if ((current + ' ' + sentence).trim().length <= maxLength) {
                current = `${current} ${sentence}`.trim();
                return;
            }

            if (current) {
                chunks.push(current);
            }

            if (sentence.length <= maxLength) {
                current = sentence;
                return;
            }

            let remaining = sentence;
            while (remaining.length > maxLength) {
                chunks.push(remaining.slice(0, maxLength));
                remaining = remaining.slice(maxLength).trim();
            }
            current = remaining;
        });

        if (current) {
            chunks.push(current);
        }

        return chunks;
    }

    debugLog(...args) {
        if (this.debug) {
            console.log(...args);
        }
    }
}

document.addEventListener('DOMContentLoaded', () => {
    new VoiceHooksClient();
});
