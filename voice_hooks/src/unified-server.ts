#!/usr/bin/env node

import express from 'express';
import type { Request, Response } from 'express';
import cors from 'cors';
import path from 'path';
import { fileURLToPath } from 'url';
import { randomUUID } from 'crypto';
import { exec, execFile, spawn } from 'child_process';
import { promisify } from 'util';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { debugLog } from './debug.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import multer from 'multer';
import fs from 'fs';
import OpenAI from 'openai';
import os from 'os';

// Configure multer for temporary file storage
const upload = multer({ dest: os.tmpdir() });

// Initialize OpenAI client
const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Constants
const WAIT_TIMEOUT_SECONDS = 60;
const HTTP_PORT = process.env.MCP_VOICE_HOOKS_PORT ? parseInt(process.env.MCP_VOICE_HOOKS_PORT) : 5111;
const HTTP_HOST = '0.0.0.0';
const TRANSCRIBE_MODEL = process.env.MCP_VOICE_HOOKS_TRANSCRIBE_MODEL || 'whisper-1';
const TRANSCRIBE_LANGUAGE = 'en';
const MIN_TRANSCRIBE_TEXT_LENGTH = 4;
const MAX_TRANSCRIBE_RETRIES = 2;

// Promisified exec for async/await
const execAsync = promisify(exec);
const execFileAsync = promisify(execFile);

// Function to play a sound notification
async function playNotificationSound() {
  const candidates: Array<{ command: string; args: string[] }> = [];

  if (process.platform === 'darwin') {
    candidates.push({
      command: 'afplay',
      args: ['/System/Library/Sounds/Funk.aiff'],
    });
  }

  if (process.platform === 'linux') {
    candidates.push(
      { command: 'paplay', args: ['/usr/share/sounds/freedesktop/stereo/message-new-instant.oga'] },
      { command: 'aplay', args: ['/usr/share/sounds/alsa/Front_Center.wav'] }
    );
  }

  for (const candidate of candidates) {
    try {
      if (!(await commandExists(candidate.command))) {
        continue;
      }

      await execFileAsync(candidate.command, candidate.args);
      debugLog(`[Sound] Played notification using ${candidate.command}`);
      return;
    } catch (error) {
      debugLog(`[Sound] ${candidate.command} failed: ${error}`);
    }
  }

  debugLog('[Sound] No notification sound backend available');
}

function clampNumber(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function getNetworkUrls(port: number): string[] {
  const urls = new Set<string>([`http://localhost:${port}`]);
  const interfaces = os.networkInterfaces();

  Object.values(interfaces).forEach((entries) => {
    if (!entries) {
      return;
    }

    entries.forEach((entry) => {
      if (entry.family !== 'IPv4' || entry.internal) {
        return;
      }

      urls.add(`http://${entry.address}:${port}`);
    });
  });

  return Array.from(urls);
}

function getPrimaryPhoneUrl(port: number): string {
  const urls = getNetworkUrls(port);
  return urls.find((url) => !url.includes('localhost')) || urls[0];
}

function buildEnglishTranscriptionPrompt(extraHints?: string): string {
  const defaultHints = [
    'mcp',
    'ros',
    'server',
    'drone',
    'px4',
    'mavros',
    'take off',
    'land',
    'arm',
    'disarm',
    'hover',
    'move forward',
    'move backward',
    'move left',
    'move right',
    'move up',
    'move down',
    'meter',
    'circle',
    'square',
    'altitude',
  ];

  const parts = [
    'Transcribe only spoken English commands for drone control.',
    'Keep punctuation simple.',
    `Domain terms: ${defaultHints.join(', ')}.`,
  ];

  if (extraHints && extraHints.trim()) {
    parts.push(`Additional hints: ${extraHints.trim()}.`);
  }

  return parts.join(' ');
}

function inferUploadExtension(file: { originalname?: string; mimetype?: string }): string {
  const byName = path.extname(file.originalname || '').replace('.', '').toLowerCase();
  if (byName) {
    return byName;
  }

  const mime = (file.mimetype || '').toLowerCase();
  const mimeToExtension: Record<string, string> = {
    'audio/webm': 'webm',
    'audio/ogg': 'ogg',
    'audio/oga': 'oga',
    'audio/wav': 'wav',
    'audio/x-wav': 'wav',
    'audio/mpeg': 'mp3',
    'audio/mp3': 'mp3',
    'audio/mp4': 'm4a',
    'audio/x-m4a': 'm4a',
    'audio/flac': 'flac',
  };

  return mimeToExtension[mime] || 'webm';
}

function isTranscriptionLikelyNoise(text: string): boolean {
  const normalized = text.trim().toLowerCase();
  if (!normalized) {
    return true;
  }

  if (normalized.length < MIN_TRANSCRIBE_TEXT_LENGTH) {
    return true;
  }

  const noisePatterns = [
    'thank you',
    'thanks for watching',
    'subtitle',
    'subtitles by',
    'you',
    'okay',
    'ok',
  ];

  return noisePatterns.some((pattern) => normalized === pattern);
}

function scoreTranscription(text: string): number {
  const normalized = text.trim();
  if (!normalized) {
    return -1;
  }

  const words = normalized.split(/\s+/).filter(Boolean);
  let score = normalized.length;
  score += words.length * 3;

  if (isTranscriptionLikelyNoise(normalized)) {
    score -= 30;
  }

  return score;
}

interface SystemTTSEngine {
  name: string;
  command: string;
  buildArgs: (text: string, rate: number) => string[];
}

let systemTTSEnginePromise: Promise<SystemTTSEngine | null> | null = null;
let activeSystemTTSProcess: ReturnType<typeof spawn> | null = null;

async function commandExists(command: string): Promise<boolean> {
  const probe = process.platform === 'win32' ? `where ${command}` : `command -v ${command}`;
  try {
    await execAsync(probe);
    return true;
  } catch {
    return false;
  }
}

async function resolveSystemTTSEngine(): Promise<SystemTTSEngine | null> {
  if (systemTTSEnginePromise) {
    return systemTTSEnginePromise;
  }

  systemTTSEnginePromise = (async () => {
    if (process.platform === 'darwin') {
      return {
        name: 'say',
        command: 'say',
        buildArgs: (text, rate) => ['-r', String(clampNumber(Math.round(rate), 80, 320)), text],
      };
    }

    if (process.platform === 'linux') {
      if (await commandExists('spd-say')) {
        return {
          name: 'spd-say',
          command: 'spd-say',
          buildArgs: (text, rate) => {
            const mappedRate = clampNumber(Math.round(((rate - 150) / 110) * 100), -100, 100);
            return ['-r', String(mappedRate), text];
          },
        };
      }

      if (await commandExists('espeak-ng')) {
        return {
          name: 'espeak-ng',
          command: 'espeak-ng',
          buildArgs: (text, rate) => ['-s', String(clampNumber(Math.round(rate), 80, 320)), text],
        };
      }

      if (await commandExists('espeak')) {
        return {
          name: 'espeak',
          command: 'espeak',
          buildArgs: (text, rate) => ['-s', String(clampNumber(Math.round(rate), 80, 320)), text],
        };
      }
    }

    return null;
  })();

  return systemTTSEnginePromise;
}

async function stopActiveSystemTTS(): Promise<boolean> {
  if (!activeSystemTTSProcess) {
    return false;
  }

  const processToStop = activeSystemTTSProcess;
  activeSystemTTSProcess = null;

  try {
    processToStop.kill('SIGTERM');
    setTimeout(() => {
      try {
        if (!processToStop.killed) {
          processToStop.kill('SIGKILL');
        }
      } catch {
        // no-op
      }
    }, 250);
    return true;
  } catch {
    return false;
  }
}

function runSystemTTS(engine: SystemTTSEngine, text: string, rate: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(engine.command, engine.buildArgs(text, rate), {
      stdio: 'ignore',
    });
    activeSystemTTSProcess = child;

    child.on('error', (error) => {
      if (activeSystemTTSProcess === child) {
        activeSystemTTSProcess = null;
      }
      reject(error);
    });

    child.on('close', (code, signal) => {
      if (activeSystemTTSProcess === child) {
        activeSystemTTSProcess = null;
      }

      if (signal === 'SIGTERM' || signal === 'SIGKILL') {
        resolve();
        return;
      }

      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`System TTS exited with code ${code}`));
      }
    });
  });
}

// Shared utterance queue
interface Utterance {
  id: string;
  text: string;
  timestamp: Date;
  status: 'pending' | 'delivered' | 'responded';
}

// Conversation message type for full conversation history
interface ConversationMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  timestamp: Date;
  status?: 'pending' | 'delivered' | 'responded'; // Only for user messages
}

class UtteranceQueue {
  utterances: Utterance[] = [];
  messages: ConversationMessage[] = []; // Full conversation history

  add(text: string, timestamp?: Date): Utterance {
    const utterance: Utterance = {
      id: randomUUID(),
      text: text.trim(),
      timestamp: timestamp || new Date(),
      status: 'pending'
    };

    this.utterances.push(utterance);

    // Also add to conversation messages
    this.messages.push({
      id: utterance.id,
      role: 'user',
      text: utterance.text,
      timestamp: utterance.timestamp,
      status: utterance.status
    });

    debugLog(`[Queue] queued: "${utterance.text}"	[id: ${utterance.id}]`);
    return utterance;
  }

  addAssistantMessage(text: string): ConversationMessage {
    const message: ConversationMessage = {
      id: randomUUID(),
      role: 'assistant',
      text: text.trim(),
      timestamp: new Date()
    };
    this.messages.push(message);
    debugLog(`[Queue] assistant message: "${message.text}"	[id: ${message.id}]`);
    return message;
  }

  getRecentMessages(limit: number = 50): ConversationMessage[] {
    return this.messages
      .sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime()) // Oldest first
      .slice(-limit); // Get last N messages
  }

  getRecent(limit: number = 10): Utterance[] {
    return this.utterances
      .sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime())
      .slice(0, limit);
  }

  markDelivered(id: string): void {
    const utterance = this.utterances.find(u => u.id === id);
    if (utterance) {
      utterance.status = 'delivered';
      debugLog(`[Queue] delivered: "${utterance.text}"	[id: ${id}]`);

      // Sync status in messages array
      const message = this.messages.find(m => m.id === id && m.role === 'user');
      if (message) {
        message.status = 'delivered';
      }
    }
  }

  delete(id: string): boolean {
    const utterance = this.utterances.find(u => u.id === id);

    // Only allow deleting pending messages
    if (utterance && utterance.status === 'pending') {
      this.utterances = this.utterances.filter(u => u.id !== id);
      this.messages = this.messages.filter(m => m.id !== id);
      debugLog(`[Queue] Deleted pending message: "${utterance.text}"	[id: ${id}]`);
      return true;
    }

    return false;
  }

  clear(): void {
    const count = this.utterances.length;
    this.utterances = [];
    this.messages = []; // Clear conversation history too
    debugLog(`[Queue] Cleared ${count} utterances and conversation history`);
  }
}

// Determine if we're running in MCP-managed mode
const IS_MCP_MANAGED = process.argv.includes('--mcp-managed');

// Global state
const queue = new UtteranceQueue();
let lastToolUseTimestamp: Date | null = null;
let lastSpeakTimestamp: Date | null = null;
let lastSpeakPayload: { normalizedText: string; timestampMs: number } | null = null;

// Voice preferences (controlled by browser)
let voicePreferences = {
  voiceResponsesEnabled: false,
  voiceInputActive: false
};

// HTTP Server Setup (always created)
const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, '..', 'public')));

// API Routes

// API for audio transcription using OpenAI Whisper
app.post('/api/transcribe', upload.single('audio'), async (req: Request, res: Response): Promise<void> => {
  if (!process.env.OPENAI_API_KEY) {
    res.status(500).json({
      error: 'OPENAI_API_KEY is not configured',
      details: 'Set OPENAI_API_KEY so audio can be transcribed.',
    });
    return;
  }

  if (!req.file) {
    res.status(400).json({ error: 'No audio file uploaded' });
    return;
  }

  const uploadedTempPath = req.file.path;
  const uploadExtension = inferUploadExtension({
    originalname: req.file.originalname,
    mimetype: req.file.mimetype,
  });
  const transcribeTempPath = `${uploadedTempPath}.${uploadExtension}`;
  let tempFilePath = uploadedTempPath;
  const hints = typeof req.body?.hints === 'string' ? req.body.hints : '';
  const prompt = buildEnglishTranscriptionPrompt(hints);
  const candidateModels = Array.from(new Set([TRANSCRIBE_MODEL, 'whisper-1']));

  try {
    try {
      fs.copyFileSync(uploadedTempPath, transcribeTempPath);
      tempFilePath = transcribeTempPath;
    } catch (copyError) {
      debugLog(`[Transcribe] Failed to create extension-preserving temp copy: ${copyError}`);
      tempFilePath = uploadedTempPath;
    }

    debugLog(
      `[Transcribe] Received audio file: ${req.file.originalname} (${req.file.size} bytes), uploadMime=${req.file.mimetype}, transcribePath=${tempFilePath}, models=${candidateModels.join(',')}`
    );

    const candidates: Array<{ text: string; score: number; model: string; attempt: number }> = [];
    let lastError: Error | null = null;

    for (const model of candidateModels) {
      for (let attempt = 0; attempt <= MAX_TRANSCRIBE_RETRIES; attempt += 1) {
        try {
          const transcription = await openai.audio.transcriptions.create({
            file: fs.createReadStream(tempFilePath),
            model,
            prompt,
            language: TRANSCRIBE_LANGUAGE,
            temperature: 0,
          } as any);

          const text = transcription.text?.trim() || '';
          const score = scoreTranscription(text);
          candidates.push({ text, score, model, attempt });
          debugLog(`[Transcribe] model=${model} attempt=${attempt} => "${text}" (score=${score})`);

          if (!isTranscriptionLikelyNoise(text)) {
            res.json({
              success: true,
              text,
              language: TRANSCRIBE_LANGUAGE,
              model,
              lowConfidence: false,
            });
            return;
          }
        } catch (error) {
          lastError = error instanceof Error ? error : new Error(String(error));
          debugLog(`[Transcribe] model=${model} attempt=${attempt} failed: ${lastError.message}`);
        }
      }
    }

    const best = candidates.sort((a, b) => b.score - a.score)[0];
    if (best && best.text) {
      res.json({
        success: true,
        text: best.text,
        language: TRANSCRIBE_LANGUAGE,
        model: best.model,
        lowConfidence: true,
      });
      return;
    }

    throw lastError || new Error('No transcription result produced');
  } catch (error: any) {
    debugLog(`[Transcribe] Error during transcription: ${error.message}`);
    res.status(500).json({
      error: 'Transcription failed',
      details: error.message,
    });
  } finally {
    const cleanupTargets = Array.from(new Set([uploadedTempPath, transcribeTempPath]));
    cleanupTargets.forEach((targetPath) => {
      if (fs.existsSync(targetPath)) {
        fs.unlink(targetPath, (err) => {
          if (err) {
            debugLog(`[Transcribe] Error deleting temp file (${targetPath}): ${err.message}`);
          }
        });
      }
    });
  }
});
app.post('/api/potential-utterances', (req: Request, res: Response) => {
  const { text, timestamp } = req.body;

  if (!text || !text.trim()) {
    res.status(400).json({ error: 'Text is required' });
    return;
  }

  const parsedTimestamp = timestamp ? new Date(timestamp) : undefined;
  const utterance = queue.add(text, parsedTimestamp);
  res.json({
    success: true,
    utterance: {
      id: utterance.id,
      text: utterance.text,
      timestamp: utterance.timestamp,
      status: utterance.status,
    },
  });
});

app.get('/api/utterances', (req: Request, res: Response) => {
  const limit = parseInt(req.query.limit as string) || 10;
  const utterances = queue.getRecent(limit);

  res.json({
    utterances: utterances.map(u => ({
      id: u.id,
      text: u.text,
      timestamp: u.timestamp,
      status: u.status,
    })),
  });
});

// GET /api/conversation - Returns full conversation history
app.get('/api/conversation', (req: Request, res: Response) => {
  const limit = parseInt(req.query.limit as string) || 50;
  const messages = queue.getRecentMessages(limit);

  res.json({
    messages: messages.map(m => ({
      id: m.id,
      role: m.role,
      text: m.text,
      timestamp: m.timestamp,
      status: m.status // Only present for user messages
    }))
  });
});

app.get('/api/utterances/status', (_req: Request, res: Response) => {
  const total = queue.utterances.length;
  const pending = queue.utterances.filter(u => u.status === 'pending').length;
  const delivered = queue.utterances.filter(u => u.status === 'delivered').length;

  res.json({
    total,
    pending,
    delivered,
  });
});

app.get('/api/network-info', (_req: Request, res: Response) => {
  const urls = getNetworkUrls(HTTP_PORT);
  res.json({
    success: true,
    host: HTTP_HOST,
    port: HTTP_PORT,
    phoneUrl: getPrimaryPhoneUrl(HTTP_PORT),
    urls,
  });
});

// Shared dequeue logic
function dequeueUtterancesCore() {
  // Always dequeue pending utterances regardless of voiceInputActive
  // This allows both typed and spoken messages to be dequeued
  const pendingUtterances = queue.utterances
    .filter(u => u.status === 'pending')
    .sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime());

  // Mark as delivered
  pendingUtterances.forEach(u => {
    queue.markDelivered(u.id);
  });

  return {
    success: true,
    utterances: pendingUtterances.map(u => ({
      text: u.text,
      timestamp: u.timestamp,
    })),
  };
}

// MCP server integration
app.post('/api/dequeue-utterances', (_req: Request, res: Response) => {
  const result = dequeueUtterancesCore();
  res.json(result);
});

// Shared wait for utterance logic
async function waitForUtteranceCore() {
  // Check if voice input is active
  if (!voicePreferences.voiceInputActive) {
    return {
      success: false,
      error: 'Voice input is not active. Cannot wait for utterances when voice input is disabled.'
    };
  }

  const secondsToWait = WAIT_TIMEOUT_SECONDS;
  const maxWaitMs = secondsToWait * 1000;
  const startTime = Date.now();

  debugLog(`[WaitCore] Starting wait_for_utterance (${secondsToWait}s)`);

  // Notify frontend that wait has started
  notifyWaitStatus(true);

  let firstTime = true;

  // Poll for utterances
  while (Date.now() - startTime < maxWaitMs) {
    // Check if voice input is still active
    if (!voicePreferences.voiceInputActive) {
      debugLog('[WaitCore] Voice input deactivated during wait_for_utterance');
      notifyWaitStatus(false); // Notify wait has ended
      return {
        success: true,
        utterances: [],
        message: 'Voice input was deactivated',
        waitTime: Date.now() - startTime,
      };
    }

    const pendingUtterances = queue.utterances.filter(
      u => u.status === 'pending'
    );

    if (pendingUtterances.length > 0) {
      // Found utterances

      // Sort by timestamp (oldest first)
      const sortedUtterances = pendingUtterances
        .sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime());

      // Mark utterances as delivered
      sortedUtterances.forEach(u => {
        queue.markDelivered(u.id);
      });

      notifyWaitStatus(false); // Notify wait has ended
      return {
        success: true,
        utterances: sortedUtterances.map(u => ({
          id: u.id,
          text: u.text,
          timestamp: u.timestamp,
          status: 'delivered', // They are now delivered
        })),
        count: pendingUtterances.length,
        waitTime: Date.now() - startTime,
      };
    }

    if (firstTime) {
      firstTime = false;
      // Play notification sound since we're about to start waiting
      await playNotificationSound();
    }

    // Wait 100ms before checking again
    await new Promise(resolve => setTimeout(resolve, 100));
  }

  // Timeout reached - no utterances found
  notifyWaitStatus(false); // Notify wait has ended
  return {
    success: true,
    utterances: [],
    message: `No utterances found after waiting ${secondsToWait} seconds.`,
    waitTime: maxWaitMs,
  };
}

// Wait for utterance endpoint
app.post('/api/wait-for-utterances', async (_req: Request, res: Response) => {
  const result = await waitForUtteranceCore();

  // If error response, return 400 status
  if (!result.success && result.error) {
    res.status(400).json(result);
    return;
  }

  res.json(result);
});


// API for pre-tool hook to check for pending utterances
app.get('/api/has-pending-utterances', (_req: Request, res: Response) => {
  const pendingCount = queue.utterances.filter(u => u.status === 'pending').length;
  const hasPending = pendingCount > 0;

  res.json({
    hasPending,
    pendingCount
  });
});

// Unified action validation endpoint
app.post('/api/validate-action', (req: Request, res: Response) => {
  const { action } = req.body;
  const voiceResponsesEnabled = voicePreferences.voiceResponsesEnabled;

  if (!action || !['tool-use', 'stop'].includes(action)) {
    res.status(400).json({ error: 'Invalid action. Must be "tool-use" or "stop"' });
    return;
  }

  // Only check for pending utterances if voice input is active
  if (voicePreferences.voiceInputActive) {
    const pendingUtterances = queue.utterances.filter(u => u.status === 'pending');
    if (pendingUtterances.length > 0) {
      res.json({
        allowed: false,
        requiredAction: 'dequeue_utterances',
        reason: `${pendingUtterances.length} pending utterance(s) must be dequeued first. Please use dequeue_utterances to process them.`
      });
      return;
    }
  }

  // Check for delivered but unresponded utterances (when voice enabled)
  if (voiceResponsesEnabled) {
    const deliveredUtterances = queue.utterances.filter(u => u.status === 'delivered');
    if (deliveredUtterances.length > 0) {
      res.json({
        allowed: false,
        requiredAction: 'speak',
        reason: `${deliveredUtterances.length} delivered utterance(s) require voice response. Please use the speak tool to respond before proceeding.`
      });
      return;
    }
  }

  // For stop action, check if we should wait (only if voice input is active)
  if (action === 'stop' && voicePreferences.voiceInputActive) {
    if (queue.utterances.length > 0) {
      res.json({
        allowed: false,
        requiredAction: 'wait_for_utterance',
        reason: 'Assistant tried to end its response. Stopping is not allowed without first checking for voice input. Assistant should now use wait_for_utterance to check for voice input'
      });
      return;
    }
  }

  // All checks passed - action is allowed
  res.json({
    allowed: true
  });
});

// Unified hook handler
function handleHookRequest(attemptedAction: 'tool' | 'speak' | 'stop' | 'post-tool'): { decision: 'approve' | 'block', reason?: string } | Promise<{ decision: 'approve' | 'block', reason?: string }> {
  const voiceResponsesEnabled = voicePreferences.voiceResponsesEnabled;
  const voiceInputActive = voicePreferences.voiceInputActive;

  // 1. Check for pending utterances and auto-dequeue
  // Always check for pending utterances regardless of voiceInputActive
  // This allows typed messages to be dequeued even when mic is off
  const pendingUtterances = queue.utterances.filter(u => u.status === 'pending');
  if (pendingUtterances.length > 0) {
    // Always dequeue (dequeueUtterancesCore no longer requires voiceInputActive)
    const dequeueResult = dequeueUtterancesCore();

    if (dequeueResult.success && dequeueResult.utterances && dequeueResult.utterances.length > 0) {
      // Reverse to show oldest first
      const reversedUtterances = dequeueResult.utterances.reverse();

      return {
        decision: 'block',
        reason: formatVoiceUtterances(reversedUtterances)
      };
    }
  }

  // 2. Check for delivered utterances (when voice enabled)
  if (voiceResponsesEnabled) {
    const deliveredUtterances = queue.utterances.filter(u => u.status === 'delivered');
    if (deliveredUtterances.length > 0) {
      // Only allow speak to proceed
      if (attemptedAction === 'speak') {
        return { decision: 'approve' };
      }
      return {
        decision: 'block',
        reason: `${deliveredUtterances.length} delivered utterance(s) require voice response. Please use the speak tool to respond before proceeding.`
      };
    }
  }

  // 3. Handle tool and post-tool actions
  if (attemptedAction === 'tool' || attemptedAction === 'post-tool') {
    lastToolUseTimestamp = new Date();
    return { decision: 'approve' };
  }

  // 4. Handle speak
  if (attemptedAction === 'speak') {
    return { decision: 'approve' };
  }

  // 5. Handle stop
  if (attemptedAction === 'stop') {
    // Check if must speak after tool use
    if (voiceResponsesEnabled && lastToolUseTimestamp &&
      (!lastSpeakTimestamp || lastSpeakTimestamp < lastToolUseTimestamp)) {
      return {
        decision: 'block',
        reason: 'Assistant must speak after using tools. Please use the speak tool to respond before proceeding.'
      };
    }

    // Auto-wait for utterances (only if voice input is active)
    if (voiceInputActive) {
      return (async () => {
        try {
          debugLog(`[Stop Hook] Auto-calling wait_for_utterance...`);
          const data = await waitForUtteranceCore();
          debugLog(`[Stop Hook] wait_for_utterance response: ${JSON.stringify(data)}`);

          // If error (voice input not active), treat as no utterances found
          if (!data.success && data.error) {
            return {
              decision: 'approve' as const,
              reason: data.error
            };
          }

          // If utterances were found, block and return them
          if (data.utterances && data.utterances.length > 0) {
            return {
              decision: 'block' as const,
              reason: formatVoiceUtterances(data.utterances)
            };
          }

          // If no utterances found (including when voice was deactivated), approve stop
          return {
            decision: 'approve' as const,
            reason: data.message || 'No utterances found during wait'
          };
        } catch (error) {
          debugLog(`[Stop Hook] Error calling wait_for_utterance: ${error}`);
          // Fail open on errors
          return {
            decision: 'approve' as const,
            reason: 'Auto-wait encountered an error, proceeding'
          };
        }
      })();
    }

    return {
      decision: 'approve',
      reason: 'No utterances since last timeout'
    };
  }

  // Default to approve (shouldn't reach here)
  return { decision: 'approve' };
}

// Dedicated hook endpoints that return in Gemini's expected format
app.post('/api/hooks/stop', async (_req: Request, res: Response) => {
  const result = await handleHookRequest('stop');
  res.json(result);
});

// Pre-speak hook endpoint
app.post('/api/hooks/pre-speak', (_req: Request, res: Response) => {
  const result = handleHookRequest('speak');
  res.json(result);
});

// Post-tool hook endpoint
app.post('/api/hooks/post-tool', (_req: Request, res: Response) => {
  // Use the unified handler with 'post-tool' action
  const result = handleHookRequest('post-tool');
  res.json(result);
});

// API to clear all utterances
// Delete specific utterance by ID
app.delete('/api/utterances/:id', (req: Request, res: Response) => {
  const { id } = req.params;

  const deleted = queue.delete(id);

  if (deleted) {
    res.json({
      success: true,
      message: 'Message deleted'
    });
  } else {
    res.status(400).json({
      error: 'Only pending messages can be deleted',
      success: false
    });
  }
});

// Delete all utterances
app.delete('/api/utterances', (_req: Request, res: Response) => {
  const clearedCount = queue.utterances.length;
  queue.clear();

  res.json({
    success: true,
    message: `Cleared ${clearedCount} utterances`,
    clearedCount
  });
});

// Server-Sent Events for TTS notifications
const ttsClients = new Set<Response>();

app.get('/api/tts-events', (_req: Request, res: Response) => {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
  });

  // Send initial connection message
  res.write('data: {"type":"connected"}\n\n');

  // Add client to set
  ttsClients.add(res);

  // Remove client on disconnect
  res.on('close', () => {
    ttsClients.delete(res);
    
    // If no clients remain, disable voice features
    if (ttsClients.size === 0) {
      debugLog('[SSE] Last browser disconnected, disabling voice features');
      if (voicePreferences.voiceInputActive || voicePreferences.voiceResponsesEnabled) {
        debugLog(`[SSE] Voice features disabled - Input: ${voicePreferences.voiceInputActive} -> false, Responses: ${voicePreferences.voiceResponsesEnabled} -> false`);
        voicePreferences.voiceInputActive = false;
        voicePreferences.voiceResponsesEnabled = false;
      }
    } else {
      debugLog(`[SSE] Browser disconnected, ${ttsClients.size} client(s) remaining`);
    }
  });
});

// Helper function to notify all connected TTS clients
function notifyTTSClients(text: string) {
  const message = JSON.stringify({ type: 'speak', text });
  ttsClients.forEach(client => {
    client.write(`data: ${message}\n\n`);
  });
}

// Helper function to notify all connected clients about wait status
function notifyWaitStatus(isWaiting: boolean) {
  const message = JSON.stringify({ type: 'waitStatus', isWaiting });
  ttsClients.forEach(client => {
    client.write(`data: ${message}\n\n`);
  });
}

// Helper function to format voice utterances for display
function formatVoiceUtterances(utterances: any[]): string {
  const utteranceTexts = utterances
    .map(u => `"${u.text}"`)
    .join('\n');

  return `Assistant received voice input from the user (${utterances.length} utterance${utterances.length !== 1 ? 's' : ''}):\n\n${utteranceTexts}${getVoiceResponseReminder()}`;
}

// API for voice preferences
app.post('/api/voice-preferences', (req: Request, res: Response) => {
  const { voiceResponsesEnabled } = req.body;

  // Update preferences
  voicePreferences.voiceResponsesEnabled = !!voiceResponsesEnabled;

  debugLog(`[Preferences] Updated: voiceResponses=${voicePreferences.voiceResponsesEnabled}`);

  res.json({
    success: true,
    preferences: voicePreferences
  });
});

// Backward-compatible alias
app.post('/api/voice-responses', (req: Request, res: Response) => {
  const { enabled } = req.body;
  voicePreferences.voiceResponsesEnabled = !!enabled;
  debugLog(`[Preferences] Updated (legacy): voiceResponses=${voicePreferences.voiceResponsesEnabled}`);
  res.json({
    success: true,
    preferences: voicePreferences,
  });
});

// API for voice input state
app.post('/api/voice-input-state', (req: Request, res: Response) => {
  const { active } = req.body;

  // Update voice input state
  voicePreferences.voiceInputActive = !!active;

  debugLog(`[Voice Input] ${voicePreferences.voiceInputActive ? 'Started' : 'Stopped'} listening`);

  res.json({
    success: true,
    voiceInputActive: voicePreferences.voiceInputActive
  });
});

// Backward-compatible alias
app.post('/api/voice-input', (req: Request, res: Response) => {
  const { active } = req.body;
  voicePreferences.voiceInputActive = !!active;
  debugLog(`[Voice Input] (legacy) ${voicePreferences.voiceInputActive ? 'Started' : 'Stopped'} listening`);
  res.json({
    success: true,
    voiceInputActive: voicePreferences.voiceInputActive,
  });
});

// API for text-to-speech
app.post('/api/speak', async (req: Request, res: Response) => {
  const { text } = req.body;

  if (!text || !text.trim()) {
    res.status(400).json({ error: 'Text is required' });
    return;
  }

  // Check if voice responses are enabled
  if (!voicePreferences.voiceResponsesEnabled) {
    debugLog(`[Speak] Voice responses disabled, returning error`);
    res.status(400).json({
      error: 'Voice responses are disabled',
      message: 'Cannot speak when voice responses are disabled'
    });
    return;
  }

  try {
    const normalizedText = String(text).replace(/\s+/g, ' ').trim().toLowerCase();
    const nowMs = Date.now();
    if (
      normalizedText &&
      lastSpeakPayload &&
      lastSpeakPayload.normalizedText === normalizedText &&
      nowMs - lastSpeakPayload.timestampMs < 4000
    ) {
      debugLog('[Speak] Duplicate speak request detected, skipping duplicate output');
      res.json({
        success: true,
        message: 'Duplicate speak skipped',
        duplicateSkipped: true,
      });
      return;
    }

    lastSpeakPayload = {
      normalizedText,
      timestampMs: nowMs,
    };

    // Always notify browser clients - they decide how to speak
    notifyTTSClients(text);
    debugLog(`[Speak] Sent text to browser for TTS: "${text}"`);

    // Note: The browser will decide whether to use system voice or browser voice

    // Store assistant's response in conversation history
    queue.addAssistantMessage(text);

    // Mark all delivered utterances as responded
    const deliveredUtterances = queue.utterances.filter(u => u.status === 'delivered');
    deliveredUtterances.forEach(u => {
      u.status = 'responded';
      debugLog(`[Queue] marked as responded: "${u.text}"	[id: ${u.id}]`);

      // Sync status in messages array
      const message = queue.messages.find(m => m.id === u.id && m.role === 'user');
      if (message) {
        message.status = 'responded';
      }
    });

    lastSpeakTimestamp = new Date();

    res.json({
      success: true,
      message: 'Text spoken successfully',
      respondedCount: deliveredUtterances.length
    });
  } catch (error) {
    debugLog(`[Speak] Failed to speak text: ${error}`);
    res.status(500).json({
      error: 'Failed to speak text',
      details: error instanceof Error ? error.message : String(error)
    });
  }
});

app.get('/api/speak-status', (_req: Request, res: Response) => {
  const nowMs = Date.now();
  const msSinceLastSpeak = lastSpeakPayload ? nowMs - lastSpeakPayload.timestampMs : null;
  res.json({
    success: true,
    hasRecentSpeak: msSinceLastSpeak !== null && msSinceLastSpeak < 4000,
    msSinceLastSpeak,
  });
});

// API for system text-to-speech (cross-platform fallback)
app.post('/api/speak-system', async (req: Request, res: Response) => {
  const { text, rate: requestedRate = 150 } = req.body;

  if (!text || !text.trim()) {
    res.status(400).json({ error: 'Text is required' });
    return;
  }

  try {
    const rateNumber = Number.isFinite(Number(requestedRate)) ? Number(requestedRate) : 150;
    const rate = clampNumber(Math.round(rateNumber), 80, 320);
    const engine = await resolveSystemTTSEngine();

    if (!engine) {
      res.status(500).json({
        error: 'No system TTS engine found',
        details: 'Install spd-say, espeak-ng, or espeak on Linux (or use browser voice).',
      });
      return;
    }

    const normalizedText = String(text).replace(/\s+/g, ' ').trim();
    await stopActiveSystemTTS();
    await runSystemTTS(engine, normalizedText, rate);
    debugLog(`[Speak System] Spoke text via ${engine.name}: "${normalizedText}" (rate: ${rate})`);

    res.json({
      success: true,
      message: `Text spoken successfully via ${engine.name}`
    });
  } catch (error) {
    debugLog(`[Speak System] Failed to speak text: ${error}`);
    res.status(500).json({
      error: 'Failed to speak text via system voice',
      details: error instanceof Error ? error.message : String(error)
    });
  }
});

app.post('/api/speak-system/stop', async (_req: Request, res: Response) => {
  const stopped = await stopActiveSystemTTS();
  res.json({
    success: true,
    stopped,
  });
});

// UI Routing
app.get('/', (_req: Request, res: Response) => {
  debugLog(`[HTTP] Serving index.html for root route`);
  res.sendFile(path.join(__dirname, '..', 'public', 'index.html'));
});

app.get('/legacy', (_req: Request, res: Response) => {
  res.sendFile(path.join(__dirname, '..', 'public', 'legacy.html'));
});

// Start HTTP server
app.listen(HTTP_PORT, HTTP_HOST, async () => {
  const logFn = IS_MCP_MANAGED ? console.error : console.log;
  const urls = getNetworkUrls(HTTP_PORT);
  const phoneUrls = urls.filter((url) => !url.includes('localhost'));

  logFn(`[HTTP] Server listening on http://localhost:${HTTP_PORT}`);
  if (phoneUrls.length > 0) {
    phoneUrls.forEach((url) => {
      logFn(`[HTTP] Phone access URL: ${url}`);
    });
  }

  logFn(`[Mode] Running in ${IS_MCP_MANAGED ? 'MCP-managed' : 'standalone'} mode`);

  // Auto-open browser if no frontend connects within 3 seconds
  const autoOpenBrowser = process.env.MCP_VOICE_HOOKS_AUTO_OPEN_BROWSER !== 'false'; // Default to true
  if (IS_MCP_MANAGED && autoOpenBrowser) {
    setTimeout(async () => {
      if (ttsClients.size === 0) {
        debugLog('[Browser] No frontend connected, opening browser...');
        try {
          const open = (await import('open')).default;
          // Open default UI (messenger is now at root)
          await open(`http://localhost:${HTTP_PORT}`);
        } catch (error) {
          debugLog('[Browser] Failed to open browser:', error);
        }
      } else {
        debugLog(`[Browser] Frontend already connected (${ttsClients.size} client(s))`)
      }
    }, 3000);
  }
});

// Helper function to get voice response reminder
function getVoiceResponseReminder(): string {
  const voiceResponsesEnabled = voicePreferences.voiceResponsesEnabled;
  return voiceResponsesEnabled
    ? '\n\nThe user has enabled voice responses, so use the \'speak\' tool to respond to the user\'s voice input before proceeding.'
    : '';
}

// MCP Server Setup (only if MCP-managed)
if (IS_MCP_MANAGED) {
  // Use stderr in MCP mode to avoid interfering with protocol
  console.error('[MCP] Initializing MCP server...');

  const mcpServer = new Server(
    {
      name: 'voice-hooks',
      version: '1.0.0',
    },
    {
      capabilities: {
        tools: {},
      },
    }
  );

  // Tool handlers
  mcpServer.setRequestHandler(ListToolsRequestSchema, async () => {
    // Only expose the speak tool - voice input is auto-delivered via hooks
    return {
      tools: [
        {
          name: 'speak',
          description: 'Speak text using text-to-speech and mark delivered utterances as responded',
          inputSchema: {
            type: 'object',
            properties: {
              text: {
                type: 'string',
                description: 'The text to speak',
              },
            },
            required: ['text'],
          },
        }
      ]
    };
  });

  mcpServer.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;

    try {
      if (name === 'speak') {
        const text = args?.text as string;

        if (!text || !text.trim()) {
          return {
            content: [
              {
                type: 'text',
                text: 'Error: Text is required for speak tool',
              },
            ],
            isError: true,
          };
        }

        const response = await fetch(`http://localhost:${HTTP_PORT}/api/speak`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        });

        const data = await response.json() as any;

        if (response.ok) {
          return {
            content: [
              {
                type: 'text',
                text: '',  // Return empty string for success
              },
            ],
          };
        } else {
          return {
            content: [
              {
                type: 'text',
                text: `Error speaking text: ${data.error || 'Unknown error'}`,
              },
            ],
            isError: true,
          };
        }
      }

      throw new Error(`Unknown tool: ${name}`);
    } catch (error) {
      return {
        content: [
          {
            type: 'text',
            text: `Error: ${error instanceof Error ? error.message : String(error)}`,
          },
        ],
        isError: true,
      };
    }
  });

  // Connect via stdio
  const transport = new StdioServerTransport();
  mcpServer.connect(transport);
  // Use stderr in MCP mode to avoid interfering with protocol
  console.error('[MCP] Server connected via stdio');
} else {
  // Only log in standalone mode
  if (!IS_MCP_MANAGED) {
    console.log('[MCP] Skipping MCP server initialization (not in MCP-managed mode)');
  }
}
