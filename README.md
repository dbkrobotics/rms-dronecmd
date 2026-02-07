# RMS Drone Command (Action Server Refactor)

## Prerequisites Installation

### 1. PX4 Autopilot
Official Guide: [PX4 User Guide](https://docs.px4.io/main/en/ros2/user_guide)

```bash
cd ~
git clone https://github.com/PX4/PX4-Autopilot.git --recursive
bash ./PX4-Autopilot/Tools/setup/ubuntu.sh
cd PX4-Autopilot/
make px4_sitl
# Verify installation by running Gazebo simulation:
make px4_sitl gz_x500
```

### 2. MAVROS (ROS2 Jazzy)
Official Guide: [MAVROS Guide](https://docs.ros.org/en/jazzy/p/mavros/)

```bash
cd ~
sudo apt install ros-jazzy-mavros
wget https://raw.githubusercontent.com/mavlink/mavros/ros2/mavros/scripts/install_geographiclib_datasets.sh
sudo bash ./install_geographiclib_datasets.sh
```

### 3. QGroundControl
Official Guide: [QGC Installation](https://docs.qgroundcontrol.com/master/en/qgc-user-guide/getting_started/download_and_install.html)

```bash
# Install dependencies
sudo usermod -a -G dialout $USER
sudo apt-get remove modemmanager -y
sudo apt install gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-gl -y
sudo apt install libfuse2 -y
sudo apt install libxcb-xinerama0 libxkbcommon-x11-0 libxcb-cursor-dev -y

# Download and Run
cd ~/Downloads
wget https://d176tv9ibo4jno.cloudfront.net/latest/QGroundControl-x86_64.AppImage
chmod +x ./QGroundControl-x86_64.AppImage
./QGroundControl-x86_64.AppImage
```

## RMS Drone Command Installation

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

2. **Start QGroundControl** (Terminal 2):
    ```bash
    cd ~/{$download folder}
    ./QGroundControl-x86_64.AppImage
    ```

3.  **Start Drone Controller** (Terminal 3):
    ```bash
    ros2 launch drone_controller sitl.launch.py
    ```

## Control Interfaces (ROS2 Actions)

The controller now exposes **Actions** instead of Services for better feedback and control.

| Action Topic | Type | Description |
| :--- | :--- | :--- |
| `/drone_control/takeoff` | `DroneTakeoff` | Takeoff to a specific altitude. |
| `/drone_control/navigate` | `DroneNavigate` | Fly to (x, y, z) coordinates. |
| `/drone_control/orbit` | `DroneOrbit` | Orbit around a center point. |