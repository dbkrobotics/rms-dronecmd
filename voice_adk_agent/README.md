# Voice ADK Agent (Independent from `voice_hooks`)

This folder provides a new real-time voice control stack based on **Google ADK streaming**.
It does **not** change or depend on `voice_hooks`.

## Goal

- Human speaks to a real-time voice agent.
- The agent directly uses `ros-mcp-server` tools via MCP stdio (preferred path).
- Noise, filler words, and accidental immediate duplicates are filtered before execution.
- Every command is staged first and is executed only after explicit `confirm`.

## Architecture

1. Browser captures microphone audio (`16kHz PCM`) and sends it via WebSocket.
2. `server.py` forwards audio to ADK `Runner.run_live(...)` in `BIDI` streaming mode.
3. ADK agent (`app/drone_voice_agent/agent.py`) calls:
- `sanitize_voice_command` (guardrail tool)
- ROS MCP tools (through `McpToolset` + `StdioConnectionParams`)
4. Assistant audio (`24kHz PCM`) streams back to browser and plays in near real time.

## Files

- `app/drone_voice_agent/agent.py`: ADK agent + command guardrail + direct ROS MCP toolset
- `server.py`: FastAPI + WebSocket real-time streaming server
- `web/index.html`: browser UI
- `web/app.js`: mic capture + websocket + audio playback
- `web/audio-capture-worklet.js`: PCM capture worklet
- `.env.example`: runtime configuration template

## Setup (Ubuntu 24.04)

```bash
cd /home/husl-ai/workspace/ros2_ws/src/rms-dronecmd/voice_adk_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

- Set `GOOGLE_API_KEY`
- Keep `ROS_MCP_SERVER_PYTHON` and `ROS_MCP_SERVER_SCRIPT` aligned with your runtime machine paths
- Optional: set `ROS_MCP_ROBOT_SPEC_PATH` to force-load `drone_px4.yaml` context
- Default behavior redirects noisy ROS MCP stderr logs to `/tmp/ros_mcp_server_stderr.log`
- Optional: tune third-party SDK log verbosity with `VOICE_AGENT_THIRD_PARTY_LOG_LEVEL` (default: `CRITICAL`)

## Run (ngrok only)

```bash
cd /home/husl-ai/workspace/ros2_ws/src/rms-dronecmd/voice_adk_agent
source .venv/bin/activate
./scripts/run_with_ngrok.sh
```

Open either:

- Printed `ngrok public URL` (phone/browser external access)
- `http://localhost:8787` (same machine)

Then:

1. Click `Connect`
2. Click `Start Mic`
3. Speak a command
4. Say `confirm` to execute (say `cancel` to discard)

## Notes

- This implementation is designed to replace the old hook-based voice loop behavior without deleting existing code.
- If you want to run in text-only mode, set `VOICE_AGENT_RESPONSE_MODALITY=TEXT`.
- The guardrail currently blocks immediate duplicate commands within `VOICE_AGENT_DUPLICATE_WINDOW_SEC`.
- Execution safety: commands are queued and require explicit `confirm`.
- Read-only status queries (position/altitude/battery/state) run without confirmation using ROS MCP read tools.
- Tool observability: terminal now prints `TOOL_CALL` / `TOOL_RESULT` traces, and the web UI event log shows the same.
- If needed, inspect low-level ROS MCP stderr in `/tmp/ros_mcp_server_stderr.log`.
- Optional retry for transient live model drops is controlled by `VOICE_AGENT_LIVE_RETRY_COUNT` (default: `4`).
