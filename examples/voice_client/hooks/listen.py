#!/usr/bin/env python3
import sys
import json
import urllib.request
import urllib.error
import os

SERVER_URL = "http://localhost:5111/hook/listen"
LOG_FILE = "/tmp/gemini_voice_hooks.log"

def log(msg):
    # Log to stderr (captured by some tools, but maybe hidden)
    sys.stderr.write(f"[VoiceHook-Listen] {msg}\n")
    # Log to file for certainty
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[VoiceHook-Listen] {msg}\n")
    except Exception:
        pass

def main():
    log("Starting Listen Hook...")
    
    # Debug: Dump stdin to see what Context we are getting
    try:
        # We peek at stdin without consuming it all if we needed to pass it on, 
        # but here we are just reacting.
        # Let's verify if the hook is even running.
        pass
    except Exception:
        pass

    try:
        log(f"Connecting to {SERVER_URL}...")
        # Check if server is up and listening
        # We use a timeout to avoid blocking Gemini forever if server is down
        req = urllib.request.Request(SERVER_URL)
        with urllib.request.urlopen(req, timeout=35) as response:
            if response.status == 200:
                data = json.load(response)
                voice_text = data.get("text")
                
                if voice_text:
                    log(f"Received voice input: {voice_text}")
                    # Return as system message injection
                    print(json.dumps({
                        "systemMessage": f"User voice input: {voice_text}"
                    }))
                    return
                else:
                    log("No voice input received (timeout or empty).")
            else:
                log(f"Server responded with status {response.status}")
                    
    except urllib.error.URLError as e:
        log(f"Server connection failed: {e}")
    except Exception as e:
        log(f"Error: {e}")

    # Default: do nothing
    log("Exiting with empty JSON.")
    print(json.dumps({}))

if __name__ == "__main__":
    main()
