from __future__ import annotations

import math
import re
import threading
import time

import cv2
import numpy as np
import rclpy
import tf2_geometry_msgs  # noqa: F401 - registers geometry messages for tf2
from geometry_msgs.msg import Point, PointStamped, PoseStamped
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo
from tf2_ros import Buffer, ConnectivityException, ExtrapolationException, LookupException, TransformListener

from drone_interfaces.srv import GetObject3D

try:
    import torch
    import torch.nn.functional as F
    from PIL import Image
    from transformers import AutoImageProcessor, AutoModelForDepthEstimation
except Exception:
    torch = None
    F = None
    Image = None
    AutoImageProcessor = None
    AutoModelForDepthEstimation = None

try:
    from ultralytics import YOLOWorld
except Exception:
    YOLOWorld = None


_STOPWORDS = {
    "a",
    "an",
    "the",
    "to",
    "of",
    "on",
    "in",
    "at",
    "with",
    "and",
    "or",
    "please",
    "go",
    "move",
    "fly",
    "toward",
    "towards",
}


class ObjectLocatorNode(Node):
    """Perception-only object localization node.

    Inputs: target query text + camera frame.
    Outputs: detected object location in world frame.
    """

    def __init__(self) -> None:
        super().__init__("object_locator")

        self.declare_parameter("service_name", "/drone_vision/get_object_3d")
        self.declare_parameter("camera_device", "/dev/video0")
        self.declare_parameter("camera_info_topic", "/camera/camera_info")
        self.declare_parameter("local_pose_topic", "/mavros/local_position/pose")
        self.declare_parameter("default_camera_frame", "camera_link")
        self.declare_parameter("default_world_frame", "map")

        self.declare_parameter("detector_model_id", "yolov8s-worldv2.pt")
        self.declare_parameter("detector_confidence", 0.25)
        self.declare_parameter("detector_iou", 0.45)
        self.declare_parameter("detector_imgsz", 640)
        self.declare_parameter("detector_max_classes", 6)

        self.declare_parameter("depth_model_id", "depth-anything/Depth-Anything-V2-Small-hf")
        self.declare_parameter("depth_scale", 1.0)
        self.declare_parameter("min_depth_m", 0.2)
        self.declare_parameter("max_depth_m", 40.0)
        self.declare_parameter("depth_patch_radius", 3)

        self.declare_parameter("camera_hfov_deg", 78.0)
        self.declare_parameter("capture_width", 1280)
        self.declare_parameter("capture_height", 720)
        self.declare_parameter("capture_fps", 20.0)
        self.declare_parameter("capture_warmup_frames", 12)

        self.service_name = str(self.get_parameter("service_name").value)
        self.camera_device = str(self.get_parameter("camera_device").value)
        self.default_camera_frame = str(self.get_parameter("default_camera_frame").value)
        self.default_world_frame = str(self.get_parameter("default_world_frame").value)

        self.detector_model_id = str(self.get_parameter("detector_model_id").value)
        self.detector_confidence = float(self.get_parameter("detector_confidence").value)
        self.detector_iou = float(self.get_parameter("detector_iou").value)
        self.detector_imgsz = int(self.get_parameter("detector_imgsz").value)
        self.detector_max_classes = int(self.get_parameter("detector_max_classes").value)

        self.depth_model_id = str(self.get_parameter("depth_model_id").value)
        self.depth_scale = float(self.get_parameter("depth_scale").value)
        self.min_depth_m = float(self.get_parameter("min_depth_m").value)
        self.max_depth_m = float(self.get_parameter("max_depth_m").value)
        self.depth_patch_radius = int(self.get_parameter("depth_patch_radius").value)

        self.camera_hfov_deg = float(self.get_parameter("camera_hfov_deg").value)
        self.capture_width = int(self.get_parameter("capture_width").value)
        self.capture_height = int(self.get_parameter("capture_height").value)
        self.capture_fps = float(self.get_parameter("capture_fps").value)
        self.capture_warmup_frames = int(self.get_parameter("capture_warmup_frames").value)

        camera_info_topic = str(self.get_parameter("camera_info_topic").value)
        local_pose_topic = str(self.get_parameter("local_pose_topic").value)

        self.latest_camera_info: CameraInfo | None = None
        self.latest_local_pose: PoseStamped | None = None

        self.create_subscription(CameraInfo, camera_info_topic, self._camera_info_cb, qos_profile_sensor_data)
        self.create_subscription(PoseStamped, local_pose_topic, self._local_pose_cb, qos_profile_sensor_data)

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        self._capture_lock = threading.Lock()
        self._capture: cv2.VideoCapture | None = None

        self.detector = self._load_detector()
        self.depth_processor, self.depth_model = self._load_depth_model()

        self.create_service(GetObject3D, self.service_name, self._handle_get_object_3d)

        self.get_logger().info(
            "Object locator ready: "
            f"service={self.service_name}, camera={self.camera_device}, "
            f"detector={'on' if self.detector else 'off'}, depth={'on' if self.depth_model else 'off'}"
        )

    def _camera_info_cb(self, msg: CameraInfo) -> None:
        self.latest_camera_info = msg

    def _local_pose_cb(self, msg: PoseStamped) -> None:
        self.latest_local_pose = msg

    def _load_detector(self):
        if not self.detector_model_id:
            self.get_logger().error("detector_model_id is empty")
            return None

        if YOLOWorld is None:
            self.get_logger().error("ultralytics YOLOWorld is unavailable. Install ultralytics.")
            return None

        try:
            detector = YOLOWorld(self.detector_model_id)
            self.get_logger().info(f"Loaded detector: {self.detector_model_id}")
            return detector
        except Exception as exc:
            self.get_logger().error(f"Failed to load detector ({self.detector_model_id}): {exc}")
            return None

    def _load_depth_model(self):
        if not self.depth_model_id:
            self.get_logger().warn("depth_model_id is empty")
            return None, None

        if torch is None or AutoImageProcessor is None or AutoModelForDepthEstimation is None or Image is None:
            self.get_logger().error("Depth dependencies missing. Install torch, transformers, pillow.")
            return None, None

        try:
            processor = AutoImageProcessor.from_pretrained(self.depth_model_id)
            model = AutoModelForDepthEstimation.from_pretrained(self.depth_model_id)
            model.to("cpu")
            model.eval()
            self.get_logger().info(f"Loaded depth model: {self.depth_model_id}")
            return processor, model
        except Exception as exc:
            self.get_logger().error(f"Failed to load depth model ({self.depth_model_id}): {exc}")
            return None, None

    def _camera_sources(self) -> list[int | str]:
        device = self.camera_device.strip()
        if device.startswith("/dev/video"):
            suffix = device.rsplit("video", 1)[-1]
            if suffix.isdigit():
                return [device, int(suffix)]
            return [device]
        if device.isdigit():
            return [int(device)]
        return [device]

    def _camera_backends(self) -> list[int | None]:
        backends: list[int | None] = []
        if hasattr(cv2, "CAP_V4L2"):
            backends.append(int(cv2.CAP_V4L2))
        if hasattr(cv2, "CAP_ANY"):
            backends.append(int(cv2.CAP_ANY))
        if not backends:
            backends.append(None)
        return backends

    def _open_capture(self) -> bool:
        for source in self._camera_sources():
            for backend in self._camera_backends():
                try:
                    cap = cv2.VideoCapture(source) if backend is None else cv2.VideoCapture(source, backend)
                except Exception:
                    continue

                if not cap.isOpened():
                    cap.release()
                    continue

                cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.capture_width))
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.capture_height))
                cap.set(cv2.CAP_PROP_FPS, float(self.capture_fps))
                try:
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                except Exception:
                    pass

                for _ in range(max(1, self.capture_warmup_frames)):
                    ok, frame = cap.read()
                    if ok and frame is not None and frame.size > 0:
                        self._capture = cap
                        return True
                    time.sleep(0.02)

                cap.release()

        return False

    def _reset_capture(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def _read_frame(self) -> np.ndarray | None:
        with self._capture_lock:
            if self._capture is None and not self._open_capture():
                return None

            assert self._capture is not None
            ok, frame = self._capture.read()
            if ok and frame is not None and frame.size > 0:
                return frame

            self._reset_capture()
            if not self._open_capture():
                return None

            assert self._capture is not None
            ok, frame = self._capture.read()
            if ok and frame is not None and frame.size > 0:
                return frame

        return None

    def _query_classes(self, query: str) -> list[str]:
        tokens = re.findall(r"[a-zA-Z0-9_]+", query.lower())
        classes: list[str] = [query.strip().lower()]

        for token in tokens:
            if token in _STOPWORDS or len(token) < 3:
                continue
            classes.append(token)
            if len(classes) >= max(1, self.detector_max_classes):
                break

        deduped: list[str] = []
        seen: set[str] = set()
        for text in classes:
            normalized = text.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)

        return deduped if deduped else [query.strip().lower()]

    def _run_detector(
        self,
        frame_bgr: np.ndarray,
        target_query: str,
        min_confidence: float,
    ) -> tuple[tuple[int, int, int, int], float, str] | None:
        if self.detector is None:
            return None

        classes = self._query_classes(target_query)
        threshold = max(self.detector_confidence, float(min_confidence), 0.0)

        try:
            self.detector.set_classes(classes)
            results = self.detector.predict(
                source=frame_bgr,
                conf=threshold,
                iou=self.detector_iou,
                imgsz=self.detector_imgsz,
                device="cpu",
                verbose=False,
            )
        except Exception as exc:
            self.get_logger().warn(f"Detection failed: {exc}")
            return None

        if not results:
            return None

        result = results[0]
        boxes = getattr(result, "boxes", None)
        names = getattr(result, "names", {}) or {}
        if boxes is None or len(boxes) == 0:
            return None

        h, w = frame_bgr.shape[:2]
        best_conf = -1.0
        best_bbox: tuple[int, int, int, int] | None = None
        best_label = ""

        for box in boxes:
            try:
                conf = float(box.conf[0].item())
                if conf < threshold:
                    continue

                cls_idx = int(box.cls[0].item())
                label = str(names.get(cls_idx, classes[min(cls_idx, len(classes) - 1)]))

                xyxy = box.xyxy[0].detach().cpu().numpy().astype(np.float32)
                x1, y1, x2, y2 = [float(v) for v in xyxy]
                bx1 = int(np.clip(round(x1), 0, w - 1))
                by1 = int(np.clip(round(y1), 0, h - 1))
                bx2 = int(np.clip(round(x2), bx1 + 1, w))
                by2 = int(np.clip(round(y2), by1 + 1, h))

                if conf > best_conf:
                    best_conf = conf
                    best_bbox = (bx1, by1, bx2 - bx1, by2 - by1)
                    best_label = label
            except Exception:
                continue

        if best_bbox is None:
            return None

        return best_bbox, float(best_conf), best_label

    def _run_depth(self, frame_bgr: np.ndarray) -> np.ndarray | None:
        if self.depth_processor is None or self.depth_model is None:
            return None

        assert torch is not None and F is not None and Image is not None

        try:
            image = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
            inputs = self.depth_processor(images=image, return_tensors="pt")

            with torch.no_grad():
                outputs = self.depth_model(**inputs)
                predicted = outputs.predicted_depth

            upsampled = F.interpolate(
                predicted.unsqueeze(1),
                size=(image.height, image.width),
                mode="bicubic",
                align_corners=False,
            ).squeeze(1)

            depth_map = upsampled.squeeze(0).detach().cpu().numpy().astype(np.float32)
            depth_map *= max(self.depth_scale, 1e-6)
            depth_map = np.clip(depth_map, self.min_depth_m, self.max_depth_m)
            return depth_map
        except Exception as exc:
            self.get_logger().warn(f"Depth inference failed: {exc}")
            return None

    def _sample_depth(self, depth_map: np.ndarray, u: int, v: int) -> tuple[float, float]:
        h, w = depth_map.shape[:2]
        radius = max(1, self.depth_patch_radius)
        x1 = max(0, u - radius)
        y1 = max(0, v - radius)
        x2 = min(w, u + radius + 1)
        y2 = min(h, v + radius + 1)

        patch = depth_map[y1:y2, x1:x2]
        if patch.size == 0:
            return float("nan"), float("nan")

        depth_m = float(np.median(patch))
        depth_uncertainty = float(np.std(patch))
        if not np.isfinite(depth_m):
            return float("nan"), float("nan")
        return depth_m, depth_uncertainty

    def _pixel_to_camera(self, u: int, v: int, depth_m: float, frame_w: int, frame_h: int) -> tuple[float, float, float]:
        info = self.latest_camera_info
        if info and len(info.k) >= 9 and info.k[0] > 0.0 and info.k[4] > 0.0:
            fx = float(info.k[0])
            fy = float(info.k[4])
            cx = float(info.k[2])
            cy = float(info.k[5])
        else:
            hfov = math.radians(max(10.0, min(170.0, self.camera_hfov_deg)))
            fx = frame_w / (2.0 * math.tan(hfov * 0.5))
            fy = fx
            cx = frame_w * 0.5
            cy = frame_h * 0.5

        x_c = ((u - cx) / fx) * depth_m
        y_c = ((v - cy) / fy) * depth_m
        return float(x_c), float(y_c), float(depth_m)

    def _resolve_camera_frame(self, requested_frame: str) -> str:
        requested = requested_frame.strip()
        if requested:
            return requested
        if self.latest_camera_info and self.latest_camera_info.header.frame_id:
            return self.latest_camera_info.header.frame_id
        return self.default_camera_frame

    def _transform_to_world(self, camera_point: tuple[float, float, float], camera_frame: str, world_frame: str) -> Point | None:
        stamped = PointStamped()
        stamped.header.frame_id = camera_frame
        stamped.header.stamp = self.get_clock().now().to_msg()
        stamped.point.x = camera_point[0]
        stamped.point.y = camera_point[1]
        stamped.point.z = camera_point[2]

        try:
            transformed = self.tf_buffer.transform(stamped, world_frame, timeout=Duration(seconds=0.15))
            point = Point()
            point.x = float(transformed.point.x)
            point.y = float(transformed.point.y)
            point.z = float(transformed.point.z)
            return point
        except (LookupException, ConnectivityException, ExtrapolationException) as exc:
            self.get_logger().warn(f"TF transform failed ({camera_frame} -> {world_frame}): {exc}")
            return None

    def _distance_from_drone(self, world_point: Point) -> float:
        if self.latest_local_pose is None:
            return float("nan")

        p = self.latest_local_pose.pose.position
        dx = float(world_point.x - p.x)
        dy = float(world_point.y - p.y)
        dz = float(world_point.z - p.z)
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def _fill_depth_unknown(self, response: GetObject3D.Response, message: str) -> GetObject3D.Response:
        response.success = True
        response.message = message
        response.depth_m = float("nan")
        response.depth_uncertainty_m = float("nan")
        response.distance_m = float("nan")
        return response

    def _handle_get_object_3d(self, request: GetObject3D.Request, response: GetObject3D.Response) -> GetObject3D.Response:
        target_query = str(request.target_query or "").strip()
        world_frame = str(request.world_frame or self.default_world_frame).strip() or self.default_world_frame
        response.world_frame = world_frame

        if not target_query:
            response.success = False
            response.message = "target_query is empty"
            return response

        frame = self._read_frame()
        if frame is None:
            response.success = False
            response.message = f"cannot read camera frame from {self.camera_device}"
            return response

        frame_h, frame_w = frame.shape[:2]
        detection = self._run_detector(frame, target_query, float(request.min_confidence))
        if detection is None:
            response.success = True
            response.found = False
            response.message = f"target '{target_query}' not found"
            return response

        bbox, confidence, matched_label = detection
        x, y, w, h = bbox
        u = x + (w // 2)
        v = y + (h // 2)

        response.found = True
        response.confidence = float(confidence)
        response.pixel_u = int(u)
        response.pixel_v = int(v)

        depth_map = self._run_depth(frame)
        if depth_map is None:
            return self._fill_depth_unknown(response, f"target found ({matched_label}) but depth model is unavailable")

        depth_m, depth_uncertainty_m = self._sample_depth(depth_map, u, v)
        response.depth_m = float(depth_m)
        response.depth_uncertainty_m = float(depth_uncertainty_m)
        if not np.isfinite(depth_m):
            return self._fill_depth_unknown(response, "target found but depth sample is invalid")

        camera_frame = self._resolve_camera_frame(str(request.camera_frame or ""))
        x_c, y_c, z_c = self._pixel_to_camera(u, v, depth_m, frame_w, frame_h)
        world_point = self._transform_to_world((x_c, y_c, z_c), camera_frame, world_frame)
        if world_point is None:
            response.success = False
            response.message = f"target found but transform to '{world_frame}' failed"
            return response

        response.world_position = world_point
        response.distance_m = float(self._distance_from_drone(world_point))
        response.success = True
        response.message = f"target localized ({matched_label})"
        return response


def main() -> None:
    rclpy.init()
    node = ObjectLocatorNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        with node._capture_lock:
            node._reset_capture()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
