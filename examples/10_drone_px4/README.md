# Example 10: PX4 Drone with MCP

This example demonstrates how to control a PX4-based drone using the MCP Server and custom ROS 2 Actions.

## 1. Prerequisites (Installation)

### 1a. PX4 Autopilot
Official Guide: [PX4 User Guide](https://docs.px4.io/main/en/ros2/user_guide)

**Open Terminal:**
```bash
cd ~
git clone https://github.com/PX4/PX4-Autopilot.git --recursive
bash ./PX4-Autopilot/Tools/setup/ubuntu.sh
cd PX4-Autopilot/
make px4_sitl
```
*Verification:*
```bash
cd ~/PX4-Autopilot/
make px4_sitl gz_x500
```
(Close Gazebo after verification)

### 1b. MAVROS (ROS 2 Jazzy)
Official Guide: [MAVROS Guide](https://docs.ros.org/en/jazzy/p/mavros/)

**Open Terminal:**
```bash
cd ~
sudo apt install ros-jazzy-mavros
wget https://raw.githubusercontent.com/mavlink/mavros/ros2/mavros/scripts/install_geographiclib_datasets.sh
sudo bash ./install_geographiclib_datasets.sh
```

### 1c. QGroundControl
Official Guide: [QGC Installation](https://docs.qgroundcontrol.com/master/en/qgc-user-guide/getting_started/download_and_install.html)

**Open Terminal:**
```bash
sudo usermod -a -G dialout $USER
sudo apt-get remove modemmanager -y
sudo apt install gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-gl -y
sudo apt install libfuse2 -y
sudo apt install libxcb-xinerama0 libxkbcommon-x11-0 libxcb-cursor-dev -y

# Download and run QGC
cd ~/Downloads  # Or your preferred download folder
wget https://d176tv9ibo4jno.cloudfront.net/latest/QGroundControl-x86_64.AppImage
chmod +x ./QGroundControl-x86_64.AppImage
./QGroundControl-x86_64.AppImage
```

### 1d. Install This Package (rms-dronecmd)
**Open Terminal:**
```bash
# Assuming you cloned this repo into your ROS2 workspace src/
cd ~/ros2_ws
colcon build --packages-select drone_interfaces drone_controller
source install/setup.bash
```

---

## 2. Operational Procedure

You will need **6 separate terminal windows/tabs**.

**Terminal 1: PX4 SITL (Simulation)**
```bash
cd ~/PX4-Autopilot
make px4_sitl gz_x500
```

**Terminal 2: QGroundControl**
```bash
cd ~/Downloads
./QGroundControl-x86_64.AppImage
```

**Terminal 3: MAVROS Bridge**
```bash
# Connects ROS2 to the PX4 SITL simulation
ros2 launch mavros px4.launch fcu_url:="udp://:14540@127.0.0.1:14557"
```

**Terminal 4: ROS Bridge Server**
```bash
# Allows MCP (Gemini) to talk to ROS
ros2 launch rosbridge_server rosbridge_websocket_launch.xml 
```

**Terminal 5: Drone Controller Node**
```bash
# Runs the custom bridge node that handles safety & high-level actions
ros2 run drone_controller bridge
```

**Terminal 6: Gemini / MCP Client**
1.  Run `gemini` (or your MCP client).
2.  **Prompt:** "I am controlling a drone_px4. Connect to localhost and load the drone_px4 robot configuration."
3.  **Usage:**
    -   "Takeoff to 10 meters"
    -   "Go to 30, 30, 30"
    -   "Fly square pattern (Side length 10m)"
    -   "Orbit at 10m radius"
    -   "Return to launch" (Lands at home)

---

## 3. Available Actions (Technical)

The `drone_controller` node exposes these high-level actions:

### **Features**
-   **Smoothed 50Hz Control Loop**: Updates at 50Hz for responsive control.
-   **Setpoint Interpolation**: "Carrot-following" logic eliminates jerkiness by moving a virtual setpoint at constant speed.
-   **Fly Through Mode**: Smoothly transitions between waypoints without stopping.

### **Actions**

1.  **Takeoff** (`drone_interfaces/action/DroneTakeoff`)
    -   Server: `/drone_control/takeoff`
    -   Goal: `float32 target_altitude`

2.  **Trajectory** (`drone_interfaces/action/DroneTrajectory`)
    -   Server: `/drone_control/trajectory`
    -   Goal: 
        -   `geometry_msgs/Point[] points`
        -   `float32 speed` (m/s, default 1.0)
        -   `float32 tolerance` (Arrival radius)
        -   `bool fly_through` (True = Continuous motion)
        -   `int32 repeat`

