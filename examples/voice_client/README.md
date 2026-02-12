# Voice-Enabled CLI Wrapper for Gemini

This project provides a **web-based voice interface** that wraps your existing CLI tools (like `gemini-cli` or `claude`). It uses the browser's native Speech API for high-quality speech recognition and synthesis, eliminating the need for complex local audio setup.

## Architecture

1.  **Voice Server (`voice_server.py`)**: A local bridge server (FastAPI + Socket.IO) running on `localhost:5111`.
2.  **Web Frontend (`static/index.html`)**: A lightweight web page that captures your voice and plays responses.
3.  **CLI Wrapper (`web_voice_wrapper.py`)**: A Python script that wraps your target CLI command, injecting voice input and capturing text output.

## Prerequisites

-   Python 3.10+
-   Modern Browser (Chrome, Edge, Safari) for Web Speech API support.
-   Your target CLI tool installed (e.g., `gemini` or `gemini-cli`).

## Setup

1.  **Install Dependencies**:
    ```bash
    uv venv voice_env
    source voice_env/bin/activate
    uv pip install -r requirements.txt
    ```

## Usage

You need two terminal windows running simultaneously.

### Terminal 1: Start the Voice Server
This will host the web interface and bridge.
```bash
python voice_server.py
```
*Open `http://localhost:5111` in your browser if it doesn't open automatically.*

### Terminal 2: Run the CLI Wrapper
Replace `gemini` with whatever command you usually use to run your CLI.
```bash
python web_voice_wrapper.py --command "gemini"
```

**Now, simply speak to the web page!**
-   Your voice will be transcribed and sent to the CLI.
-   The CLI's text output will be read aloud by the browser.
