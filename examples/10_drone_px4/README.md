# Example 10: PX4 Drone with MCP

This example demonstrates how to control a PX4-based drone using the MCP Server and custom ROS 2 Actions.

## Prerequisites

1.  **PX4 Autopilot**: [Installation Guide](https://docs.px4.io/main/en/ros2/user_guide)
2.  **MAVROS**: `sudo apt install ros-jazzy-mavros`
3.  **QGroundControl**: For monitoring.

## Installation

This folder contains two ROS 2 packages:
- `drone_interfaces`: Custom Action definitions.
- `drone_controller`: The Bridge Node that translates Actions to MAVROS commands.

To install:
1.  Copy or symlink these packages to your ROS 2 workspace `src`.
2.  `colcon build --packages-select drone_interfaces drone_controller`
3.  `source install/setup.bash`

## Running the Example

1. Start PX4 SITL:
   ```bash
   cd ~/PX4-Autopilot && make px4_sitl gz_x500
   ```
2. Start the MCP Bridge Node:
   ```bash
   ros2 run drone_controller bridge
   ```
3. Connect your MCP Client (Claude/Gemini) and start flying!
