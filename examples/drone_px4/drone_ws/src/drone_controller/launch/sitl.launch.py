import os
from ament_index_python.packages import get_package_prefix
from launch import LaunchDescription
from launch_ros.actions import Node
from launch.substitutions import EnvironmentVariable


def generate_launch_description():
    venv_dir = os.environ.get('VIRTUAL_ENV')
    if venv_dir:
        python_exec = os.path.join(venv_dir, 'bin', 'python')
    else:
        python_exec = 'python3'

    try:
        drone_controller_prefix = get_package_prefix('drone_controller')
        object_locator_script = os.path.join(drone_controller_prefix, 'lib', 'drone_controller', 'object_locator')
    except Exception:
        object_locator_script = 'object_locator'

    return LaunchDescription([
        Node(
            package='mavros',
            executable='mavros_node',
            output='screen',
            parameters=[
                {'fcu_url': 'udp://:14540@127.0.0.1:14557'},
                {'system_id': 1},
                {'component_id': 240},
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
            executable=python_exec,
            arguments=[object_locator_script],
            output='screen',
            parameters=[
                {'frame_jpeg_url': EnvironmentVariable('VISION_CAMERA_FRAME_JPEG_URL', default_value='http://127.0.0.1:8787/api/camera/latest.jpg')},
                {'gemini_model_id': EnvironmentVariable('VISION_GEMINI_MODEL_ID', default_value='gemini-flash-latest')},
                {'gemini_api_version': EnvironmentVariable('VISION_GEMINI_API_VERSION', default_value='v1beta')},
                {'depth_model_id': EnvironmentVariable('VISION_DEPTH_MODEL_ID', default_value='depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf')},
                {'use_sim_time': True},
            ],
        ),
        Node(
            package='tf2_ros',
            executable='static_transform_publisher',
            name='camera_link_broadcaster',
            arguments=['0.1', '0', '0', '-1.5708', '0', '-1.5708', 'base_link', 'camera_link']
        ),
    ])
