import rclpy
from rclpy.node import Node
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import qos_profile_sensor_data, QoSProfile, ReliabilityPolicy, HistoryPolicy

from geometry_msgs.msg import PoseStamped
from mavros_msgs.msg import State
from mavros_msgs.srv import CommandBool, SetMode

from drone_interfaces.action import DroneTakeoff, DroneNavigate, DroneOrbit

import math
import time
import threading

class DroneMCPBridge(Node):
    def __init__(self):
        super().__init__('drone_mcp_bridge')
        self.callback_group = ReentrantCallbackGroup()

        setpoint_qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST, depth=10)
        self.local_pos_pub = self.create_publisher(PoseStamped, '/mavros/setpoint_position/local', setpoint_qos)
        self.state_sub = self.create_subscription(State, '/mavros/state', self.state_cb, 10)
        self.local_pos_sub = self.create_subscription(PoseStamped, '/mavros/local_position/pose', self.local_cb, qos_profile_sensor_data)

        self.arm_cli = self.create_client(CommandBool, '/mavros/cmd/arming', callback_group=self.callback_group)
        self.mode_cli = self.create_client(SetMode, '/mavros/set_mode', callback_group=self.callback_group)

        self._action_takeoff = ActionServer(
            self, DroneTakeoff, 'drone_control/takeoff', 
            self.execute_takeoff, callback_group=self.callback_group)
        
        self._action_navigate = ActionServer(
            self, DroneNavigate, 'drone_control/navigate', 
            self.execute_navigate, callback_group=self.callback_group)

        self._action_orbit = ActionServer(
            self, DroneOrbit, 'drone_control/orbit', 
            self.execute_orbit, callback_group=self.callback_group)

        self.current_state = State()
        self.current_pose = PoseStamped()
        
        self.target_pose = PoseStamped()
        self.active_pattern = None 
        self.pattern_params = {}

        self.is_primed = False

        self.timer = self.create_timer(0.05, self.timer_callback, callback_group=self.callback_group)
        self.get_logger().info('--- Stabilized Drone Bridge (Actions) Online ---')

    def state_cb(self, msg): 
        self.current_state = msg

    def local_cb(self, msg):
        self.current_pose = msg
        if not self.is_primed:
            self.target_pose.pose.position.x = msg.pose.position.x
            self.target_pose.pose.position.y = msg.pose.position.y
            self.target_pose.pose.position.z = 0.0
            self.is_primed = True
            self.get_logger().info(f'Ground coordinates locked: {msg.pose.position.x:.2f}, {msg.pose.position.y:.2f}')

    def timer_callback(self):
        if not self.is_primed:
            return

        if self.active_pattern == 'orbit':
            params = self.pattern_params
            params['angle'] += params['speed']
            
            self.target_pose.pose.position.x = params['center_x'] + params['radius'] * math.cos(params['angle'])
            self.target_pose.pose.position.y = params['center_y'] + params['radius'] * math.sin(params['angle'])

        self.target_pose.header.stamp = self.get_clock().now().to_msg()
        self.target_pose.header.frame_id = "map"
        self.local_pos_pub.publish(self.target_pose)

    async def prepare_for_flight(self):
        if not self.current_state.connected:
            self.get_logger().error("FCU not connected!")
            return False
            
        if not self.is_primed:
            self.get_logger().warn("Waiting for local position lock...")
            return False

        if self.current_state.mode != "OFFBOARD":
            req = SetMode.Request(custom_mode="OFFBOARD")
            resp = await self.mode_cli.call_async(req)
            if not resp.mode_sent:
                self.get_logger().error("Failed to set OFFBOARD mode")
                return False
            time.sleep(0.5) 

        if not self.current_state.armed:
            req = CommandBool.Request(value=True)
            resp = await self.arm_cli.call_async(req)
            if not resp.success:
                self.get_logger().error("Failed to ARM")
                return False
                
        return True

    async def execute_takeoff(self, goal_handle):
        self.get_logger().info(f'Executing Takeoff to {goal_handle.request.target_altitude}m')
        
        if not await self.prepare_for_flight():
            goal_handle.abort()
            return DroneTakeoff.Result(success=False, message="Failed to arm/offboard")

        self.active_pattern = None
        self.target_pose.pose.position.x = self.current_pose.pose.position.x
        self.target_pose.pose.position.y = self.current_pose.pose.position.y
        self.target_pose.pose.position.z = goal_handle.request.target_altitude

        feedback_msg = DroneTakeoff.Feedback()
        
        while rclpy.ok():
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                return DroneTakeoff.Result(success=False, message="Canceled")

            current_z = self.current_pose.pose.position.z
            error = abs(goal_handle.request.target_altitude - current_z)
            
            feedback_msg.current_altitude = current_z
            goal_handle.publish_feedback(feedback_msg)

            if error < 0.2:
                break
                
            time.sleep(0.5)

        goal_handle.succeed()
        return DroneTakeoff.Result(success=True, message="Takeoff complete")

    async def execute_navigate(self, goal_handle):
        req = goal_handle.request
        self.get_logger().info(f'Navigating to ({req.x}, {req.y}, {req.z}) Relative={req.relative}')

        if not await self.prepare_for_flight():
            goal_handle.abort()
            return DroneNavigate.Result(success=False, message="Failed to arm/offboard")

        self.active_pattern = None
        
        target_x = req.x
        target_y = req.y
        target_z = req.z

        if req.relative:
            target_x += self.current_pose.pose.position.x
            target_y += self.current_pose.pose.position.y
            target_z += self.current_pose.pose.position.z
            
        self.target_pose.pose.position.x = target_x
        self.target_pose.pose.position.y = target_y
        self.target_pose.pose.position.z = target_z

        feedback_msg = DroneNavigate.Feedback()

        while rclpy.ok():
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                return DroneNavigate.Result(success=False, message="Canceled")

            dx = target_x - self.current_pose.pose.position.x
            dy = target_y - self.current_pose.pose.position.y
            dz = target_z - self.current_pose.pose.position.z
            dist = math.sqrt(dx*dx + dy*dy + dz*dz)
            
            feedback_msg.distance_remaining = dist
            goal_handle.publish_feedback(feedback_msg)

            if dist < 0.3:
                break
            
            time.sleep(0.5)

        goal_handle.succeed()
        return DroneNavigate.Result(success=True, message="Navigation complete")

    async def execute_orbit(self, goal_handle):
        req = goal_handle.request
        self.get_logger().info(f'Starting Orbit. Radius={req.radius}, Alt={req.altitude}')

        if not await self.prepare_for_flight():
            goal_handle.abort()
            return DroneOrbit.Result(success=False, message="Failed to arm/offboard")

        current_x = self.current_pose.pose.position.x
        current_y = self.current_pose.pose.position.y
        
        self.pattern_params = {
            'radius': req.radius,
            'speed': 0.05 if req.speed == 0.0 else req.speed,
            'center_x': current_x - req.radius,
            'center_y': current_y,
            'angle': 0.0
        }
        
        self.target_pose.pose.position.z = req.altitude
        self.active_pattern = 'orbit'

        feedback_msg = DroneOrbit.Feedback()
        
        while rclpy.ok():
            if goal_handle.is_cancel_requested:
                self.active_pattern = None
                goal_handle.canceled()
                return DroneOrbit.Result(success=True, message="Orbit stopped")
            
            feedback_msg.current_angle = self.pattern_params['angle']
            goal_handle.publish_feedback(feedback_msg)
            time.sleep(1.0)

        goal_handle.abort()
        return DroneOrbit.Result(success=False, message="Node shutdown")

def main():
    rclpy.init()
    node = DroneMCPBridge()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

