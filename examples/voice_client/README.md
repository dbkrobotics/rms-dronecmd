# Voice-Enabled Gemini MCP Client

This client allows you to control your ROS 2 system using natural voice commands via Gemini. It connects to the local MCP server and uses Google's Speech Recognition and Text-to-Speech for a conversational interface.

## Prerequisites

- Python 3.10+
- A working microphone
- `uv` installed (for running the MCP server)
- `mpv` or `afplay` (macOS default) or `mpg123` for playing audio.
- Google Gemini API Key

## Setup

1. **Install Dependencies**:
   It is recommended to use a virtual environment.
   ```bash
   uv venv voice_env
   source voice_env/bin/activate
   uv pip install -r requirements.txt
   ```
   
   *Note: usage of `uv` is optional but recommended as per project standards.*

3. **Set Environment Variables**:
   ```bash
   export GOOGLE_API_KEY="your-api-key-here"
   # Optional: Set paths if mcp-ros-server is potentially in a different location
   # export MCP_SERVER_PATH="/path/to/mcp-ros-server" 
   ```

## Usage

Run the client:
```bash
python3 voice_client.py
```

- Be sure to speak clearly into your microphone.
- The terminal will show the current status (Listening, Thinking, Executing).
- You can say "exit" or "quit" to stop the program.

## Troubleshooting

- **Microphone issues**: Ensure PyAudio is installed correctly. On macOS, you might need `brew install portaudio`.
- **TTS issues**: Ensure you have a command-line audio player installed (e.g., `afplay` on macOS, `mpg123` on Linux).
