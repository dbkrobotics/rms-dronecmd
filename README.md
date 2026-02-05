
1. ROS2 PX4 User guide: https://docs.px4.io/main/en/ros2/user_guide

Installation of PX4 stacks
Open Terminal
- cd
- git clone https://github.com/PX4/PX4-Autopilot.git --recursive
- bash ./PX4-Autopilot/Tools/setup/ubuntu.sh
- cd PX4-Autopilot/
- make px4_sitl

Run gazebo simulation to verify installation
- cd PX4-Autopilot/
- make px4_sitl gz_x500


2. ROS2 Jazzy MAVROS guide: https://docs.ros.org/en/jazzy/p/mavros/

Open Terminal
- cd
- sudo apt install ros-jazzy-mavros
- wget https://raw.githubusercontent.com/mavlink/mavros/ros2/mavros/scripts/install_geographiclib_datasets.sh
- sudo bash ./install_geographiclib_datasets.sh



3. QGC Installation: https://docs.qgroundcontrol.com/master/en/qgc-user-guide/getting_started/download_and_install.html

Open Terminal
- sudo usermod -a -G dialout $USER
- sudo apt-get remove modemmanager -y
- sudo apt install gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-gl -y
- sudo apt install libfuse2 -y
- sudo apt install libxcb-xinerama0 libxkbcommon-x11-0 libxcb-cursor-dev -y
- Download QGC: https://d176tv9ibo4jno.cloudfront.net/latest/QGroundControl-x86_64.AppImage
- cd {$download folder}
- chmod +x ./QGroundControl-x86_64.AppImage
- ./QGroundControl-x86_64.AppImage

4. DBK drone code for extra node (/drone/cmd)
- clone rms-dronecmd in your ROS2 workspace's src folder
- cd {$ROS2 workspace}
- colcon build
- source install/setup.bash

5.Operational Procedure 

Terminal 1
- cd PX4-AutoPilot
- make px4_sitl gz_x500

Terminal 2
- cd {$download folder}
- ./QGroundControl-x86_64.AppImage

Terminal 3
- ros2 launch mavros px4.launch fcu_url:="udp://:14540@127.0.0.1:14557"

Terminal 4
-ros2 launch rosbridge_server rosbridge_websocket_launch.xml 

Terminal 5
- ros2 run drone_controller bridge

Terminal 6
- gemini
- Prompt in the beginning
- Connect the drone on localhost and list all ros topics and services   
- Use drone/cmd service for hover,takeoff, navigation, and pattern flight and use mavros for others      
