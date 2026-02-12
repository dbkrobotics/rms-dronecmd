#!/usr/bin/env node

/**
 * Gemini Hook Adapter for mcp-voice-hooks
 * 
 * Bridges Gemini CLI hook events to the mcp-voice-hooks Unified Server.
 * 
 * Usage: node gemini-adapter.js <event-name>
 */

import http from 'http';

const SERVER_PORT = process.env.MCP_VOICE_HOOKS_PORT || 5111;
const SERVER_HOST = 'localhost';

const EVENT = process.argv[2];

if (!EVENT) {
  console.error("Error: No event specified.");
  process.exit(1);
}

// Read stdin (Hook Payload)
let inputData = '';
process.stdin.on('data', chunk => {
  inputData += chunk;
});

process.stdin.on('end', () => {
  try {
    const payload = inputData ? JSON.parse(inputData) : {};
    handleEvent(EVENT, payload);
  } catch (e) {
    // If invalid JSON or empty, just proceed with empty object
    handleEvent(EVENT, {});
  }
});

function handleEvent(event, payload) {
  // Map Gemini Events to mcp-voice-hooks Actions
  // mcp-voice-hooks generic API: /api/hooks/<action>
  // Actions: 'pre-speak' (for BeforeModel/AfterModel?), 'stop', 'post-tool'
  
  if (event === 'BeforeAgent') {
     // Gemini: BeforeAgent
     // Action: Check for voice input. If found, inject it.
     // mcp-voice-hooks logic: `wait-for-utterances`
     // We want to fetch utterances and if they exist, inject them.
     
     callApi('/api/wait-for-utterances', {}, (response) => {
         if (response && response.success && response.utterances && response.utterances.length > 0) {
             // We have voice input!
             // Inject it into Gemini.
             // Strategy: Return a systemMessage describing the voice input.
             const text = response.utterances.map(u => u.text).join(' ');
             console.log(JSON.stringify({
                 systemMessage: `User voice input: "${text}"`
             }));
         } else {
             // No input, do nothing
             console.log('{}');
         }
     });

  } else if (event === 'AfterModel') {
      // Gemini: AfterModel
      // Action: Speak the response.
      // Payload has `modelResponse` or `response.content`.
      
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
          // Send to TTS API
          callApi('/api/speak', { text: textToSpeak }, () => {
              console.log('{}');
          });
      } else {
          console.log('{}');
      }

  } else {
      // Unknown event, ignore
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
        timeout: 5000 // 5s timeout
    };

    const req = http.request(options, (res) => {
        let responseData = '';
        res.on('data', chunk => responseData += chunk);
        res.on('end', () => {
            try {
                callback(JSON.parse(responseData));
            } catch (e) {
                callback(null);
            }
        });
    });

    req.on('error', (e) => {
        // Silently fail if server is down (don't break Gemini)
        // console.error(`Problem with request: ${e.message}`);
        callback(null);
    });
    
    req.on('timeout', () => {
        req.destroy();
        callback(null);
    });

    req.write(data);
    req.end();
}
