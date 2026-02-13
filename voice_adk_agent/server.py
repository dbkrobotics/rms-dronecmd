from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
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

from app.drone_voice_agent import root_agent

load_dotenv()

logger = logging.getLogger("voice_adk_agent")
logging.basicConfig(level=os.getenv("VOICE_AGENT_LOG_LEVEL", "INFO"))

APP_NAME = os.getenv("VOICE_AGENT_APP_NAME", "drone_voice_agent_app")
WEB_DIR = Path(__file__).resolve().parent / "web"

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
    response_modalities = ["AUDIO"] if response_modality == "AUDIO" else ["TEXT"]

    kwargs: dict[str, Any] = {
        "streaming_mode": StreamingMode.BIDI,
        "response_modalities": response_modalities,
        "input_audio_transcription": {},
        "output_audio_transcription": {},
    }

    if "AUDIO" in response_modalities:
        kwargs["speech_config"] = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=os.getenv("VOICE_AGENT_VOICE_NAME", "Aoede")
                )
            )
        )

    return RunConfig(**kwargs)


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


async def _forward_event(websocket: WebSocket, event: Any) -> None:
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

    if bool(getattr(event, "interrupted", False)):
        await websocket.send_json({"type": "interrupted"})

    if bool(getattr(event, "turn_complete", False)):
        await websocket.send_json({"type": "turn_complete"})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()

    user_id = f"user-{uuid4().hex}"
    session_id = f"session-{uuid4().hex}"

    await session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
        state={},
    )

    await websocket.send_json(
        {
            "type": "session_started",
            "session_id": session_id,
            "user_id": user_id,
        }
    )

    live_request_queue = LiveRequestQueue()
    run_config = _build_run_config()

    async def upstream() -> None:
        try:
            while True:
                message = await websocket.receive()

                if message.get("type") == "websocket.disconnect":
                    break

                audio_bytes = message.get("bytes")
                text_message = message.get("text")

                if audio_bytes is not None:
                    live_request_queue.send_realtime(
                        types.Blob(mime_type="audio/pcm;rate=16000", data=audio_bytes)
                    )
                    continue

                if text_message is not None:
                    await _handle_text_message(text_message, live_request_queue)

        except WebSocketDisconnect:
            logger.info("Client disconnected: %s", session_id)
        finally:
            live_request_queue.close()

    async def downstream() -> None:
        async for event in runner.run_live(
            user_id=user_id,
            session_id=session_id,
            live_request_queue=live_request_queue,
            run_config=run_config,
        ):
            await _forward_event(websocket, event)

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
            logger.exception("WebSocket task failed: %s", exception)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.getenv("VOICE_AGENT_HOST", "0.0.0.0"),
        port=int(os.getenv("VOICE_AGENT_PORT", "8787")),
        reload=False,
    )
