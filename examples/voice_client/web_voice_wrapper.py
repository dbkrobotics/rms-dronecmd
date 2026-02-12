import asyncio
import os
import sys
import subprocess
import argparse
import socketio
import queue
from rich.console import Console

# --- Configuration ---
VOICE_SERVER_URL = "http://localhost:5111"

console = Console()

class WebVoiceWrapper:
    def __init__(self, command):
        self.command = command
        self.sio = socketio.AsyncClient()
        self.process = None
        self.input_queue = asyncio.Queue()

    async def connect_server(self):
        try:
            await self.sio.connect(VOICE_SERVER_URL)
            console.print(f"[green]Connected to Voice Bridge at {VOICE_SERVER_URL}[/green]")
        except Exception as e:
            console.print(f"[bold red]Connection Error:[/bold red] {e}")
            sys.exit(1)
            
        @self.sio.on('user_message')
        async def on_user_message(data):
            console.print(f"[bold green]Voice Input:[/bold green] {data}")
            if self.process and self.process.stdin:
                # Inject into CLI stdin
                self.process.stdin.write((data + "\n").encode())
                await self.process.stdin.drain()

    async def run_subprocess(self):
        """Runs the CLI command as a subprocess."""
        console.print(f"[bold blue]Launching CLI:[/bold blue] {self.command}")
        
        self.process = await asyncio.create_subprocess_shell(
            self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        async def read_stream(stream, channel):
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode().strip()
                if text:
                    console.print(f"[{channel}] {text}")
                    # Send stdout to Web for TTS
                    if channel == "stdout":
                         if self.sio.connected:
                            await self.sio.emit('bot_output', text)

        await asyncio.gather(
            read_stream(self.process.stdout, "stdout"),
            read_stream(self.process.stderr, "stderr"),
            self.process.wait()
        )

    async def run(self):
        await self.connect_server()
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
        pass
