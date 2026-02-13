class MessengerClient {
    constructor() {
        this.baseUrl = window.location.origin;

        // Conversation elements
        this.conversationMessages = document.getElementById('conversationMessages');
        this.conversationContainer = document.getElementById('conversationContainer');

        // Text input elements
        this.messageInput = document.getElementById('messageInput');
        this.micBtn = document.getElementById('micBtn');

        // Send mode controls
        this.sendModeRadios = document.querySelectorAll('input[name="sendMode"]');
        this.triggerWordInputContainer = document.getElementById('triggerWordInputContainer');
        this.triggerWordInput = document.getElementById('triggerWordInput');

        // Settings
        this.settingsToggleHeader = document.getElementById('settingsToggleHeader');
        this.settingsContent = document.getElementById('settingsContent');
        this.voiceResponsesToggle = document.getElementById('voiceResponsesToggle');
        this.voiceOptions = document.getElementById('voiceOptions');
        this.languageSelect = document.getElementById('languageSelect');
        this.voiceSelect = document.getElementById('voiceSelect');
        this.localVoicesGroup = document.getElementById('localVoicesGroup');
        this.cloudVoicesGroup = document.getElementById('cloudVoicesGroup');
        this.speechRateSlider = document.getElementById('speechRate');
        this.speechRateInput = document.getElementById('speechRateInput');
        this.testTTSBtn = document.getElementById('testTTSBtn');
        this.rateWarning = document.getElementById('rateWarning');
        this.systemVoiceInfo = document.getElementById('systemVoiceInfo');

        // State
        this.sendMode = 'automatic'; // 'automatic' or 'trigger'
        this.triggerWord = 'send';
        this.isListening = false;
        this.isInterimText = false;
        this.accumulatedText = ''; // For trigger word mode
        this.debug = localStorage.getItem('voiceHooksDebug') === 'true';

        // TTS state
        this.voices = [];
        this.selectedVoice = 'system';
        this.speechRate = 1.0;
        this.speechPitch = 1.0;

        // Initialize
        this.initializeSpeechRecognition();
        this.initializeSpeechSynthesis();
        this.initializeTTSEvents();
        this.setupEventListeners();
        this.loadPreferences();
        this.loadData();

        // Auto-refresh every 2 seconds
        setInterval(() => this.loadData(), 2000);
    }

    debugLog(...args) {
        if (this.debug) {
            console.log(...args);
        }
    }

    initializeSpeechSynthesis() {
        // Check for browser support
        if (!window.speechSynthesis) {
            console.warn('Speech synthesis not supported in this browser');
            return;
        }

        // Get available voices
        this.voices = [];

        // Enhanced voice loading with deduplication
        const loadVoices = () => {
            const voices = window.speechSynthesis.getVoices();

            // Deduplicate voices - keep the first occurrence of each unique voice
            const deduplicatedVoices = [];
            const seen = new Set();

            voices.forEach(voice => {
                // Create a unique key based on name, language, and URI
                const key = `${voice.name}-${voice.lang}-${voice.voiceURI}`;
                if (!seen.has(key)) {
                    seen.add(key);
                    deduplicatedVoices.push(voice);
                }
            });

            this.voices = deduplicatedVoices;
            this.populateVoiceList();
        };

        // Load voices initially and with a delayed retry for reliability
        loadVoices();
        setTimeout(loadVoices, 100);

        // Set up voice change listener
        if (window.speechSynthesis.onvoiceschanged !== undefined) {
            window.speechSynthesis.onvoiceschanged = loadVoices;
        }
    }

    initializeTTSEvents() {
        // Connect to SSE for TTS events
        this.eventSource = new EventSource(`${this.baseUrl}/api/tts-events`);

        this.eventSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);

                if (data.type === 'speak' && data.text) {
                    this.speakText(data.text);
                } else if (data.type === 'waitStatus') {
                    this.handleWaitStatus(data.isWaiting);
                }
            } catch (error) {
                console.error('Failed to parse TTS event:', error);
            }
        };

        this.eventSource.onerror = (error) => {
            console.error('SSE connection error:', error);
        };
    }

    handleWaitStatus(isWaiting) {
        const waitingIndicator = document.getElementById('waitingIndicator');
        if (waitingIndicator) {
            waitingIndicator.style.display = isWaiting ? 'block' : 'none';
            if (isWaiting) {
                this.scrollToBottom();
            }
        }
    }

    async speakText(text) {
        // Check if we should use system voice
        if (this.selectedVoice === 'system') {
            // Use Mac system voice via server
            try {
                const response = await fetch(`${this.baseUrl}/api/speak-system`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        text: text,
                        rate: Math.round(this.speechRate * 150) // Convert rate to words per minute
                    }),
                });

                if (!response.ok) {
                    const error = await response.json();
                    console.error('Failed to speak via system voice:', error);
                }
            } catch (error) {
                console.error('Failed to call speak-system API:', error);
            }
        } else {
            // Use browser voice
            if (!window.speechSynthesis) {
                console.error('Speech synthesis not available');
                return;
            }

            // Cancel any ongoing speech
            window.speechSynthesis.cancel();

            // Create utterance
            const utterance = new SpeechSynthesisUtterance(text);

            // Set voice if using browser voice
            if (this.selectedVoice && this.selectedVoice.startsWith('browser:')) {
                const voiceIndex = parseInt(this.selectedVoice.substring(8));
                if (this.voices[voiceIndex]) {
                    utterance.voice = this.voices[voiceIndex];
                }
            }

            // Set speech properties
            utterance.rate = this.speechRate;
            utterance.pitch = this.speechPitch;

            // Event handlers
            utterance.onstart = () => {
                this.debugLog('Started speaking:', text);
            };

            utterance.onend = () => {
                this.debugLog('Finished speaking');
            };

            utterance.onerror = (event) => {
                console.error('Speech synthesis error:', event);
            };

            // Speak the text
            window.speechSynthesis.speak(utterance);
        }
    }

    loadPreferences() {
        // Load voice responses preference from localStorage
        const savedVoiceResponses = localStorage.getItem('voiceResponsesEnabled');
        if (savedVoiceResponses !== null) {
            const enabled = savedVoiceResponses === 'true';
            this.voiceResponsesToggle.checked = enabled;
            this.voiceOptions.style.display = enabled ? 'block' : 'none';
            this.updateVoiceResponses(enabled);
        }

        // Load voice selection
        const savedVoice = localStorage.getItem('selectedVoice');
        if (savedVoice) {
            this.selectedVoice = savedVoice;
        }

        // Load speech rate
        const savedRate = localStorage.getItem('speechRate');
        if (savedRate) {
            this.speechRate = parseFloat(savedRate);
            if (this.speechRateSlider) this.speechRateSlider.value = this.speechRate.toString();
            if (this.speechRateInput) this.speechRateInput.value = this.speechRate.toFixed(1);
        }
    }

    populateLanguageFilter() {
        if (!this.languageSelect || !this.voices) return;

        const currentSelection = this.languageSelect.value || 'en-US';
        this.languageSelect.innerHTML = '';

        const allOption = document.createElement('option');
        allOption.value = 'all';
        allOption.textContent = 'All Languages';
        this.languageSelect.appendChild(allOption);

        const languageCodes = new Set();
        this.voices.forEach(voice => {
            languageCodes.add(voice.lang);
        });

        Array.from(languageCodes).sort().forEach(lang => {
            const option = document.createElement('option');
            option.value = lang;
            option.textContent = lang;
            this.languageSelect.appendChild(option);
        });

        this.languageSelect.value = currentSelection;
        if (this.languageSelect.value !== currentSelection) {
            this.languageSelect.value = 'en-US';
        }
    }

    populateVoiceList() {
        if (!this.voiceSelect || !this.localVoicesGroup || !this.cloudVoicesGroup) return;

        this.populateLanguageFilter();

        this.localVoicesGroup.innerHTML = '';
        this.cloudVoicesGroup.innerHTML = '';

        const excludedVoices = [
            'Eddy', 'Flo', 'Grandma', 'Grandpa', 'Reed', 'Rocko', 'Sandy', 'Shelley',
            'Albert', 'Bad News', 'Bahh', 'Bells', 'Boing', 'Bubbles', 'Cellos',
            'Good News', 'Jester', 'Organ', 'Superstar', 'Trinoids', 'Whisper',
            'Wobble', 'Zarvox', 'Fred', 'Junior', 'Kathy', 'Ralph'
        ];

        const selectedLanguage = this.languageSelect ? this.languageSelect.value : 'en-US';

        this.voices.forEach((voice, index) => {
            const voiceLang = voice.lang;
            let shouldInclude = selectedLanguage === 'all' || voiceLang === selectedLanguage;

            if (shouldInclude) {
                const voiceName = voice.name;
                const isExcluded = excludedVoices.some(excluded =>
                    voiceName.toLowerCase().startsWith(excluded.toLowerCase())
                );

                if (!isExcluded) {
                    const option = document.createElement('option');
                    option.value = `browser:${index}`;
                    option.textContent = `${voice.name} (${voice.lang})`;

                    if (voice.localService) {
                        this.localVoicesGroup.appendChild(option);
                    } else {
                        this.cloudVoicesGroup.appendChild(option);
                    }
                }
            }
        });

        if (this.localVoicesGroup.children.length === 0) {
            this.localVoicesGroup.style.display = 'none';
        } else {
            this.localVoicesGroup.style.display = '';
        }

        if (this.cloudVoicesGroup.children.length === 0) {
            this.cloudVoicesGroup.style.display = 'none';
        } else {
            this.cloudVoicesGroup.style.display = '';
        }

        // Restore selection or find default
        if (this.selectedVoice) {
            this.voiceSelect.value = this.selectedVoice;
        }

        this.updateVoiceWarnings();
    }

    updateVoiceWarnings() {
        if (this.selectedVoice === 'system') {
            this.systemVoiceInfo.style.display = 'flex';
            this.rateWarning.style.display = 'none';
        } else if (this.selectedVoice && this.selectedVoice.startsWith('browser:')) {
            const voiceIndex = parseInt(this.selectedVoice.substring(8));
            const voice = this.voices[voiceIndex];

            if (voice) {
                const isGoogleVoice = voice.name.toLowerCase().includes('google');
                this.rateWarning.style.display = isGoogleVoice ? 'flex' : 'none';
                this.systemVoiceInfo.style.display = voice.localService ? 'flex' : 'none';
            } else {
                this.rateWarning.style.display = 'none';
                this.systemVoiceInfo.style.display = 'none';
            }
        } else {
            this.rateWarning.style.display = 'none';
            this.systemVoiceInfo.style.display = 'none';
        }
    }

    setupEventListeners() {
        // Text input events
        this.messageInput.addEventListener('keydown', (e) => this.handleTextInputKeydown(e));
        this.messageInput.addEventListener('input', () => this.autoGrowTextarea());

        // Microphone button
        this.micBtn.addEventListener('click', () => this.toggleVoiceDictation());

        // Send mode radio buttons
        this.sendModeRadios.forEach(radio => {
            radio.addEventListener('change', (e) => {
                this.sendMode = e.target.value;
                this.triggerWordInputContainer.style.display =
                    this.sendMode === 'trigger' ? 'flex' : 'none';
            });
        });

        // Trigger word input
        this.triggerWordInput.addEventListener('input', (e) => {
            this.triggerWord = e.target.value.trim().toLowerCase();
        });

        // Settings toggle
        this.settingsToggleHeader.addEventListener('click', () => {
            const arrow = this.settingsToggleHeader.querySelector('.toggle-arrow');
            if (this.settingsContent.classList.contains('open')) {
                this.settingsContent.classList.remove('open');
                arrow.classList.remove('open');
            } else {
                this.settingsContent.classList.add('open');
                arrow.classList.add('open');
            }
        });

        // Voice responses toggle
        this.voiceResponsesToggle.addEventListener('change', async (e) => {
            const enabled = e.target.checked;
            await this.updateVoiceResponses(enabled);
            // Show/hide voice options based on toggle
            this.voiceOptions.style.display = enabled ? 'block' : 'none';
        });

        // Voice selection
        this.voiceSelect.addEventListener('change', (e) => {
            this.selectedVoice = e.target.value;
            localStorage.setItem('selectedVoice', this.selectedVoice);
            this.updateVoiceWarnings();
        });

        // Language filter
        if (this.languageSelect) {
            this.languageSelect.addEventListener('change', () => {
                this.populateVoiceList();
            });
        }

        // Speech rate slider
        if (this.speechRateSlider) {
            this.speechRateSlider.addEventListener('input', (e) => {
                this.speechRate = parseFloat(e.target.value);
                this.speechRateInput.value = this.speechRate.toFixed(1);
                localStorage.setItem('speechRate', this.speechRate.toString());
            });
        }

        // Speech rate text input
        if (this.speechRateInput) {
            this.speechRateInput.addEventListener('input', (e) => {
                let value = parseFloat(e.target.value);
                if (!isNaN(value)) {
                    value = Math.max(0.5, Math.min(5, value));
                    this.speechRate = value;
                    this.speechRateSlider.value = value.toString();
                    this.speechRateInput.value = value.toFixed(1);
                    localStorage.setItem('speechRate', this.speechRate.toString());
                }
            });
        }

        // Test TTS button
        if (this.testTTSBtn) {
            this.testTTSBtn.addEventListener('click', () => {
                this.speakText('This is Voice Mode for Claude Code. How can I help you today?');
            });
        }
    }

    async loadData() {
        try {
            // Load full conversation
            const conversationResponse = await fetch(`${this.baseUrl}/api/conversation?limit=50`);
            if (conversationResponse.ok) {
                const data = await conversationResponse.json();
                this.updateConversation(data.messages);
            }
        } catch (error) {
            console.error('Failed to load data:', error);
        }
    }

    updateConversation(messages) {
        const container = this.conversationMessages;
        const emptyState = container.querySelector('.empty-state');

        if (messages.length === 0) {
            emptyState.style.display = 'flex';
            container.querySelectorAll('.message-bubble').forEach(el => el.remove());
            return;
        }

        emptyState.style.display = 'none';

        // Get existing message IDs to avoid duplicates
        const existingBubbles = container.querySelectorAll('.message-bubble');
        const existingIds = new Set();
        existingBubbles.forEach(bubble => {
            if (bubble.dataset.messageId) {
                existingIds.add(bubble.dataset.messageId);
            }
        });

        // Get waiting indicator to insert messages before it
        const waitingIndicator = container.querySelector('.waiting-indicator');

        // Only render new messages and update status for existing ones
        messages.forEach(message => {
            if (!existingIds.has(message.id)) {
                // New message - create bubble and insert before waiting indicator
                const bubble = this.createMessageBubble(message);
                if (waitingIndicator) {
                    container.insertBefore(bubble, waitingIndicator);
                } else {
                    container.appendChild(bubble);
                }
            } else {
                // Existing message - update status if it's a user message
                if (message.role === 'user' && message.status) {
                    const bubble = container.querySelector(`[data-message-id="${message.id}"]`);
                    if (bubble) {
                        const statusEl = bubble.querySelector('.message-status');
                        if (statusEl) {
                            // Check if status changed from pending to something else
                            const wasPending = statusEl.classList.contains('pending');
                            const isPending = message.status === 'pending';

                            if (wasPending && !isPending) {
                                // Status changed from pending - remove delete button
                                const deleteBtn = statusEl.querySelector('.delete-message-btn');
                                if (deleteBtn) {
                                    deleteBtn.remove();
                                }
                            }

                            // Update status class and text
                            statusEl.className = `message-status ${message.status}`;
                            const statusText = statusEl.querySelector('span:last-child');
                            if (statusText) {
                                statusText.textContent = message.status.toUpperCase();
                            }
                        }
                    }
                }
            }
        });

        this.scrollToBottom();
    }

    createMessageBubble(message) {
        const bubble = document.createElement('div');
        bubble.className = `message-bubble ${message.role}`;
        bubble.dataset.messageId = message.id;

        const messageText = document.createElement('div');
        messageText.className = 'message-text';
        messageText.textContent = message.text;

        const messageMeta = document.createElement('div');
        messageMeta.className = 'message-meta';

        const timestamp = document.createElement('span');
        timestamp.className = 'message-timestamp';
        timestamp.textContent = this.formatTimestamp(message.timestamp);
        messageMeta.appendChild(timestamp);

        // Only show status for user messages
        if (message.role === 'user' && message.status) {
            const statusContainer = document.createElement('div');
            statusContainer.className = `message-status ${message.status}`;

            // Add delete button for pending messages (shows on hover)
            if (message.status === 'pending') {
                const deleteBtn = document.createElement('span');
                deleteBtn.className = 'delete-message-btn';
                deleteBtn.innerHTML = `
                    <svg class="delete-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
                        <path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/>
                    </svg>
                `;
                deleteBtn.onclick = (e) => {
                    e.stopPropagation();
                    this.deleteMessage(message.id);
                };
                statusContainer.appendChild(deleteBtn);
            }

            const statusText = document.createElement('span');
            statusText.textContent = message.status.toUpperCase();
            statusContainer.appendChild(statusText);

            messageMeta.appendChild(statusContainer);
        }

        bubble.appendChild(messageText);
        bubble.appendChild(messageMeta);

        return bubble;
    }

    scrollToBottom() {
        this.conversationContainer.scrollTo({
            top: this.conversationContainer.scrollHeight,
            behavior: 'smooth'
        });
    }

    formatTimestamp(timestamp) {
        const date = new Date(timestamp);
        return date.toLocaleTimeString();
    }

    // Text input handling
    handleTextInputKeydown(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            this.sendTypedMessage();
        }
        // Shift+Enter allows new line
    }

    autoGrowTextarea() {
        const textarea = this.messageInput;
        textarea.style.height = 'auto';
        textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px';
    }

    async sendTypedMessage() {
        const text = this.messageInput.value.trim();
        if (!text || this.isInterimText) return;

        this.messageInput.value = '';
        this.messageInput.style.height = 'auto';

        await this.sendMessage(text);
    }

    async sendMessage(text) {
        try {
            const response = await fetch(`${this.baseUrl}/api/potential-utterances`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, timestamp: new Date().toISOString() })
            });

            if (response.ok) {
                this.loadData();
            }
        } catch (error) {
            console.error('Failed to send message:', error);
        }
    }

    // Voice dictation
    toggleVoiceDictation() {
        if (this.isListening) {
            this.stopVoiceDictation();
        } else {
            this.startVoiceDictation();
        }
    }

    async startVoiceDictation() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            alert('Audio recording not supported in this browser');
            return;
        }

        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            this.mediaRecorder = new MediaRecorder(stream);
            this.audioChunks = [];

            this.mediaRecorder.ondataavailable = (event) => {
                this.audioChunks.push(event.data);
            };

            this.mediaRecorder.start();
            this.isListening = true;
            this.micBtn.classList.add('listening');

            // Activate voice input state
            await this.updateVoiceInputState(true);

            this.debugLog('Started recording audio...');
        } catch (e) {
            console.error('Failed to start recording:', e);
            alert('Failed to start audio recording: ' + e.message);
        }
    }

    async stopVoiceDictation() {
        if (this.mediaRecorder && this.isListening) {
            this.isListening = false;
            this.micBtn.classList.remove('listening');

            // Create a promise to handle the onstop event
            const stopPromise = new Promise((resolve) => {
                this.mediaRecorder.onstop = resolve;
            });

            this.mediaRecorder.stop();
            await stopPromise;

            // Stop all tracks
            this.mediaRecorder.stream.getTracks().forEach(track => track.stop());

            // Process the recorded audio
            const audioBlob = new Blob(this.audioChunks, { type: 'audio/wav' });
            await this.transcribeAudio(audioBlob);

            this.audioChunks = [];

            // Deactivate voice input state
            await this.updateVoiceInputState(false);
        }
    }

    async transcribeAudio(audioBlob) {
        // Show loading state
        const originalPlaceholder = this.messageInput.placeholder;
        this.messageInput.placeholder = 'Transcribing audio...';
        this.messageInput.disabled = true;

        try {
            const formData = new FormData();
            formData.append('audio', audioBlob, 'recording.wav');

            const response = await fetch(`${this.baseUrl}/api/transcribe`, {
                method: 'POST',
                body: formData
            });

            if (response.ok) {
                const data = await response.json();
                if (data.success && data.text) {
                    const text = data.text.trim();
                    if (text) {
                        this.sendMessage(text);
                    }
                }
            } else {
                console.error('Transcription failed:', await response.text());
            }
        } catch (error) {
            console.error('Error sending audio for transcription:', error);
        } finally {
            this.messageInput.placeholder = originalPlaceholder;
            this.messageInput.disabled = false;
            this.messageInput.focus();
        }
    }

    // initializeSpeechRecognition is no longer used but kept empty to avoid breaking init chain
    initializeSpeechRecognition() {
        // Legacy SpeechRecognition functionality removed in favor of Whisper API
        console.log('Using MediaRecorder + Whisper API for voice input');
    }

    containsTriggerWord(text) {
        if (!this.triggerWord) return false;
        const words = text.toLowerCase().split(/\s+/);
        return words.includes(this.triggerWord.toLowerCase());
    }

    removeTriggerWord(text) {
        if (!this.triggerWord) return text;
        const words = text.split(/\s+/);
        const filtered = words.filter(w => w.toLowerCase() !== this.triggerWord.toLowerCase());
        return filtered.join(' ');
    }

    async deleteMessage(messageId) {
        try {
            const response = await fetch(`${this.baseUrl}/api/utterances/${messageId}`, {
                method: 'DELETE'
            });

            if (response.ok) {
                // Remove the message bubble from DOM immediately
                const bubble = this.conversationMessages.querySelector(`[data-message-id="${messageId}"]`);
                if (bubble) {
                    bubble.remove();
                }
                // Refresh to sync with server
                this.loadData();
            } else {
                const error = await response.json();
                console.error('Failed to delete message:', error);
                alert(`Failed to delete: ${error.error || 'Unknown error'}`);
            }
        } catch (error) {
            console.error('Failed to delete message:', error);
        }
    }

    async updateVoiceInputState(active) {
        try {
            await fetch(`${this.baseUrl}/api/voice-input-state`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ active })
            });
        } catch (error) {
            console.error('Failed to update voice input state:', error);
        }
    }

    async updateVoiceResponses(enabled) {
        try {
            // Save to localStorage
            localStorage.setItem('voiceResponsesEnabled', enabled.toString());

            // Update server
            await fetch(`${this.baseUrl}/api/voice-preferences`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ voiceResponsesEnabled: enabled })
            });
        } catch (error) {
            console.error('Failed to update voice responses:', error);
        }
    }
}

// Initialize when page loads
document.addEventListener('DOMContentLoaded', () => {
    new MessengerClient();
});
