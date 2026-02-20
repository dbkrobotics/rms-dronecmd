from __future__ import annotations

import logging
import math
import os
import re
import time
from pathlib import Path
from textwrap import dedent
from typing import Any

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.tools import FunctionTool, ToolContext
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

load_dotenv()
logger = logging.getLogger("voice_adk_agent.agent")

_CONFIRM_PHRASES = {
    "confirm",
    "confirm it",
    "execute",
    "execute it",
    "run",
    "run it",
    "do it",
    "go ahead",
    "approved",
}

_CANCEL_PHRASES = {
    "cancel",
    "discard",
    "never mind",
    "drop it",
    "stop",
}


def _normalize_phrase(text: str) -> str:
    normalized = text.lower().strip()
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _is_confirm_phrase(text: str) -> bool:
    return _normalize_phrase(text) in _CONFIRM_PHRASES


def _is_cancel_phrase(text: str) -> bool:
    normalized = _normalize_phrase(text)
    if normalized in _CANCEL_PHRASES:
        return True
    return any(token in normalized for token in _CANCEL_PHRASES)


def _confirm_timeout_sec() -> float:
    raw = os.getenv("VOICE_AGENT_CONFIRM_TIMEOUT_SEC", "45")
    try:
        value = float(raw)
    except Exception:
        value = 45.0
    return max(3.0, value)


def _clear_pending(state: Any) -> None:
    state["pending_execution_payload"] = ""
    state["pending_execution_ts"] = 0.0


def _get_pending(state: Any) -> tuple[str, float]:
    payload = str(state.get("pending_execution_payload", "") or "")
    ts = float(state.get("pending_execution_ts", 0.0) or 0.0)
    return payload, ts


def confirm_gate(
    user_utterance: str,
    requires_confirmation: bool,
    execution_payload: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:

    if tool_context is None:
        return {
            "decision": "error",
            "reason": "tool_context is required",
            "command_to_execute": "",
            "pending_command": "",
        }

    state = tool_context.state
    now = time.time()
    timeout_sec = _confirm_timeout_sec()

    pending_command, pending_ts = _get_pending(state)
    if pending_command and pending_ts > 0 and (now - pending_ts) > timeout_sec:
        _clear_pending(state)
        pending_command = ""

    utterance = (user_utterance or "").strip()
    payload = (execution_payload or "").strip()

    if _is_confirm_phrase(utterance):
        if pending_command:
            _clear_pending(state)
            state["last_executed_payload"] = pending_command
            state["last_executed_ts"] = now
            return {
                "decision": "execute_pending",
                "reason": "Confirmation accepted",
                "command_to_execute": pending_command,
                "pending_command": "",
                "confirmation_required": False,
            }
        return {
            "decision": "noop",
            "reason": "No staged command to confirm",
            "command_to_execute": "",
            "pending_command": "",
            "confirmation_required": False,
        }

    if _is_cancel_phrase(utterance):
        if pending_command:
            _clear_pending(state)
            return {
                "decision": "cancelled",
                "reason": "Staged command cancelled",
                "command_to_execute": "",
                "pending_command": "",
                "confirmation_required": False,
            }
        return {
            "decision": "noop",
            "reason": "No staged command to cancel",
            "command_to_execute": "",
            "pending_command": "",
            "confirmation_required": False,
        }

    if requires_confirmation:
        if not payload:
            return {
                "decision": "error",
                "reason": "requires_confirmation=true but execution_payload is empty",
                "command_to_execute": "",
                "pending_command": pending_command,
                "confirmation_required": True,
                "confirmation_phrase": "confirm",
            }

        state["pending_execution_payload"] = payload
        state["pending_execution_ts"] = now

        return {
            "decision": "staged",
            "reason": "Command staged. Wait for explicit confirm.",
            "command_to_execute": "",
            "pending_command": payload,
            "confirmation_required": True,
            "confirmation_phrase": "confirm",
            "timeout_sec": timeout_sec,
        }

    if pending_command:
        return {
            "decision": "passthrough",
            "reason": "Non-motion request. A staged command still exists.",
            "command_to_execute": "",
            "pending_command": pending_command,
            "confirmation_required": True,
            "confirmation_phrase": "confirm",
        }

    return {
        "decision": "passthrough",
        "reason": "No confirmation required",
        "command_to_execute": "",
        "pending_command": "",
        "confirmation_required": False,
    }


def compute_standoff_waypoint(
    drone_x: float,
    drone_y: float,
    drone_z: float,
    target_x: float,
    target_y: float,
    target_z: float,
    standoff_m: float = 0.5,
    min_altitude_m: float = 0.5,
    max_altitude_m: float = 2.0,
    keep_target_altitude: bool = True,
) -> dict[str, Any]:

    standoff = max(0.0, float(standoff_m))
    min_alt = float(min_altitude_m)
    max_alt = max(min_alt, float(max_altitude_m))

    vx = float(target_x) - float(drone_x)
    vy = float(target_y) - float(drone_y)
    vz = float(target_z) - float(drone_z)
    distance = math.sqrt(vx * vx + vy * vy + vz * vz)

    if distance < 1e-6:
        waypoint_x = float(drone_x)
        waypoint_y = float(drone_y)
    else:
        travel = max(0.0, distance - standoff)
        scale = travel / distance
        waypoint_x = float(drone_x) + (vx * scale)
        waypoint_y = float(drone_y) + (vy * scale)

    if keep_target_altitude:
        waypoint_z = float(target_z)
    else:
        waypoint_z = float(drone_z)

    waypoint_z = min(max_alt, max(min_alt, waypoint_z))

    remaining = math.sqrt(
        (float(target_x) - waypoint_x) ** 2
        + (float(target_y) - waypoint_y) ** 2
        + (float(target_z) - waypoint_z) ** 2
    )

    return {
        "success": True,
        "waypoint": {
            "x": waypoint_x,
            "y": waypoint_y,
            "z": waypoint_z,
        },
        "distance_drone_to_target_m": distance,
        "distance_waypoint_to_target_m": remaining,
        "requested_standoff_m": standoff,
    }


def _extract_prompts_block_from_yaml(raw_yaml: str) -> str:
    match = re.search(r"(?ms)^prompts:\s*\|\s*\n(.*)$", raw_yaml)
    if not match:
        return ""
    return dedent(match.group(1)).strip()


def _candidate_robot_spec_paths() -> list[Path]:
    explicit = os.getenv("ROS_MCP_ROBOT_SPEC_PATH", "").strip()
    candidates: list[Path] = []

    if explicit:
        candidates.append(Path(explicit).expanduser())

    ros_mcp_script = os.getenv("ROS_MCP_SERVER_SCRIPT", "").strip()
    if ros_mcp_script:
        script_path = Path(ros_mcp_script).expanduser()
        candidates.append(script_path.parent / "robot_specifications" / "drone_px4.yaml")

    workspace_root = Path(__file__).resolve().parents[4]
    candidates.append(workspace_root / "mcp-ros-server" / "robot_specifications" / "drone_px4.yaml")
    candidates.append(workspace_root / "rms-dronecmd" / "robot_specifications" / "drone_px4.yaml")

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            deduped.append(path)

    return deduped


def _load_robot_spec_context() -> tuple[str, str]:
    for path in _candidate_robot_spec_paths():
        try:
            if not path.exists():
                continue
            raw = path.read_text(encoding="utf-8")
            prompts = _extract_prompts_block_from_yaml(raw)
            if prompts:
                return prompts, str(path)
        except Exception:
            continue

    return "", ""


def _build_ros_mcp_toolset() -> McpToolset:
    ros_mcp_python = os.getenv("ROS_MCP_SERVER_PYTHON")
    if not ros_mcp_python:
        raise ValueError("ROS_MCP_SERVER_PYTHON environment variable is not set. Please check your .env file.")

    ros_mcp_script = os.getenv("ROS_MCP_SERVER_SCRIPT")
    if not ros_mcp_script:
        raise ValueError("ROS_MCP_SERVER_SCRIPT environment variable is not set. Please check your .env file.")
    ros_mcp_timeout = int(os.getenv("ROS_MCP_TIMEOUT_SEC", "30"))
    ros_mcp_stderr_log = os.getenv("ROS_MCP_STDERR_LOG_PATH", "/tmp/ros_mcp_server_stderr.log")
    wrap_stderr = os.getenv("VOICE_AGENT_WRAP_ROS_MCP_STDERR", "true").lower() not in {
        "0",
        "false",
        "no",
    }

    wrapper_path = Path(__file__).resolve().parents[2] / "scripts" / "ros_mcp_stdio_wrapper.sh"
    if wrap_stderr and wrapper_path.exists():
        command = str(wrapper_path)
        args = [ros_mcp_python, ros_mcp_script, ros_mcp_stderr_log]
        logger.info("Using ROS MCP stdio wrapper. stderr -> %s", ros_mcp_stderr_log)
    else:
        command = ros_mcp_python
        args = [ros_mcp_script]
        logger.info("Using direct ROS MCP stdio process.")

    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=command,
                args=args,
            ),
            timeout=ros_mcp_timeout,
        ),
    )


BASE_AGENT_INSTRUCTION = """
You are a perception-first drone agent. Respond in ENGLISH.

Core role split:
- Vision node (`/drone_vision/get_object_3d`) is the eye: perception only.
- You are the brain: intent reasoning, planning, and control sequencing.
- Always follow this order for navigation tasks: Scanning -> Planning -> Moving.

Mandatory workflow for every user turn:
1. Decide intent with your own reasoning (do not rely on keyword regex patterns).
2. Determine whether this turn requires confirmation:
   - `requires_confirmation=true` for any command that can move the drone or change flight mode/state.
   - `requires_confirmation=false` for read-only status queries or pure visual analysis.
3. Call `confirm_gate(user_utterance, requires_confirmation, execution_payload)` exactly once.
   - `execution_payload` must be a concise JSON string only when `requires_confirmation=true`.
   - Use schema like: '{"intent": "fly_to_target", "target_query": "red cup", "standoff_m": 1.0}'.
4. Obey gate result strictly:
   - `staged`: do not execute ROS control tools. Ask user to say `confirm` (or `cancel`).
   - `execute_pending`: execute only `command_to_execute`.
   - `cancelled`: acknowledge cancellation and wait.
   - `passthrough`: proceed with non-motion tools or response.
   - `noop`/`error`: explain briefly and wait.

Perception-first planning rules:
- For camera-grounded navigation (e.g., move to visible target, avoid obstacle, pass through opening):
  1) Call `/drone_vision/get_object_3d` with a precise `target_query`.
     - Send only `target_query` and `min_confidence`.
  2) If not found or depth/transform is invalid, do not move. Ask for a better view or target phrase.
  3) Read drone pose from `/mavros/local_position/pose`.
  4) Call `compute_standoff_waypoint(...)` to compute a safe waypoint.
  5) Execute `/drone_control/trajectory` with absolute coordinates.

Safety:
- Never invent tool outputs.
- Never execute movement without `execute_pending` from `confirm_gate`.
- If confidence/depth is weak or missing, refuse movement and explain why.
- Do not call local file/image tools (for example `analyze_previously_received_image`).
- For visual grounding, use only `/drone_vision/get_object_3d`.
- Keep responses concise and operational.
""".strip()

ROBOT_SPEC_CONTEXT, ROBOT_SPEC_PATH = _load_robot_spec_context()
if ROBOT_SPEC_CONTEXT:
    logger.info("Loaded drone specification context from %s", ROBOT_SPEC_PATH)
    AGENT_INSTRUCTION = (
        f"{BASE_AGENT_INSTRUCTION}\n\n"
        "Robot-specific execution context:\n"
        f"DRONE_SPEC_CONTEXT_START\n{ROBOT_SPEC_CONTEXT}\nDRONE_SPEC_CONTEXT_END"
    )
else:
    logger.warning(
        "Could not load drone specification context. Set ROS_MCP_ROBOT_SPEC_PATH or ROS_MCP_SERVER_SCRIPT correctly."
    )
    AGENT_INSTRUCTION = BASE_AGENT_INSTRUCTION

voice_agent_model = os.getenv("VOICE_AGENT_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")

root_agent = Agent(
    name="drone_voice_agent",
    model=voice_agent_model,
    description="Realtime voice agent for PX4 drone control via ROS MCP server.",
    instruction=AGENT_INSTRUCTION,
    tools=[
        FunctionTool(func=confirm_gate),
        FunctionTool(func=compute_standoff_waypoint),
        _build_ros_mcp_toolset(),
    ],
)
