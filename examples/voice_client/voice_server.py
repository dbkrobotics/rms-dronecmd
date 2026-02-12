import os
import uvicorn
import socketio
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# --- Configuration ---
PORT = int(os.getenv("PORT", 5111))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# --- Setup ---
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
socket_app = socketio.ASGIApp(sio, app)

# --- Routes ---
@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

# --- Socket.IO Events ---
@sio.event
async def connect(sid, environ):
    print(f"Client connected: {sid}")

@sio.event
async def disconnect(sid):
    print(f"Client disconnected: {sid}")

# Browser -> Server -> Gemini Client
@sio.event
async def audio_input(sid, data):
    """
    Received transcribed text from browser (User's voice).
    Forward this to the Gemini Client listening on a specific channel/event mechanism.
    For simplicity, we broadcast to all clients designed as 'gemini_client'.
    """
    print(f"User said: {data}")
    # Broadcast to all connected clients (including the local python script)
    # The Python script will filter for this event
    await sio.emit('user_message', data)

# Gemini Client -> Server -> Browser
@sio.event
async def bot_output(sid, data):
    """
    Received text response from Gemini Client.
    Forward this to the Browser for TTS.
    """
    print(f"Gemini says: {data}")
    await sio.emit('speak_message', data)

if __name__ == "__main__":
    uvicorn.run(socket_app, host="0.0.0.0", port=PORT)
