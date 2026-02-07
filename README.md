# RMS Drone Command (Action Server Refactor)

This repository provides a ROS2-based drone controller using **Actions** to communicate with PX4 Autopilot via MAVROS.

## Prerequisites

1.  **ROS2 Jazzy** (or Humble/Rolling)
2.  **PX4 Autopilot** (for SITL simulation)
3.  **MAVROS** (`ros-jazzy-mavros`)
4.  **QGroundControl** (Optional, for monitoring)

## Installation

```bash
# 1. Clone the repository into your workspace src/
cd ~/ros2_ws/src
git clone https://github.com/dbkrobotics/rms-dronecmd.git

# 2. Build the packages
cd ~/ros2_ws
colcon build --packages-select drone_interfaces drone_controller

# 3. Source the setup script
source install/setup.bash
```

## Usage

### Simulation (SITL)

1.  **Start PX4 SITL** (Terminal 1):
    ```bash
    cd ~/PX4-Autopilot
    make px4_sitl gz_x500
    ```

2.  **Start Drone Controller** (Terminal 2):
    ```bash
    ros2 launch drone_controller sitl.launch.py
    ```

### Real Hardware

1.  **Start Drone Controller** (Terminal 1):
    ```bash
    # Ensure your FCU is connected via UART/Serial
    ros2 launch drone_controller real.launch.py
    ```

## Control Interfaces (ROS2 Actions)

The controller now exposes **Actions** instead of Services for better feedback and control.

| Action Topic | Type | Description |
| :--- | :--- | :--- |
| `/drone_control/takeoff` | `DroneTakeoff` | Takeoff to a specific altitude. |
| `/drone_control/navigate` | `DroneNavigate` | Fly to (x, y, z) coordinates. |
| `/drone_control/orbit` | `DroneOrbit` | Orbit around a center point. |

### Example CLI Verification

```bash
ros2 action send_goal /drone_control/takeoff drone_interfaces/action/DroneTakeoff "{target_altitude: 5.0}"
```
