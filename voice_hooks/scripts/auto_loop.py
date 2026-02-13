#!/usr/bin/env python3
import pexpect
import sys
import threading
import json
import http.client
import os
import time

SERVER_HOST = "localhost"
SERVER_PORT = int(os.environ.get("MCP_VOICE_HOOKS_PORT", 5111))
GEMINI_CMD = "gemini cli"

def call_api(method, path, body=None):
    try:
        conn = http.client.HTTPConnection(SERVER_HOST, SERVER_PORT, timeout=65)
        headers = {"Content-Type": "application/json"}
        json_body = json.dumps(body) if body else None
        
        conn.request(method, path, json_body, headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        
        if 200 <= resp.status < 300:
            return json.loads(data) if data else {}
        else:
            return None
    except Exception:
        return None

def voice_listener_loop(child):
    time.sleep(1)
    
    call_api("POST", "/api/voice-input-state", {"active": True})

    try:
        while child.isalive():
            resp = call_api("POST", "/api/wait-for-utterances", {})
            
            if resp and resp.get("success") and resp.get("utterances"):
                texts = [u["text"] for u in resp["utterances"]] 
                if texts:
                    combined_text = " ".join(texts).strip()
                    if combined_text:
                        sys.stderr.write(f"\r\n[Voice Detected]: {combined_text}\n")
                        sys.stderr.flush()
                        
                        child.send(combined_text)
                        time.sleep(0.05)
                        child.send("\r")
            
            time.sleep(0.1)
    except Exception as e:
        sys.stderr.write(f"Voice listener error: {e}\n")

def main():
    print("Starting Gemini Voice Loop... (Interactive Mode)")

    env = os.environ.copy()
    env["MCP_VOICE_HOOKS_INPUT_MODE"] = "auto-loop"

    child = pexpect.spawn(GEMINI_CMD, encoding='utf-8', env=env)
    
    try:
        rows, cols = os.popen('stty size', 'r').read().split()
        child.setwinsize(int(rows), int(cols))
    except:
        pass

    t = threading.Thread(target=voice_listener_loop, args=(child,), daemon=True)
    t.start()

    try:
        child.interact()
    except Exception:
        pass

if __name__ == "__main__":
    main()
