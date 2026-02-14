from __future__ import annotations

import logging
import os
import re
import threading
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

_FALLBACK_LOCK = threading.RLock()
_STAGED_BY_CONTEXT: dict[str, tuple[str, float]] = {}
_APPROVED_BY_CONTEXT: dict[str, tuple[str, float]] = {}

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
}

ACTION_PATTERNS = [
    r"\btake\s*off\b",
    r"\bland\b",
    r"\barm\b",
    r"\bdisarm\b",
    r"\bhover\b",
    r"\breturn\s*to\s*launch\b",
    r"\breturn\s*home\b",
    r"\brtl\b",
    r"\bstop\b",
    r"\bmove\b",
    r"\bgo\b",
    r"\bforward\b",
    r"\bbackward\b",
    r"\bleft\b",
    r"\bright\b",
    r"\bup\b",
    r"\bdown\b",
    r"\bturn\b",
    r"\brotate\b",
    r"\bcircle\b",
    r"\bsquare\b",
]

EXPLICIT_REPEAT_PATTERNS = ["again", "repeat", "one more"]
NON_COMMAND_EXACT_PHRASES = {
    "command staged",
    "staged command",
    "i m sorry",
    "sorry",
    "okay",
    "ok",
    "thanks",
    "thank you",
}
NON_COMMAND_PATTERNS = [
    r"\bask user to say confirm\b",
    r"\bdo not execute yet\b",
    r"\bconfirmation accepted\b",
    r"\bno staged command\b",
    r"\bstaged command was cancelled\b",
    r"\bwaiting for\b.*\bconfirm\b",
]
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
}
CANCEL_KEYWORDS = {
    "cancel",
    "discard",
    "never mind",
    "start over",
    "drop it",
}


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except Exception:
        return default
    return value if value >= minimum else default


def _context_key(tool_context: ToolContext) -> str:
    for attr in ("session_id", "user_id", "invocation_id"):
        value = getattr(tool_context, attr, None)
        if value:
            return f"{attr}:{value}"

    nested = getattr(tool_context, "invocation_context", None) or getattr(
        tool_context, "_invocation_context", None
    )
    if nested is not None:
        for attr in ("session_id", "user_id", "id"):
            value = getattr(nested, attr, None)
            if value:
                return f"nested-{attr}:{value}"

        session = getattr(nested, "session", None)
        if session is not None:
            sid = getattr(session, "session_id", None) or getattr(session, "id", None)
            if sid:
                return f"session:{sid}"

    return "default"


def _set_fallback_staged(context_key: str, command: str, timestamp: float) -> None:
    with _FALLBACK_LOCK:
        _STAGED_BY_CONTEXT[context_key] = (command, timestamp)


def _get_fallback_staged(context_key: str) -> tuple[str, float]:
    with _FALLBACK_LOCK:
        return _STAGED_BY_CONTEXT.get(context_key, ("", 0.0))


def _clear_fallback_staged(context_key: str) -> None:
    with _FALLBACK_LOCK:
        _STAGED_BY_CONTEXT.pop(context_key, None)


def _set_fallback_approved(context_key: str, command: str, timestamp: float) -> None:
    with _FALLBACK_LOCK:
        _APPROVED_BY_CONTEXT[context_key] = (command, timestamp)


def _get_fallback_approved(context_key: str) -> tuple[str, float]:
    with _FALLBACK_LOCK:
        return _APPROVED_BY_CONTEXT.get(context_key, ("", 0.0))


def _clear_fallback_approved(context_key: str) -> None:
    with _FALLBACK_LOCK:
        _APPROVED_BY_CONTEXT.pop(context_key, None)


def _collapse_adjacent_duplicates(tokens: list[str]) -> list[str]:
    if not tokens:
        return tokens

    collapsed = [tokens[0]]
    for token in tokens[1:]:
        prev = collapsed[-1]
        if token == prev and re.fullmatch(r"[xyz]?-?\d+(?:\.\d+)?", token):
            collapsed.append(token)
            continue
        if token != prev:
            collapsed.append(token)
    return collapsed


def _collapse_duplicate_bigrams(tokens: list[str]) -> list[str]:
    if len(tokens) < 4:
        return tokens

    cleaned: list[str] = []
    i = 0
    while i < len(tokens):
        if i + 3 < len(tokens) and tokens[i : i + 2] == tokens[i + 2 : i + 4]:
            pair = tokens[i : i + 2]
            if any(re.fullmatch(r"[xyz]?-?\d+(?:\.\d+)?", item) for item in pair):
                cleaned.append(tokens[i])
                i += 1
                continue
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
    lowered = re.sub(r"```[\s\S]*?```", " ", lowered)
    lowered = re.sub(r"[^0-9a-zA-Z/_\-\.\s]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()

    if not lowered:
        return ""

    tokens = [token for token in lowered.split(" ") if token and token not in FILLER_WORDS]
    tokens = _collapse_adjacent_duplicates(tokens)
    tokens = _collapse_duplicate_bigrams(tokens)
    return " ".join(tokens).strip()


def _normalize_phrase_for_match(text: str) -> str:
    cleaned = text.lower().strip()
    cleaned = re.sub(r"[^0-9a-zA-Z\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _clear_pending_command(state: Any) -> None:
    # ADK State is not a dict and doesn't implement pop(); overwrite keys instead.
    state["pending_command"] = ""
    state["pending_command_ts"] = 0.0


def _clear_staged_backup(state: Any) -> None:
    state["staged_command_backup"] = ""
    state["staged_command_backup_ts"] = 0.0


def _clear_execution_approval(state: Any) -> None:
    state["approved_command"] = ""
    state["approved_command_ts"] = 0.0


def _contains_action(normalized_text: str) -> bool:
    return any(re.search(pattern, normalized_text) for pattern in ACTION_PATTERNS)


def _is_non_command_chatter(normalized_text: str) -> bool:
    if normalized_text in NON_COMMAND_EXACT_PHRASES:
        return True
    return any(re.search(pattern, normalized_text) for pattern in NON_COMMAND_PATTERNS)


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
    context_key = _context_key(tool_context)
    now = time.time()
    confirm_timeout_sec = _env_float("VOICE_AGENT_CONFIRM_TIMEOUT_SEC", 45.0, 3.0)
    duplicate_window_sec = _env_float("VOICE_AGENT_DUPLICATE_WINDOW_SEC", 8.0, 0.0)
    approval_timeout_sec = _env_float("VOICE_AGENT_APPROVAL_TIMEOUT_SEC", 20.0, 3.0)

    pending_command = str(state.get("pending_command", ""))
    pending_ts = float(state.get("pending_command_ts", 0.0) or 0.0)
    staged_backup = str(state.get("staged_command_backup", ""))
    staged_backup_ts = float(state.get("staged_command_backup_ts", 0.0) or 0.0)
    approved_command = str(state.get("approved_command", ""))
    approved_ts = float(state.get("approved_command_ts", 0.0) or 0.0)

    fallback_staged, fallback_staged_ts = _get_fallback_staged(context_key)
    fallback_approved, fallback_approved_ts = _get_fallback_approved(context_key)

    if not pending_command and fallback_staged and fallback_staged_ts > 0:
        if (now - fallback_staged_ts) <= confirm_timeout_sec:
            pending_command = fallback_staged
        else:
            _clear_fallback_staged(context_key)
    if not approved_command and fallback_approved and fallback_approved_ts > 0:
        if (now - fallback_approved_ts) <= approval_timeout_sec:
            approved_command = fallback_approved
            approved_ts = fallback_approved_ts
        else:
            _clear_fallback_approved(context_key)

    if pending_command and pending_ts > 0 and (now - pending_ts) > confirm_timeout_sec:
        _clear_pending_command(state)
        pending_command = ""
        _clear_execution_approval(state)
        approved_command = ""
        _clear_staged_backup(state)
        staged_backup = ""
        _clear_fallback_staged(context_key)
        _clear_fallback_approved(context_key)

    if staged_backup and staged_backup_ts > 0 and (now - staged_backup_ts) > confirm_timeout_sec:
        _clear_staged_backup(state)
        staged_backup = ""
        _clear_fallback_staged(context_key)

    if approved_command and approved_ts > 0 and (now - approved_ts) > approval_timeout_sec:
        _clear_execution_approval(state)
        approved_command = ""
        _clear_fallback_approved(context_key)

    if _is_confirm_phrase(raw_text):
        if not pending_command and staged_backup:
            pending_command = staged_backup
        if not pending_command:
            fallback_staged, fallback_staged_ts = _get_fallback_staged(context_key)
            if fallback_staged and (now - fallback_staged_ts) <= confirm_timeout_sec:
                pending_command = fallback_staged

        if pending_command:
            state["last_normalized_command"] = pending_command
            state["last_normalized_command_ts"] = now
            state["approved_command"] = pending_command
            state["approved_command_ts"] = now
            _set_fallback_approved(context_key, pending_command, now)
            _clear_pending_command(state)
            _clear_staged_backup(state)
            _clear_fallback_staged(context_key)
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
        _clear_execution_approval(state)
        _clear_staged_backup(state)
        _clear_fallback_staged(context_key)
        _clear_fallback_approved(context_key)
        if pending_command:
            _clear_pending_command(state)
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

    has_action = _contains_action(normalized)
    if _is_non_command_chatter(normalized):
        if pending_command:
            return {
                "decision": "needs_confirmation",
                "command_to_execute": "",
                "pending_command": pending_command,
                "normalized_command": normalized,
                "actionable": False,
                "duplicate": False,
                "confidence": 0.0,
                "confirmation_required": True,
                "confirmation_phrase": "confirm",
                "reason": "Ignored non-command speech. Still waiting for confirm or cancel.",
            }
        return {
            "decision": "noop",
            "command_to_execute": "",
            "pending_command": "",
            "normalized_command": normalized,
            "actionable": False,
            "duplicate": False,
            "confidence": 0.0,
            "confirmation_required": False,
            "reason": "Ignored non-command speech.",
        }

    if not has_action:
        if pending_command:
            return {
                "decision": "needs_confirmation",
                "command_to_execute": "",
                "pending_command": pending_command,
                "normalized_command": normalized,
                "actionable": False,
                "duplicate": False,
                "confidence": 0.4,
                "confirmation_required": True,
                "confirmation_phrase": "confirm",
                "reason": "No clear drone action detected. Keep pending command and wait for confirm/cancel.",
            }
        return {
            "decision": "noop",
            "command_to_execute": "",
            "pending_command": "",
            "normalized_command": normalized,
            "actionable": False,
            "duplicate": False,
            "confidence": 0.2,
            "confirmation_required": False,
            "reason": "No clear drone action detected. Say a drone command first.",
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

    confidence = 0.8 if has_action else 0.6

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

    # Stage only actionable drone commands. Execution is gated by explicit confirm.
    state["pending_command"] = normalized
    state["pending_command_ts"] = now
    state["staged_command_backup"] = normalized
    state["staged_command_backup_ts"] = now
    _set_fallback_staged(context_key, normalized, now)

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
You are a real-time drone voice control agent connected to ROS MCP tools.

Always follow this exact workflow for every user turn:
1. Call `sanitize_voice_command` with the user utterance first.
2. Read the `decision` field from the tool response and obey it strictly.
3. If `decision` is `needs_confirmation`, do not call ROS tools. Tell the user which command is staged and ask them to say `confirm`.
4. If `decision` is `duplicate_blocked`, do not call ROS tools. Ask the user to update the command or say `confirm`.
5. If `decision` is `cancelled`, acknowledge cancellation and wait for a new command.
6. If `decision` is `execute_pending`, execute only `command_to_execute` via ROS MCP tools.
7. Never execute ROS tools unless `decision` is `execute_pending`.
8. After tool execution, summarize what was executed and current status in <= 2 short sentences.

Safety and UX rules:
- Ignore filler words, stutters, and non-command chatter.
- Always stage first, then require explicit confirmation.
- Treat confirmation approval as one-time and time-limited.
- Prefer high-level safe commands (takeoff, land, hover, rtl, move with distance/altitude).
- For known PX4 actions from loaded spec, avoid extra introspection calls (for example `get_action_details`) unless a tool call fails.
- For ROS action execution calls, always set an explicit timeout (at least 60 seconds).
- If critical details are missing (for example altitude for takeoff), ask a brief follow-up.
- Never invent ROS tool results.
- Keep responses concise and spoken-language friendly.
- Preserve user intent exactly; do not rewrite to a different action.
- Never treat your own responses or status text as user commands.
- Reply in English only.
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
        _build_ros_mcp_toolset(),
    ],
)
