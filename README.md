# RMS Drone Command (Bridge for ROS-MCP)

This repository contains the **Drone Bridge** integration for `ros-mcp-server`. 
It is structured as a standalone example that can be merged into `robotmcp/ros-mcp-server`.

## Directory Structure

- `robot_specifications/`: Contains the LLM context for the drone.
- `examples/10_drone_px4/`: Contains the source code.
  - `drone_interfaces`: Custom definitions for Takeoff, Navigate, Orbit Actions.
  - `drone_controller`: The Bridge Node implementing safety logic.

### 4. Gemini Integration
To use this with `ros-mcp-server`, provide the robot specification file:

```bash
# Example command (adjust based on your server setup)
ros2 run ros_mcp_server server --ros-args -p robot_config:=$(ros2 pkg prefix drone_controller)/share/drone_controller/config/drone_px4.yaml
```

The specification file is located at `robot_specifications/drone_px4.yaml` and defines the available actions for the LLM.

## Usage

See [examples/10_drone_px4/README.md](examples/10_drone_px4/README.md) for detailed instructions.