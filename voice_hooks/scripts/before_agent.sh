#!/bin/bash
# Wrapper for BeforeAgent hook
# This ensures we don't need to put arguments in the 'command' field of settings.json,
# avoiding potential parsing issues if the CLI doesn't support shell splitting.

# Resolve absolute path to the adapter script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER="$SCRIPT_DIR/gemini-adapter.js"

# Execute with node
# Using system node
node "$ADAPTER" BeforeAgent
