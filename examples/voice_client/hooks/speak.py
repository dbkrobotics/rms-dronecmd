#!/usr/bin/env python3
import sys
import json
import urllib.request
import urllib.error

SERVER_URL = "http://localhost:5111/hook/speak"
LOG_FILE = "/tmp/gemini_voice_hooks.log"

def log(msg):
    sys.stderr.write(f"[VoiceHook-Speak] {msg}\n")
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[VoiceHook-Speak] {msg}\n")
    except Exception:
        pass

def main():
    log("Starting Speak Hook...")
    
    # Read Gemini's output from stdin (State)
    try:
        input_data = sys.stdin.read()
        if not input_data:
            log("No input data on stdin.")
            print(json.dumps({})) # Pass through
            return

        payload = json.loads(input_data)
        
        # We look for the model's text response.
        # The schema depends on the specific hook event `AfterModel` or `AfterTool`.
        # Usually it contains `modelResponse` or `messages`.
        
        # Checking `modelResponse` text
        model_text = ""
        
        # If AfterModel, we might have a `response` object
        if "response" in payload and "content" in payload["response"]:
             # content can be string or list of blocks
             content = payload["response"]["content"]
             if isinstance(content, str):
                 model_text = content
             elif isinstance(content, list):
                 # Concatenate text blocks
                 for block in content:
                     if isinstance(block, dict) and block.get("type") == "text":
                         model_text += block.get("text", "") + " "
        
        # Fallback/Debug: check other fields if schema differs
        if not model_text and "modelResponse" in payload:
             model_text = str(payload["modelResponse"])

        if model_text:
            log(f"Sending to TTS: {model_text[:50]}...")
            
            # Send to server
            req = urllib.request.Request(
                SERVER_URL,
                data=json.dumps({"text": model_text}).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    pass # Success
            except Exception as e:
                log(f"Failed to send to TTS server: {e}")
        
    except json.JSONDecodeError:
        log("Invalid JSON on stdin")
    except Exception as e:
        log(f"Error: {e}")

    # Always return valid JSON to avoid breaking Gemini
    print(json.dumps({}))

if __name__ == "__main__":
    main()
