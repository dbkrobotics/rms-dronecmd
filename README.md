# RMS Drone Command (Bridge for ROS-MCP)

This repository contains the **Drone Bridge** integration for `ros-mcp-server`. 
It is structured as a standalone example that can be merged into `robotmcp/ros-mcp-server`.

## Directory Structure

- `robot_specifications/`: Contains the LLM context for the drone.
- `examples/10_drone_px4/`: Contains the source code.
  - `drone_interfaces`: Custom definitions for Takeoff, Navigate, Orbit Actions.
  - `drone_controller`: The Bridge Node implementing safety logic.

## Usage

See [examples/10_drone_px4/README.md](examples/10_drone_px4/README.md) for detailed instructions.