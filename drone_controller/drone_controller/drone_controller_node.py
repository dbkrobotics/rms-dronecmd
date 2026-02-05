import rclpy
from rclpy.node import Node
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.qos import qos_profile_sensor_data, QoSProfile, ReliabilityPolicy, HistoryPolicy
from geometry_msgs.msg import PoseStamped
from mavros_msgs.msg import State
from mavros_msgs.srv import CommandBool, SetMode
from drone_interfaces.srv import DroneCommand 
import time
import math

class DroneMCPBridge(Node):
    def __init__(self):
        super().__init__('drone_mcp_bridge')
        
        # 1. Callback Group to allow nested service calls (Mode/Arm) without deadlocking
        self.callback_group = ReentrantCallbackGroup()
        
        # 2. QoS for Setpoints (Reliable/Transient Local)
        setpoint_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )

        # --- Publishers ---
        # Using global namespace '/' to ensure it hits MAVROS
        self.local_pos_pub = self.create_publisher(
            PoseStamped, 
            '/mavros/setpoint_position/local', 
            setpoint_qos)

        # --- Subscriptions ---
        self.state_sub = self.create_subscription(
            State, '/mavros/state', self.state_cb, 10)
        
        # Using Sensor Data QoS for High-Frequency feedback
        self.local_pos_sub = self.create_subscription(
            PoseStamped, 
            '/mavros/local_position/pose', 
            self.local_cb, 
            qos_profile_sensor_data)

        # --- Clients ---
        self.arm_cli = self.create_client(CommandBool, '/mavros/cmd/arming', callback_group=self.callback_group)
        self.mode_cli = self.create_client(SetMode, '/mavros/set_mode', callback_group=self.callback_group)

        # --- Services ---
        # Simplified Raw String service to bypass AI JSON errors
        self.srv_command = self.create_service(
            DroneCommand, 'drone/cmd', self.command_callback, callback_group=self.callback_group)

        # Internal State
        self.current_state = State()
        self.current_pose = PoseStamped()
        self.target_pose = PoseStamped()
        
        # Set initial target (Z=0) immediately to satisfy PX4
        self.target_pose.pose.position.x = 0.0
        self.target_pose.pose.position.y = 0.0
        self.target_pose.pose.position.z = 0.0

        # 3. CRITICAL: 20Hz Heartbeat (0.05s)
        # This MUST run to keep the topic valid for PX4.
        self.timer = self.create_timer(0.05, self.timer_callback)
        
        self.get_logger().info('--- Fail-Safe Bridge Online: 20Hz Heartbeat Active ---')

    def state_cb(self, msg):
        self.current_state = msg

    def local_cb(self, msg):
        self.current_pose = msg

    def timer_callback(self):
        # Continually stream the target pose to PX4
        self.target_pose.header.stamp = self.get_clock().now().to_msg()
        self.target_pose.header.frame_id = "map"
        self.local_pos_pub.publish(self.target_pose)

    async def prepare_for_flight(self):
        """Sequential check: Connect -> Start Streaming -> Set OFFBOARD -> Arm"""
        if not self.current_state.connected:
            self.get_logger().error("Flight Aborted: No connection to PX4!")
            return False

        # Request OFFBOARD mode
        if self.current_state.mode != "OFFBOARD":
            self.get_logger().info("Switching to OFFBOARD...")
            req = SetMode.Request(custom_mode="OFFBOARD")
            result = await self.mode_cli.call_async(req)
            if not result.mode_sent:
                self.get_logger().error("OFFBOARD Rejected. Check 20Hz stream.")
                return False
            time.sleep(1.0) # Buffer for state transition

        # Request Arming
        if not self.current_state.armed:
            self.get_logger().info("Arming Drone...")
            req = CommandBool.Request(value=True)
            result = await self.arm_cli.call_async(req)
            if not result.success:
                self.get_logger().error("Arming Failed!")
                return False
        
        return True

    async def command_callback(self, request, response):
        raw = request.command.strip().lower()
        self.get_logger().info(f"Intelligent Parsing: {raw}")
        parts = raw.split()
        
        # Define keyword filters for better natural language mapping
        hover_synonyms = {'hover', 'takeoff', 'altitude', 'up'}
        nav_synonyms = {'nav', 'goto', 'move', 'navigate', 'position'}
        pattern_synonyms = {'pattern', 'shape'}

        try:
            # 1. Takeoff / Hover Filter
            if any(word in raw for word in hover_synonyms):
                # Extract the first number found in the string as altitude
                val = next((float(s) for s in parts if s.replace('.','',1).isdigit()), 2.5)
                self.target_pose.pose.position.z = val
                response.success = await self.prepare_for_flight()
                response.message = f"Intent: Hover. Target Altitude: {val}m"

            # 2. Navigation / Move Filter
            elif any(word in raw for word in nav_synonyms):
                # Look for 3 numbers (x, y, z)
                coords = [float(s) for s in parts if s.replace('.','',1).lstrip('-').isdigit()]
                if len(coords) >= 2:
                    self.target_pose.pose.position.x = coords[0]
                    self.target_pose.pose.position.y = coords[1]
                    self.target_pose.pose.position.z = coords[2] if len(coords) > 2 else self.target_pose.pose.position.z
                    response.success = await self.prepare_for_flight()
                    response.message = f"Intent: Navigate. Target: {coords}"
                else:
                    response.success = False
                    response.message = "Navigation requires at least X and Y coordinates."

            # 3. Pattern Filter
            elif any(word in raw for word in pattern_synonyms):
                # Detect the shape from the string
                shape = next((s for s in ['square', 'circle', 'triangle'] if s in raw), 'square')
                size = next((float(s) for s in parts if s.replace('.','',1).isdigit()), 5.0)
                
                if await self.prepare_for_flight():
                    self.execute_pattern(shape, size)
                    response.success = True
                    response.message = f"Intent: Pattern. Executing {shape} of size {size}m"
                else:
                    response.success = False

            # 4. Landing / Safety Filter
            elif "land" in raw:
                # To land in PX4 Offboard, we typically just set Z to 0 or switch modes
                self.target_pose.pose.position.z = 0.0
                response.success = True
                response.message = "Intent: Land. Setting altitude to 0."

            else:
                response.success = False
                response.message = "Command not recognized. Try 'hover', 'nav', or 'pattern'."

        except Exception as e:
            response.success = False
            response.message = f"Processing error: {str(e)}"
        
        return response

    def execute_pattern(self, shape, size):
        """Calculates waypoints and updates target_pose sequentially."""
        waypoints = []
        # Ensure minimum altitude for safety
        z_alt = max(self.target_pose.pose.position.z, 20.0)

        if shape == "square":
            waypoints = [(0.0, 0.0), (size, 0.0), (size, size), (0.0, size), (0.0, 0.0)]
        elif shape == "triangle":
            height = size * (math.sqrt(3)/2)
            waypoints = [(0.0, 0.0), (size, 0.0), (size/2, height), (0.0, 0.0)]
        elif shape == "circle":
            steps = 100
            for i in range(steps + 1):
                angle = (2 * math.pi * i) / steps
                waypoints.append((size * math.cos(angle), size * math.sin(angle)))

        for pt in waypoints:
            self.get_logger().info(f"Pattern Point: {pt}")
            self.target_pose.pose.position.x = pt[0]
            self.target_pose.pose.position.y = pt[1]
            self.target_pose.pose.position.z = z_alt
            # Simple wait; replace with distance logic if more precision is needed
            time.sleep(0.5) 

def main():
    rclpy.init()
    node = DroneMCPBridge()
    # MultiThreadedExecutor is required to keep the timer/heartbeat running during pattern loops
    executor = rclpy.executors.MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
