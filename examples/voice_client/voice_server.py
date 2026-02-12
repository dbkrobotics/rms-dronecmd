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
# Store the latest voice input from the user to be picked up by the CLI hook
latest_voice_input: Optional[str] = None
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
    Endpoint for Gemini CLI Hook (AfterModel/Output) to send text to be spoken.
    """
    text = request.text
    print(f"[Server] Gemini Output -> Browser: {text}")
    await sio.emit('speak_message', text)
    return {"status": "ok", "message": "Sent to browser"}

@app.get("/hook/listen")
async def hook_listen():
    """
    Endpoint for Gemini CLI Hook (BeforeAgent/Input) to fetch user voice input.
    This implementation uses long-polling: it waits until voice input is available.
    """
    global latest_voice_input
    print("[Server] CLI is listening for voice input...")
    
    # Wait for input (with a timeout to prevent hanging forever if needed, 
    # but hooks might have their own timeout. Let's wait up to 30s)
    try:
        await asyncio.wait_for(input_event.wait(), timeout=30.0)
        
        # Reset event and retrieve data
        input_text = latest_voice_input
        latest_voice_input = None
        input_event.clear()
        
        # If input was cleared or empty, return nothing
        if not input_text:
             return {"text": None}

        print(f"[Server] Voice Input -> CLI: {input_text}")
        return {"text": input_text}
        
    except asyncio.TimeoutError:
        print("[Server] Listen timed out.")
        return {"text": None}

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
    Received transcribed text from browser (User's voice).
    Store it so the /hook/listen endpoint can pick it up.
    """
    global latest_voice_input
    print(f"[Socket] Browser -> Server: {data}")
    
    latest_voice_input = data
    # Notify waiting listeners
    input_event.set()

if __name__ == "__main__":
    uvicorn.run(socket_app, host="0.0.0.0", port=PORT)
