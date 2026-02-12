# Gemini Voice Client (Hooks Edition)

This project provides a **voice interface** for `gemini-cli` using native **Hooks**.
It allows you to speak to Gemini and hear its responses, using your browser's speech recognition and synthesis.

## Architecture

1.  **Voice Server (`voice_server.py`)**: A local bridge server (FastAPI + Socket.IO) running on `localhost:5111`.
    -   Serves the web client.
    -   Receives "Output" from Gemini Hook -> Sends to Browser (TTS).
    -   Receives "Input" from Browser -> Holds it for Gemini Hook (STT).
2.  **Web Client (`static/index.html`)**: Captures microphone input and plays audio.
3.  **Hooks (`hooks/*.py`)**: Python scripts triggered by `gemini-cli` events.
    -   `listen.py`: Runs `BeforeAgent`. Fetches voice input from server.
    -   `speak.py`: Runs `AfterModel`. Sends text to server for TTS.

## Setup

1.  **Install Python Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
    *(Ensure you have `fastapi`, `uvicorn`, `python-socketio` installed)*

2.  **Configure Gemini CLI Hooks**:
    Run the helper script to see the configuration you need to add to your `.gemini/config.json`:
    ```bash
    ./examples/install_hooks.sh
    ```
    Add the output JSON to your config file.

## Usage

1.  **Start the Voice Server**:
    ```bash
    python voice_server.py
    ```
    *Open `http://localhost:5111` in your browser.*

2.  **Activate Voice Mode**:
    -   Click the **Microphone Icon** on the web page to start listening.
    -   You can leave it running.

3.  **Run Gemini CLI**:
    ```bash
    gemini chat
    ```
    -   **Voice Input**: When the CLI is waiting for input (or just starting), speak to the browser. The `BeforeAgent` hook will inject your voice input.
    -   **Voice Output**: When Gemini responds, the browser will read the text aloud.

## Troubleshooting

-   **Timeout**: The `BeforeAgent` hook waits 60 seconds for voice input. If you don't speak, it might time out and proceed with empty input (or asking you to type).
-   **Permissions**: Ensure your browser has microphone permission allowed for `localhost:5111`.
