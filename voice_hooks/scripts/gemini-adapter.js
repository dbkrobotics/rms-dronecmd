#!/usr/bin/env node

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
        console.error(`[${timestamp}] [${EVENT}] ${msg}`);
        fs.appendFileSync(LOG_FILE, `[${timestamp}] [${EVENT}] ${msg}\n`);
    } catch (e) {
    }
}

process.on('uncaughtException', (err) => {
    log(`UNCAUGHT EXCEPTION: ${err.message}\n${err.stack}`);
    process.exit(1);
});

process.on('unhandledRejection', (reason, promise) => {
    log(`UNHANDLED REJECTION: ${reason}`);
    process.exit(1);
});

log(`Hook triggered. Checking server connectivity...`);

const checkReq = http.request({
    hostname: SERVER_HOST,
    port: SERVER_PORT,
    path: '/api/utterances/status',
    method: 'GET',
    timeout: 2000
}, (res) => {
    log(`Server is reachable (Status: ${res.statusCode})`);
    checkReq.destroy();
    startProcessing();
});

checkReq.on('error', (e) => {
    log(`SERVER UNREACHABLE: ${e.message}. Is 'npm start' running?`);
    process.exit(0);
});

checkReq.on('timeout', () => {
    log(`SERVER CHECK TIMEOUT. Is 'npm start' running?`);
    checkReq.destroy();
    process.exit(0);
});

checkReq.end();

function startProcessing() {
    log('Starting input processing...');
    let inputData = '';
    let processed = false;

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
            handleEvent(EVENT, {});
        }
    }
}

function handleEvent(event, payload) {
    log(`Handling event: ${event}`);

    if (event === 'BeforeAgent') {
        log('Entering BeforeAgent logic...');
        log(`Payload: ${JSON.stringify(payload)}`);

        if (process.env.MCP_VOICE_HOOKS_INPUT_MODE === 'auto-loop') {
            log('auto-loop input mode detected. Skipping BeforeAgent dequeue.');
            console.log('{}');
            return;
        }

        if (payload.prompt && payload.prompt.trim().length > 0) {
            console.log('{}');
            return;
        }
        log('Forcing voice input activation...');
        callApi('/api/voice-input-state', { active: true }, (activateResp) => {
            log(`Voice activation response: ${JSON.stringify(activateResp)}`);

            log('Calling /api/wait-for-utterances...');
            callApi('/api/wait-for-utterances', {}, (response) => {
                log(`Wait response received: ${JSON.stringify(response)}`);

                if (response && response.success && response.utterances && response.utterances.length > 0) {
                    const text = response.utterances.map(u => u.text).join(' ');
                    const message = `User voice input: "${text}"`;
                    log(`Injecting system message: ${message}`);

                    console.log(JSON.stringify({
                        hookSpecificOutput: {
                            additionalContext: message
                        },
                        systemMessage: message
                    }));
                } else {
                    log('No voice input found or timeout.');
                    console.log('{}');
                }
            });
        });
    } else if (event === 'AfterAgent') {
        log('Entering AfterAgent logic...');

        if (process.env.MCP_VOICE_HOOKS_AFTER_AGENT_SPEAK === 'false') {
            log('AfterAgent speak disabled by MCP_VOICE_HOOKS_AFTER_AGENT_SPEAK=false');
            console.log('{}');
            return;
        }

        let textToSpeak = "";

        if (payload.prompt_response) {
            textToSpeak = sanitizeForSpeech(payload.prompt_response);
        }

        if (textToSpeak) {
            callGetApi('/api/speak-status', (statusResp) => {
                if (statusResp && statusResp.hasRecentSpeak) {
                    log('Recent speak already happened. Skipping AfterAgent TTS to avoid duplicate voice output.');
                    console.log('{}');
                    return;
                }

                log(`Sending text to TTS (length: ${textToSpeak.length})...`);
                callApi('/api/speak', { text: textToSpeak }, () => {
                    log('TTS request sent.');
                    console.log('{}');
                });
            });
        } else {
            log('No text found to speak in AfterAgent payload.');
            console.log('{}');
        }

    } else {
        log(`Ignoring unknown event: ${event}`);
        console.log('{}');
    }
}

function callGetApi(path, callback) {
    const options = {
        hostname: SERVER_HOST,
        port: SERVER_PORT,
        path: path,
        method: 'GET',
        timeout: 3000
    };

    const req = http.request(options, (res) => {
        let responseData = '';
        res.on('data', chunk => responseData += chunk);
        res.on('end', () => {
            try {
                callback(JSON.parse(responseData));
            } catch (e) {
                log(`Error parsing GET API response: ${e.message}`);
                callback(null);
            }
        });
    });

    req.on('error', (e) => {
        log(`GET API Request failed: ${e.message}`);
        callback(null);
    });

    req.on('timeout', () => {
        log('GET API Request timed out.');
        req.destroy();
        callback(null);
    });

    req.end();
}

function sanitizeForSpeech(text) {
    if (!text || typeof text !== 'string') {
        return '';
    }

    let cleaned = text;
    cleaned = cleaned.replace(/```[\s\S]*?```/g, '');
    cleaned = cleaned.replace(/`([^`]+)`/g, '$1');
    cleaned = cleaned.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1');
    cleaned = cleaned.replace(/^\s*[#>*-]+\s*/gm, '');
    cleaned = cleaned.replace(/\s+/g, ' ').trim();

    // Keep speech concise so TTS stays natural and responsive.
    if (cleaned.length > 700) {
        cleaned = `${cleaned.slice(0, 700).trim()} ...`; 
    }

    return cleaned;
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
        timeout: 65000
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
