#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: ros_mcp_stdio_wrapper.sh <python_bin> <server_script> [stderr_log_path]" >&2
  exit 2
fi

PYTHON_BIN="$1"
SERVER_SCRIPT="$2"
STDERR_LOG="${3:-/tmp/ros_mcp_server_stderr.log}"

mkdir -p "$(dirname "$STDERR_LOG")"

# Keep MCP stdio on stdout/stdin and move noisy diagnostics to a file.
exec "$PYTHON_BIN" "$SERVER_SCRIPT" 2>>"$STDERR_LOG"
