#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

if [[ -f ".env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source ".env"
  set +a
fi

HOST="${VOICE_AGENT_HOST:-0.0.0.0}"
PORT="${VOICE_AGENT_PORT:-8787}"

exec uvicorn server:app --host "$HOST" --port "$PORT"
