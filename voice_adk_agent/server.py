from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import threading
import time
import warnings
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

try:
    import cv2
except Exception:
    cv2 = None

load_dotenv()

warnings.filterwarnings(
    "ignore",
    message=r".*response_modalities.*",
    category=UserWarning,
)
warnings.filterwarnings(
    "ignore",
    message=r"^Pydantic serializer warnings:.*",
    category=UserWarning,
)
warnings.filterwarnings(
    "ignore",
    message=r".*\[EXPERIMENTAL\].*BASE_AUTHENTICATED_TOOL.*",
    category=UserWarning,
)

from app.drone_voice_agent import root_agent

logger = logging.getLogger("voice_adk_agent")
logging.basicConfig(level=os.getenv("VOICE_AGENT_LOG_LEVEL", "INFO"))
third_party_level_name = os.getenv("VOICE_AGENT_THIRD_PARTY_LOG_LEVEL", "CRITICAL").upper()
third_party_level = getattr(logging, third_party_level_name, logging.CRITICAL)
for noisy_logger in (
    "google_adk",
    "google.adk",
    "google_genai",
    "google.genai",
    "google_genai.types",
):
    logging.getLogger(noisy_logger).setLevel(third_party_level)

APP_NAME = os.getenv("VOICE_AGENT_APP_NAME", "drone_voice_agent_app")
WEB_DIR = Path(__file__).resolve().parent / "web"
TRACE_TOOLS = os.getenv("VOICE_AGENT_TRACE_TOOLS", "true").lower() not in {"0", "false", "no"}
TOOL_LOG_MAX_CHARS = int(os.getenv("VOICE_AGENT_TOOL_LOG_MAX_CHARS", "900"))
LIVE_RETRY_COUNT = int(os.getenv("VOICE_AGENT_LIVE_RETRY_COUNT", "4"))
LIVE_RETRY_BACKOFF_SEC = float(os.getenv("VOICE_AGENT_LIVE_RETRY_BACKOFF_SEC", "1.0"))
MAX_IMAGE_FRAME_BYTES = int(os.getenv("VOICE_AGENT_MAX_IMAGE_FRAME_BYTES", "200000"))
ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
CAMERA_DEVICE_DEFAULT = os.getenv("VOICE_AGENT_CAMERA_DEVICE", "/dev/video0").strip() or "/dev/video0"
CAMERA_WIDTH = int(os.getenv("VOICE_AGENT_CAMERA_WIDTH", "1280"))
CAMERA_HEIGHT = int(os.getenv("VOICE_AGENT_CAMERA_HEIGHT", "720"))
CAMERA_CAPTURE_FPS = float(os.getenv("VOICE_AGENT_CAMERA_CAPTURE_FPS", "20.0"))
CAMERA_PREVIEW_FPS = float(os.getenv("VOICE_AGENT_CAMERA_PREVIEW_FPS", "4.0"))
CAMERA_MODEL_FPS = float(os.getenv("VOICE_AGENT_CAMERA_MODEL_FPS", "2.0"))
CAMERA_JPEG_QUALITY = int(os.getenv("VOICE_AGENT_CAMERA_JPEG_QUALITY", "65"))
CAMERA_RETRY_SEC = float(os.getenv("VOICE_AGENT_CAMERA_RETRY_SEC", "1.0"))
CAMERA_FILTER_UNREADABLE = os.getenv("VOICE_AGENT_CAMERA_FILTER_UNREADABLE", "true").lower() not in {
    "0",
    "false",
    "no",
}
CAMERA_PROBE_READABLE = os.getenv("VOICE_AGENT_CAMERA_PROBE_READABLE", "false").lower() not in {
    "0",
    "false",
    "no",
}
CAMERA_SET_FPS = os.getenv("VOICE_AGENT_CAMERA_SET_FPS", "false").lower() not in {
    "0",
    "false",
    "no",
}
CAMERA_FORCE_MJPG = os.getenv("VOICE_AGENT_CAMERA_FORCE_MJPG", "false").lower() not in {
    "0",
    "false",
    "no",
}

session_service = InMemorySessionService()
runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

app = FastAPI(title="Drone Voice ADK Agent")
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

_latest_camera_frame_lock = threading.Lock()
_latest_camera_frame: dict[str, Any] = {
    "data": b"",
    "width": 0,
    "height": 0,
    "device": "",
    "ts": 0.0,
}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "app": APP_NAME})


def _set_latest_camera_frame(frame_bytes: bytes, width: int, height: int, device: str) -> None:
    with _latest_camera_frame_lock:
        _latest_camera_frame["data"] = frame_bytes
        _latest_camera_frame["width"] = int(width)
        _latest_camera_frame["height"] = int(height)
        _latest_camera_frame["device"] = str(device)
        _latest_camera_frame["ts"] = time.time()


def _get_latest_camera_frame() -> tuple[bytes, int, int, str, float] | None:
    with _latest_camera_frame_lock:
        data = _latest_camera_frame.get("data", b"")
        width = int(_latest_camera_frame.get("width", 0) or 0)
        height = int(_latest_camera_frame.get("height", 0) or 0)
        device = str(_latest_camera_frame.get("device", "") or "")
        ts = float(_latest_camera_frame.get("ts", 0.0) or 0.0)

    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        return None
    return bytes(data), width, height, device, ts


@app.get("/api/camera/latest.jpg")
async def latest_camera_jpeg() -> Response:
    latest = _get_latest_camera_frame()
    if latest is None:
        return Response(
            content=b"camera frame not ready",
            media_type="text/plain",
            status_code=503,
        )

    frame_bytes, width, height, device, ts = latest
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "X-Frame-Width": str(width),
        "X-Frame-Height": str(height),
        "X-Camera-Device": device,
        "X-Frame-Timestamp": f"{ts:.6f}",
    }
    return Response(content=frame_bytes, media_type="image/jpeg", headers=headers)


def _camera_source_value(camera_device: str) -> int | str:
    value = camera_device.strip()
    if value.isdigit():
        return int(value)
    return value or CAMERA_DEVICE_DEFAULT


def _discover_camera_devices() -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    dev_root = Path("/dev")
    sys_root = Path("/sys/class/video4linux")

    for device_path in sorted(dev_root.glob("video*"), key=lambda path: path.name):
        if not device_path.exists():
            continue

        label = device_path.name
        name_path = sys_root / device_path.name / "name"
        if name_path.exists():
            with suppress(Exception):
                raw_name = name_path.read_text(encoding="utf-8").strip()
                if raw_name:
                    label = raw_name

        devices.append({"id": str(device_path), "label": f"{label} ({device_path.name})"})

    if not devices:
        default_source = str(_camera_source_value(CAMERA_DEVICE_DEFAULT))
        devices.append({"id": default_source, "label": f"Default camera ({default_source})"})

    return devices


def _select_preferred_camera(devices: Sequence[Mapping[str, str]]) -> str:
    keywords = ("rgb", "color", "webcam", "uvc", "camera")
    for keyword in keywords:
        for device in devices:
            label = str(device.get("label", "")).lower()
            if keyword in label:
                return str(device.get("id", ""))
    return str(devices[0].get("id", "")) if devices else CAMERA_DEVICE_DEFAULT


def _probe_camera_device(camera_device: str) -> dict[str, Any]:
    try:
        capture, source = _open_camera_capture(camera_device)
    except Exception as exc:
        return {
            "readable": False,
            "source": camera_device,
            "error": str(exc),
        }

    try:
        frame_packet = _read_camera_frame_jpeg(capture)
        if frame_packet is None:
            return {
                "readable": False,
                "source": source,
                "error": "camera opened but no readable frame",
            }

        _, width, height = frame_packet
        return {
            "readable": True,
            "source": source,
            "width": width,
            "height": height,
        }
    finally:
        with suppress(Exception):
            capture.release()


@app.get("/api/cameras")
async def list_cameras() -> JSONResponse:
    raw_cameras = _discover_camera_devices()
    cameras: list[dict[str, Any]] = []
    if CAMERA_PROBE_READABLE:
        for item in raw_cameras:
            camera_id = str(item.get("id", "")).strip()
            probe = await asyncio.to_thread(_probe_camera_device, camera_id)
            merged = {
                "id": camera_id,
                "label": str(item.get("label", camera_id)),
                **probe,
            }
            if CAMERA_FILTER_UNREADABLE and not bool(merged.get("readable")):
                continue
            cameras.append(merged)
    else:
        for item in raw_cameras:
            camera_id = str(item.get("id", "")).strip()
            cameras.append(
                {
                    "id": camera_id,
                    "label": str(item.get("label", camera_id)),
                    "readable": True,
                    "source": camera_id,
                }
            )

    if not cameras and raw_cameras:
        # Fallback for diagnostics: show all when nothing is readable.
        for item in raw_cameras:
            camera_id = str(item.get("id", "")).strip()
            cameras.append(
                {
                    "id": camera_id,
                    "label": str(item.get("label", camera_id)),
                    "readable": False,
                    "source": camera_id,
                }
            )

    available_ids = {str(item.get("id", "")) for item in cameras if bool(item.get("readable", True))}
    if CAMERA_DEVICE_DEFAULT in available_ids:
        default_camera = CAMERA_DEVICE_DEFAULT
    else:
        readable_candidates = [item for item in cameras if bool(item.get("readable", False))]
        default_camera = _select_preferred_camera(readable_candidates or cameras)

    return JSONResponse(
        {
            "cameras": cameras,
            "opencv_available": bool(cv2),
            "default_camera": default_camera,
            "filter_unreadable": CAMERA_FILTER_UNREADABLE,
            "probe_readable": CAMERA_PROBE_READABLE,
        }
    )


def _open_camera_capture(camera_device: str) -> tuple[Any, str]:
    if cv2 is None:
        raise RuntimeError("OpenCV is not available. Install voice_adk_agent requirements first.")

    source_value = _camera_source_value(camera_device)
    source_candidates: list[Any] = [source_value]
    source_text = str(source_value)
    if source_text.startswith("/dev/video"):
        suffix = source_text.rsplit("video", 1)[-1]
        if suffix.isdigit():
            source_candidates.append(int(suffix))

    backend_candidates: list[int | None] = []
    if hasattr(cv2, "CAP_ANY"):
        backend_candidates.append(int(cv2.CAP_ANY))
    if hasattr(cv2, "CAP_V4L2"):
        backend_candidates.append(int(cv2.CAP_V4L2))
    if not backend_candidates:
        backend_candidates.append(None)

    tried: list[str] = []
    for source in source_candidates:
        for backend in backend_candidates:
            try:
                capture = cv2.VideoCapture(source) if backend is None else cv2.VideoCapture(source, backend)
            except Exception:
                continue

            if not capture.isOpened():
                capture.release()
                tried.append(f"{source}@{backend}")
                continue

            capture.set(cv2.CAP_PROP_FRAME_WIDTH, float(CAMERA_WIDTH))
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(CAMERA_HEIGHT))
            if CAMERA_SET_FPS:
                capture.set(cv2.CAP_PROP_FPS, float(CAMERA_CAPTURE_FPS))
            if CAMERA_FORCE_MJPG:
                with suppress(Exception):
                    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            with suppress(Exception):
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            for _ in range(12):
                ok, frame = capture.read()
                if ok and frame is not None:
                    return capture, str(source)
                time.sleep(0.03)

            capture.release()
            tried.append(f"{source}@{backend}")

    tried_text = ", ".join(tried) if tried else str(source_value)
    raise RuntimeError(f"Cannot open readable server camera stream: {camera_device} (tried: {tried_text})")


def _read_camera_frame_jpeg(capture: Any) -> tuple[bytes, int, int] | None:
    if cv2 is None:
        return None

    frame = None
    for _ in range(3):
        ok, maybe_frame = capture.read()
        if ok and maybe_frame is not None:
            frame = maybe_frame
            break
        time.sleep(0.01)
    if frame is None:
        return None

    if frame.dtype != "uint8":
        frame = cv2.normalize(frame, None, 0, 255, cv2.NORM_MINMAX).astype("uint8")

    if len(frame.shape) == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    elif len(frame.shape) == 3 and frame.shape[2] == 4:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    height, width = frame.shape[:2]
    success, encoded = cv2.imencode(
        ".jpg",
        frame,
        [int(cv2.IMWRITE_JPEG_QUALITY), int(CAMERA_JPEG_QUALITY)],
    )
    if not success:
        return None

    return encoded.tobytes(), int(width), int(height)


async def _run_server_camera_stream(
    websocket: WebSocket,
    queue_ref: dict[str, LiveRequestQueue],
    camera_ref: dict[str, str],
) -> None:
    preview_interval = 1.0 / max(CAMERA_PREVIEW_FPS, 0.2)
    model_interval = 1.0 / max(CAMERA_MODEL_FPS, 0.1)
    last_preview_at = 0.0
    last_model_at = 0.0
    last_error = ""
    capture = None
    camera_device = str(camera_ref.get("value", "")).strip() or CAMERA_DEVICE_DEFAULT
    source_label = camera_device
    loop = asyncio.get_running_loop()

    try:
        while True:
            desired_camera = str(camera_ref.get("value", "")).strip() or CAMERA_DEVICE_DEFAULT
            if desired_camera != camera_device:
                camera_device = desired_camera
                if capture is not None:
                    with suppress(Exception):
                        await asyncio.to_thread(capture.release)
                    capture = None
                await websocket.send_json({"type": "camera_info", "device": camera_device, "switching": True})

            if capture is None:
                try:
                    capture, source_label = await asyncio.to_thread(_open_camera_capture, camera_device)
                    last_error = ""
                    await websocket.send_json(
                        {
                            "type": "camera_info",
                            "device": source_label,
                        }
                    )
                except Exception as exc:
                    detail = str(exc)
                    if detail != last_error:
                        last_error = detail
                        await websocket.send_json(
                            {
                                "type": "camera_error",
                                "detail": detail,
                            }
                        )
                    await asyncio.sleep(max(CAMERA_RETRY_SEC, 0.2))
                    continue

            frame_packet = await asyncio.to_thread(_read_camera_frame_jpeg, capture)
            if frame_packet is None:
                with suppress(Exception):
                    await asyncio.to_thread(capture.release)
                capture = None
                await asyncio.sleep(0.2)
                continue

            frame_bytes, width, height = frame_packet
            _set_latest_camera_frame(frame_bytes, width, height, source_label)
            now = loop.time()

            if now - last_model_at >= model_interval and len(frame_bytes) <= MAX_IMAGE_FRAME_BYTES:
                with suppress(Exception):
                    queue_ref["value"].send_realtime(
                        types.Blob(
                            mime_type="image/jpeg",
                            data=frame_bytes,
                        )
                    )
                last_model_at = now

            if now - last_preview_at >= preview_interval:
                await websocket.send_json(
                    {
                        "type": "camera_preview",
                        "mime_type": "image/jpeg",
                        "width": width,
                        "height": height,
                        "device": source_label,
                        "data": base64.b64encode(frame_bytes).decode("ascii"),
                    }
                )
                last_preview_at = now

            await asyncio.sleep(0)
    finally:
        if capture is not None:
            with suppress(Exception):
                await asyncio.to_thread(capture.release)


def _build_run_config() -> RunConfig:
    response_modality = os.getenv("VOICE_AGENT_RESPONSE_MODALITY", "AUDIO").upper()
    modality_enum = getattr(types, "Modality", None)
    if modality_enum:
        audio_modality = getattr(modality_enum, "AUDIO", "AUDIO")
        text_modality = getattr(modality_enum, "TEXT", "TEXT")
    else:
        audio_modality = "AUDIO"
        text_modality = "TEXT"

    response_modalities = [audio_modality] if response_modality == "AUDIO" else [text_modality]

    kwargs: dict[str, Any] = {
        "streaming_mode": StreamingMode.BIDI,
        "response_modalities": response_modalities,
        "input_audio_transcription": {},
        "output_audio_transcription": {},
    }

    if response_modality == "AUDIO":
        kwargs["speech_config"] = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=os.getenv("VOICE_AGENT_VOICE_NAME", "Aoede")
                )
            )
        )

    return RunConfig(**kwargs)


def _plain(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "<max-depth>"

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"

    if isinstance(value, Mapping):
        return {str(k): _plain(v, depth + 1) for k, v in value.items()}

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [_plain(v, depth + 1) for v in value]

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _plain(model_dump(exclude_none=True), depth + 1)
        except Exception:
            pass

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _plain(to_dict(), depth + 1)
        except Exception:
            pass

    as_dict = getattr(value, "__dict__", None)
    if isinstance(as_dict, dict):
        return _plain(as_dict, depth + 1)

    return str(value)


def _member(value: Any, key: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _compact(value: Any) -> str:
    text = json.dumps(_plain(value), ensure_ascii=False, sort_keys=True)
    if len(text) <= TOOL_LOG_MAX_CHARS:
        return text
    return f"{text[:TOOL_LOG_MAX_CHARS]} ...<truncated>"


def _summarize_tool_payload(kind: str, tool_name: str, payload: Any) -> Any:
    plain_payload = _plain(payload)

    if kind == "tool_call":
        return plain_payload

    if not isinstance(plain_payload, Mapping):
        return plain_payload

    if "error" in plain_payload:
        return {"error": plain_payload.get("error")}

    if "structuredContent" in plain_payload:
        structured = plain_payload.get("structuredContent")
        if isinstance(structured, Mapping):
            keys = sorted(structured.keys())
            summary: dict[str, Any] = {"keys": keys}
            for key in ("action_count", "service_count", "topic_count", "node_count", "success", "message"):
                if key in structured:
                    summary[key] = structured.get(key)
            return {"structuredContent": summary}
        return {"structuredContent": structured}

    if "content" in plain_payload and isinstance(plain_payload["content"], Sequence):
        items = plain_payload["content"]
        return {"content_items": len(items), "tool": tool_name}

    return plain_payload


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number):
        return None
    return number


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    return int(round(number))


def _point_from_mapping(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    x = _to_float(value.get("x"))
    y = _to_float(value.get("y"))
    if x is None or y is None:
        return None
    z = _to_float(value.get("z"))
    return {"x": x, "y": y, "z": 0.0 if z is None else z}


def _point_list_from_sequence(value: Any) -> list[dict[str, float]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray, memoryview)):
        return []
    points: list[dict[str, float]] = []
    for item in value:
        point = _point_from_mapping(item)
        if point is not None:
            points.append(point)
    return points


def _find_trajectory_points(value: Any, depth: int = 0) -> list[dict[str, float]]:
    if depth > 6:
        return []

    preferred_keys = ("points", "waypoints", "suggested_waypoints", "planned_points")

    if isinstance(value, Mapping):
        for key in preferred_keys:
            candidate = value.get(key)
            points = _point_list_from_sequence(candidate)
            if points:
                return points
        for nested in value.values():
            points = _find_trajectory_points(nested, depth + 1)
            if points:
                return points
        return []

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        for nested in value:
            points = _find_trajectory_points(nested, depth + 1)
            if points:
                return points
        return []

    return []


def _extract_planned_trajectory(tool_name: str, payload: Any) -> dict[str, Any] | None:
    plain_payload = _plain(payload)
    points = _find_trajectory_points(plain_payload)
    if not points:
        return None

    tool_name_lower = tool_name.lower()
    payload_text = _compact(plain_payload).lower()
    is_trajectory_related = (
        "trajectory" in tool_name_lower
        or "/drone_control/trajectory" in payload_text
        or "suggested_waypoints" in payload_text
    )

    if not is_trajectory_related and len(points) < 2:
        return None

    return {
        "source": tool_name,
        "points": points[:200],
    }


def _find_object_detection_result(value: Any, depth: int = 0) -> Mapping[str, Any] | None:
    if depth > 7:
        return None

    if isinstance(value, Mapping):
        has_found = "found" in value
        has_pixel = "pixel_u" in value and "pixel_v" in value
        has_bbox = all(key in value for key in ("bbox_x", "bbox_y", "bbox_w", "bbox_h"))
        if has_found and (has_pixel or has_bbox):
            return value

        for nested in value.values():
            found = _find_object_detection_result(nested, depth + 1)
            if found is not None:
                return found
        return None

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        for nested in value:
            found = _find_object_detection_result(nested, depth + 1)
            if found is not None:
                return found

    return None


def _extract_detection_bbox_event(tool_name: str, payload: Any) -> dict[str, Any] | None:
    plain_payload = _plain(payload)
    result = _find_object_detection_result(plain_payload)
    if result is None:
        return None

    event: dict[str, Any] = {
        "source": tool_name,
        "found": bool(result.get("found")),
    }
    if not event["found"]:
        return event

    bbox_x = _to_int(result.get("bbox_x"))
    bbox_y = _to_int(result.get("bbox_y"))
    bbox_w = _to_int(result.get("bbox_w"))
    bbox_h = _to_int(result.get("bbox_h"))

    if (
        bbox_x is None
        or bbox_y is None
        or bbox_w is None
        or bbox_h is None
        or bbox_w <= 0
        or bbox_h <= 0
    ):
        pixel_u = _to_int(result.get("pixel_u"))
        pixel_v = _to_int(result.get("pixel_v"))
        if pixel_u is None or pixel_v is None:
            return event
        bbox_x = max(0, pixel_u - 12)
        bbox_y = max(0, pixel_v - 12)
        bbox_w = 24
        bbox_h = 24

    event["bbox"] = {
        "x": int(bbox_x),
        "y": int(bbox_y),
        "w": int(max(1, bbox_w)),
        "h": int(max(1, bbox_h)),
    }

    image_width = _to_int(result.get("image_width"))
    image_height = _to_int(result.get("image_height"))
    if image_width is not None and image_width > 0:
        event["image_width"] = int(image_width)
    if image_height is not None and image_height > 0:
        event["image_height"] = int(image_height)

    confidence = _to_float(result.get("confidence"))
    if confidence is not None:
        event["confidence"] = float(max(0.0, min(1.0, confidence)))

    matched_label = str(result.get("matched_label", "")).strip()
    if matched_label:
        event["label"] = matched_label

    return event


def _extract_tool_activity_from_part(part: Any) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []

    function_call = (
        _member(part, "function_call")
        or _member(part, "functionCall")
        or _member(part, "tool_call")
        or _member(part, "toolCall")
    )
    if function_call is not None:
        name = _member(function_call, "name") or _member(function_call, "id") or "unknown_tool"
        args = (
            _member(function_call, "args")
            or _member(function_call, "arguments")
            or _member(function_call, "parameters")
            or {}
        )
        activities.append({"kind": "tool_call", "name": str(name), "payload": args})

    function_response = (
        _member(part, "function_response")
        or _member(part, "functionResponse")
        or _member(part, "tool_response")
        or _member(part, "toolResponse")
    )
    if function_response is not None:
        name = _member(function_response, "name") or _member(function_response, "id") or "unknown_tool"
        payload = _member(function_response, "response")
        if payload is None:
            payload = _member(function_response, "result")
        if payload is None:
            payload = function_response
        activities.append({"kind": "tool_result", "name": str(name), "payload": payload})

    return activities


def _is_retryable_live_error(exception: Exception) -> bool:
    status_code = getattr(exception, "status_code", None)
    if isinstance(status_code, int):
        if status_code == 1007:
            return False
        if status_code == 1011:
            return True
        if status_code in {429, 500, 502, 503, 504}:
            return True

    text = str(exception).lower()
    if "1007" in text or "invalid argument" in text:
        return False
    if "1011" in text or "internal error occurred" in text:
        return True
    return (
        "connection closed" in text
        or "temporarily unavailable" in text
        or "timeout" in text
    )


def _scan_tool_activity(obj: Any, collected: list[dict[str, Any]]) -> None:
    plain = _plain(obj)
    if isinstance(plain, Mapping):
        for key, value in plain.items():
            lk = str(key).lower()

            if lk in {"function_call", "functioncall", "tool_call", "toolcall"}:
                name = _member(value, "name") or _member(value, "id") or "unknown_tool"
                args = _member(value, "args") or _member(value, "arguments") or value
                collected.append({"kind": "tool_call", "name": str(name), "payload": args})

            if lk in {"function_response", "functionresponse", "tool_response", "toolresponse"}:
                name = _member(value, "name") or _member(value, "id") or "unknown_tool"
                payload = _member(value, "response")
                if payload is None:
                    payload = _member(value, "result")
                if payload is None:
                    payload = value
                collected.append({"kind": "tool_result", "name": str(name), "payload": payload})

            _scan_tool_activity(value, collected)

    elif isinstance(plain, Sequence) and not isinstance(plain, str):
        for item in plain:
            _scan_tool_activity(item, collected)


async def _emit_tool_activity(
    websocket: WebSocket, activities: list[dict[str, Any]], seen_signatures: set[str]
) -> None:
    for item in activities:
        detection_event = None
        if item["kind"] == "tool_result":
            detection_event = _extract_detection_bbox_event(item["name"], item["payload"])
        if detection_event is not None:
            detection_signature = f"detection_bbox::{_compact(detection_event)}"
            if detection_signature not in seen_signatures:
                seen_signatures.add(detection_signature)
                await websocket.send_json(
                    {
                        "type": "detection_bbox",
                        **detection_event,
                    }
                )

        trajectory = _extract_planned_trajectory(item["name"], item["payload"])
        if trajectory is not None:
            trajectory_signature = f"planned_trajectory::{_compact(trajectory['points'])}"
            if trajectory_signature not in seen_signatures:
                seen_signatures.add(trajectory_signature)
                await websocket.send_json(
                    {
                        "type": "planned_trajectory",
                        "source": trajectory["source"],
                        "points": trajectory["points"],
                    }
                )

        summarized_payload = _summarize_tool_payload(item["kind"], item["name"], item["payload"])
        signature = f"{item['kind']}::{item['name']}::{_compact(summarized_payload)}"
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)

        if TRACE_TOOLS:
            payload_plain = _plain(summarized_payload)
            is_error_result = (
                item["kind"] == "tool_result"
                and isinstance(payload_plain, Mapping)
                and "error" in payload_plain
            )
            if is_error_result:
                logger.error(
                    "[%s] %s payload=%s",
                    item["kind"].upper(),
                    item["name"],
                    _compact(summarized_payload),
                )
            else:
                logger.info(
                    "[%s] %s payload=%s",
                    item["kind"].upper(),
                    item["name"],
                    _compact(summarized_payload),
                )

        await websocket.send_json(
            {
                "type": item["kind"],
                "name": item["name"],
                "payload": _plain(summarized_payload),
            }
        )


async def _handle_text_message(raw_message: str, live_request_queue: LiveRequestQueue) -> None:
    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        payload = {"type": "text", "text": raw_message}

    if not isinstance(payload, Mapping):
        return

    message_type = str(payload.get("type", "")).strip()

    if message_type == "text":
        text = str(payload.get("text", "")).strip()
        if text:
            live_request_queue.send_content(
                types.Content(
                    role="user",
                    parts=[types.Part(text=text)],
                )
            )
        return

    if message_type == "video_frame":
        mime_type = str(payload.get("mime_type", "image/jpeg")).strip().lower()
        if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
            return

        encoded_data = payload.get("data")
        if not isinstance(encoded_data, str) or not encoded_data:
            return

        try:
            frame_bytes = base64.b64decode(encoded_data, validate=True)
        except Exception:
            return

        if not frame_bytes or len(frame_bytes) > MAX_IMAGE_FRAME_BYTES:
            return

        if mime_type == "image/jpg":
            mime_type = "image/jpeg"

        live_request_queue.send_realtime(
            types.Blob(
                mime_type=mime_type,
                data=frame_bytes,
            )
        )


async def _forward_event(websocket: WebSocket, event: Any, seen_tool_signatures: set[str]) -> None:
    tool_activities: list[dict[str, Any]] = []

    input_tx = getattr(event, "input_transcription", None)
    if input_tx and getattr(input_tx, "text", None):
        await websocket.send_json(
            {
                "type": "input_transcript",
                "text": input_tx.text,
                "final": bool(getattr(input_tx, "finished", False)),
            }
        )

    output_tx = getattr(event, "output_transcription", None)
    if output_tx and getattr(output_tx, "text", None):
        await websocket.send_json(
            {
                "type": "output_transcript",
                "text": output_tx.text,
                "final": bool(getattr(output_tx, "finished", False)),
            }
        )

    content = getattr(event, "content", None)
    if content and getattr(content, "parts", None):
        for part in content.parts:
            tool_activities.extend(_extract_tool_activity_from_part(part))

            text_part = getattr(part, "text", None)
            if text_part:
                await websocket.send_json(
                    {
                        "type": "assistant_text",
                        "text": text_part,
                        "partial": bool(getattr(event, "partial", False)),
                    }
                )

            inline_data = getattr(part, "inline_data", None)
            if inline_data:
                mime_type = str(getattr(inline_data, "mime_type", ""))
                blob_data = getattr(inline_data, "data", b"")

                if isinstance(blob_data, str):
                    try:
                        blob_data = base64.b64decode(blob_data)
                    except Exception:
                        blob_data = blob_data.encode("utf-8")
                elif isinstance(blob_data, memoryview):
                    blob_data = blob_data.tobytes()
                elif not isinstance(blob_data, (bytes, bytearray)):
                    blob_data = bytes(blob_data)

                if mime_type.startswith("audio/pcm") and blob_data:
                    await websocket.send_bytes(blob_data)

    if not tool_activities:
        _scan_tool_activity(event, tool_activities)

    if tool_activities:
        await _emit_tool_activity(websocket, tool_activities, seen_tool_signatures)

    if bool(getattr(event, "interrupted", False)):
        await websocket.send_json({"type": "interrupted"})

    if bool(getattr(event, "turn_complete", False)):
        await websocket.send_json({"type": "turn_complete"})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    selected_camera = str(websocket.query_params.get("camera_device", "")).strip() or CAMERA_DEVICE_DEFAULT
    camera_ref: dict[str, str] = {"value": selected_camera}

    user_id = f"user-{uuid4().hex}"
    session_ref: dict[str, str] = {"value": f"session-{uuid4().hex}"}

    await session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_ref["value"],
        state={},
    )

    await websocket.send_json(
        {
            "type": "session_started",
            "session_id": session_ref["value"],
            "user_id": user_id,
            "camera_device": camera_ref["value"],
        }
    )

    queue_ref: dict[str, LiveRequestQueue] = {"value": LiveRequestQueue()}
    seen_tool_signatures: set[str] = set()

    logger.info(
        "Live session started: session_id=%s user_id=%s trace_tools=%s camera_device=%s",
        session_ref["value"],
        user_id,
        TRACE_TOOLS,
        camera_ref["value"],
    )

    async def upstream() -> None:
        try:
            while True:
                message = await websocket.receive()

                if message.get("type") == "websocket.disconnect":
                    break

                audio_bytes = message.get("bytes")
                text_message = message.get("text")

                if audio_bytes is not None:
                    try:
                        queue_ref["value"].send_realtime(
                            types.Blob(mime_type="audio/pcm;rate=16000", data=audio_bytes)
                        )
                    except Exception:
                        continue
                    continue

                if text_message is not None:
                    try:
                        parsed_payload = json.loads(text_message)
                    except json.JSONDecodeError:
                        parsed_payload = None

                    if isinstance(parsed_payload, Mapping):
                        message_type = str(parsed_payload.get("type", "")).strip()
                        if message_type == "camera_select":
                            next_camera = str(parsed_payload.get("camera_device", "")).strip()
                            if next_camera:
                                camera_ref["value"] = next_camera
                                await websocket.send_json(
                                    {
                                        "type": "camera_info",
                                        "device": next_camera,
                                        "switching": True,
                                    }
                                )
                            continue

                    try:
                        await _handle_text_message(text_message, queue_ref["value"])
                    except Exception:
                        continue

        except WebSocketDisconnect:
            logger.info("Client disconnected: %s", session_ref["value"])
        finally:
            queue_ref["value"].close()

    async def downstream() -> None:
        attempt = 0
        while True:
            try:
                active_queue = queue_ref["value"]
                run_config = _build_run_config()
                async for event in runner.run_live(
                    user_id=user_id,
                    session_id=session_ref["value"],
                    live_request_queue=active_queue,
                    run_config=run_config,
                ):
                    await _forward_event(websocket, event, seen_tool_signatures)
                return
            except Exception as exc:
                retryable = _is_retryable_live_error(exc)
                if not retryable:
                    logger.error("Live model stream failed: %s", exc)
                    try:
                        await websocket.send_json(
                            {"type": "error", "detail": f"Live model error: {exc}"}
                        )
                    except Exception:
                        pass
                    raise

                if attempt >= LIVE_RETRY_COUNT:
                    logger.warning(
                        "Live model stream failed (%s). Retry budget reached; continuing with rolling retries.",
                        exc,
                    )
                    attempt = 0

                attempt += 1
                delay = LIVE_RETRY_BACKOFF_SEC * attempt
                previous_queue = queue_ref["value"]
                queue_ref["value"] = LiveRequestQueue()
                previous_queue.close()
                previous_session = session_ref["value"]
                session_ref["value"] = f"session-{uuid4().hex}"
                await session_service.create_session(
                    app_name=APP_NAME,
                    user_id=user_id,
                    session_id=session_ref["value"],
                    state={},
                )
                seen_tool_signatures.clear()
                logger.warning(
                    "Live model stream failed (%s). Retrying %d/%d in %.1fs with new session %s (prev=%s)",
                    exc,
                    attempt,
                    LIVE_RETRY_COUNT,
                    delay,
                    session_ref["value"],
                    previous_session,
                )
                await websocket.send_json(
                    {
                        "type": "error",
                        "detail": f"Live model connection dropped. Retrying ({attempt}/{LIVE_RETRY_COUNT})...",
                    }
                )
                await websocket.send_json(
                    {
                        "type": "session_started",
                        "session_id": session_ref["value"],
                        "user_id": user_id,
                        "camera_device": camera_ref["value"],
                    }
                )
                await asyncio.sleep(delay)

    upstream_task = asyncio.create_task(upstream())
    downstream_task = asyncio.create_task(downstream())
    camera_task = asyncio.create_task(_run_server_camera_stream(websocket, queue_ref, camera_ref))

    done, pending = await asyncio.wait(
        {upstream_task, downstream_task, camera_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()

    await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        exception = task.exception()
        if exception:
            logger.error("WebSocket task failed: %s", exception)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.getenv("VOICE_AGENT_HOST", "0.0.0.0"),
        port=int(os.getenv("VOICE_AGENT_PORT", "8787")),
        reload=False,
    )
