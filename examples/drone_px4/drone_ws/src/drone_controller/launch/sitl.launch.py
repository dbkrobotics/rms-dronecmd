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
                {'fcu_url': 'udp://:14540@127.0.0.1:14557'},
                {'system_id': 1},
                {'component_id': 1},
                {'target_system_id': 1},
                {'target_component_id': 1},
            ],
        ),
        Node(
            package='drone_controller',
            executable='bridge',
            output='screen',
            parameters=[{'use_sim_time': True}],
        ),
        Node(
            package='drone_controller',
            executable='object_locator',
            output='screen',
            parameters=[
                {'frame_jpeg_url': EnvironmentVariable('VISION_CAMERA_FRAME_JPEG_URL', default_value='http://127.0.0.1:8787/api/camera/latest.jpg')},
                {'gemini_model_id': EnvironmentVariable('VISION_GEMINI_MODEL_ID', default_value='gemini-3-pro-preview')},
                {'gemini_api_version': EnvironmentVariable('VISION_GEMINI_API_VERSION', default_value='v1beta')},
                {'depth_model_id': EnvironmentVariable('VISION_DEPTH_MODEL_ID', default_value='depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf')},
                {'use_sim_time': True},
            ],
        ),
    ])
