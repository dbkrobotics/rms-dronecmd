#!/usr/bin/env node

/**
 * Gemini Hook Adapter for mcp-voice-hooks
 * 
 * Bridges Gemini CLI hook events to the mcp-voice-hooks Unified Server.
 * 
 * Usage: node gemini-adapter.js <event-name>
 */

import http from 'http';
import fs from 'fs';

const SERVER_PORT = process.env.MCP_VOICE_HOOKS_PORT || 5111;
const SERVER_HOST = 'localhost';
const LOG_FILE = '/tmp/gemini_hook_adapter.log';

const EVENT = process.argv[2];

if (!EVENT) {
  console.error("Error: No event specified.");
  process.exit(1);
}

function log(msg) {
    try {
        const timestamp = new Date().toISOString();
        fs.appendFileSync(LOG_FILE, `[${timestamp}] [${EVENT}] ${msg}\n`);
    } catch (e) {
        // Ignore logging errors
    }
}

log(`Hook triggered. Processing...`);

// Read stdin (Hook Payload)
let inputData = '';
let processed = false;

// Set a timeout to avoid hanging if stdin doesn't close
const inputTimeout = setTimeout(() => {
    if (!processed) {
        log('Timeout waiting for stdin. Processing with collected data...');
        processInput();
    }
}, 1000);

process.stdin.on('data', chunk => {
    log(`Received stdin chunk: ${chunk.length} bytes`);
  inputData += chunk;
});

process.stdin.on('end', () => {
    if (!processed) {
        clearTimeout(inputTimeout);
        processInput();
    }
});

function processInput() {
    processed = true;
    try {
        log(`Payload received (length: ${inputData.length})`);
        const payload = inputData ? JSON.parse(inputData) : {};
        handleEvent(EVENT, payload);
    } catch (e) {
        log(`Error parsing payload: ${e.message}`);
        // If invalid JSON or empty, just proceed with empty object
        handleEvent(EVENT, {});
    }
}

function handleEvent(event, payload) {
    log(`Handling event: ${event}`);
  
  if (event === 'BeforeAgent') {
      log('Entering BeforeAgent logic...');
     // Gemini: BeforeAgent
      // Action: Check for voice input. If found, inject it.
     
      log('Calling /api/wait-for-utterances...');
     callApi('/api/wait-for-utterances', {}, (response) => {
         log(`Wait response received: ${JSON.stringify(response)}`);

         if (response && response.success && response.utterances && response.utterances.length > 0) {
             // We have voice input!
             const text = response.utterances.map(u => u.text).join(' ');
             const message = `User voice input: "${text}"`;
             log(`Injecting system message: ${message}`);

             // Inject it into Gemini.
             console.log(JSON.stringify({
                 systemMessage: message
             }));
         } else {
             log('No voice input found or timeout.');
             // No input, do nothing
             console.log('{}');
         }
     });

  } else if (event === 'AfterModel') {
      log('Entering AfterModel logic...');
      // Gemini: AfterModel
      // Action: Speak the response.
      
      let textToSpeak = "";
      
      // 1. Check for `response.content` (Standard Gemini/Vertex schema)
      if (payload.response && payload.response.content) {
          const content = payload.response.content;
          if (typeof content === 'string') textToSpeak = content;
          else if (Array.isArray(content)) {
              textToSpeak = content.map(c => c.text || '').join(' ');
          }
      } 
      // 2. Check for `modelResponse` (Gemini CLI simplified)
      else if (payload.modelResponse) {
          textToSpeak = payload.modelResponse;
      }

      if (textToSpeak) {
          log(`Sending text to TTS (length: ${textToSpeak.length})...`);
          callApi('/api/speak', { text: textToSpeak }, () => {
              log('TTS request sent.');
              console.log('{}');
          });
      } else {
          log('No text found to speak.');
          console.log('{}');
      }

  } else {
      log(`Ignoring unknown event: ${event}`);
      console.log('{}');
  }
}

function callApi(path, body, callback) {
    const data = JSON.stringify(body);
    const options = {
        hostname: SERVER_HOST,
        port: SERVER_PORT,
        path: path,
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Content-Length': data.length
        },
        timeout: 65000 // 65s timeout (slightly longer than server wait)
    };

    const req = http.request(options, (res) => {
        let responseData = '';
        res.on('data', chunk => responseData += chunk);
        res.on('end', () => {
            try {
                callback(JSON.parse(responseData));
            } catch (e) {
                log(`Error parsing API response: ${e.message}`);
                callback(null);
            }
        });
    });

    req.on('error', (e) => {
        log(`API Request failed: ${e.message}`);
        callback(null);
    });
    
    req.on('timeout', () => {
        log('API Request timed out.');
        req.destroy();
        callback(null);
    });

    req.write(data);
    req.end();
}
