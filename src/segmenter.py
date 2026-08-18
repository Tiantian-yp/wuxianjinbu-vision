import cv2
import numpy as np
import logging
from typing import List, Optional, Callable
from dataclasses import dataclass
from .court_detector import CourtDetector
from .shuttlecock_tracker import ShuttlecockTracker, ShuttlecockDetection
from .player_pose import PlayerPoseDetector
from .tracknetv3_integrator import TrackNetV3Integrator, TrackNetV3Detection

logger = logging.getLogger(__name__)


@dataclass
class Segment:
    start_time: float
    end_time: float
    start_frame: int
    end_frame: int
    valid: bool = True
    confidence: float = 0.0

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time


class VideoSegmenter:
    def __init__(self, min_segment_duration: float = 3.0,
                 max_segment_duration: float = 60.0,
                 padding_before: float = 0.5,
                 padding_after: float = 0.5,
                 sample_interval: int = 2):
        self.min_segment_duration = float(min_segment_duration)
        self.max_segment_duration = float(max_segment_duration)
        self.padding_before = float(padding_before)
        self.padding_after = float(padding_after)
        self.sample_interval = max(1, int(sample_interval))
        self.segments: List[Segment] = []
        self.current_segment: Optional[Segment] = None
        self.video_fps = 30.0
        self.video_duration = 0.0
        self.total_frames = 0
        self.video_width = None
        self.video_height = None

    def process_video(self, video_path: str, output_dir: str = None,
                      use_tracknet: bool = False,
                      sample_interval: Optional[int] = None,
                      progress_callback: Optional[Callable[[float, str], None]] = None) -> List[Segment]:
        interval = int(sample_interval) if sample_interval and sample_interval > 0 else self.sample_interval
        self.sample_interval = interval
        if progress_callback:
            try:
                progress_callback(0.0, "视频打开中…")
            except Exception:
                logger.exception('progress_callback(0) failed')
        if use_tracknet:
            return self._process_with_tracknet(video_path, sample_interval=interval,
                                               progress_callback=progress_callback)
        return self._process_with_simple_detector(video_path, progress_callback=progress_callback)

    def _emit_progress(self, progress_callback: Optional[Callable], progress: float, message: str) -> None:
        if not progress_callback:
            return
        try:
            progress_callback(max(0.0, min(1.0, float(progress))), message)
        except Exception:
            logger.exception('progress_callback failed progress=%.4f msg=%s', progress, message)

    def _process_with_tracknet(self, video_path: str, sample_interval: int = 2,
                               progress_callback: Optional[Callable[[float, str], None]] = None) -> List[Segment]:
        logger.info("Using TrackNetV3 for shuttlecock detection (sample_interval=%d)", sample_interval)
        tracknet_path = './models/TrackNetV3/ckpts/ckpts/TrackNet_best.pt'
        inpaintnet_path = './models/TrackNetV3/ckpts/ckpts/InpaintNet_best.pt'
        integrator = TrackNetV3Integrator(tracknet_path, inpaintnet_path)
        if integrator.tracknet is None:
            logger.warning("TrackNet model not available, falling back to simple detector")
            return self._process_with_simple_detector(video_path, progress_callback=progress_callback)
        self._emit_progress(progress_callback, 0.05, "TrackNet 模型载入完成")
        detections = integrator.process_video(video_path, sample_interval=sample_interval)
        cap = cv2.VideoCapture(video_path)
        self.video_fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.video_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) or None
        self.video_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0) or None
        self.video_duration = self.total_frames / max(1.0, self.video_fps)
        cap.release()
        logger.info("video info: total_frames=%d, fps=%.2f, detections=%d",
                    self.total_frames, self.video_fps, len(detections))
        self._emit_progress(progress_callback, 0.75, "TrackNet 推理完成，开始生成片段")
        segments = self._build_segments_from_detections(detections)
        self._emit_progress(progress_callback, 1.0, f"TrackNet 片段生成完成：{len(segments)} 段")
        return segments

    def _process_with_simple_detector(self, video_path: str,
                                       progress_callback: Optional[Callable[[float, str], None]] = None) -> List[Segment]:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError(f"无法打开视频文件: {video_path}")
        self.video_fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.video_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) or None
        self.video_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0) or None
        self.video_duration = self.total_frames / max(1.0, self.video_fps)
        shuttle_tracker = ShuttlecockTracker(detector_type='simple', fps=self.video_fps)
        frame_idx = 0
        segment_start_frame = -1
        segment_start_time = 0.0
        shuttle_detections_in_current_segment = 0
        detection_history = []
        max_history = 30
        gap_threshold = int(self.video_fps * 0.5)
        min_detections_for_segment = 5
        logger.info("Processing video: %s frames=%d fps=%.2f duration=%.1fs sample_interval=%d",
                    video_path, self.total_frames, self.video_fps, self.video_duration, self.sample_interval)
        try:
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break
                timestamp = frame_idx / max(1.0, self.video_fps)
                if frame_idx % self.sample_interval == 0:
                    shuttle_detection = shuttle_tracker.process_frame(frame)
                    if shuttle_detection:
                        detection_history.append(frame_idx)
                        if len(detection_history) > max_history:
                            detection_history.pop(0)
                        shuttle_detections_in_current_segment += 1
                        if segment_start_frame == -1:
                            segment_start_frame = frame_idx
                            segment_start_time = timestamp
                            logger.debug("segment started at %.2fs (frame %d)", timestamp, frame_idx)
                    else:
                        if segment_start_frame != -1 and detection_history:
                            last_detection_frame = detection_history[-1]
                            if frame_idx - last_detection_frame >= gap_threshold:
                                if shuttle_detections_in_current_segment >= min_detections_for_segment:
                                    segment_end_time = (last_detection_frame + gap_threshold) / max(1.0, self.video_fps)
                                    segment_end_frame = last_detection_frame + gap_threshold
                                    adjusted_start = max(0.0, segment_start_time - self.padding_before)
                                    adjusted_end = min(self.video_duration, segment_end_time + self.padding_after)
                                    duration = adjusted_end - adjusted_start
                                    if self.min_segment_duration <= duration <= self.max_segment_duration:
                                        segment = Segment(
                                            start_time=adjusted_start,
                                            end_time=adjusted_end,
                                            start_frame=int(adjusted_start * self.video_fps),
                                            end_frame=int(adjusted_end * self.video_fps),
                                            valid=True,
                                            confidence=min(shuttle_detections_in_current_segment / 30.0, 1.0)
                                        )
                                        self.segments.append(segment)
                                        logger.debug(
                                            "segment ended at %.2fs duration=%.1fs detections=%d",
                                            segment_end_time, duration, shuttle_detections_in_current_segment
                                        )
                                segment_start_frame = -1
                                shuttle_detections_in_current_segment = 0
                                detection_history = []
                if progress_callback and self.total_frames > 0 and frame_idx % 300 == 0:
                    p = frame_idx / self.total_frames
                    self._emit_progress(progress_callback, p,
                                        f"逐帧识别中 {frame_idx}/{self.total_frames}（已发现 {len(self.segments)} 段）")
                frame_idx += 1
        finally:
            cap.release()
        if segment_start_frame != -1 and shuttle_detections_in_current_segment >= min_detections_for_segment:
            adjusted_start = max(0.0, segment_start_time - self.padding_before)
            adjusted_end = max(adjusted_start + 0.01, min(self.video_duration, self.video_duration))
            duration = adjusted_end - adjusted_start
            if self.min_segment_duration <= duration <= self.max_segment_duration:
                self.segments.append(Segment(
                    start_time=adjusted_start,
                    end_time=adjusted_end,
                    start_frame=int(adjusted_start * self.video_fps),
                    end_frame=int(adjusted_end * self.video_fps),
                    valid=True,
                    confidence=min(shuttle_detections_in_current_segment / 30.0, 1.0),
                ))
        self._merge_overlapping_segments()
        logger.info("Total segments detected: %d", len(self.segments))
        self._emit_progress(progress_callback, 1.0, f"识别完成，共 {len(self.segments)} 段")
        return self.segments

    def _build_segments_from_detections(self, detections: List[TrackNetV3Detection]) -> List[Segment]:
        if not detections:
            return []
        segments = []
        segment_start = None
        segment_start_time = 0.0
        consecutive_invisible = 0
        max_consecutive_invisible = int(self.video_fps * 0.5)
        min_visible_in_segment = 5
        visible_count = 0
        for det in detections:
            if det.visibility == 1:
                consecutive_invisible = 0
                visible_count += 1
                if segment_start is None:
                    segment_start = det.frame_idx
                    segment_start_time = det.timestamp
            else:
                if segment_start is not None:
                    consecutive_invisible += 1
                    if consecutive_invisible >= max_consecutive_invisible:
                        if visible_count >= min_visible_in_segment:
                            segment_end_time = det.timestamp
                            adjusted_start = max(0.0, segment_start_time - self.padding_before)
                            adjusted_end = min(self.video_duration, segment_end_time + self.padding_after)
                            duration = adjusted_end - adjusted_start
                            if self.min_segment_duration <= duration <= self.max_segment_duration:
                                segments.append(Segment(
                                    start_time=adjusted_start,
                                    end_time=adjusted_end,
                                    start_frame=int(adjusted_start * self.video_fps),
                                    end_frame=int(adjusted_end * self.video_fps),
                                    valid=True,
                                    confidence=min(visible_count / 30.0, 1.0)
                                ))
                        segment_start = None
                        visible_count = 0
        if segment_start is not None and visible_count >= min_visible_in_segment:
            adjusted_start = max(0.0, segment_start_time - self.padding_before)
            adjusted_end = min(self.video_duration, self.video_duration)
            duration = adjusted_end - adjusted_start
            if self.min_segment_duration <= duration <= self.max_segment_duration:
                segments.append(Segment(
                    start_time=adjusted_start,
                    end_time=adjusted_end,
                    start_frame=int(adjusted_start * self.video_fps),
                    end_frame=int(adjusted_end * self.video_fps),
                    valid=True,
                    confidence=min(visible_count / 30.0, 1.0)
                ))
        self.segments = segments
        self._merge_overlapping_segments()
        logger.info("TrackNet segments built: %d", len(self.segments))
        return self.segments

    def _start_segment(self, timestamp: float):
        adjusted_start = max(0.0, timestamp - self.padding_before)
        self.current_segment = Segment(
            start_time=adjusted_start,
            end_time=timestamp,
            start_frame=int(adjusted_start * self.video_fps),
            end_frame=int(timestamp * self.video_fps),
            valid=True
        )

    def _end_segment(self, timestamp: float):
        if self.current_segment is None:
            return
        adjusted_end = min(self.video_duration, timestamp + self.padding_after)
        self.current_segment.end_time = adjusted_end
        self.current_segment.end_frame = int(adjusted_end * self.video_fps)
        if self.current_segment.duration >= self.min_segment_duration:
            self.segments.append(self.current_segment)
        self.current_segment = None

    def _filter_segments(self, segments: List[Segment]) -> List[Segment]:
        filtered = []
        for seg in segments:
            duration = seg.end_time - seg.start_time
            if self.min_segment_duration <= duration <= self.max_segment_duration:
                seg.valid = True
                filtered.append(seg)
            else:
                seg.valid = False
        return filtered

    def _merge_overlapping_segments(self):
        if len(self.segments) < 2:
            return
        self.segments.sort(key=lambda x: x.start_time)
        merged = [self.segments[0]]
        for seg in self.segments[1:]:
            last = merged[-1]
            if seg.start_time <= last.end_time:
                last.end_time = max(last.end_time, seg.end_time)
                last.end_frame = int(last.end_time * max(1.0, self.video_fps))
                last.confidence = max(last.confidence or 0.0, seg.confidence or 0.0)
            else:
                merged.append(seg)
        self.segments = merged

    def filter_by_duration(self, min_duration: Optional[float] = None,
                           max_duration: Optional[float] = None) -> List[Segment]:
        min_d = float(min_duration) if min_duration is not None else self.min_segment_duration
        max_d = float(max_duration) if max_duration is not None else self.max_segment_duration
        return [seg for seg in self.segments
                if min_d <= (seg.end_time - seg.start_time) <= max_d]

    def get_segment_stats(self) -> dict:
        if not self.segments:
            return {
                'total_segments': 0,
                'total_duration': 0,
                'avg_duration': 0,
                'min_duration': 0,
                'max_duration': 0
            }
        durations = [seg.end_time - seg.start_time for seg in self.segments]
        return {
            'total_segments': len(self.segments),
            'total_duration': sum(durations),
            'avg_duration': sum(durations) / len(durations),
            'min_duration': min(durations),
            'max_duration': max(durations)
        }

    def save_segments_to_file(self, file_path: str):
        import json
        segments_data = [{
            'start_time': seg.start_time,
            'end_time': seg.end_time,
            'duration': seg.end_time - seg.start_time,
            'start_frame': seg.start_frame,
            'end_frame': seg.end_frame,
            'valid': seg.valid,
            'confidence': seg.confidence,
        } for seg in self.segments]
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(segments_data, f, indent=2, ensure_ascii=False)

    def load_segments_from_file(self, file_path: str) -> List[Segment]:
        import json
        with open(file_path, 'r', encoding='utf-8') as f:
            segments_data = json.load(f)
        self.segments = [Segment(
            start_time=float(seg['start_time']),
            end_time=float(seg['end_time']),
            start_frame=int(seg['start_frame']),
            end_frame=int(seg['end_frame']),
            valid=seg.get('valid', True),
            confidence=float(seg.get('confidence') or 0.0),
        ) for seg in segments_data]
        return self.segments
