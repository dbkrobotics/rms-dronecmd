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
                {'component_id': 240},
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
                {'frame_jpeg_url': EnvironmentVariable('VISION_CAMERA_FRAME_JPEG_URL', default_value='http://127.0.0.1:8787/api/camera/latest.jpg')},
                {'gemini_model_id': EnvironmentVariable('VISION_GEMINI_MODEL_ID', default_value='gemini-3.1-pro-preview')},
                {'gemini_api_version': EnvironmentVariable('VISION_GEMINI_API_VERSION', default_value='v1')},
                {'depth_model_id': EnvironmentVariable('VISION_DEPTH_MODEL_ID', default_value='depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf')},
                {'use_sim_time': False},
            ],
        ),
    ])
