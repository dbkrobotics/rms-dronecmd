import subprocess
import threading
import sys
import os
import queue
import argparse
import time
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
import speech_recognition as sr
from gtts import gTTS

# -- UI Setup --
console = Console()

def speak(text):
    """Synthesize speech and play it."""
    if not text or not text.strip():
        return
        
    try:
        tts = gTTS(text=text, lang='en')
        filename = f"temp_voice_{int(time.time())}.mp3"
        tts.save(filename)
        
        if sys.platform == "darwin":
            os.system(f"afplay {filename}")
        else:
            os.system(f"mpg123 -q {filename}") 
            
        if os.path.exists(filename):
            os.remove(filename)
    except Exception as e:
        console.print(f"[bold red]TTS Error:[/bold red] {e}")

def listen_microphone(recognizer, microphone, live_status):
    """Listen to microphone input once."""
    with microphone as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.5)
        live_status.update(Spinner("dots", text="Listening..."))
        
        try:
            audio = recognizer.listen(source, timeout=5, phrase_time_limit=10)
            live_status.update(Spinner("dots", text="Transcribing..."))
            text = recognizer.recognize_google(audio)
            console.print(f"[bold green]User (Voice):[/bold green] {text}")
            return text
        except sr.WaitTimeoutError:
            return None
        except sr.UnknownValueError:
            return None
        except sr.RequestError as e:
            console.print(f"[bold red]STT Error:[/bold red] {e}")
            return None

def reader_thread(process, output_queue):
    """Reads stdout from the subprocess and puts it in a queue."""
    while True:
        output = process.stdout.readline()
        if output:
            text = output.strip()
            if text:
                console.print(f"[cyan]CLI Output:[/cyan] {text}")
                output_queue.put(text)
        if process.poll() is not None:
            break

def monitor_output(output_queue):
    """Monitors the output queue and speaks it."""
    while True:
        try:
            text = output_queue.get(timeout=0.1)
            # Simple heuristic: don't speak everything if it's too fast or technical
            # For now, just speak everything that looks like a sentence
            if len(text) > 3: 
                speak(text)
        except queue.Empty:
            continue

def main():
    parser = argparse.ArgumentParser(description="Voice Wrapper for CLI Tools")
    parser.add_argument("--command", type=str, default="gemini", help="The CLI command to run")
    args = parser.parse_args()

    cmd_list = args.command.split()
    console.print(f"[bold blue]Starting Voice Wrapper for: {args.command}[/bold blue]")

    try:
        # Start the subprocess
        # bufsize=0 and universal_newlines=True (text=True) for unbuffered text IO
        process = subprocess.Popen(
            cmd_list,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered
        )
    except FileNotFoundError:
        console.print(f"[bold red]Error:[/bold red] Command '{cmd_list[0]}' not found. Is it installed?")
        sys.exit(1)

    # Queue for TTS
    output_queue = queue.Queue()

    # Thread to read stdout
    t_read = threading.Thread(target=reader_thread, args=(process, output_queue), daemon=True)
    t_read.start()

    # Thread to speak output (optional, strictly sequential might be better for turn-taking, but CLI implies async)
    t_speak = threading.Thread(target=monitor_output, args=(output_queue,), daemon=True)
    t_speak.start()

    # Setup STT
    recognizer = sr.Recognizer()
    recognizer.energy_threshold = 300
    microphone = sr.Microphone()

    console.print("[bold green]Wrapper Running. Speak to send input to the CLI.[/bold green]")

    try:
        while process.poll() is None:
            with Live(Spinner("dots", text="Ready"), refresh_per_second=10) as live_status:
                user_text = listen_microphone(recognizer, microphone, live_status)
                
                if user_text:
                    if user_text.lower() in ["exit", "quit", "stop wrapper"]:
                        break
                    
                    # Write to subprocess stdin
                    console.print(f"[dim]Sending to stdin: {user_text}[/dim]")
                    try:
                        process.stdin.write(user_text + "\n")
                        process.stdin.flush()
                    except BrokenPipeError:
                        break
            
            # small sleep to prevent busy loop if STT returns None quickly
            time.sleep(0.1)

    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping wrapper...[/yellow]")
    finally:
        process.terminate()
        console.print("[blue]Wrapper exited.[/blue]")

if __name__ == "__main__":
    main()
