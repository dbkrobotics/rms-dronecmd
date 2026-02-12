import os
import sys
import asyncio
import time
from typing import Optional

# Third-party libraries
import speech_recognition as sr
from gtts import gTTS, gTTSError
from pydantic import BaseModel
import google.generativeai as genai
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.text import Text
from rich.spinner import Spinner
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# -- Configuration --
API_KEY = os.getenv("GOOGLE_API_KEY")
# Default assumption: mcp-ros-server is a sibling directory or provided via env
MCP_SERVER_DIR = os.getenv("MCP_SERVER_PATH", os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../mcp-ros-server")))
MCP_SERVER_SCRIPT = os.path.join(MCP_SERVER_DIR, "server.py")

# -- UI Setup --
console = Console()

class VoiceClient:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.microphone = sr.Microphone()
        
        # Adjust recognizer settings for responsiveness
        self.recognizer.energy_threshold = 300  # Lower default threshold
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.pause_threshold = 0.8   # Faster end-of-speech detection
        
        if not API_KEY:
            console.print("[bold red]Error:[/bold red] GOOGLE_API_KEY not found.")
            sys.exit(1)
            
        genai.configure(api_key=API_KEY)
        self.model = None # Initialized later with tools
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
                # Fallback or other OS
                os.system(f"mpg123 {filename}") 
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
            args=["run", "server.py"],
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

                # Initialize Gemini with tools
                # Using gemini-1.5-flash for speed
                # Note: 'google.generativeai' is deprecated, but we are fixing the schema issue first.
                self.model = genai.GenerativeModel(
                    model_name=model_name,
                    tools=gemini_tools
                )
                self.chat = self.model.start_chat()

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
                        
                        # Send to Gemini
                        response = self.chat.send_message(user_input)
                        
                        # Check for function calls
                        for part in response.parts:
                            if fn := part.function_call:
                                tool_name_gemini = fn.name
                                tool_name_mcp = tool_name_gemini.replace("_", "-") # simplistic reverse mapping
                                args = dict(fn.args)
                                
                                # Pre-action feedback
                                self.speak(f"Executing {tool_name_mcp}...")
                                live_status.update(Spinner("runner", text=f"Executing {tool_name_mcp}..."))
                                
                                try:
                                    result = await session.call_tool(tool_name_mcp, arguments=args)
                                    
                                    # Send result back to Gemini
                                    # Construct response based on Gemini's expectation for tool outputs
                                    # (Needs specific format mapping, simplifiying here)
                                    tool_response = {
                                        "name": fn.name,
                                        "response": {"result": result.content}
                                    }
                                    
                                    response = self.chat.send_message(
                                        genai.protos.Content(
                                            parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(
                                                name=fn.name,
                                                response={"result": result.content} # Simplified
                                            ))]
                                        )
                                    )
                                    
                                except Exception as e:
                                    console.print(f"[bold red]Tool Execution Error:[/bold red] {e}")
                                    self.speak("There was an error executing the tool.")

                        # Final response
                        if response.text:
                            self.speak(response.text)

if __name__ == "__main__":
    client = VoiceClient()
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass
