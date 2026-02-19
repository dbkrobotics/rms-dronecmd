from launch import LaunchDescription
from launch_ros.actions import Node
from launch.substitutions import EnvironmentVariable


def generate_launch_description():
    return LaunchDescription([
        Node(
            package='mavros',
            executable='mavros_node',
            output='screen',
            parameters=[
                {'fcu_url': '/dev/ttyUSB0:57600'},
                {'system_id': 1},
                {'component_id': 1},
            ],
        ),
        Node(
            package='drone_controller',
            executable='bridge',
            output='screen',
            parameters=[{'use_sim_time': False}],
        ),
        Node(
            package='drone_controller',
            executable='object_locator',
            output='screen',
            parameters=[
                {'camera_device': '/dev/video0'},
                {'detector_model_id': EnvironmentVariable('DRONE_DETECTOR_MODEL_ID', default_value='yolov8s-worldv2.pt')},
                {'depth_model_id': EnvironmentVariable('DRONE_DEPTH_MODEL_ID', default_value='depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf')},
                {'use_sim_time': False},
            ],
        ),
    ])
