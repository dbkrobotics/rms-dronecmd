# Voice ADK Agent

## Goal

- Run a real-time voice agent for PX4 control via ROS MCP.
- Stream server-side camera frames (USB RGB camera) directly into the same live ADK session.
- Use perception-first control:
  - Vision node only localizes objects.
  - Agent plans and controls trajectory.

## Runtime Flow

1. Browser sends microphone audio over WebSocket.
2. `server.py` captures server camera (`/dev/video*`) frames in memory (JPEG, no file save).
3. Audio + image frames stream into ADK live session.
4. Agent (`app/drone_voice_agent/agent.py`) uses:
   - `confirm_gate` (state-only confirm/cancel gate)
   - `compute_standoff_waypoint` (deterministic waypoint helper)
   - ROS MCP tools (actions/services/topics)
5. Browser receives assistant audio, camera preview, planned trajectory, and latest detection bbox overlay.

## Important Behavior

- Intent parsing is LLM-driven (regex-heavy command classifier removed).
- Any movement/flight-state command is staged first.
- Drone movement executes only after explicit `confirm`.
- Visual navigation uses `/drone_vision/get_object_3d` first, then trajectory planning.
- Control loop is explicitly `Scanning -> Planning -> Moving`.

## Files

- `app/drone_voice_agent/agent.py`: LLM instruction + confirm gate + waypoint helper + MCP toolset
- `server.py`: FastAPI + WebSocket + camera streaming
- `web/index.html`: UI
- `web/app.js`: mic/audio + ws + server camera selection
- `.env.example`: config template

## Setup (Ubuntu 24.04)

```bash
cd /home/husl-ai/workspace/ros2_ws/src/rms-dronecmd/voice_adk_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

- `GOOGLE_API_KEY`
- `ROS_MCP_SERVER_PYTHON`
- `ROS_MCP_SERVER_SCRIPT`
- Optional: `ROS_MCP_ROBOT_SPEC_PATH`
- Camera source: `VOICE_AGENT_CAMERA_DEVICE` (example: `/dev/video0`)

## Run (ngrok)

```bash
cd /home/husl-ai/workspace/ros2_ws/src/rms-dronecmd/voice_adk_agent
source .venv/bin/activate
./scripts/run_with_ngrok.sh
```

Then:

1. Open the printed ngrok URL.
2. Click `Connect`.
3. Select server camera.
4. Click `Start Mic`.
5. Speak command.
6. Say `confirm` to execute staged motion command (`cancel` to drop).

## Notes

- Camera selection in UI always targets server-side `/dev/video*`, not phone cameras.
- `object_locator` uses a separate Gemini detection model (`DRONE_GEMINI_MODEL_ID`, optional `DRONE_GEMINI_API_VERSION`) from `VOICE_AGENT_MODEL`, plus `DRONE_DEPTH_MODEL_ID`.
- To avoid camera lock conflicts, set `DRONE_CAMERA_FRAME_JPEG_URL=http://127.0.0.1:8787/api/camera/latest.jpg` so `object_locator` consumes server-owned frames instead of opening `/dev/video*` again.
- If you see repeated V4L2 timeout warnings, choose a different `/dev/video*` and keep `VOICE_AGENT_CAMERA_PROBE_READABLE=false`; only enable probing for diagnostics.
- For camera-grounded movement, if object/depth/localization confidence is insufficient, the agent should refuse movement and ask for a clearer view/target.
- Use `VOICE_AGENT_TRACE_TOOLS=true` to inspect tool call/result logs.
