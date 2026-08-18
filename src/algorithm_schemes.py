import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from .shuttlecock_tracker import SimpleShuttlecockDetector
from .tracknetv3_integrator import TrackNetV3Integrator


@dataclass
class FrameScore:
    frame_idx: int
    timestamp: float
    active: bool
    score: float
    note: str = ""


@dataclass
class Interval:
    start_time: float
    end_time: float
    confidence: float = 1.0
    source: str = ""

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time


@dataclass
class AlgorithmResult:
    name: str
    available: bool
    scores: List[FrameScore]
    intervals: List[Interval]
    elapsed_seconds: float
    error: Optional[str] = None
    notes: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class VideoInfo:
    fps: float
    total_frames: int
    duration: float
    width: int
    height: int


def probe_video(video_path: str) -> VideoInfo:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()
    duration = total_frames / max(fps, 1.0)
    return VideoInfo(fps=fps, total_frames=total_frames, duration=duration, width=width, height=height)


def iter_sampled_frames(video_path: str, sample_interval: int):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    frame_idx = 0
    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
            if frame_idx % max(1, int(sample_interval)) == 0:
                yield frame_idx, frame_idx / max(fps, 1.0), frame
            frame_idx += 1
    finally:
        cap.release()


def build_intervals(
    scores: List[FrameScore],
    video_duration: float,
    min_duration: float = 1.0,
    max_gap_seconds: float = 0.8,
    padding_before: float = 0.2,
    padding_after: float = 0.2,
) -> List[Interval]:
    if not scores:
        return []
    runs: List[Tuple[int, int, List[float]]] = []
    start_index: Optional[int] = None
    run_scores: List[float] = []
    for index, item in enumerate(scores):
        if item.active:
            if start_index is None:
                start_index = index
            run_scores.append(float(item.score))
        elif start_index is not None:
            runs.append((start_index, index - 1, run_scores))
            start_index = None
            run_scores = []
    if start_index is not None:
        runs.append((start_index, len(scores) - 1, run_scores))

    intervals: List[Interval] = []
    for start_index, end_index, run_scores in runs:
        start_time = max(0.0, scores[start_index].timestamp - padding_before)
        end_time = min(video_duration, scores[end_index].timestamp + padding_after)
        confidence = float(np.mean(run_scores)) if run_scores else 0.0
        intervals.append(Interval(start_time=start_time, end_time=end_time, confidence=confidence))

    merged: List[Interval] = []
    for interval in intervals:
        if not merged:
            merged.append(interval)
            continue
        last = merged[-1]
        if interval.start_time <= last.end_time + max_gap_seconds:
            last.end_time = max(last.end_time, interval.end_time)
            last.confidence = max(last.confidence, interval.confidence)
        else:
            merged.append(interval)

    return [item for item in merged if item.duration >= float(min_duration)]


def run_simple_hsv(video_path: str, sample_interval: int = 2, min_duration: float = 1.0) -> AlgorithmResult:
    start = time.perf_counter()
    detector = SimpleShuttlecockDetector(min_size=3, max_size=220)
    scores: List[FrameScore] = []
    try:
        for frame_idx, timestamp, frame in iter_sampled_frames(video_path, sample_interval):
            det = detector.detect(frame)
            active = bool(det and det.confidence >= 0.30)
            score = float(det.confidence) if det else 0.0
            note = "white_region" if det else ""
            scores.append(FrameScore(frame_idx, timestamp, active, score, note))
        info = probe_video(video_path)
        intervals = build_intervals(scores, info.duration, min_duration=min_duration)
        return AlgorithmResult("simple_hsv", True, scores, intervals, time.perf_counter() - start)
    except Exception as exc:
        return AlgorithmResult("simple_hsv", False, scores, [], time.perf_counter() - start, str(exc))


def run_motion_flow(video_path: str, sample_interval: int = 2, min_duration: float = 1.0) -> AlgorithmResult:
    start = time.perf_counter()
    scores: List[FrameScore] = []
    subtractor = cv2.createBackgroundSubtractorMOG2(history=45, varThreshold=20, detectShadows=False)
    close_kernel = np.ones((5, 5), np.uint8)
    open_kernel = np.ones((3, 3), np.uint8)
    smoothed_ratio = 0.0
    try:
        for frame_idx, timestamp, frame in iter_sampled_frames(video_path, sample_interval):
            height, width = frame.shape[:2]
            target_width = min(480, width)
            scale = target_width / max(width, 1)
            target_size = (target_width, max(1, int(height * scale)))
            resized = cv2.resize(frame, target_size)
            gray = cv2.GaussianBlur(cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY), (5, 5), 0)
            foreground = subtractor.apply(gray, learningRate=0.015)
            _, foreground = cv2.threshold(foreground, 200, 255, cv2.THRESH_BINARY)
            foreground = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, close_kernel)
            foreground = cv2.morphologyEx(foreground, cv2.MORPH_OPEN, open_kernel)
            motion_pixels = int(cv2.countNonZero(foreground))
            motion_ratio = motion_pixels / float(foreground.shape[0] * foreground.shape[1])
            smoothed_ratio = 0.65 * smoothed_ratio + 0.35 * motion_ratio
            active = motion_ratio >= 0.0014 or smoothed_ratio >= 0.0019
            score = min(1.0, max(motion_ratio, smoothed_ratio) * 90.0)
            note = f"motion_ratio={motion_ratio:.4f},smoothed={smoothed_ratio:.4f}"
            scores.append(FrameScore(frame_idx, timestamp, active, score, note))
        info = probe_video(video_path)
        intervals = build_intervals(scores, info.duration, min_duration=min_duration)
        return AlgorithmResult("motion_flow", True, scores, intervals, time.perf_counter() - start)
    except Exception as exc:
        return AlgorithmResult("motion_flow", False, scores, [], time.perf_counter() - start, str(exc))


def run_pose_activity(video_path: str, sample_interval: int = 2, min_duration: float = 1.0) -> AlgorithmResult:
    start = time.perf_counter()
    scores: List[FrameScore] = []
    previous_center = None
    pose = None
    try:
        import mediapipe as mp
        mp_pose = mp.solutions.pose
        pose = mp_pose.Pose(model_complexity=0, min_detection_confidence=0.45, min_tracking_confidence=0.45)
        landmark_indexes = [
            mp_pose.PoseLandmark.LEFT_SHOULDER,
            mp_pose.PoseLandmark.RIGHT_SHOULDER,
            mp_pose.PoseLandmark.LEFT_ELBOW,
            mp_pose.PoseLandmark.RIGHT_ELBOW,
            mp_pose.PoseLandmark.LEFT_WRIST,
            mp_pose.PoseLandmark.RIGHT_WRIST,
            mp_pose.PoseLandmark.LEFT_HIP,
            mp_pose.PoseLandmark.RIGHT_HIP,
            mp_pose.PoseLandmark.LEFT_KNEE,
            mp_pose.PoseLandmark.RIGHT_KNEE,
            mp_pose.PoseLandmark.LEFT_ANKLE,
            mp_pose.PoseLandmark.RIGHT_ANKLE,
        ]
        for frame_idx, timestamp, frame in iter_sampled_frames(video_path, sample_interval):
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = pose.process(rgb)
            if not result.pose_landmarks:
                scores.append(FrameScore(frame_idx, timestamp, False, 0.0, "no_person"))
                previous_center = None
                continue
            landmarks = result.pose_landmarks.landmark
            visible = []
            points = []
            shoulder_y = []
            wrist_y = []
            for index in landmark_indexes:
                point = landmarks[index]
                if point.visibility >= 0.35:
                    visible.append(point.visibility)
                    points.append((point.x, point.y))
                if index in (mp_pose.PoseLandmark.LEFT_SHOULDER, mp_pose.PoseLandmark.RIGHT_SHOULDER):
                    shoulder_y.append(point.y)
                if index in (mp_pose.PoseLandmark.LEFT_WRIST, mp_pose.PoseLandmark.RIGHT_WRIST):
                    wrist_y.append(point.y)
            if len(visible) < 5 or not points:
                scores.append(FrameScore(frame_idx, timestamp, False, 0.0, "low_visibility"))
                previous_center = None
                continue
            center = np.mean(np.array(points), axis=0)
            diagonal = float(np.hypot(frame.shape[1], frame.shape[0]))
            displacement = 0.0 if previous_center is None else float(np.linalg.norm(center - previous_center) * diagonal / diagonal)
            hand_raised = bool(wrist_y and shoulder_y and min(wrist_y) < min(shoulder_y) - 0.02)
            visibility_score = float(np.mean(visible))
            motion_score = min(1.0, displacement / 0.03)
            score = min(1.0, visibility_score * 0.45 + motion_score * 0.55)
            active = visibility_score >= 0.45 and (hand_raised or displacement >= 0.008)
            note = f"visibility={visibility_score:.2f},displacement={displacement:.4f}"
            scores.append(FrameScore(frame_idx, timestamp, active, score, note))
            previous_center = center
        info = probe_video(video_path)
        intervals = build_intervals(scores, info.duration, min_duration=min_duration)
        return AlgorithmResult("pose_activity", True, scores, intervals, time.perf_counter() - start)
    except Exception as exc:
        return AlgorithmResult("pose_activity", False, scores, [], time.perf_counter() - start, str(exc))
    finally:
        if pose is not None:
            pose.close()


def run_tracknet(video_path: str, sample_interval: int = 2, min_duration: float = 1.0) -> AlgorithmResult:
    start = time.perf_counter()
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tracknet_path = os.path.join(project_root, "models", "TrackNetV3", "ckpts", "ckpts", "TrackNet_best.pt")
    inpaintnet_path = os.path.join(project_root, "models", "TrackNetV3", "ckpts", "ckpts", "InpaintNet_best.pt")
    if not os.path.exists(tracknet_path):
        return AlgorithmResult("tracknet_v3", False, [], [], time.perf_counter() - start, None, "缺少 TrackNetV3 权重")
    try:
        integrator = TrackNetV3Integrator(tracknet_path, inpaintnet_path)
        if integrator.tracknet is None:
            return AlgorithmResult("tracknet_v3", False, [], [], time.perf_counter() - start, "模型加载失败")
        detections = integrator.process_video(video_path, sample_interval=max(1, int(sample_interval)))
        detection_map = {item.frame_idx: item for item in detections}
        scores = []
        for frame_idx, timestamp, _ in iter_sampled_frames(video_path, sample_interval):
            item = detection_map.get(frame_idx)
            active = bool(item and item.visibility == 1)
            scores.append(FrameScore(frame_idx, timestamp, active, 1.0 if active else 0.0, "heatmap"))
        info = probe_video(video_path)
        intervals = build_intervals(scores, info.duration, min_duration=min_duration)
        return AlgorithmResult("tracknet_v3", True, scores, intervals, time.perf_counter() - start)
    except Exception as exc:
        return AlgorithmResult("tracknet_v3", False, [], [], time.perf_counter() - start, str(exc))


def _vlm_config() -> Tuple[Optional[str], str, str]:
    api_key = os.getenv("VLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("VLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model = os.getenv("VLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
    return api_key, base_url.rstrip("/"), model


def _encode_frame(frame: np.ndarray) -> str:
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return base64.b64encode(buffer.tobytes()).decode("ascii")


def _vlm_request(image_b64: str, api_key: str, base_url: str, model: str) -> Dict:
    prompt = "你是羽毛球比赛视频分析助手。请判断这一帧是否处于回合进行中，即球员在场上明显移动、击球或羽毛球处于飞行状态。只输出JSON：{\"is_rally\":true或false,\"confidence\":0到1,\"reason\":\"不超过20字\"}"
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
            ]
        }],
        "temperature": 0,
        "max_tokens": 120,
        "response_format": {"type": "json_object"}
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        body = json.loads(response.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return json.loads(content)


def run_vlm_sparse(video_path: str, sample_interval: int = 2, min_duration: float = 1.0,
                   keyframe_interval_seconds: float = 2.0) -> AlgorithmResult:
    start = time.perf_counter()
    api_key, base_url, model = _vlm_config()
    if not api_key:
        return AlgorithmResult("vlm_sparse", False, [], [], time.perf_counter() - start, None, "未设置 VLM_API_KEY/OPENAI_API_KEY")
    info = probe_video(video_path)
    keyframe_stride = max(1, int(round(keyframe_interval_seconds * info.fps / max(1, sample_interval))))
    labels: Dict[int, Dict] = {}
    failures = 0
    try:
        sampled = list(iter_sampled_frames(video_path, sample_interval))
        for position, (frame_idx, _timestamp, frame) in enumerate(sampled):
            if position % keyframe_stride != 0:
                continue
            try:
                image_b64 = _encode_frame(frame)
                labels[frame_idx] = _vlm_request(image_b64, api_key, base_url, model)
            except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError) as exc:
                failures += 1
                labels[frame_idx] = {"is_rally": False, "confidence": 0.0, "reason": str(exc)[:80]}
        if not labels:
            return AlgorithmResult("vlm_sparse", False, [], [], time.perf_counter() - start, "没有可用关键帧")
        keyframe_indices = sorted(labels.keys())
        scores = []
        for frame_idx, timestamp, _frame in sampled:
            nearest = min(keyframe_indices, key=lambda idx: abs(idx - frame_idx))
            label = labels[nearest]
            active = bool(label.get("is_rally"))
            confidence = float(label.get("confidence") or 0.0)
            note = str(label.get("reason") or "")
            scores.append(FrameScore(frame_idx, timestamp, active, confidence, note))
        intervals = build_intervals(scores, info.duration, min_duration=min_duration, max_gap_seconds=keyframe_interval_seconds * 1.5)
        available = failures < len(labels)
        notes = f"model={model}, keyframes={len(labels)}, failures={failures}"
        return AlgorithmResult("vlm_sparse", available, scores, intervals, time.perf_counter() - start, None if available else "大模型调用全部失败", notes)
    except Exception as exc:
        return AlgorithmResult("vlm_sparse", False, [], [], time.perf_counter() - start, str(exc))


ALGORITHMS: Dict[str, Callable[..., AlgorithmResult]] = {
    "simple_hsv": run_simple_hsv,
    "motion_flow": run_motion_flow,
    "pose_activity": run_pose_activity,
    "tracknet_v3": run_tracknet,
    "vlm_sparse": run_vlm_sparse,
}


def run_algorithm(name: str, video_path: str, sample_interval: int = 2, min_duration: float = 1.0, **kwargs) -> AlgorithmResult:
    if name not in ALGORITHMS:
        raise ValueError(f"未知算法: {name}，可选: {', '.join(ALGORITHMS)}")
    return ALGORITHMS[name](video_path, sample_interval=sample_interval, min_duration=min_duration, **kwargs)
