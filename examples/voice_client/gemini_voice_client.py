import os
import sys
import asyncio
import time
import warnings
from typing import Optional

# Suppress Warnings
warnings.filterwarnings("ignore")

# Third-party libraries
import socketio
import google.generativeai as genai
from rich.console import Console
from rich.spinner import Spinner
from rich.live import Live
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# -- Configuration --
API_KEY = os.getenv("GOOGLE_API_KEY")
MCP_SERVER_DIR = os.getenv("MCP_SERVER_PATH", os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../mcp-ros-server")))
VOICE_SERVER_URL = "http://localhost:5111"

# -- UI Setup --
console = Console()

class GeminiVoiceClient:
    def __init__(self):
        self.sio = socketio.AsyncClient()
        self.input_queue = asyncio.Queue()
        
        if not API_KEY:
            console.print("[bold red]Error:[/bold red] GOOGLE_API_KEY not found.")
            sys.exit(1)
            
        genai.configure(api_key=API_KEY)
        self.model = None
        self.chat = None

    async def connect_voice_server(self):
        """Connect to the local voice bridge server."""
        try:
            await self.sio.connect(VOICE_SERVER_URL)
            console.print(f"[green]Connected to Voice Server at {VOICE_SERVER_URL}[/green]")
        except Exception as e:
            console.print(f"[bold red]Failed to connect to Voice Server:[/bold red] {e}")
            console.print("[yellow]Ensure 'python voice_server.py' is running.[/yellow]")
            sys.exit(1)

        # Setup event handlers
        @self.sio.on('user_message')
        async def on_user_message(data):
            console.print(f"[bold green]User (Web):[/bold green] {data}")
            await self.input_queue.put(data)

    async def speak(self, text: str):
        """Send text to the voice server to be spoken by the browser."""
        if not text:
            return
        console.print(f"[bold cyan]Gemini:[/bold cyan] {text}")
        if self.sio.connected:
            await self.sio.emit('bot_output', text)

    async def run(self):
        # 1. Connect to Voice Server
        await self.connect_voice_server()

        # 2. Connect to MCP Server
        server_params = StdioServerParameters(
            command="uv",
            args=["run", "--active", "server.py"],
            cwd=MCP_SERVER_DIR,
            env=os.environ.copy()
        )

        console.print(f"[bold blue]Connecting to MCP Server at: {MCP_SERVER_DIR}[/bold blue]")
        
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # List tools
                tools_response = await session.list_tools()
                gemini_tools = []
                
                # ... (Tool Conversion Logic - Same as before) ...
                for tool in tools_response.tools:
                    def sanitize_schema(schema):
                        if not isinstance(schema, dict): return schema
                        new_schema = schema.copy()
                        for key in ["anyOf", "oneOf", "allOf"]:
                            if key in new_schema:
                                options = new_schema[key]
                                selected = next((opt for opt in options if opt.get("type") != "null"), options[0] if options else None)
                                if selected: new_schema.update(sanitize_schema(selected))
                                del new_schema[key]
                        for field in ["additionalProperties", "title", "$schema", "default"]: 
                            if field in new_schema: del new_schema[field]
                        if "type" in new_schema and isinstance(new_schema["type"], str):
                            new_schema["type"] = new_schema["type"].upper()
                        if "properties" in new_schema:
                            new_schema["properties"] = {k: sanitize_schema(v) for k, v in new_schema["properties"].items()}
                        if "items" in new_schema:
                            new_schema["items"] = sanitize_schema(new_schema["items"])
                        return new_schema

                    sanitized_input_schema = sanitize_schema(tool.inputSchema)
                    gemini_tools.append({
                        "name": tool.name.replace("-", "_"),
                        "description": tool.description,
                        "parameters": sanitized_input_schema
                    })

                console.print(f"[green]Loaded {len(gemini_tools)} tools.[/green]")

                # Initialize Gemini with fallback model selection
                available_models = []
                try:
                    for m in genai.list_models():
                        if 'generateContent' in m.supported_generation_methods:
                            available_models.append(m.name)
                except Exception: pass

                model_name = "models/gemini-1.5-flash"
                priorities = ["gemini-1.5-flash", "gemini-2.0-flash", "gemini-1.5-pro"]
                for p in priorities:
                    found = next((m for m in available_models if p in m), None)
                    if found: 
                        model_name = found
                        break
                
                console.print(f"[bold blue]Using Gemini Model: {model_name}[/bold blue]")

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
"""
                self.model = genai.GenerativeModel(
                    model_name=model_name,
                    tools=gemini_tools,
                    system_instruction=SYSTEM_INSTRUCTION
                )
                self.chat = self.model.start_chat()
                
                # Process Response Helper
                async def process_response(response, retry_delay=2, max_retries=3):
                    if not response: return

                    for part in response.parts:
                        if fn := part.function_call:
                            tool_name_mcp = fn.name.replace("_", "-")
                            args = dict(fn.args)
                            
                            await self.speak(f"Executing {tool_name_mcp}...")
                            with Live(Spinner("runner", text=f"Executing {tool_name_mcp}..."), refresh_per_second=10):
                                try:
                                    result = await session.call_tool(tool_name_mcp, arguments=args)
                                    
                                    # Send result back to Gemini
                                    tool_response = self.chat.send_message(
                                        genai.protos.Content(
                                            parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(
                                                name=fn.name,
                                                response={"result": result.content}
                                            ))]
                                        )
                                    )
                                    await process_response(tool_response)
                                except Exception as e:
                                    console.print(f"[bold red]Tool Execution Error:[/bold red] {e}")
                                    await self.speak("There was an error executing the tool.")

                    if response.text:
                        await self.speak(response.text)

                # Ready
                await self.speak("System connected. Ready for commands.")
                
                while True:
                    # Wait for input from Queue (populated by SocketIO)
                    console.print("[dim]Waiting for voice input...[/dim]")
                    user_input = await self.input_queue.get()
                    
                    if user_input.lower() in ["exit", "quit", "stop"]:
                        await self.speak("Goodbye.")
                        break

                    # Send to Gemini
                    try:
                        response = self.chat.send_message(user_input)
                        await process_response(response)
                    except Exception as e:
                        console.print(f"[bold red]Gemini API Error:[/bold red] {e}")
                        await self.speak("I'm having trouble connecting to the AI service.")

        await self.sio.disconnect()

if __name__ == "__main__":
    client = GeminiVoiceClient()
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass
