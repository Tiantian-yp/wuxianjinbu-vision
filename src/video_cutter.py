import subprocess
import os
import shutil
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from fractions import Fraction
from typing import List, Optional, Callable, Dict
from .segmenter import Segment

logger = logging.getLogger(__name__)


MAX_DURATION_LIMIT_SECONDS = 15 * 60
BASE_DETECT_RATIO = 0.08
BASE_CUT_RATIO = 0.22


def _estimate_interval(estimate: float):
    center = max(8.0, float(estimate))
    low = center * 0.42
    best = center
    high = center * 1.2
    if high - low < 10.0:
        pad = (10.0 - (high - low)) / 2.0
        low = max(5.0, low - pad)
        high = low + 10.0
    if best <= low:
        best = low * 1.3
    if high <= best:
        high = best * 1.25
    return low, best, high


class VideoCutter:
    SOFTWARE_H264 = 'libx264'
    DEFAULT_QUALITY = 23

    def __init__(self, output_format: str = 'mp4', codec: Optional[str] = None, quality: int = DEFAULT_QUALITY):
        self.output_format = output_format
        self.user_requested_codec = codec
        self.quality = int(quality) if quality is not None else self.DEFAULT_QUALITY
        self._check_ffmpeg()
        self.video_encoder_info = self._detect_video_encoder()
        self.audio_encoder = 'aac'
        self.lock = Lock()
        logger.info(
            'VideoCutter initialized: video_encoder=%s (fallback=%s), quality=%d, format=%s',
            self.video_encoder, self.SOFTWARE_H264, self.quality, self.output_format
        )

    @property
    def video_encoder(self) -> str:
        return self.video_encoder_info['encoder']

    @property
    def encoder_preset(self) -> Optional[str]:
        return self.video_encoder_info.get('preset')

    @property
    def encoder_name_label(self) -> str:
        return self.video_encoder_info.get('label') or self.video_encoder

    @property
    def encoder_speed_ratio(self) -> float:
        return float(self.video_encoder_info.get('speed_ratio', 1.0))

    def _run(self, cmd: List[str]) -> subprocess.CompletedProcess:
        logger.debug('subprocess.run: %s', ' '.join(cmd))
        return subprocess.run(cmd, capture_output=True, text=True, check=True)

    def _check_ffmpeg(self):
        missing = []
        for binary in ('ffmpeg', 'ffprobe'):
            try:
                self._run([binary, '-version'])
            except (FileNotFoundError, subprocess.CalledProcessError):
                missing.append(binary)
        if missing:
            raise RuntimeError(f"Required binaries missing: {', '.join(missing)}. Please install FFmpeg.")

    def _encoder_candidates(self) -> List[Dict]:
        candidates = []
        candidates.append({
            'encoder': self.SOFTWARE_H264,
            'label': 'libx264 (CPU)',
            'preset': 'medium',
            'probe_args': ['-hide_banner', '-f', 'lavfi', '-i', 'color=c=black:s=16x16:d=0.04', '-frames:v', '1', '-c:v', self.SOFTWARE_H264, '-preset', 'medium', '-f', 'null', '-'],
            'speed_ratio': 1.0,
        })
        # macOS VideoToolbox
        candidates.append({
            'encoder': 'h264_videotoolbox',
            'label': 'VideoToolbox (macOS GPU)',
            'preset': None,
            'probe_args': ['-hide_banner', '-f', 'lavfi', '-i', 'color=c=black:s=16x16:d=0.04', '-frames:v', '1', '-c:v', 'h264_videotoolbox', '-realtime', '1', '-f', 'null', '-'],
            'speed_ratio': 2.8,
        })
        # NVIDIA NVENC
        candidates.append({
            'encoder': 'h264_nvenc',
            'label': 'NVENC (NVIDIA GPU)',
            'preset': 'p4',
            'probe_args': ['-hide_banner', '-f', 'lavfi', '-i', 'color=c=black:s=16x16:d=0.04', '-frames:v', '1', '-c:v', 'h264_nvenc', '-preset', 'p4', '-f', 'null', '-'],
            'speed_ratio': 3.0,
        })
        # Intel QSV
        candidates.append({
            'encoder': 'h264_qsv',
            'label': 'QSV (Intel GPU)',
            'preset': 'medium',
            'probe_args': ['-hide_banner', '-f', 'lavfi', '-i', 'color=c=black:s=16x16:d=0.04', '-frames:v', '1', '-c:v', 'h264_qsv', '-preset', 'medium', '-f', 'null', '-'],
            'speed_ratio': 2.2,
        })
        return candidates

    def _detect_video_encoder(self) -> Dict:
        if self.user_requested_codec:
            logger.info('using user-requested video encoder: %s', self.user_requested_codec)
            return {
                'encoder': self.user_requested_codec,
                'label': f'{self.user_requested_codec} (user)',
                'preset': self._infer_preset_for_encoder(self.user_requested_codec),
                'speed_ratio': 1.2 if self.user_requested_codec != self.SOFTWARE_H264 else 1.0,
            }
        for candidate in self._encoder_candidates():
            try:
                self._run(['ffmpeg'] + candidate['probe_args'])
                logger.info('detected usable video encoder: %s', candidate['label'])
                return candidate
            except (FileNotFoundError, subprocess.CalledProcessError) as e:
                logger.debug('encoder probe failed for %s: %s', candidate['encoder'], e)
                continue
        logger.warning('no hardware encoder available, fallback to libx264 (CPU)')
        return {
            'encoder': self.SOFTWARE_H264,
            'label': 'libx264 (CPU, fallback)',
            'preset': 'medium',
            'speed_ratio': 1.0,
        }

    def _infer_preset_for_encoder(self, encoder: str) -> Optional[str]:
        if encoder == 'h264_nvenc':
            return 'p4'
        if encoder in ('h264_qsv', self.SOFTWARE_H264):
            return 'medium'
        return None

    def _format_time(self, seconds: float) -> str:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:.3f}"

    def _build_encode_args(self, start_time_str: str, duration: float) -> List[str]:
        args = [
            'ffmpeg',
            '-y',
            '-hide_banner',
            '-loglevel', 'error',
            '-stats',
        ]
        args += ['-ss', start_time_str]
        return args

    def cut_segment(self, input_path: str, segment: Segment, output_path: str) -> bool:
        duration = max(0.0, float(segment.end_time) - float(segment.start_time))
        if duration <= 0:
            logger.warning('skip invalid segment: %s -> %s', segment.start_time, segment.end_time)
            return False
        start_time = self._format_time(segment.start_time)

        command = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-ss', start_time,
            '-i', input_path,
            '-t', f'{duration:.4f}',
            '-c:v', self.video_encoder,
        ]
        if self.video_encoder == 'h264_videotoolbox':
            command += ['-realtime', '1', '-allow_sw', '1', '-b:v', '5000k']
        preset = self.encoder_preset
        if preset:
            command += ['-preset', preset]
        if self.video_encoder == self.SOFTWARE_H264:
            command += ['-crf', str(self.quality)]
        else:
            target_kbps = 3200
            command += ['-b:v', f'{target_kbps}k']
        command += [
            '-c:a', self.audio_encoder,
            '-b:a', '128k',
            '-reset_timestamps', '1',
            '-movflags', '+faststart',
            '-fflags', '+genpts',
            output_path,
        ]
        try:
            self._run(command)
            valid = self._validate_output(output_path, expected_duration=duration)
            if not valid:
                logger.error('output validation failed: %s', output_path)
                if os.path.exists(output_path):
                    os.remove(output_path)
                return False
            return True
        except subprocess.CalledProcessError as e:
            logger.error('cut_segment failed for %s: %s', os.path.basename(output_path), e.stderr.strip().splitlines()[-1] if e.stderr else e)
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass
            return False

    def _validate_output(self, output_path: str, expected_duration: float) -> bool:
        info = self.get_video_info(output_path)
        if not info:
            return False
        duration = float(info['duration'])
        ratio = duration / max(0.001, expected_duration)
        if not 0.85 <= ratio <= 1.15:
            logger.warning('output duration ratio out of range: %.3f (%s vs %s)', ratio, duration, expected_duration)
            return False
        size = int(info.get('size') or 0)
        if size <= 10240:
            logger.warning('output file too small: %d bytes for %s', size, os.path.basename(output_path))
            return False
        return True

    def _merge_adjacent_segments(self, segments: List[Segment], max_gap: float = 0.3) -> List[Segment]:
        if len(segments) <= 1:
            return segments.copy()
        sorted_segments = sorted(
            [s for s in segments if s.valid and s.end_time > s.start_time],
            key=lambda s: s.start_time
        )
        merged: List[Segment] = []
        for seg in sorted_segments:
            if not merged:
                merged.append(seg)
                continue
            last = merged[-1]
            gap = seg.start_time - last.end_time
            if gap <= max_gap:
                last.end_time = max(last.end_time, seg.end_time)
                last.end_frame = max(last.end_frame, seg.end_frame)
                last.confidence = max(last.confidence, seg.confidence)
            else:
                merged.append(seg)
        return merged

    def _default_workers(self, segment_count: int) -> int:
        try:
            cpu = os.cpu_count() or 2
        except Exception:
            cpu = 2
        max_workers = max(2, min(cpu, 8))
        return min(max_workers, max(1, segment_count))

    def cut_all_segments(self, input_path: str, segments: List[Segment],
                         output_dir: str, prefix: str = 'segment',
                         workers: Optional[int] = None,
                         progress_callback: Optional[Callable[[int, int, Segment], None]] = None) -> int:
        os.makedirs(output_dir, exist_ok=True)
        input_filename = os.path.splitext(os.path.basename(input_path))[0]
        valid_segments = [s for s in segments if s.valid and s.end_time > s.start_time]
        if not valid_segments:
            logger.warning('no valid segments to cut')
            return 0
        merged = self._merge_adjacent_segments(valid_segments, max_gap=0.3)
        tasks = []
        for i, segment in enumerate(merged):
            output_filename = f"{prefix}_{input_filename}_{i:04d}.{self.output_format}"
            output_path = os.path.join(output_dir, output_filename)
            tasks.append((i, segment, output_path))
        total = len(tasks)
        worker_count = int(workers) if workers and workers > 0 else self._default_workers(total)
        logger.info('cutting %d segments with %d workers (merged from %d valid)', total, worker_count, len(valid_segments))
        success_count = 0
        done = 0
        lock = Lock()

        def task(index: int, seg: Segment, out_path: str):
            logger.info('cut segment %d/%d: %s - %s (%.1fs)', index + 1, total,
                        self._format_time(seg.start_time), self._format_time(seg.end_time),
                        seg.end_time - seg.start_time)
            ok = self.cut_segment(input_path, seg, out_path)
            return index, ok, seg

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(task, i, seg, out): (i, seg, out)
                for i, seg, out in tasks
            }
            for future in as_completed(future_map):
                index, ok, seg = future.result()
                with lock:
                    done += 1
                    if ok:
                        success_count += 1
                    if progress_callback:
                        try:
                            progress_callback(done, total, seg)
                        except Exception:
                            logger.exception('progress_callback raised')
        logger.info('cutting complete: success=%d/%d', success_count, total)
        return success_count

    def cut_segments_with_filter(self, input_path: str, segments: List[Segment],
                                 output_dir: str, min_duration: float = None,
                                 max_duration: float = None, prefix: str = 'segment',
                                 workers: Optional[int] = None,
                                 progress_callback: Optional[Callable[[int, int, Segment], None]] = None) -> int:
        filtered = segments.copy()
        if min_duration is not None:
            filtered = [s for s in filtered if (s.end_time - s.start_time) >= float(min_duration)]
        if max_duration is not None:
            filtered = [s for s in filtered if (s.end_time - s.start_time) <= float(max_duration)]
        logger.info('Filtered %d segments to %d segments (min=%s, max=%s)',
                    len(segments), len(filtered), min_duration, max_duration)
        return self.cut_all_segments(input_path, filtered, output_dir, prefix,
                                     workers=workers, progress_callback=progress_callback)

    def preview_segment(self, input_path: str, segment: Segment, preview_duration: float = 5.0) -> bool:
        preview_end = min(segment.start_time + preview_duration, segment.end_time)
        duration = preview_end - segment.start_time
        if duration <= 0:
            return False
        command = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-ss', self._format_time(segment.start_time),
            '-i', input_path,
            '-t', str(duration),
            '-c:v', self.video_encoder,
            '-c:a', 'copy',
            '-movflags', '+faststart',
            '-f', 'mp4',
            '-',
        ]
        try:
            self._run(command)
            return True
        except subprocess.CalledProcessError as e:
            logger.error('preview_segment failed: %s', e)
            return False

    def convert_to_mp4(self, input_path: str, output_path: str = None) -> bool:
        if output_path is None:
            output_path = os.path.splitext(input_path)[0] + '.mp4'
        command = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-i', input_path,
            '-c:v', self.video_encoder,
        ]
        preset = self.encoder_preset
        if preset:
            command += ['-preset', preset]
        if self.video_encoder == self.SOFTWARE_H264:
            command += ['-crf', str(self.quality)]
        command += [
            '-c:a', self.audio_encoder,
            '-b:a', '128k',
            '-movflags', '+faststart',
            output_path,
        ]
        try:
            self._run(command)
            logger.info('Successfully converted %s to %s', input_path, output_path)
            return True
        except subprocess.CalledProcessError as e:
            logger.error('convert_to_mp4 failed for %s: %s', input_path, e)
            return False

    def get_video_info(self, input_path: str) -> Optional[dict]:
        command = [
            'ffprobe',
            '-v', 'quiet',
            '-print_format', 'json',
            '-show_format',
            '-show_streams',
            input_path
        ]
        try:
            result = self._run(command)
            import json
            info = json.loads(result.stdout)

            video_stream = None
            for stream in info.get('streams', []):
                if stream.get('codec_type') == 'video':
                    video_stream = stream
                    break

            fps = None
            if video_stream:
                r_frame_rate = video_stream.get('r_frame_rate') or video_stream.get('avg_frame_rate') or '0/1'
                try:
                    fps = float(Fraction(r_frame_rate))
                except Exception:
                    fps = None
            return {
                'duration': float(info['format']['duration']),
                'size': int(info['format']['size']),
                'bit_rate': int(info['format']['bit_rate']) if info['format'].get('bit_rate') else None,
                'video_codec': video_stream.get('codec_name') if video_stream else None,
                'width': int(video_stream.get('width')) if video_stream and video_stream.get('width') else None,
                'height': int(video_stream.get('height')) if video_stream and video_stream.get('height') else None,
                'fps': fps,
            }
        except Exception as e:
            logger.error('get_video_info failed for %s: %s', input_path, e)
            return None

    @classmethod
    def estimate_process_seconds(cls, source_duration_seconds: float,
                                 encoder_speed_ratio: float = 1.0,
                                 worker_count: Optional[int] = None,
                                 resolution_penalty: float = 1.0) -> Dict[str, float]:
        duration = max(0.0, float(source_duration_seconds))
        speed = max(0.1, float(encoder_speed_ratio))
        workers = float(worker_count) if worker_count and worker_count > 0 else 4.0
        detect_ratio = BASE_DETECT_RATIO
        cut_ratio = BASE_CUT_RATIO / speed / workers
        total_ratio = (detect_ratio + cut_ratio) * max(0.55, float(resolution_penalty))
        estimate = duration * total_ratio
        low, best, high = _estimate_interval(estimate)
        return {
            'low_seconds': round(low, 1),
            'high_seconds': round(high, 1),
            'best_effort_seconds': round(best, 1),
            'warning_cutoff_exceeded': duration > MAX_DURATION_LIMIT_SECONDS,
        }
