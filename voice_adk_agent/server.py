from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import warnings
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

load_dotenv()

# ADK currently emits a noisy pydantic serializer warning for response_modalities
# in some versions. This does not affect runtime behavior for this app.
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
LIVE_RETRY_COUNT = int(os.getenv("VOICE_AGENT_LIVE_RETRY_COUNT", "2"))
LIVE_RETRY_BACKOFF_SEC = float(os.getenv("VOICE_AGENT_LIVE_RETRY_BACKOFF_SEC", "1.0"))

session_service = InMemorySessionService()
runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

app = FastAPI(title="Drone Voice ADK Agent")
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "app": APP_NAME})


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
            # Invalid argument / payload problems are usually deterministic.
            return False
        if status_code == 1011:
            # Model-side internal error is often transient.
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

    message_type = payload.get("type")

    if message_type == "text":
        text = str(payload.get("text", "")).strip()
        if text:
            live_request_queue.send_content(
                types.Content(
                    role="user",
                    parts=[types.Part(text=text)],
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
        }
    )

    queue_ref: dict[str, LiveRequestQueue] = {"value": LiveRequestQueue()}
    seen_tool_signatures: set[str] = set()

    logger.info(
        "Live session started: session_id=%s user_id=%s trace_tools=%s",
        session_ref["value"],
        user_id,
        TRACE_TOOLS,
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
                    queue_ref["value"].send_realtime(
                        types.Blob(mime_type="audio/pcm;rate=16000", data=audio_bytes)
                    )
                    continue

                if text_message is not None:
                    await _handle_text_message(text_message, queue_ref["value"])

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
                if attempt >= LIVE_RETRY_COUNT or not _is_retryable_live_error(exc):
                    logger.error("Live model stream failed: %s", exc)
                    try:
                        await websocket.send_json(
                            {"type": "error", "detail": f"Live model error: {exc}"}
                        )
                    except Exception:
                        pass
                    raise

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
                    }
                )
                await asyncio.sleep(delay)

    upstream_task = asyncio.create_task(upstream())
    downstream_task = asyncio.create_task(downstream())

    done, pending = await asyncio.wait(
        {upstream_task, downstream_task},
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
