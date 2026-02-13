# RMS Drone Command (Bridge for ROS-MCP)

This repository contains the **Drone Bridge** integration for `ros-mcp-server`. 
It is structured as a standalone example that can be merged into `robotmcp/ros-mcp-server`.

## Directory Structure

- `robot_specifications/`: Contains the LLM context for the drone.
- `examples/drone_px4/`: Contains the source code and documentation.
  - `gazebo_sim`: Simulation setup and workspace (including `drone_interfaces` and `drone_controller`).
  - `real_robot`: Placeholder for real robot setup.
  - `images`: Screenshots and diagrams.
- `voice_hooks/`: Existing hook-based voice integration (kept as-is).
- `voice_adk_agent/`: New independent real-time voice agent based on Google ADK + direct ROS MCP tool connection.

## Usage

See [examples/drone_px4/gazebo_sim/README.md](examples/drone_px4/gazebo_sim/README.md) for detailed instructions on running the simulation.
