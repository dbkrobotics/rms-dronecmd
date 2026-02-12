#!/bin/bash
# Wrapper for AfterModel hook

# Resolve absolute path to the adapter script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER="$SCRIPT_DIR/gemini-adapter.js"

# Execute with node. stdin will be passed through automatically.
node "$ADAPTER" AfterModel
