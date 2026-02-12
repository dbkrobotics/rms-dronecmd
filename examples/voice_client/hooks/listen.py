#!/usr/bin/env python3
import sys
import json
import urllib.request
import urllib.error
import os

SERVER_URL = "http://localhost:5111/hook/listen"

# Strict JSON requirement: No print() allowed to stdout unless it's the final JSON.
# Use stderr for logging.

def log(msg):
    sys.stderr.write(f"[VoiceHook-Listen] {msg}\n")

def main():
    log("Starting Listen Hook...")

    try:
        # Check if server is up and listening
        # We use a timeout to avoid blocking Gemini forever if server is down
        req = urllib.request.Request(SERVER_URL)
        with urllib.request.urlopen(req, timeout=35) as response:
            if response.status == 200:
                data = json.load(response)
                voice_text = data.get("text")
                
                if voice_text:
                    log(f"Received voice input: {voice_text}")
                    # Inject voice text as a user message
                    # We use 'merge_messages' approach if possible, or just append to the last user message?
                    # The structure for BeforeAgent return is: { "userMessage": "..." } or modifying context.
                    # Best valid return for Gemini CLI hook to MODIFY input?
                    # Documentation says we can "Add context" or "Validate". 
                    # If we return a "userMessage", it might REPLACE or APPEND.
                    # Let's try returning a simple User Message injection.
                    
                    # NOTE: Gemini CLI Hook schema for 'BeforeAgent' is likely just receiving context. 
                    # But if we want to DRIVE the agent, we might need a different approach or this just adds info.
                    # Per docs: "Add context: Inject relevant information...".
                    # Let's try adding it to the system instructions or as a new message.
                    # Actually, if we want to *replace* the prompt, `BeforeAgent` might be too late?
                    # But wait, `BeforeAgent` fires *before* the agent loop.
                    
                    # For now, let's inject it into the `userMessage` field if supported, 
                    # or append to the existing input if we can read stdin.
                    
                    # Input to hook (stdin) is the current state.
                    # We output the *modifications*.
                    
                    # Assuming we can return `{"input": "new text"}` or similar.
                    # If uncertain, let's just dump it as a system message to guide the model.
                    
                    # SAFE BET: Return a systemMessage that says "User also said via voice: ..."
                    print(json.dumps({
                        "systemMessage": f"User voice input: {voice_text}"
                    }))
                    return
                else:
                    log("No voice input received (timeout or empty).")
                    
    except urllib.error.URLError as e:
        log(f"Server connection failed: {e}")
    except Exception as e:
        log(f"Error: {e}")

    # Default: do nothing (allow continue)
    print(json.dumps({}))

if __name__ == "__main__":
    main()
