import os
import uvicorn
import socketio
import asyncio
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

# --- Configuration ---
PORT = int(os.getenv("PORT", 5111))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# --- Setup ---
# Initialize Socket.IO server
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
socket_app = socketio.ASGIApp(sio, app)

# --- Global State ---
# Store voice inputs in a list to support multiple sentences/fragments
voice_input_buffer = []
# Event to notify when new input is available (for long polling)
input_event = asyncio.Event()

# --- Models ---
class SpeakRequest(BaseModel):
    text: str

# --- Routes ---
@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.post("/hook/speak")
async def hook_speak(request: SpeakRequest):
    """
    Endpoint for Gemini CLI Hook to send text to be spoken.
    """
    text = request.text
    print(f"[Server] Gemini Output -> Browser: {text}")
    await sio.emit('speak_message', text)
    return {"status": "ok", "message": "Sent to browser"}

@app.get("/hook/listen")
async def hook_listen():
    """
    Endpoint for Gemini CLI Hook to fetch user voice input.
    """
    global voice_input_buffer
    print("[Server] CLI is listening for voice input...")
    
    # Wait for input if buffer is empty
    if not voice_input_buffer:
        try:
            # Wait up to 30s for input
            await asyncio.wait_for(input_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            print("[Server] Listen timed out.")
            return {"text": None}
    
    # Retrieve all buffered input
    input_text = " ".join(voice_input_buffer)
    voice_input_buffer = [] # Clear buffer
    input_event.clear() # Reset event
    
    print(f"[Server] Voice Input -> CLI: {input_text}")
    return {"text": input_text.strip()}

# --- Socket.IO Events ---
@sio.event
async def connect(sid, environ):
    print(f"[Socket] Client connected: {sid}")

@sio.event
async def disconnect(sid):
    print(f"[Socket] Client disconnected: {sid}")

@sio.event
async def audio_input(sid, data):
    """
    Received transcribed text from browser.
    Append to buffer.
    """
    global voice_input_buffer
    print(f"[Socket] Browser -> Server: {data}")
    
    if data:
        voice_input_buffer.append(data)
        input_event.set()

if __name__ == "__main__":
    uvicorn.run(socket_app, host="0.0.0.0", port=PORT)
