#!/bin/bash

# Configuration
REPO_DIR="$(pwd)/voice_hooks"
ADAPTER_SCRIPT="$REPO_DIR/scripts/gemini-adapter.js"

# Ensure absolute paths
REPO_DIR=$(cd "$REPO_DIR" && pwd)
ADAPTER_SCRIPT="$REPO_DIR/scripts/gemini-adapter.js"

echo "Installing Gemini CLI hooks from: $REPO_DIR"

# Check if adapter exists
if [ ! -f "$ADAPTER_SCRIPT" ]; then
    echo "Error: Adapter script not found at $ADAPTER_SCRIPT"
    exit 1
fi

echo ""
echo "Please add the following configuration to your Gemini CLI config file:"
echo "Location: ~/.gemini/config.json"
echo ""
echo "---------------------------------------------------------"
cat <<EOF
{
  "hooks": {
    "BeforeAgent": [
      {
        "command": "node $ADAPTER_SCRIPT BeforeAgent",
        "timeout": 60000
      }
    ],
    "AfterAgent": [
      {
        "command": "node $ADAPTER_SCRIPT AfterAgent",
        "timeout": 10000
      }
    ]
  }
}
EOF
echo "---------------------------------------------------------"
echo ""
echo "Then, start the Voice Server in a separate terminal:"
echo "cd $REPO_DIR && npm start"
