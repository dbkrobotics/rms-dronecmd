import rclpy
from rclpy.node import Node
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.qos import qos_profile_sensor_data, QoSProfile, ReliabilityPolicy, HistoryPolicy
from geometry_msgs.msg import PoseStamped
from mavros_msgs.msg import State
from mavros_msgs.srv import CommandBool, SetMode
from drone_interfaces.srv import DroneCommand 
import math
import time

class DroneMCPBridge(Node):
    def __init__(self):
        super().__init__('drone_mcp_bridge')
        self.callback_group = ReentrantCallbackGroup()
        
        # --- Publishers & Subscriptions ---
        setpoint_qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST, depth=10)
        self.local_pos_pub = self.create_publisher(PoseStamped, '/mavros/setpoint_position/local', setpoint_qos)
        self.state_sub = self.create_subscription(State, '/mavros/state', self.state_cb, 10)
        self.local_pos_sub = self.create_subscription(PoseStamped, '/mavros/local_position/pose', self.local_cb, qos_profile_sensor_data)

        # --- Clients & Services ---
        self.arm_cli = self.create_client(CommandBool, '/mavros/cmd/arming', callback_group=self.callback_group)
        self.mode_cli = self.create_client(SetMode, '/mavros/set_mode', callback_group=self.callback_group)
        self.srv_command = self.create_service(DroneCommand, 'drone/cmd', self.command_callback, callback_group=self.callback_group)

        # --- Flight State ---
        self.current_state = State()
        self.current_pose = PoseStamped()
        self.target_pose = PoseStamped()
        
        # COORDINATE LOCKING
        self.is_primed = False  # Track if we have a valid ground position
        self.active_pattern = None
        self.pattern_size = 0.0
        self.angle = 0.0
        
        # 20Hz Heartbeat
        self.timer = self.create_timer(0.05, self.timer_callback)
        self.get_logger().info('--- Stabilized Takeoff Bridge Online ---')

    def state_cb(self, msg): 
        self.current_state = msg

    def local_cb(self, msg):
        self.current_pose = msg
        # CRITICAL: Lock the first coordinate we receive as the "Safe Home"
        if not self.is_primed:
            self.target_pose.pose.position.x = msg.pose.position.x
            self.target_pose.pose.position.y = msg.pose.position.y
            self.target_pose.pose.position.z = 0.0
            self.is_primed = True
            self.get_logger().info('Ground coordinates locked. Takeoff will be vertical.')

    def timer_callback(self):
        if self.active_pattern == 'circle':
            self.angle += 0.02
            self.target_pose.pose.position.x = self.pattern_size * math.cos(self.angle)
            self.target_pose.pose.position.y = self.pattern_size * math.sin(self.angle)

        self.target_pose.header.stamp = self.get_clock().now().to_msg()
        self.target_pose.header.frame_id = "map"
        self.local_pos_pub.publish(self.target_pose)

    async def prepare_for_flight(self):
        """Sequential safety check"""
        if not self.current_state.connected: return False
        if not self.is_primed:
            self.get_logger().warn("Waiting for local position before flight...")
            return False

        if self.current_state.mode != "OFFBOARD":
            req = SetMode.Request(custom_mode="OFFBOARD")
            await self.mode_cli.call_async(req)
            time.sleep(0.5) 

        if not self.current_state.armed:
            req = CommandBool.Request(value=True)
            await self.arm_cli.call_async(req)
        return True

    async def command_callback(self, request, response):
        raw = request.command.strip().lower()
        parts = raw.split()
        self.active_pattern = None

        try:
            # 1. Takeoff / Hover (LOCKED TO CURRENT X,Y)
            if any(word in raw for word in {'hover', 'takeoff', 'altitude'}):
                val = next((float(s) for s in parts if s.replace('.','',1).isdigit()), 2.5)
                
                # Use current position to ensure strictly vertical climb
                self.target_pose.pose.position.x = self.current_pose.pose.position.x
                self.target_pose.pose.position.y = self.current_pose.pose.position.y
                self.target_pose.pose.position.z = val
                
                response.success = await self.prepare_for_flight()
                response.message = f"Vertical takeoff to {val}m."

            # 2. Navigation / Home
            elif any(word in raw for word in {'nav', 'goto', 'home'}):
                if 'home' in raw:
                    self.target_pose.pose.position.x = 0.0
                    self.target_pose.pose.position.y = 0.0
                else:
                    coords = [float(s) for s in parts if s.replace('.','',1).lstrip('-').isdigit()]
                    if len(coords) >= 2:
                        self.target_pose.pose.position.x, self.target_pose.pose.position.y = coords[0], coords[1]
                response.success = await self.prepare_for_flight()
                response.message = "Navigating."

            # 3. Pattern
            elif 'circle' in raw:
                self.pattern_size = next((float(s) for s in parts if s.replace('.','',1).isdigit()), 5.0)
                if await self.prepare_for_flight():
                    self.active_pattern = 'circle'
                    response.success = True

        except Exception as e:
            response.success = False
            response.message = f"Error: {str(e)}"
        
        return response

def main():
    rclpy.init()
    node = DroneMCPBridge()
    executor = rclpy.executors.MultiThreadedExecutor()
    executor.add_node(node)
    executor.spin()
