from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

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
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None


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

    Scan: Gemini bbox detection
    3D: monocular depth + pinhole projection + TF
    """

    def __init__(self) -> None:
        super().__init__("object_locator")

        self.declare_parameter("service_name", "/drone_vision/get_object_3d")
        self.declare_parameter("frame_jpeg_url", "http://127.0.0.1:8787/api/camera/latest.jpg")
        self.declare_parameter("max_remote_frame_age_sec", 1.5)
        self.declare_parameter("camera_info_topic", "/camera/camera_info")
        self.declare_parameter("local_pose_topic", "/mavros/local_position/pose")
        self.declare_parameter("default_camera_frame", "camera_link")
        self.declare_parameter("default_world_frame", "map")

        self.declare_parameter("gemini_model_id", "gemini-3-flash-preview")
        self.declare_parameter("gemini_api_version", "v1beta")
        self.declare_parameter("gemini_temperature", 0.0)
        self.declare_parameter("gemini_max_objects", 10)
        self.declare_parameter("gemini_retries", 2)
        self.declare_parameter("scan_frames", 2)
        self.declare_parameter("scan_interval_sec", 0.08)
        self.declare_parameter("scan_iou_threshold", 0.35)

        self.declare_parameter("depth_model_id", "depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf")
        self.declare_parameter("depth_scale", 1.0)
        self.declare_parameter("min_depth_m", 0.2)
        self.declare_parameter("max_depth_m", 40.0)
        self.declare_parameter("depth_patch_radius", 3)

        self.declare_parameter("camera_hfov_deg", 78.0)

        self.service_name = str(self.get_parameter("service_name").value)
        self.frame_jpeg_url = str(self.get_parameter("frame_jpeg_url").value).strip()
        self.max_remote_frame_age_sec = float(self.get_parameter("max_remote_frame_age_sec").value)
        self.default_camera_frame = str(self.get_parameter("default_camera_frame").value)
        self.default_world_frame = str(self.get_parameter("default_world_frame").value)

        self.gemini_model_id = str(self.get_parameter("gemini_model_id").value)
        self.gemini_api_version = str(self.get_parameter("gemini_api_version").value)
        self.gemini_temperature = float(self.get_parameter("gemini_temperature").value)
        self.gemini_max_objects = int(self.get_parameter("gemini_max_objects").value)
        self.gemini_retries = int(self.get_parameter("gemini_retries").value)
        self.scan_frames = int(self.get_parameter("scan_frames").value)
        self.scan_interval_sec = float(self.get_parameter("scan_interval_sec").value)
        self.scan_iou_threshold = float(self.get_parameter("scan_iou_threshold").value)

        self.depth_model_id = str(self.get_parameter("depth_model_id").value)
        self.depth_scale = float(self.get_parameter("depth_scale").value)
        self.min_depth_m = float(self.get_parameter("min_depth_m").value)
        self.max_depth_m = float(self.get_parameter("max_depth_m").value)
        self.depth_patch_radius = int(self.get_parameter("depth_patch_radius").value)

        self.camera_hfov_deg = float(self.get_parameter("camera_hfov_deg").value)

        camera_info_topic = str(self.get_parameter("camera_info_topic").value)
        local_pose_topic = str(self.get_parameter("local_pose_topic").value)

        self.latest_camera_info: CameraInfo | None = None
        self.latest_local_pose: PoseStamped | None = None

        self.create_subscription(CameraInfo, camera_info_topic, self._camera_info_cb, qos_profile_sensor_data)
        self.create_subscription(PoseStamped, local_pose_topic, self._local_pose_cb, qos_profile_sensor_data)

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        self._last_remote_frame_error_at = 0.0

        self.gemini_client = self._load_gemini_client()
        self.depth_processor, self.depth_model = self._load_depth_model()

        self.create_service(GetObject3D, self.service_name, self._handle_get_object_3d)

        self.get_logger().info(
            "Object locator ready: "
            f"service={self.service_name}, frame_source={self.frame_jpeg_url or '<unset>'}, "
            f"gemini={'on' if self.gemini_client else 'off'}, depth={'on' if self.depth_model else 'off'}"
        )

    def _camera_info_cb(self, msg: CameraInfo) -> None:
        self.latest_camera_info = msg

    def _local_pose_cb(self, msg: PoseStamped) -> None:
        self.latest_local_pose = msg

    def _load_gemini_client(self):
        if genai is None or genai_types is None:
            self.get_logger().error("google-genai is unavailable. Install `google-genai`.")
            return None

        if not self.gemini_model_id:
            self.get_logger().error("gemini_model_id is empty")
            return None

        api_version = self.gemini_api_version.strip() or "v1alpha"
        try:
            client = genai.Client(
                http_options=genai_types.HttpOptions(api_version=api_version),
            )
            self.get_logger().info(
                f"Gemini detector enabled: model={self.gemini_model_id}, api_version={api_version}"
            )
            return client
        except Exception as exc:
            self.get_logger().error(f"Failed to initialize Gemini client: {exc}")
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

    def _read_frame(self) -> np.ndarray | None:
        return self._read_remote_frame()

    def _warn_remote_frame_error(self, message: str) -> None:
        now = time.time()
        if now - self._last_remote_frame_error_at > 2.0:
            self._last_remote_frame_error_at = now
            self.get_logger().warn(message)

    def _read_remote_frame(self) -> np.ndarray | None:
        url = self.frame_jpeg_url
        if not url:
            return None

        request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        try:
            with urllib.request.urlopen(request, timeout=0.8) as response:
                if int(getattr(response, "status", 200)) >= 400:
                    self._warn_remote_frame_error(f"remote frame endpoint returned status={response.status}")
                    return None
                frame_bytes = response.read()
                timestamp_header = response.headers.get("X-Frame-Timestamp")
        except urllib.error.URLError as exc:
            self._warn_remote_frame_error(f"remote frame fetch failed: {exc}")
            return None
        except Exception as exc:
            self._warn_remote_frame_error(f"remote frame fetch failed: {exc}")
            return None

        if not frame_bytes:
            self._warn_remote_frame_error("remote frame endpoint returned empty body")
            return None

        if timestamp_header:
            try:
                frame_ts = float(timestamp_header)
                max_age = max(0.2, self.max_remote_frame_age_sec)
                if time.time() - frame_ts > max_age:
                    self._warn_remote_frame_error(
                        f"remote frame is stale: age={time.time() - frame_ts:.2f}s > {max_age:.2f}s"
                    )
                    return None
            except Exception:
                pass

        array = np.frombuffer(frame_bytes, dtype=np.uint8)
        frame = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if frame is None or frame.size == 0:
            self._warn_remote_frame_error("remote frame decode failed")
            return None
        return frame

    def _encode_frame_jpeg(self, frame_bgr: np.ndarray) -> bytes | None:
        ok, encoded = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            return None
        return encoded.tobytes()

    def _query_tokens(self, text: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-zA-Z0-9_]+", text.lower())
            if token and token not in _STOPWORDS and len(token) > 1
        }

    def _bbox_schema(self) -> dict[str, Any]:
        return {
            "type": "ARRAY",
            "description": "Detected objects that match the user query.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "box_2d": {
                        "type": "ARRAY",
                        "description": "Bounding box as [y_min, x_min, y_max, x_max], normalized to 0..1000.",
                        "items": {"type": "INTEGER"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "label": {
                        "type": "STRING",
                        "description": "Short label that uniquely identifies the detected object.",
                    },
                    "score": {
                        "type": "NUMBER",
                        "description": "Optional confidence in range 0..1.",
                    },
                },
                "required": ["box_2d", "label"],
            },
        }

    def _build_detection_prompt(self, target_query: str) -> str:
        max_objects = max(1, self.gemini_max_objects)
        return (
            f"Find objects in the image that match this target query: '{target_query}'. "
            f"Return at most {max_objects} detections. "
            "If no matching object is visible, return an empty list."
        )

    def _normalize_box(self, box_raw: Sequence[Any]) -> list[int] | None:
        if len(box_raw) != 4:
            return None
        values: list[int] = []
        for value in box_raw:
            try:
                number = int(round(float(value)))
            except Exception:
                return None
            values.append(int(np.clip(number, 0, 1000)))

        y1, x1, y2, x2 = values
        if y2 <= y1 or x2 <= x1:
            return None
        return [y1, x1, y2, x2]

    def _parse_detection_items(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes, bytearray, memoryview)):
            return []

        items: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            box = self._normalize_box(item.get("box_2d") if isinstance(item.get("box_2d"), Sequence) else [])
            if box is None:
                continue
            label = str(item.get("label", "")).strip()
            if not label:
                continue
            score_raw = item.get("score")
            score: float | None = None
            if score_raw is not None:
                try:
                    score = float(score_raw)
                except Exception:
                    score = None
            items.append({"box_2d": box, "label": label, "score": score})
        return items

    def _response_items(self, response: Any) -> list[dict[str, Any]]:
        parsed = getattr(response, "parsed", None)
        items = self._parse_detection_items(parsed)
        if items:
            return items

        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            return []

        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"\s*```$", "", text).strip()

        try:
            loaded = json.loads(text)
        except Exception:
            return []
        return self._parse_detection_items(loaded)

    def _choose_best_detection(self, target_query: str, detections: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not detections:
            return None

        query_tokens = self._query_tokens(target_query)
        best_item: dict[str, Any] | None = None
        best_score = -1.0

        for item in detections:
            label_tokens = self._query_tokens(item["label"])
            overlap = 0.0
            if query_tokens and label_tokens:
                overlap = float(len(query_tokens.intersection(label_tokens))) / float(len(query_tokens))

            y1, x1, y2, x2 = item["box_2d"]
            area = float(max(1, (y2 - y1) * (x2 - x1))) / 1_000_000.0
            model_score = item["score"] if isinstance(item["score"], float) else 0.0

            total = (0.6 * overlap) + (0.3 * model_score) + (0.1 * min(area * 4.0, 1.0))
            if total > best_score:
                best_score = total
                best_item = {
                    "box_2d": item["box_2d"],
                    "label": item["label"],
                    "confidence": float(max(0.0, min(1.0, total))),
                }

        return best_item

    def _detect_with_gemini(self, frame_bgr: np.ndarray, target_query: str, min_confidence: float) -> tuple[tuple[int, int, int, int], float, str] | None:
        if self.gemini_client is None or genai_types is None:
            return None

        frame_bytes = self._encode_frame_jpeg(frame_bgr)
        if not frame_bytes:
            return None

        prompt = self._build_detection_prompt(target_query)
        config = genai_types.GenerateContentConfig(
            system_instruction=(
                "Return bounding boxes as an array with labels. "
                "Use only the provided response schema."
            ),
            temperature=float(max(0.0, self.gemini_temperature)),
            response_mime_type="application/json",
            response_schema=self._bbox_schema(),
        )

        attempts = max(1, self.gemini_retries)
        for attempt in range(attempts):
            try:
                response = self.gemini_client.models.generate_content(
                    model=self.gemini_model_id,
                    contents=[
                        prompt,
                        genai_types.Part.from_bytes(data=frame_bytes, mime_type="image/jpeg"),
                    ],
                    config=config,
                )
            except Exception as exc:
                self.get_logger().warn(f"Gemini detection failed (attempt {attempt + 1}/{attempts}): {exc}")
                continue

            items = self._response_items(response)
            self.get_logger().info(f"[DEBUG] Gemini detected {len(items)} items: {items}")
            
            best = self._choose_best_detection(target_query, items)
            if best is None:
                self.get_logger().info(f"[DEBUG] choose_best_detection returned None for query '{target_query}'")
                continue

            confidence = float(best["confidence"])
            self.get_logger().info(f"[DEBUG] Best candidate: label='{best['label']}', box_2d={best['box_2d']}, confidence={confidence:.3f} (required >= {float(min_confidence):.3f})")
            
            if confidence < max(0.0, float(min_confidence)):
                self.get_logger().info(f"[DEBUG] Rejected due to low confidence.")
                continue

            y1, x1, y2, x2 = best["box_2d"]
            h, w = frame_bgr.shape[:2]
            bx1 = int(np.clip(round((x1 / 1000.0) * w), 0, w - 1))
            by1 = int(np.clip(round((y1 / 1000.0) * h), 0, h - 1))
            bx2 = int(np.clip(round((x2 / 1000.0) * w), bx1 + 1, w))
            by2 = int(np.clip(round((y2 / 1000.0) * h), by1 + 1, h))

            bbox = (bx1, by1, bx2 - bx1, by2 - by1)
            return bbox, confidence, best["label"]

        return None

    def _bbox_iou(self, a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
        ax1, ay1, aw, ah = a
        bx1, by1, bw, bh = b
        ax2, ay2 = ax1 + aw, ay1 + ah
        bx2, by2 = bx1 + bw, by1 + bh

        ix1 = max(ax1, bx1)
        iy1 = max(ay1, by1)
        ix2 = min(ax2, bx2)
        iy2 = min(ay2, by2)
        iw = max(0, ix2 - ix1)
        ih = max(0, iy2 - iy1)
        inter = float(iw * ih)
        if inter <= 0.0:
            return 0.0
        union = float((aw * ah) + (bw * bh) - inter)
        if union <= 0.0:
            return 0.0
        return inter / union

    def _choose_stable_detection(
        self,
        samples: list[tuple[np.ndarray, tuple[tuple[int, int, int, int], float, str]]],
    ) -> tuple[np.ndarray, tuple[tuple[int, int, int, int], float, str]] | None:
        if not samples:
            return None
        if len(samples) == 1:
            return samples[0]

        threshold = float(max(0.05, min(0.95, self.scan_iou_threshold)))
        boxes = [item[1][0] for item in samples]
        confidences = [float(item[1][1]) for item in samples]

        best_index = 0
        best_support = -1
        best_conf = -1.0
        for idx, box in enumerate(boxes):
            support = 0
            for other in boxes:
                if self._bbox_iou(box, other) >= threshold:
                    support += 1
            if support > best_support or (support == best_support and confidences[idx] > best_conf):
                best_support = support
                best_conf = confidences[idx]
                best_index = idx

        cluster_indices = [
            idx
            for idx, box in enumerate(boxes)
            if self._bbox_iou(boxes[best_index], box) >= threshold
        ]
        if len(cluster_indices) <= 1:
            return samples[best_index]

        x1 = int(np.median([boxes[idx][0] for idx in cluster_indices]))
        y1 = int(np.median([boxes[idx][1] for idx in cluster_indices]))
        x2 = int(np.median([boxes[idx][0] + boxes[idx][2] for idx in cluster_indices]))
        y2 = int(np.median([boxes[idx][1] + boxes[idx][3] for idx in cluster_indices]))
        median_bbox = (x1, y1, max(1, x2 - x1), max(1, y2 - y1))
        mean_conf = float(np.mean([confidences[idx] for idx in cluster_indices]))
        label = samples[best_index][1][2]
        frame = samples[best_index][0]
        return frame, (median_bbox, mean_conf, label)

    def _scan_for_target(
        self,
        target_query: str,
        min_confidence: float,
    ) -> tuple[np.ndarray, tuple[tuple[int, int, int, int], float, str]] | None:
        frame_count = max(1, self.scan_frames)
        pause_sec = max(0.0, self.scan_interval_sec)

        samples: list[tuple[np.ndarray, tuple[tuple[int, int, int, int], float, str]]] = []
        for index in range(frame_count):
            frame = self._read_frame()
            if frame is None:
                continue
            detection = self._detect_with_gemini(frame, target_query, min_confidence)
            if detection is not None:
                samples.append((frame, detection))
            if index + 1 < frame_count and pause_sec > 0.0:
                time.sleep(pause_sec)

        return self._choose_stable_detection(samples)

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

    def _resolve_camera_frame(self) -> str:
        if self.latest_camera_info and self.latest_camera_info.header.frame_id:
            return self.latest_camera_info.header.frame_id
        return self.default_camera_frame

    def _resolve_world_frame(self) -> str:
        return self.default_world_frame

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
        world_frame = self._resolve_world_frame()
        response.world_frame = world_frame
        response.found = False
        response.confidence = 0.0
        response.pixel_u = 0
        response.pixel_v = 0
        response.image_width = 0
        response.image_height = 0
        response.bbox_x = 0
        response.bbox_y = 0
        response.bbox_w = 0
        response.bbox_h = 0
        response.matched_label = ""

        if not target_query:
            response.success = False
            response.message = "target_query is empty"
            return response

        sample = self._scan_for_target(target_query, float(request.min_confidence))
        if sample is None:
            self.get_logger().info(f"[DEBUG] _scan_for_target returned None for '{target_query}'")
            frame = self._read_frame()
            if frame is None:
                response.success = False
                response.message = f"cannot read camera frame from {self.frame_jpeg_url}"
                return response
            frame_h, frame_w = frame.shape[:2]
            response.image_width = int(frame_w)
            response.image_height = int(frame_h)
            response.success = True
            response.found = False
            response.message = f"target '{target_query}' not found"
            return response

        frame, detection = sample
        if frame is None:
            response.success = False
            response.message = f"cannot read camera frame from {self.frame_jpeg_url}"
            return response

        frame_h, frame_w = frame.shape[:2]
        bbox, confidence, matched_label = detection
        x, y, w, h = bbox
        u = x + (w // 2)
        v = y + (h // 2)

        response.found = True
        response.confidence = float(confidence)
        response.pixel_u = int(u)
        response.pixel_v = int(v)
        response.image_width = int(frame_w)
        response.image_height = int(frame_h)
        response.bbox_x = int(x)
        response.bbox_y = int(y)
        response.bbox_w = int(w)
        response.bbox_h = int(h)
        response.matched_label = str(matched_label)

        depth_map = self._run_depth(frame)
        if depth_map is None:
            return self._fill_depth_unknown(response, f"target found ({matched_label}) but depth model is unavailable")

        depth_m, depth_uncertainty_m = self._sample_depth(depth_map, u, v)
        response.depth_m = float(depth_m)
        response.depth_uncertainty_m = float(depth_uncertainty_m)
        if not np.isfinite(depth_m):
            return self._fill_depth_unknown(response, "target found but depth sample is invalid")

        camera_frame = self._resolve_camera_frame()
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
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
