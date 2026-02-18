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
const cameraPreview = document.getElementById("cameraPreview");
const cameraSelect = document.getElementById("cameraSelect");
const refreshCameraBtn = document.getElementById("refreshCameraBtn");

let ws = null;
let microphoneStream = null;
let captureContext = null;
let captureSourceNode = null;
let captureWorkletNode = null;
let captureMuteNode = null;
let micActive = false;
let shouldResumeMicAfterReconnect = false;
let manualDisconnect = false;
let reconnectAttempts = 0;
let reconnectTimer = null;
const MAX_RECONNECT_ATTEMPTS = 3;

let playbackContext = null;
let nextPlaybackTime = 0;
const activePlaybackSources = new Set();
let suppressMicUntilMs = 0;

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
  const url = new URL(`${protocol}//${window.location.host}/ws`);
  const selectedCamera = (cameraSelect.value || "").trim();
  if (selectedCamera) {
    url.searchParams.set("camera_device", selectedCamera);
  }
  return url.toString();
}

function normalizeTranscript(text) {
  return text
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ");
}

function populateServerCameraOptions(cameras, defaultCamera = "") {
  const previous = cameraSelect.value;
  cameraSelect.innerHTML = "";

  if (!Array.isArray(cameras) || cameras.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No server camera";
    cameraSelect.appendChild(option);
    cameraSelect.value = "";
    return;
  }

  cameras.forEach((camera, index) => {
    const option = document.createElement("option");
    option.value = String(camera.id || "");
    const readable = camera.readable !== false;
    const sizeText =
      Number.isFinite(camera.width) && Number.isFinite(camera.height)
        ? ` ${camera.width}x${camera.height}`
        : "";
    option.textContent = String(
      `${camera.label || `Server camera ${index + 1}`}${sizeText}${readable ? "" : " (unreadable)"}`
    );
    option.disabled = !readable;
    cameraSelect.appendChild(option);
  });

  const readableCameras = cameras.filter((camera) => camera.readable !== false);
  const candidates = readableCameras.length ? readableCameras : cameras;
  const preferredValue =
    candidates.find((camera) => String(camera.id) === previous)?.id ||
    candidates.find((camera) => String(camera.id) === defaultCamera)?.id ||
    candidates[0].id;

  cameraSelect.value = String(preferredValue || "");
}

async function refreshServerCameras() {
  try {
    const response = await fetch("/api/cameras", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const payload = await response.json();
    const cameras = Array.isArray(payload.cameras) ? payload.cameras : [];
    populateServerCameraOptions(cameras, String(payload.default_camera || ""));

    if (payload.opencv_available === false) {
      appendLog("Server camera unavailable: OpenCV dependency missing");
    }
    if (payload.filter_unreadable && cameras.length === 0) {
      appendLog("No readable server camera found");
    }
  } catch (error) {
    appendLog(`Camera list load failed: ${error}`);
    populateServerCameraOptions([], "");
  }
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
    } catch (_) {}
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
  const chunkDurationMs = Math.round((int16.length / 24000) * 1000);
  suppressMicUntilMs = Math.max(suppressMicUntilMs, Date.now() + chunkDurationMs + 120);

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
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  ws = new WebSocket(getWsUrl());
  ws.binaryType = "arraybuffer";

  setStatus("CONNECTING", "warn");
  updateControlState();

  ws.onopen = () => {
    reconnectAttempts = 0;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    setStatus("CONNECTED", "ok");
    appendLog("WebSocket connected");
    updateControlState();
    if (shouldResumeMicAfterReconnect) {
      shouldResumeMicAfterReconnect = false;
      startMicrophone().catch((error) => {
        appendLog(`Mic resume failed: ${error}`);
      });
    }
  };

  ws.onclose = () => {
    setStatus("DISCONNECTED", "danger");
    appendLog("WebSocket disconnected");
    shouldResumeMicAfterReconnect = micActive;
    ws = null;
    stopMicrophone().catch(() => {});
    updateControlState();

    if (!manualDisconnect && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
      reconnectAttempts += 1;
      const delayMs = Math.min(3000, 700 * reconnectAttempts);
      appendLog(`Auto reconnect in ${delayMs}ms (${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`);
      reconnectTimer = setTimeout(() => {
        connectWebSocket().catch((error) => {
          appendLog(`Reconnect failed: ${error}`);
        });
      }, delayMs);
    }
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
  manualDisconnect = true;
  shouldResumeMicAfterReconnect = false;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
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
    const camera = String(message.camera_device || "").trim();
    const cameraText = camera ? `, camera=${camera}` : "";
    if (camera) {
      const hasOption = Array.from(cameraSelect.options).some((option) => option.value === camera);
      if (!hasOption) {
        const option = document.createElement("option");
        option.value = camera;
        option.textContent = camera;
        cameraSelect.appendChild(option);
      }
      cameraSelect.value = camera;
    }
    appendLog(`Session started: ${message.session_id}${cameraText}`);
    return;
  }

  if (type === "camera_info") {
    const device = String(message.device || "").trim();
    if (device) {
      const switching = Boolean(message.switching);
      appendLog(`${switching ? "Switching server camera" : "Server camera active"}: ${device}`);
    }
    return;
  }

  if (type === "camera_error") {
    appendLog(`Camera error: ${message.detail || "unknown"}`);
    return;
  }

  if (type === "camera_preview") {
    const mimeType = String(message.mime_type || "image/jpeg").trim();
    const data = String(message.data || "").trim();
    if (data) {
      cameraPreview.src = `data:${mimeType};base64,${data}`;
    }
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
    if (Date.now() < suppressMicUntilMs) {
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
  manualDisconnect = false;
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

cameraSelect.addEventListener("change", () => {
  const selectedCamera = cameraSelect.value.trim();
  if (!selectedCamera) {
    return;
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(
      JSON.stringify({
        type: "camera_select",
        camera_device: selectedCamera,
      })
    );
  }
});

refreshCameraBtn.addEventListener("click", () => {
  refreshServerCameras();
});

updateControlState();
setStatus("DISCONNECTED", "danger");
refreshServerCameras();
