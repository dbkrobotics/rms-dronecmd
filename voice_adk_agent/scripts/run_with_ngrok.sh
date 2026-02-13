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

if ! command -v ngrok >/dev/null 2>&1; then
  echo "[ERROR] ngrok not found. Install ngrok first."
  exit 1
fi

HOST="${VOICE_AGENT_HOST:-0.0.0.0}"
PORT="${VOICE_AGENT_PORT:-8787}"

cleanup() {
  if [[ -n "${NGROK_PID:-}" ]] && kill -0 "$NGROK_PID" >/dev/null 2>&1; then
    kill "$NGROK_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

uvicorn server:app --host "$HOST" --port "$PORT" &
SERVER_PID=$!

echo "[INFO] Voice agent started on http://localhost:${PORT}"

sleep 1

if [[ -n "${VOICE_AGENT_NGROK_DOMAIN:-}" ]]; then
  ngrok http "${PORT}" --domain "${VOICE_AGENT_NGROK_DOMAIN}" >/tmp/voice_agent_ngrok.log 2>&1 &
else
  ngrok http "${PORT}" >/tmp/voice_agent_ngrok.log 2>&1 &
fi
NGROK_PID=$!

sleep 2

PUBLIC_URL="$(curl -s http://127.0.0.1:4040/api/tunnels | sed -n 's/.*"public_url":"\([^"]*\)".*/\1/p' | head -n 1 || true)"
if [[ -n "$PUBLIC_URL" ]]; then
  echo "[INFO] ngrok public URL: $PUBLIC_URL"
else
  echo "[WARN] Could not fetch ngrok URL automatically. Open http://127.0.0.1:4040"
fi

echo "[INFO] Press Ctrl+C to stop both uvicorn and ngrok"
wait "$SERVER_PID"
