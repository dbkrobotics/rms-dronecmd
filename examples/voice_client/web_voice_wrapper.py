import asyncio
import os
import sys
import pty
import socketio
import argparse
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
            if self.master_fd:
                # Write to PTY (simulates user typing)
                os.write(self.master_fd, (data + "\n").encode())

    def read_from_pty(self):
        """Read output from the PTY master."""
        try:
            data = os.read(self.master_fd, 1024)
            if data:
                text = data.decode(errors='replace').strip()
                # Filter out raw echo if needed, or just send everything
                # Basic cleaning to remove ANSI codes could be added here if needed for TTS
                if text:
                    console.print(f"[dim]{text}[/dim]")
                    # Emit to server for TTS
                    # We use create_task because this is called from add_reader callback
                    asyncio.create_task(self.sio.emit('bot_output', text))
        except OSError:
            pass

    async def run_subprocess(self):
        """Runs the CLI command in a PTY."""
        console.print(f"[bold blue]Launching CLI in PTY:[/bold blue] {self.command}")
        
        # Create pseudo-terminal
        master, slave = pty.openpty()
        self.master_fd = master

        # Start subprocess attached to the PTY slave
        self.process = await asyncio.create_subprocess_shell(
            self.command,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=os.setsid # Create new session
        )
        os.close(slave) # Host doesn't need the slave fd

        # Register PTY reader with asyncio loop
        loop = asyncio.get_running_loop()
        loop.add_reader(self.master_fd, self.read_from_pty)

        try:
            await self.process.wait()
        finally:
            loop.remove_reader(self.master_fd)
            os.close(self.master_fd)

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
