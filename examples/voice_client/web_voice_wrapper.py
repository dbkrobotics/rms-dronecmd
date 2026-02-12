import re
import asyncio
import os
import sys
import pty
import tty
import termios
import struct
import fcntl
import socketio
import argparse
import signal
from rich.console import Console

# --- Configuration ---
VOICE_SERVER_URL = "http://localhost:5111"

console = Console()

class WebVoiceWrapper:
    def __init__(self, command):
        self.command = command
        self.sio = socketio.AsyncClient()
        self.master_fd = None
        self.process = None
        self.tts_buffer = ""
        self.tts_timer = None
        self.old_tty_attrs = None

    async def connect_server(self):
        try:
            await self.sio.connect(VOICE_SERVER_URL)
            # We can't use console.print easily in raw mode, so we just rely on connection state
        except Exception:
            sys.exit(1)
            
        @self.sio.on('user_message')
        async def on_user_message(data):
            if self.master_fd:
                # Write to PTY (simulates user typing)
                os.write(self.master_fd, (data + "\n").encode())

    def _set_pty_size(self):
        """Sync PTY size with host terminal size."""
        if not self.master_fd:
            return
        try:
            rows, cols, x, y = struct.unpack("HHHH", fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)))
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, x, y))
        except Exception:
            pass

    async def _send_tts(self):
        """Send buffered text to TTS."""
        if self.tts_buffer.strip():
            # Basic heuristic: ignore short prompts or noise? 
            # For now, send everything that looks like text.
            await self.sio.emit('bot_output', self.tts_buffer.strip())
        self.tts_buffer = ""
        self.tts_timer = None

    def read_from_pty(self):
        """Read from PTY master, write to stdout, and buffer for TTS."""
        try:
            data = os.read(self.master_fd, 4096)
            if data:
                # 1. Passthrough to real stdout (Raw bytes)
                os.write(sys.stdout.fileno(), data)
                
                # 2. Process for TTS (Sniffing)
                text_chunk = data.decode(errors='replace')
                # Strip ANSI codes
                clean_chunk = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', text_chunk)
                
                if clean_chunk:
                    self.tts_buffer += clean_chunk
                    
                    # Debounce/Batch TTS sending
                    if self.tts_timer:
                        self.tts_timer.cancel()
                    self.tts_timer = asyncio.create_task(self._delayed_tts())

        except OSError:
            pass

    async def _delayed_tts(self):
        await asyncio.sleep(0.5) # Wait for output to settle (e.g. half a second silence)
        await self._send_tts()

    def read_from_stdin(self):
        """Read from host stdin and write to PTY."""
        try:
            data = os.read(sys.stdin.fileno(), 1024)
            if data:
                os.write(self.master_fd, data)
        except OSError:
            pass

    async def run_subprocess(self):
        # Save TTY settings
        if sys.stdin.isatty():
            self.old_tty_attrs = termios.tcgetattr(sys.stdin)
            tty.setraw(sys.stdin.fileno())
        
        # Determine shell/command
        # If command is a string like "gemini -v", we split it for pty.spawn-like behavior if needed,
        # but asyncio needs list or string. Shell=True allows string.
        
        master, slave = pty.openpty()
        self.master_fd = master
        
        self._set_pty_size()
        signal.signal(signal.SIGWINCH, lambda signum, frame: self._set_pty_size())

        self.process = await asyncio.create_subprocess_shell(
            self.command,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=os.setsid, # Create new session
            env=os.environ.copy() # Inherit environment (PATH, etc)
        )
        os.close(slave)

        loop = asyncio.get_running_loop()
        loop.add_reader(self.master_fd, self.read_from_pty)
        loop.add_reader(sys.stdin.fileno(), self.read_from_stdin)

        try:
            await self.process.wait()
        finally:
            loop.remove_reader(self.master_fd)
            loop.remove_reader(sys.stdin.fileno())
            os.close(self.master_fd)
            
            # Restore TTY settings
            if self.old_tty_attrs:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old_tty_attrs)
            
            # Reset cursor / clear artifacts if needed
            print("Wrapper exited.")

    async def run(self):
        # Connect first (while still in normal TTY mode for print)
        try:
            await self.sio.connect(VOICE_SERVER_URL)
            print(f"Connected to Voice Bridge at {VOICE_SERVER_URL}")
        except Exception as e:
            print(f"Connection Error: {e}")
            sys.exit(1)

        await self.run_subprocess()
        await self.sio.disconnect()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Web Voice Wrapper for CLI")
    parser.add_argument("--command", type=str, default="gemini", help="CLI command to wrap")
    args = parser.parse_args()

    wrapper = WebVoiceWrapper(args.command)
    try:
        asyncio.run(wrapper.run())
    except KeyboardInterrupt:
        # Restore TTY if forced stopped
        if sys.stdin.isatty():
             # We might need to handle this better, but termios usually persists
             termios.tcsetattr(sys.stdin, termios.TCSADRAIN, termios.tcgetattr(sys.stdin)) # Reset logic simplified
             pass
