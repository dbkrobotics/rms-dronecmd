#!/bin/bash

# Configuration
HOOKS_DIR="$(pwd)/examples/voice_client/hooks"
GEMINI_CONFIG_DIR="$HOME/.gemini"
GEMINI_CONFIG_FILE="$GEMINI_CONFIG_DIR/config.json"

# Ensure absolute path
HOOKS_DIR=$(cd "$HOOKS_DIR" && pwd)

echo "Installing Gemini CLI hooks from: $HOOKS_DIR"

if [ ! -d "$GEMINI_CONFIG_DIR" ]; then
    echo "Creating $GEMINI_CONFIG_DIR..."
    mkdir -p "$GEMINI_CONFIG_DIR"
fi

# Define the hooks JSON configuration
# We need to construct the JSON carefully. 
# Providing a way to just APPEND involves parsing JSON which is hard in bash.
# For this demo, we will output the JSON that needs to be added.

echo ""
echo "Please add the following configuration to your Gemini CLI config file:"
echo "Location: $GEMINI_CONFIG_FILE"
echo ""
echo "---------------------------------------------------------"
cat <<EOF
{
  "hooks": {
    "BeforeAgent": [
      {
        "command": "$HOOKS_DIR/listen.py",
        "timeout": 60000
      }
    ],
    "AfterModel": [
      {
        "command": "$HOOKS_DIR/speak.py",
        "timeout": 10000
      }
    ]
  }
}
EOF
echo "---------------------------------------------------------"
echo ""
echo "Note: If you already have hooks, merge them carefully."
echo "Note: The 'timeout' for listen.py is set to 60s to allow for speaking time."
