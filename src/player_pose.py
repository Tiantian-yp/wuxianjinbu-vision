import cv2
import numpy as np
import mediapipe as mp
from typing import Optional, Tuple, List
from dataclasses import dataclass


@dataclass
class PlayerPose:
    frame_idx: int
    timestamp: float
    is_serving: bool
    confidence: float
    racket_up: bool
    body_tilt: float
    hand_raised: bool


class PlayerPoseDetector:
    def __init__(self, model_complexity: int = 1,
                 min_detection_confidence: float = 0.5,
                 min_tracking_confidence: float = 0.5):
        self.mp_pose = mp.solutions.pose
        self.pose = self.mp_pose.Pose(
            model_complexity=model_complexity,
            min_detection_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence
        )
        self.frame_idx = 0
        self.fps = 30.0
        self.recent_poses: List[PlayerPose] = []

    def detect(self, frame: np.ndarray) -> Optional[PlayerPose]:
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.pose.process(frame_rgb)

        if not results.pose_landmarks:
            self.frame_idx += 1
            return None

        landmarks = results.pose_landmarks.landmark
        timestamp = self.frame_idx / self.fps

        is_serving, confidence = self._detect_serving_action(landmarks)
        racket_up = self._is_racket_up(landmarks)
        body_tilt = self._calculate_body_tilt(landmarks)
        hand_raised = self._is_hand_raised(landmarks)

        pose_result = PlayerPose(
            frame_idx=self.frame_idx,
            timestamp=timestamp,
            is_serving=is_serving,
            confidence=confidence,
            racket_up=racket_up,
            body_tilt=body_tilt,
            hand_raised=hand_raised
        )

        self.recent_poses.append(pose_result)
        if len(self.recent_poses) > 30:
            self.recent_poses.pop(0)

        self.frame_idx += 1
        return pose_result

    def _detect_serving_action(self, landmarks) -> Tuple[bool, float]:
        try:
            shoulder = landmarks[self.mp_pose.PoseLandmark.RIGHT_SHOULDER]
            elbow = landmarks[self.mp_pose.PoseLandmark.RIGHT_ELBOW]
            wrist = landmarks[self.mp_pose.PoseLandmark.RIGHT_WRIST]
            hip = landmarks[self.mp_pose.PoseLandmark.RIGHT_HIP]
            knee = landmarks[self.mp_pose.PoseLandmark.RIGHT_KNEE]
            ankle = landmarks[self.mp_pose.PoseLandmark.RIGHT_ANKLE]

            if not all([shoulder.visibility > 0.5, elbow.visibility > 0.5,
                       wrist.visibility > 0.5, hip.visibility > 0.5]):
                return False, 0.0

            wrist_height_ratio = wrist.y / shoulder.y
            elbow_angle = self._calculate_angle(shoulder, elbow, wrist)
            body_height_ratio = ankle.y / hip.y

            is_serving = False
            confidence = 0.0

            if wrist_height_ratio < 0.8:
                confidence += 0.3
            if elbow_angle > 150:
                confidence += 0.3
            if body_height_ratio > 1.5:
                confidence += 0.3

            if confidence > 0.6:
                is_serving = True

            return is_serving, confidence
        except Exception:
            return False, 0.0

    def _is_racket_up(self, landmarks) -> bool:
        try:
            right_shoulder = landmarks[self.mp_pose.PoseLandmark.RIGHT_SHOULDER]
            right_wrist = landmarks[self.mp_pose.PoseLandmark.RIGHT_WRIST]
            left_shoulder = landmarks[self.mp_pose.PoseLandmark.LEFT_SHOULDER]
            left_wrist = landmarks[self.mp_pose.PoseLandmark.LEFT_WRIST]

            right_raised = right_wrist.y < right_shoulder.y and right_wrist.visibility > 0.5
            left_raised = left_wrist.y < left_shoulder.y and left_wrist.visibility > 0.5

            return right_raised or left_raised
        except Exception:
            return False

    def _calculate_body_tilt(self, landmarks) -> float:
        try:
            left_shoulder = landmarks[self.mp_pose.PoseLandmark.LEFT_SHOULDER]
            right_shoulder = landmarks[self.mp_pose.PoseLandmark.RIGHT_SHOULDER]
            left_hip = landmarks[self.mp_pose.PoseLandmark.LEFT_HIP]
            right_hip = landmarks[self.mp_pose.PoseLandmark.RIGHT_HIP]

            shoulder_slope = (right_shoulder.y - left_shoulder.y) / (right_shoulder.x - left_shoulder.x + 1e-6)
            hip_slope = (right_hip.y - left_hip.y) / (right_hip.x - left_hip.x + 1e-6)

            shoulder_angle = np.arctan(shoulder_slope) * 180 / np.pi
            hip_angle = np.arctan(hip_slope) * 180 / np.pi

            return abs(shoulder_angle - hip_angle)
        except Exception:
            return 0.0

    def _is_hand_raised(self, landmarks) -> bool:
        try:
            right_wrist = landmarks[self.mp_pose.PoseLandmark.RIGHT_WRIST]
            left_wrist = landmarks[self.mp_pose.PoseLandmark.LEFT_WRIST]
            right_shoulder = landmarks[self.mp_pose.PoseLandmark.RIGHT_SHOULDER]
            left_shoulder = landmarks[self.mp_pose.PoseLandmark.LEFT_SHOULDER]

            right_raised = right_wrist.y < right_shoulder.y - 0.1 and right_wrist.visibility > 0.5
            left_raised = left_wrist.y < left_shoulder.y - 0.1 and left_wrist.visibility > 0.5

            return right_raised or left_raised
        except Exception:
            return False

    def _calculate_angle(self, a, b, c) -> float:
        try:
            ba_x = a.x - b.x
            ba_y = a.y - b.y
            bc_x = c.x - b.x
            bc_y = c.y - b.y

            dot_product = ba_x * bc_x + ba_y * bc_y
            magnitude_ba = np.sqrt(ba_x ** 2 + ba_y ** 2)
            magnitude_bc = np.sqrt(bc_x ** 2 + bc_y ** 2)

            if magnitude_ba == 0 or magnitude_bc == 0:
                return 0.0

            cos_angle = dot_product / (magnitude_ba * magnitude_bc)
            cos_angle = max(-1, min(1, cos_angle))
            angle = np.arccos(cos_angle) * 180 / np.pi

            return angle
        except Exception:
            return 0.0

    def is_serving_now(self) -> bool:
        if len(self.recent_poses) < 5:
            return False

        recent_serving = [p for p in self.recent_poses[-5:] if p.is_serving]
        return len(recent_serving) >= 3

    def draw_pose(self, frame: np.ndarray, pose: Optional[PlayerPose] = None) -> np.ndarray:
        if pose is None:
            return frame

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.pose.process(frame_rgb)

        if results.pose_landmarks:
            mp.solutions.drawing_utils.draw_landmarks(
                frame,
                results.pose_landmarks,
                self.mp_pose.POSE_CONNECTIONS,
                mp.solutions.drawing_utils.DrawingSpec(color=(0, 255, 0), thickness=2, circle_radius=2),
                mp.solutions.drawing_utils.DrawingSpec(color=(0, 255, 0), thickness=2, circle_radius=2)
            )

            if pose.is_serving:
                cv2.putText(frame, "SERVING", (50, 50),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)

        return frame

    def reset(self):
        self.frame_idx = 0
        self.recent_poses = []