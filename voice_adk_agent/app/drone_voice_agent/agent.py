from __future__ import annotations

import logging
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

FILLER_WORDS = {
    "uh",
    "um",
    "hmm",
    "like",
    "please",
    "just",
    "kind",
    "sort",
    "you",
    "know",
    "assistant",
    "gemini",
    "drone",
    "어",
    "음",
    "그",
    "저기",
    "그냥",
    "좀",
    "잠깐",
    "혹시",
}

ACTION_PATTERNS = [
    r"\\btake\\s*off\\b",
    r"\\bland\\b",
    r"\\barm\\b",
    r"\\bdisarm\\b",
    r"\\bhover\\b",
    r"\\breturn\\s*to\\s*launch\\b",
    r"\\breturn\\s*home\\b",
    r"\\brtl\\b",
    r"\\bstop\\b",
    r"\\bmove\\b",
    r"\\bgo\\b",
    r"\\bforward\\b",
    r"\\bbackward\\b",
    r"\\bleft\\b",
    r"\\bright\\b",
    r"\\bup\\b",
    r"\\bdown\\b",
    r"\\bturn\\b",
    r"\\brotate\\b",
    r"\\bcircle\\b",
    r"\\bsquare\\b",
    r"이륙",
    r"착륙",
    r"호버",
    r"정지",
    r"상승",
    r"하강",
    r"전진",
    r"후진",
    r"좌회전",
    r"우회전",
    r"복귀",
    r"귀환",
]

EXPLICIT_REPEAT_PATTERNS = ["again", "repeat", "one more", "다시", "한번 더", "한 번 더"]
CONFIRM_PHRASES = {
    "confirm",
    "confirm it",
    "execute",
    "execute it",
    "run",
    "run it",
    "do it",
    "go ahead",
    "thats correct",
    "that is correct",
    "확인",
    "실행",
    "진행",
    "맞아",
    "맞습니다",
    "오케이",
    "좋아",
}
CANCEL_KEYWORDS = {
    "cancel",
    "discard",
    "never mind",
    "start over",
    "drop it",
    "취소",
    "그만",
    "중지",
    "스탑",
}


def _collapse_adjacent_duplicates(tokens: list[str]) -> list[str]:
    if not tokens:
        return tokens

    collapsed = [tokens[0]]
    for token in tokens[1:]:
        if token != collapsed[-1]:
            collapsed.append(token)
    return collapsed


def _collapse_duplicate_bigrams(tokens: list[str]) -> list[str]:
    if len(tokens) < 4:
        return tokens

    cleaned: list[str] = []
    i = 0
    while i < len(tokens):
        if i + 3 < len(tokens) and tokens[i : i + 2] == tokens[i + 2 : i + 4]:
            cleaned.extend(tokens[i : i + 2])
            i += 4
            while i + 1 < len(tokens) and tokens[i : i + 2] == cleaned[-2:]:
                i += 2
            continue

        cleaned.append(tokens[i])
        i += 1

    return cleaned


def _normalize_text(raw_text: str) -> str:
    lowered = raw_text.lower().strip()
    lowered = re.sub(r"```[\\s\\S]*?```", " ", lowered)
    lowered = re.sub(r"[^0-9a-zA-Z가-힣/_\\-\\.\\s]", " ", lowered)
    lowered = re.sub(r"\\s+", " ", lowered).strip()

    if not lowered:
        return ""

    tokens = [token for token in lowered.split(" ") if token and token not in FILLER_WORDS]
    tokens = _collapse_adjacent_duplicates(tokens)
    tokens = _collapse_duplicate_bigrams(tokens)
    return " ".join(tokens).strip()


def _normalize_phrase_for_match(text: str) -> str:
    cleaned = text.lower().strip()
    cleaned = re.sub(r"[^0-9a-zA-Z가-힣\\s]", " ", cleaned)
    cleaned = re.sub(r"\\s+", " ", cleaned).strip()
    return cleaned


def _contains_action(normalized_text: str) -> bool:
    return any(re.search(pattern, normalized_text) for pattern in ACTION_PATTERNS)


def _is_confirm_phrase(raw_text: str) -> bool:
    normalized = _normalize_phrase_for_match(raw_text)
    return normalized in CONFIRM_PHRASES


def _is_cancel_phrase(raw_text: str) -> bool:
    normalized = _normalize_phrase_for_match(raw_text)
    return any(keyword in normalized for keyword in CANCEL_KEYWORDS)


def sanitize_voice_command(command: str, tool_context: ToolContext) -> dict[str, Any]:
    """Stages every spoken command first and requires explicit confirmation before execution."""

    raw_text = (command or "").strip()
    normalized = _normalize_text(raw_text)
    state = tool_context.state
    now = time.time()
    confirm_timeout_sec = float(os.getenv("VOICE_AGENT_CONFIRM_TIMEOUT_SEC", "45"))
    duplicate_window_sec = float(os.getenv("VOICE_AGENT_DUPLICATE_WINDOW_SEC", "8"))

    pending_command = str(state.get("pending_command", ""))
    pending_ts = float(state.get("pending_command_ts", 0.0) or 0.0)

    if pending_command and pending_ts > 0 and (now - pending_ts) > confirm_timeout_sec:
        state.pop("pending_command", None)
        state.pop("pending_command_ts", None)
        pending_command = ""

    if _is_confirm_phrase(raw_text):
        if pending_command:
            state["last_normalized_command"] = pending_command
            state["last_normalized_command_ts"] = now
            state.pop("pending_command", None)
            state.pop("pending_command_ts", None)
            return {
                "decision": "execute_pending",
                "command_to_execute": pending_command,
                "pending_command": pending_command,
                "normalized_command": pending_command,
                "actionable": True,
                "duplicate": False,
                "confirmation_required": False,
                "reason": "Confirmation accepted. Execute the staged command now.",
            }

        return {
            "decision": "noop",
            "command_to_execute": "",
            "pending_command": "",
            "normalized_command": "",
            "actionable": False,
            "duplicate": False,
            "confirmation_required": False,
            "reason": "No staged command exists. Ask for a command first.",
        }

    if _is_cancel_phrase(raw_text):
        if pending_command:
            state.pop("pending_command", None)
            state.pop("pending_command_ts", None)
            return {
                "decision": "cancelled",
                "command_to_execute": "",
                "pending_command": "",
                "normalized_command": "",
                "actionable": False,
                "duplicate": False,
                "confirmation_required": False,
                "reason": "Staged command was cancelled.",
            }

        return {
            "decision": "noop",
            "command_to_execute": "",
            "pending_command": "",
            "normalized_command": "",
            "actionable": False,
            "duplicate": False,
            "confirmation_required": False,
            "reason": "No staged command to cancel.",
        }

    if not normalized:
        return {
            "decision": "noop",
            "command_to_execute": "",
            "pending_command": pending_command,
            "normalized_command": "",
            "actionable": False,
            "duplicate": False,
            "confidence": 0.0,
            "confirmation_required": False,
            "reason": "No actionable speech detected.",
        }

    last_command = str(state.get("last_normalized_command", ""))
    last_timestamp = float(state.get("last_normalized_command_ts", 0.0) or 0.0)
    explicit_repeat = any(pattern in raw_text.lower() for pattern in EXPLICIT_REPEAT_PATTERNS)

    is_duplicate = (
        normalized == last_command
        and (now - last_timestamp) <= duplicate_window_sec
        and not explicit_repeat
    )

    if pending_command and normalized == pending_command:
        is_duplicate = True

    confidence = 0.8 if _contains_action(normalized) else 0.6

    if is_duplicate:
        return {
            "decision": "duplicate_blocked",
            "command_to_execute": "",
            "pending_command": pending_command or normalized,
            "normalized_command": normalized,
            "actionable": False,
            "duplicate": True,
            "confidence": 0.95,
            "confirmation_required": True,
            "reason": "Duplicate command detected. Waiting for an updated command or explicit confirm.",
        }

    # Stage everything user said. The execution is always gated by a follow-up confirm phrase.
    state["pending_command"] = normalized
    state["pending_command_ts"] = now

    return {
        "decision": "needs_confirmation",
        "command_to_execute": "",
        "pending_command": normalized,
        "normalized_command": normalized,
        "actionable": False,
        "duplicate": False,
        "confidence": confidence,
        "confirmation_required": True,
        "confirmation_phrase": "confirm",
        "reason": "Command staged. Do not execute yet. Ask user to say confirm.",
    }


def build_execution_prompt(staged_command: str) -> str:
    """Returns the exact command text that should be executed after confirmation."""

    normalized = _normalize_text(staged_command)
    return normalized if normalized else staged_command.strip()


def _extract_prompts_block_from_yaml(raw_yaml: str) -> str:
    match = re.search(r"(?ms)^prompts:\\s*\\|\\s*\\n(.*)$", raw_yaml)
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
            deduped.append(path)
            seen.add(key)

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
    ros_mcp_python = os.getenv(
        "ROS_MCP_SERVER_PYTHON", "/home/husl-ai/workspace/ros-mcp-server/venv/bin/python"
    )
    ros_mcp_script = os.getenv("ROS_MCP_SERVER_SCRIPT", "/home/husl-ai/workspace/ros-mcp-server/server.py")
    ros_mcp_timeout = int(os.getenv("ROS_MCP_TIMEOUT_SEC", "30"))

    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=ros_mcp_python,
                args=[ros_mcp_script],
            ),
            timeout=ros_mcp_timeout,
        ),
    )


BASE_AGENT_INSTRUCTION = """
You are a real-time drone voice control agent connected to ROS MCP tools.

Always follow this exact workflow for every user turn:
1. Call `sanitize_voice_command` with the user utterance first.
2. Read the `decision` field from the tool response and obey it strictly.
3. If `decision` is `needs_confirmation`, do not call ROS tools. Tell the user which command is staged and ask them to say `confirm`.
4. If `decision` is `duplicate_blocked`, do not call ROS tools. Ask the user to update the command or say `confirm`.
5. If `decision` is `cancelled`, acknowledge cancellation and wait for a new command.
6. If `decision` is `execute_pending`, call `build_execution_prompt` with `command_to_execute`, then execute that single command via ROS MCP tools.
7. Never execute ROS tools unless `decision` is `execute_pending`.
8. After tool execution, summarize what was executed and current status in <= 2 short sentences.

Safety and UX rules:
- Ignore filler words, stutters, and non-command chatter.
- Always stage first, then require explicit confirmation.
- Prefer high-level safe commands (takeoff, land, hover, rtl, move with distance/altitude).
- If critical details are missing (for example altitude for takeoff), ask a brief follow-up.
- Never invent ROS tool results.
- Keep responses concise and spoken-language friendly.
- Preserve user intent exactly; do not rewrite to a different action.
- Reply in Korean unless the user clearly uses English.
""".strip()

ROBOT_SPEC_CONTEXT, ROBOT_SPEC_PATH = _load_robot_spec_context()
if ROBOT_SPEC_CONTEXT:
    logger.info("Loaded drone specification context from %s", ROBOT_SPEC_PATH)
    AGENT_INSTRUCTION = (
        f"{BASE_AGENT_INSTRUCTION}\\n\\n"
        "You already have robot-specific control instructions below from drone_px4.yaml. "
        "Follow them strictly: ensure all waypoints and coordinate values are calculated based on absolute coordinates, and pay particular attention to custom action servers, RTL mode usage, and trajectory point-density rules.\\n\\n"
        f"DRONE_SPEC_CONTEXT_START\\n{ROBOT_SPEC_CONTEXT}\\nDRONE_SPEC_CONTEXT_END"
    )
else:
    logger.warning(
        "Could not load drone specification context. Set ROS_MCP_ROBOT_SPEC_PATH or ROS_MCP_SERVER_SCRIPT correctly."
    )
    AGENT_INSTRUCTION = BASE_AGENT_INSTRUCTION


root_agent = Agent(
    name="drone_voice_agent",
    model=os.getenv("VOICE_AGENT_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025"),
    description="Realtime voice agent for PX4 drone control via ROS MCP server.",
    instruction=AGENT_INSTRUCTION,
    tools=[
        FunctionTool(func=sanitize_voice_command),
        FunctionTool(func=build_execution_prompt),
        _build_ros_mcp_toolset(),
    ],
)
