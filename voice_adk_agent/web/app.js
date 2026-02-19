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
const trajectoryOverlay = document.getElementById("trajectoryOverlay");
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
let plannedTrajectoryPoints = [];
let detectedObjectBox = null;
let previewFrameWidth = 0;
let previewFrameHeight = 0;

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

function isFiniteNumber(value) {
  return Number.isFinite(Number(value));
}

function normalizeTrajectoryPoints(rawPoints) {
  if (!Array.isArray(rawPoints)) {
    return [];
  }
  return rawPoints
    .map((point) => ({
      x: Number(point?.x),
      y: Number(point?.y),
      z: Number(point?.z ?? 0),
    }))
    .filter((point) => isFiniteNumber(point.x) && isFiniteNumber(point.y) && isFiniteNumber(point.z));
}

function normalizeDetectionBox(rawPayload) {
  if (!rawPayload || typeof rawPayload !== "object") {
    return null;
  }

  if (rawPayload.found === false) {
    return null;
  }

  const rawBox = rawPayload.bbox && typeof rawPayload.bbox === "object" ? rawPayload.bbox : rawPayload;
  const x = Number(rawBox.x);
  const y = Number(rawBox.y);
  const w = Number(rawBox.w);
  const h = Number(rawBox.h);
  if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) {
    return null;
  }

  const imageWidth = Number(rawPayload.image_width ?? rawPayload.imageWidth);
  const imageHeight = Number(rawPayload.image_height ?? rawPayload.imageHeight);
  const confidence = Number(rawPayload.confidence);
  const label = String(rawPayload.label || "").trim();

  return {
    x,
    y,
    w,
    h,
    imageWidth: Number.isFinite(imageWidth) && imageWidth > 0 ? imageWidth : 0,
    imageHeight: Number.isFinite(imageHeight) && imageHeight > 0 ? imageHeight : 0,
    confidence: Number.isFinite(confidence) ? confidence : null,
    label,
  };
}

function resizeTrajectoryOverlay() {
  if (!trajectoryOverlay || !cameraPreview) {
    return;
  }
  const rect = cameraPreview.getBoundingClientRect();
  const width = Math.max(2, Math.round(rect.width));
  const height = Math.max(2, Math.round(rect.height));
  if (trajectoryOverlay.width !== width || trajectoryOverlay.height !== height) {
    trajectoryOverlay.width = width;
    trajectoryOverlay.height = height;
  }
}

function drawPlannedTrajectory() {
  if (!trajectoryOverlay) {
    return;
  }

  resizeTrajectoryOverlay();
  const ctx = trajectoryOverlay.getContext("2d");
  if (!ctx) {
    return;
  }

  const width = trajectoryOverlay.width;
  const height = trajectoryOverlay.height;
  ctx.clearRect(0, 0, width, height);

  if (plannedTrajectoryPoints.length) {
    const xs = plannedTrajectoryPoints.map((point) => point.x);
    const ys = plannedTrajectoryPoints.map((point) => point.y);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);

    const pad = 24;
    const spanX = Math.max(1e-6, maxX - minX);
    const spanY = Math.max(1e-6, maxY - minY);
    const scale = Math.min((width - pad * 2) / spanX, (height - pad * 2) / spanY);
    const centerX = (minX + maxX) * 0.5;
    const centerY = (minY + maxY) * 0.5;

    const project = (point) => ({
      x: (point.x - centerX) * scale + width * 0.5,
      y: height * 0.5 - (point.y - centerY) * scale,
    });

    ctx.strokeStyle = "#43a9ff";
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    plannedTrajectoryPoints.forEach((point, index) => {
      const p = project(point);
      if (index === 0) {
        ctx.moveTo(p.x, p.y);
      } else {
        ctx.lineTo(p.x, p.y);
      }
    });
    ctx.stroke();

    const start = project(plannedTrajectoryPoints[0]);
    const end = project(plannedTrajectoryPoints[plannedTrajectoryPoints.length - 1]);

    ctx.fillStyle = "#37d67a";
    ctx.beginPath();
    ctx.arc(start.x, start.y, 5, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = "#ff5f6d";
    ctx.beginPath();
    ctx.arc(end.x, end.y, 5, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = "#e6edf7";
    ctx.font = "12px Inter, system-ui, sans-serif";
    ctx.fillText(`Planned trajectory (${plannedTrajectoryPoints.length} wp)`, 10, 18);
  }

  if (detectedObjectBox) {
    const sourceWidth =
      detectedObjectBox.imageWidth > 0 ? detectedObjectBox.imageWidth : previewFrameWidth || width;
    const sourceHeight =
      detectedObjectBox.imageHeight > 0 ? detectedObjectBox.imageHeight : previewFrameHeight || height;
    const scale = Math.max(width / sourceWidth, height / sourceHeight);
    const renderedWidth = sourceWidth * scale;
    const renderedHeight = sourceHeight * scale;
    const offsetX = (width - renderedWidth) * 0.5;
    const offsetY = (height - renderedHeight) * 0.5;

    const x = offsetX + detectedObjectBox.x * scale;
    const y = offsetY + detectedObjectBox.y * scale;
    const w = detectedObjectBox.w * scale;
    const h = detectedObjectBox.h * scale;

    ctx.strokeStyle = "#ffd166";
    ctx.lineWidth = 3;
    ctx.strokeRect(x, y, w, h);

    const confidenceText =
      detectedObjectBox.confidence === null ? "" : ` ${(detectedObjectBox.confidence * 100).toFixed(1)}%`;
    const labelText = `${detectedObjectBox.label || "object"}${confidenceText}`;

    ctx.font = "12px Inter, system-ui, sans-serif";
    const textWidth = ctx.measureText(labelText).width;
    const textPadX = 6;
    const textPadY = 4;
    const textHeight = 16;
    const labelX = Math.max(0, x);
    const labelY = Math.max(0, y - textHeight - 2);

    ctx.fillStyle = "rgba(255, 209, 102, 0.95)";
    ctx.fillRect(labelX, labelY, textWidth + textPadX * 2, textHeight);
    ctx.fillStyle = "#0f141b";
    ctx.fillText(labelText, labelX + textPadX, labelY + textHeight - textPadY);
  }
}

function setPlannedTrajectory(rawPoints) {
  plannedTrajectoryPoints = normalizeTrajectoryPoints(rawPoints);
  drawPlannedTrajectory();
  if (plannedTrajectoryPoints.length) {
    appendLog(`Planned trajectory updated: ${plannedTrajectoryPoints.length} waypoint(s)`);
  }
}

function setDetectionBox(rawPayload) {
  const hadBox = Boolean(detectedObjectBox);
  if (rawPayload && rawPayload.found === false) {
    detectedObjectBox = null;
    drawPlannedTrajectory();
    if (hadBox) {
      appendLog("Detection cleared (target not found)");
    }
    return;
  }

  detectedObjectBox = normalizeDetectionBox(rawPayload);
  drawPlannedTrajectory();
  if (detectedObjectBox) {
    const label = detectedObjectBox.label || "object";
    appendLog(`Detection bbox updated: ${label}`);
  }
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
  setPlannedTrajectory([]);
  setDetectionBox({ found: false });
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
    setPlannedTrajectory([]);
    setDetectionBox({ found: false });
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
      if (switching) {
        setDetectionBox({ found: false });
      }
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
    const width = Number(message.width);
    const height = Number(message.height);
    if (Number.isFinite(width) && width > 0) {
      previewFrameWidth = width;
    }
    if (Number.isFinite(height) && height > 0) {
      previewFrameHeight = height;
    }
    if (data) {
      cameraPreview.src = `data:${mimeType};base64,${data}`;
    }
    drawPlannedTrajectory();
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

  if (type === "planned_trajectory") {
    setPlannedTrajectory(message.points);
    return;
  }

  if (type === "detection_bbox") {
    setDetectionBox(message);
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

cameraPreview.addEventListener("load", () => {
  drawPlannedTrajectory();
});

window.addEventListener("resize", () => {
  drawPlannedTrajectory();
});

updateControlState();
setStatus("DISCONNECTED", "danger");
refreshServerCameras();
