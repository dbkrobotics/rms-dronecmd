const connectionStatus = document.getElementById("connectionStatus");
const connectBtn = document.getElementById("connectBtn");
const disconnectBtn = document.getElementById("disconnectBtn");
const startMicBtn = document.getElementById("startMicBtn");
const stopMicBtn = document.getElementById("stopMicBtn");
const clearAudioBtn = document.getElementById("clearAudioBtn");
const sendTextBtn = document.getElementById("sendTextBtn");
const textInput = document.getElementById("textInput");
const inputTranscript = document.getElementById("inputTranscript");
const outputTranscript = document.getElementById("outputTranscript");
const logList = document.getElementById("log");

let ws = null;
let microphoneStream = null;
let captureContext = null;
let captureSourceNode = null;
let captureWorkletNode = null;
let captureMuteNode = null;
let micActive = false;

let playbackContext = null;
let nextPlaybackTime = 0;
const activePlaybackSources = new Set();

function setStatus(text, level) {
  connectionStatus.textContent = text;
  connectionStatus.classList.remove("ok", "warn", "danger");
  connectionStatus.classList.add(level);
}

function appendLog(text) {
  const entry = document.createElement("li");
  const time = new Date().toLocaleTimeString();
  entry.innerHTML = `<span class="time">${time}</span>${text}`;
  logList.prepend(entry);
}

function updateControlState() {
  const connected = ws && ws.readyState === WebSocket.OPEN;
  connectBtn.disabled = connected;
  disconnectBtn.disabled = !connected;
  startMicBtn.disabled = !connected || micActive;
  stopMicBtn.disabled = !connected || !micActive;
  clearAudioBtn.disabled = !connected;
  sendTextBtn.disabled = !connected;
  textInput.disabled = !connected;
}

function getWsUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws`;
}

function normalizeTranscript(text) {
  return text
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ");
}

async function ensurePlaybackContext() {
  if (!playbackContext) {
    playbackContext = new AudioContext({ sampleRate: 24000 });
  }
  if (playbackContext.state === "suspended") {
    await playbackContext.resume();
  }
}

function clearPlaybackQueue() {
  for (const source of activePlaybackSources) {
    try {
      source.stop();
    } catch (_) {
      // no-op
    }
  }
  activePlaybackSources.clear();

  if (playbackContext) {
    nextPlaybackTime = playbackContext.currentTime;
  } else {
    nextPlaybackTime = 0;
  }
}

async function queuePcmForPlayback(arrayBuffer) {
  if (!(arrayBuffer instanceof ArrayBuffer) || arrayBuffer.byteLength === 0) {
    return;
  }

  await ensurePlaybackContext();

  const int16 = new Int16Array(arrayBuffer);
  if (int16.length === 0) {
    return;
  }

  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i += 1) {
    float32[i] = int16[i] / 32768;
  }

  const audioBuffer = playbackContext.createBuffer(1, float32.length, 24000);
  audioBuffer.copyToChannel(float32, 0);

  const source = playbackContext.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(playbackContext.destination);

  const startAt = Math.max(playbackContext.currentTime, nextPlaybackTime);
  source.start(startAt);
  nextPlaybackTime = startAt + audioBuffer.duration;

  activePlaybackSources.add(source);
  source.onended = () => {
    activePlaybackSources.delete(source);
  };
}

async function connectWebSocket() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    return;
  }

  ws = new WebSocket(getWsUrl());
  ws.binaryType = "arraybuffer";

  setStatus("CONNECTING", "warn");
  updateControlState();

  ws.onopen = () => {
    setStatus("CONNECTED", "ok");
    appendLog("WebSocket connected");
    updateControlState();
  };

  ws.onclose = () => {
    setStatus("DISCONNECTED", "danger");
    appendLog("WebSocket disconnected");
    ws = null;
    stopMicrophone().catch(() => {});
    updateControlState();
  };

  ws.onerror = () => {
    appendLog("WebSocket error");
  };

  ws.onmessage = async (event) => {
    if (typeof event.data === "string") {
      handleJsonMessage(event.data);
      return;
    }

    if (event.data instanceof ArrayBuffer) {
      await queuePcmForPlayback(event.data);
    }
  };
}

function disconnectWebSocket() {
  if (!ws) {
    return;
  }

  ws.close();
}

function handleJsonMessage(rawJson) {
  let message;
  try {
    message = JSON.parse(rawJson);
  } catch (_) {
    appendLog(`Invalid JSON: ${rawJson}`);
    return;
  }

  const type = message.type;
  if (type === "session_started") {
    appendLog(`Session started: ${message.session_id}`);
    return;
  }

  if (type === "input_transcript") {
    const prefix = message.final ? "FINAL" : "LIVE";
    const text = String(message.text || "").trim();
    if (text) {
      inputTranscript.textContent = `[${prefix}] ${text}`;
    }
    return;
  }

  if (type === "output_transcript") {
    const prefix = message.final ? "FINAL" : "LIVE";
    const text = String(message.text || "").trim();
    if (text) {
      outputTranscript.textContent = `[${prefix}] ${text}`;
    }
    return;
  }

  if (type === "assistant_text") {
    const text = String(message.text || "").trim();
    if (text) {
      appendLog(`Assistant: ${text}`);
    }
    return;
  }

  if (type === "tool_call") {
    const name = String(message.name || "unknown_tool");
    const payload = JSON.stringify(message.payload ?? {});
    const compact = payload.length > 360 ? `${payload.slice(0, 360)}...` : payload;
    appendLog(`TOOL CALL: ${name} ${compact}`);
    return;
  }

  if (type === "tool_result") {
    const name = String(message.name || "unknown_tool");
    const payload = JSON.stringify(message.payload ?? {});
    const compact = payload.length > 360 ? `${payload.slice(0, 360)}...` : payload;
    appendLog(`TOOL RESULT: ${name} ${compact}`);
    return;
  }

  if (type === "interrupted") {
    appendLog("Assistant audio interrupted");
    clearPlaybackQueue();
    return;
  }

  if (type === "turn_complete") {
    appendLog("Turn complete");
    return;
  }

  if (type === "error") {
    appendLog(`Error: ${message.detail || "unknown"}`);
    return;
  }

  appendLog(`Event: ${rawJson}`);
}

async function startMicrophone() {
  if (!ws || ws.readyState !== WebSocket.OPEN || micActive) {
    return;
  }

  microphoneStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      sampleRate: 16000,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });

  captureContext = new AudioContext({ sampleRate: 16000 });
  if (captureContext.state === "suspended") {
    await captureContext.resume();
  }

  await captureContext.audioWorklet.addModule("/static/audio-capture-worklet.js");

  captureSourceNode = captureContext.createMediaStreamSource(microphoneStream);
  captureWorkletNode = new AudioWorkletNode(captureContext, "pcm-capture-processor");
  captureMuteNode = captureContext.createGain();
  captureMuteNode.gain.value = 0;

  captureWorkletNode.port.onmessage = (event) => {
    if (!ws || ws.readyState !== WebSocket.OPEN || !micActive) {
      return;
    }

    const audioChunk = event.data;
    if (audioChunk) {
      ws.send(audioChunk);
    }
  };

  captureSourceNode.connect(captureWorkletNode);
  captureWorkletNode.connect(captureMuteNode);
  captureMuteNode.connect(captureContext.destination);

  micActive = true;
  appendLog("Microphone started");
  updateControlState();
}

async function stopMicrophone() {
  micActive = false;

  if (captureWorkletNode) {
    captureWorkletNode.port.onmessage = null;
    captureWorkletNode.disconnect();
    captureWorkletNode = null;
  }

  if (captureSourceNode) {
    captureSourceNode.disconnect();
    captureSourceNode = null;
  }

  if (captureMuteNode) {
    captureMuteNode.disconnect();
    captureMuteNode = null;
  }

  if (captureContext) {
    await captureContext.close();
    captureContext = null;
  }

  if (microphoneStream) {
    for (const track of microphoneStream.getTracks()) {
      track.stop();
    }
    microphoneStream = null;
  }

  appendLog("Microphone stopped");
  updateControlState();
}

function sendTextMessage() {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    return;
  }

  const text = textInput.value.trim();
  if (!text) {
    return;
  }

  ws.send(
    JSON.stringify({
      type: "text",
      text,
    })
  );

  appendLog(`You: ${normalizeTranscript(text)}`);
  textInput.value = "";
}

connectBtn.addEventListener("click", () => {
  connectWebSocket().catch((error) => {
    appendLog(`Connect failed: ${error}`);
  });
});

disconnectBtn.addEventListener("click", () => {
  disconnectWebSocket();
});

startMicBtn.addEventListener("click", () => {
  startMicrophone().catch((error) => {
    appendLog(`Mic start failed: ${error}`);
  });
});

stopMicBtn.addEventListener("click", () => {
  stopMicrophone().catch((error) => {
    appendLog(`Mic stop failed: ${error}`);
  });
});

clearAudioBtn.addEventListener("click", () => {
  clearPlaybackQueue();
  appendLog("Playback queue cleared");
});

sendTextBtn.addEventListener("click", sendTextMessage);
textInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    sendTextMessage();
  }
});

updateControlState();
setStatus("DISCONNECTED", "danger");
