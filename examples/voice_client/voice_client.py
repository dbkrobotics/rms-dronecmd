import os
import sys
import asyncio
import time
import warnings
from contextlib import contextmanager
from typing import Optional

# Suppress Warnings
warnings.filterwarnings("ignore")

# Third-party libraries
import speech_recognition as sr
from gtts import gTTS
import google.generativeai as genai
from rich.console import Console
from rich.spinner import Spinner
from rich.live import Live
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# -- Configuration --
API_KEY = os.getenv("GOOGLE_API_KEY")
MCP_SERVER_DIR = os.getenv("MCP_SERVER_PATH", os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../mcp-ros-server")))

# -- UI Setup --
console = Console()

# Context manager to suppress C-level stderr (ALSA warnings) using file descriptors
# This is safer than ctypes for avoiding segfaults
@contextmanager
def ignore_stderr():
    try:
        # Open /dev/null
        devnull = os.open(os.devnull, os.O_WRONLY)
        # Save old stderr
        old_stderr = os.dup(2)
        sys.stderr.flush()
        # Redirect stderr to /dev/null
        os.dup2(devnull, 2)
        os.close(devnull)
        yield
    except Exception:
        yield
    finally:
        # Restore stderr
        try:
            os.dup2(old_stderr, 2)
            os.close(old_stderr)
        except Exception:
            pass

class VoiceClient:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        
        # Use the safe context manager
        with ignore_stderr():
            self.microphone = sr.Microphone()
        
        self.recognizer.energy_threshold = 300
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.pause_threshold = 0.8
        
        if not API_KEY:
            console.print("[bold red]Error:[/bold red] GOOGLE_API_KEY not found.")
            sys.exit(1)
            
        genai.configure(api_key=API_KEY)
        self.model = None
        self.chat = None

    def speak(self, text: str):
        """Synthesize speech and play it."""
        if not text:
            return
            
        console.print(f"[bold cyan]Gemini:[/bold cyan] {text}")
        try:
            tts = gTTS(text=text, lang='en') # Default to English for now
            filename = "temp_voice.mp3"
            tts.save(filename)
            # Use afplay on macOS
            if sys.platform == "darwin":
                os.system(f"afplay {filename}")
            else:
                os.system(f"mpg123 -q {filename}") 
            if os.path.exists(filename):
                os.remove(filename)
        except Exception as e:
            console.print(f"[bold red]TTS Error:[/bold red] {e}")

    def listen(self, live_status) -> Optional[str]:
        """Listen to microphone input."""
        with self.microphone as source:
            live_status.update(Spinner("dots", text="Adjusting for ambient noise..."))
            self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
            
            live_status.update(Spinner("dots", text="Listening..."))
            try:
                audio = self.recognizer.listen(source, timeout=10, phrase_time_limit=10)
                live_status.update(Spinner("dots", text="Transcribing..."))
                text = self.recognizer.recognize_google(audio)
                console.print(f"[bold green]User:[/bold green] {text}")
                return text
            except sr.WaitTimeoutError:
                return None
            except sr.UnknownValueError:
                return None
            except sr.RequestError as e:
                console.print(f"[bold red]STT Error:[/bold red] {e}")
                return None

    async def run(self):
        # Connect to MCP Server
        server_params = StdioServerParameters(
            command="uv",
            # Add --active to use the currently active virtual environment
            args=["run", "--active", "server.py"],
            cwd=MCP_SERVER_DIR,
            env=os.environ.copy() # Pass env for dependencies
        )

        console.print(f"[bold blue]Connecting to MCP Server at: {MCP_SERVER_DIR}[/bold blue]")
        
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # List tools
                tools_response = await session.list_tools()
                # Simplified tool conversion for Gemini
                # Note: This is a basic mapping. Complex tools might need more robust conversion.
                gemini_tools = []
                tool_map = {}
                
                for tool in tools_response.tools:
                    tool_map[tool.name] = tool
                    
                    def sanitize_schema(schema):
                        if not isinstance(schema, dict):
                            return schema
                        new_schema = schema.copy()
                        
                        # Handle combinatorial types by picking the first valid option
                        # This simplifies complex schemas (like Optional[str]) for Gemini
                        for key in ["anyOf", "oneOf", "allOf"]:
                            if key in new_schema:
                                options = new_schema[key]
                                selected = None
                                if isinstance(options, list):
                                    for opt in options:
                                        # Prefer non-null types
                                        if opt.get("type", "").lower() != "null":
                                            selected = opt
                                            break
                                    if not selected and options:
                                        selected = options[0]
                                
                                if selected:
                                    # Merge selected option
                                    new_schema.update(sanitize_schema(selected))
                                del new_schema[key]

                        # Remove fields unsupported by Gemini
                        for field in ["additionalProperties", "title", "$schema", "default"]: 
                            if field in new_schema:
                                del new_schema[field]
                        
                        if "type" in new_schema and isinstance(new_schema["type"], str):
                            new_schema["type"] = new_schema["type"].upper()
                            
                        if "properties" in new_schema:
                            new_schema["properties"] = {k: sanitize_schema(v) for k, v in new_schema["properties"].items()}
                        if "items" in new_schema:
                            new_schema["items"] = sanitize_schema(new_schema["items"])
                        return new_schema

                    sanitized_input_schema = sanitize_schema(tool.inputSchema)
                    
                    func_decl = {
                        "name": tool.name.replace("-", "_"),
                        "description": tool.description,
                        "parameters": sanitized_input_schema
                    }
                    gemini_tools.append(func_decl)

                console.print(f"[green]Loaded {len(gemini_tools)} tools.[/green]")

                console.print(f"[green]Loaded {len(gemini_tools)} tools.[/green]")

                # Dynamically select model
                available_models = []
                try:
                    for m in genai.list_models():
                        if 'generateContent' in m.supported_generation_methods:
                            available_models.append(m.name)
                except Exception as e:
                    console.print(f"[bold red]Error listing models:[/bold red] {e}")

                model_name = "models/gemini-1.5-flash" # Default fallback
                
                # Priority: flash > pro > 1.5 > 1.0
                priorities = ["gemini-1.5-flash", "gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.0-pro", "gemini-pro"]
                selected_model = None
                
                for p in priorities:
                    for m in available_models:
                        if p in m:
                            selected_model = m
                            break
                    if selected_model:
                        break
                
                if not selected_model and available_models:
                     selected_model = available_models[0]
                
                if selected_model:
                    model_name = selected_model

                console.print(f"[bold blue]Using Gemini Model: {model_name}[/bold blue]")

                # System Instruction for "Smart" Behavior
                SYSTEM_INSTRUCTION = """
You are a ROS 2 Robotics Assistant powered by Gemini.
Your purpose is to help the user control robots via the MCP server.

**Core Responsibilities:**
1.  **Identify the Robot**:
    -   If the user hasn't specified a robot, ask them or check `get_verified_robots_list()`.
    -   Once identified (e.g., 'drone_px4'), ALWAYS call `get_verified_robot_spec(name='...')` to load its specific commands and safety rules.
2.  **Safety First**:
    -   Before executing movement commands (takeoff, move), check the robot's state (battery, position, mode).
    -   If state tools are unavailable, ask the user for confirmation.
3.  **Voice-Optimized Responses**:
    -   Keep answers concise and conversational.
    -   Avoid code blocks or long lists unless explicitly asked.
    -   Confirm actions before executing them (e.g., "Taking off to 5 meters now").

**Tools & Capabilities**:
-   You have access to ROS 2 tools (topics, services, actions).
-   Use `get_verified_robot_spec` to get detailed prompts for specific robots.
"""

                # Initialize Gemini with tools and system instruction
                # Using gemini-1.5-flash for speed
                # Note: 'google.generativeai' is deprecated, but we are fixing the schema issue first.
                self.model = genai.GenerativeModel(
                    model_name=model_name,
                    tools=gemini_tools,
                    system_instruction=SYSTEM_INSTRUCTION
                )
                self.chat = self.model.start_chat()
                
                # Helper to handle Gemini responses and tool calls
                async def process_response(response, retry_delay=2, max_retries=3):
                    if not response:
                        return

                    # Check for function calls
                    for part in response.parts:
                        if fn := part.function_call:
                            tool_name_gemini = fn.name
                            tool_name_mcp = tool_name_gemini.replace("_", "-")
                            args = dict(fn.args)
                            
                            self.speak(f"Executing {tool_name_mcp}...")
                            with Live(Spinner("runner", text=f"Executing {tool_name_mcp}..."), refresh_per_second=10) as live_exec:
                                try:
                                    result = await session.call_tool(tool_name_mcp, arguments=args)
                                    
                                    # Retry loop for tool output
                                    for attempt in range(max_retries):
                                        try:
                                            # Send tool result back to Gemini
                                            tool_response = self.chat.send_message(
                                                genai.protos.Content(
                                                    parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(
                                                        name=fn.name,
                                                        response={"result": result.content}
                                                    ))]
                                                )
                                            )
                                            # Process the follow-up response recursively
                                            await process_response(tool_response)
                                            break
                                        except Exception as e:
                                            if "429" in str(e):
                                                if attempt < max_retries - 1:
                                                    time.sleep(retry_delay * (2 ** attempt))
                                                    continue
                                            console.print(f"[bold red]Error sending tool result:[/bold red] {e}")
                                            break
                                            
                                except Exception as e:
                                    console.print(f"[bold red]Tool Execution Error:[/bold red] {e}")
                                    self.speak("There was an error executing the tool.")

                    # Final response text
                    if response.text:
                        self.speak(response.text)

                # --- Auto-Initialization Removed (User requested manual control) ---
                self.speak("System connected. Ready for commands.")
                
                while True:
                    with Live(Spinner("dots", text="Ready"), refresh_per_second=10) as live_status:
                        user_input = self.listen(live_status)
                        
                        if not user_input:
                            continue
                            
                        if user_input.lower() in ["exit", "quit", "stop"]:
                            self.speak("Goodbye.")
                            break

                        live_status.update(Spinner("moon", text="Thinking..."))
                        
                        # Send to Gemini with Retry Logic
                        max_retries = 3
                        retry_delay = 2
                        response = None
                        
                        for attempt in range(max_retries):
                            try:
                                response = self.chat.send_message(user_input)
                                break
                            except Exception as e:
                                if "429" in str(e) or "Resource exhausted" in str(e):
                                    if attempt < max_retries - 1:
                                        wait_time = retry_delay * (2 ** attempt)
                                        console.print(f"[bold yellow]Rate limit hit. Retrying in {wait_time}s...[/bold yellow]")
                                        time.sleep(wait_time)
                                        continue
                                console.print(f"[bold red]Gemini API Error:[/bold red] {e}")
                                self.speak("I'm having trouble connecting to the AI service.")
                                break
                        
                        await process_response(response)

if __name__ == "__main__":
    client = VoiceClient()
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass
