import cv2
import numpy as np
import torch
from typing import Tuple, Optional, List
from dataclasses import dataclass


@dataclass
class ShuttlecockDetection:
    x: float
    y: float
    confidence: float
    frame_idx: int
    timestamp: float


class TrackNetV3Wrapper:
    def __init__(self, model_path: str = None):
        self.model = None
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self._load_model(model_path)

    def _load_model(self, model_path: str):
        try:
            if model_path and torch.cuda.is_available():
                self.model = torch.jit.load(model_path)
                self.model.to(self.device)
                self.model.eval()
        except Exception:
            pass

    def detect(self, frame: np.ndarray) -> Optional[ShuttlecockDetection]:
        if self.model is None:
            return None

        try:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frame_resized = cv2.resize(frame_rgb, (640, 360))
            tensor = torch.from_numpy(frame_resized).permute(2, 0, 1).float() / 255.0
            tensor = tensor.unsqueeze(0).to(self.device)

            with torch.no_grad():
                output = self.model(tensor)

            if isinstance(output, torch.Tensor):
                heatmap = output.squeeze().cpu().numpy()
                y, x = np.unravel_index(np.argmax(heatmap), heatmap.shape)
                confidence = float(np.max(heatmap))

                original_x = (x / heatmap.shape[1]) * frame.shape[1]
                original_y = (y / heatmap.shape[0]) * frame.shape[0]

                return ShuttlecockDetection(x=original_x, y=original_y, confidence=confidence, frame_idx=0, timestamp=0.0)
        except Exception:
            pass

        return None


class YOLOShuttlecockDetector:
    def __init__(self, model_path: str = 'yolov8n.pt'):
        self.model = None
        try:
            from ultralytics import YOLO
            self.model = YOLO(model_path)
        except ImportError:
            pass

    def detect(self, frame: np.ndarray) -> Optional[ShuttlecockDetection]:
        if self.model is None:
            return None

        try:
            results = self.model(frame, verbose=False)
            if results and len(results[0].boxes) > 0:
                box = results[0].boxes[0]
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                x = (x1 + x2) / 2
                y = (y1 + y2) / 2
                confidence = float(box.conf[0])
                return ShuttlecockDetection(x=x, y=y, confidence=confidence, frame_idx=0, timestamp=0.0)
        except Exception:
            pass

        return None


class SimpleShuttlecockDetector:
    def __init__(self, min_size: int = 5, max_size: int = 30):
        self.min_size = min_size
        self.max_size = max_size

    def detect(self, frame: np.ndarray) -> Optional[ShuttlecockDetection]:
        try:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            lower_white = np.array([0, 0, 200])
            upper_white = np.array([180, 30, 255])
            mask = cv2.inRange(hsv, lower_white, upper_white)

            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            best_match = None
            best_score = -1

            for contour in contours:
                area = cv2.contourArea(contour)
                if self.min_size < area < self.max_size:
                    x, y, w, h = cv2.boundingRect(contour)
                    aspect_ratio = w / h

                    if 0.5 < aspect_ratio < 2.0:
                        score = area / (w * h)
                        if score > best_score:
                            best_score = score
                            best_match = (x + w / 2, y + h / 2)

            if best_match:
                return ShuttlecockDetection(x=best_match[0], y=best_match[1], confidence=best_score, frame_idx=0, timestamp=0.0)
        except Exception:
            pass

        return None


class ShuttlecockTracker:
    def __init__(self, detector_type: str = 'simple', model_path: str = None,
                 confidence_threshold: float = 0.5, speed_threshold: float = 0.5,
                 drop_speed_threshold: float = 0.1, fps: float = 30.0):
        self.confidence_threshold = confidence_threshold
        self.speed_threshold = speed_threshold
        self.drop_speed_threshold = drop_speed_threshold
        self.detections: List[ShuttlecockDetection] = []
        self.frame_idx = 0
        self.fps = fps

        if detector_type == 'tracknet':
            self.detector = TrackNetV3Wrapper(model_path)
        elif detector_type == 'yolo':
            self.detector = YOLOShuttlecockDetector(model_path)
        else:
            self.detector = SimpleShuttlecockDetector()

    def process_frame(self, frame: np.ndarray) -> Optional[ShuttlecockDetection]:
        detection = self.detector.detect(frame)

        if detection and detection.confidence >= self.confidence_threshold:
            detection.frame_idx = self.frame_idx
            detection.timestamp = self.frame_idx / self.fps
            self.detections.append(detection)
            return detection

        self.frame_idx += 1
        return None

    def get_speed(self, recent_frames: int = 5) -> float:
        if len(self.detections) < recent_frames:
            return 0.0

        recent = self.detections[-recent_frames:]
        if len(recent) < 2:
            return 0.0

        total_distance = 0.0
        for i in range(1, len(recent)):
            dx = recent[i].x - recent[i-1].x
            dy = recent[i].y - recent[i-1].y
            total_distance += np.sqrt(dx * dx + dy * dy)

        return total_distance / (len(recent) - 1) / (1.0 / self.fps)

    def is_moving(self) -> bool:
        return self.get_speed() > self.speed_threshold

    def is_dropped(self) -> bool:
        if len(self.detections) < 10:
            return False

        recent_speed = self.get_speed(recent_frames=10)
        return recent_speed < self.drop_speed_threshold and len(self.detections) > 0

    def get_last_detection(self) -> Optional[ShuttlecockDetection]:
        if self.detections:
            return self.detections[-1]
        return None

    def reset(self):
        self.detections = []
        self.frame_idx = 0