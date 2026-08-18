import os
import sys
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from typing import Optional, Callable, Dict, Any

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from web.models import (
    update_task_progress,
    update_task_completed,
    update_task_started,
    get_task,
)

MAX_WORKERS = int(os.getenv('TASK_WORKERS', '2'))


class TaskQueue:
    def __init__(self, max_workers: int = MAX_WORKERS):
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='badminton-task')
        self._futures: Dict[str, Future] = {}
        self._lock = threading.Lock()
        self._subscribers: Dict[str, list] = {}
        self._subscribers_lock = threading.Lock()
        logger.info('TaskQueue initialized with %d workers', max_workers)

    def _make_progress_callback(self, upload_id: str, stage_weights: Dict[str, float]):
        detect_weight = stage_weights.get('detect', 0.35)
        cut_weight = stage_weights.get('cut', 0.60)
        merge_weight = stage_weights.get('merge', 0.05)
        stage_state = {'detect_done': False, 'cut_done': 0, 'cut_total': 0}

        def callback(stage: str, progress: float, message: str):
            if stage == 'detect':
                overall = progress * detect_weight
                msg = f'🎯 {message}'
                update_task_progress(upload_id, overall, msg, status='processing')
            elif stage == 'cut':
                done = stage_state['cut_done']
                total = stage_state['cut_total']
                if total > 0:
                    cut_progress = (done + progress) / total
                    overall = detect_weight + cut_progress * cut_weight
                else:
                    overall = detect_weight + progress * cut_weight
                msg = f'✂️ {message}'
                update_task_progress(upload_id, overall, msg, status='processing')
            elif stage == 'cut_done_one':
                stage_state['cut_done'] = stage_state.get('cut_done', 0) + 1
            elif stage == 'cut_total':
                stage_state['cut_total'] = int(progress)
            elif stage == 'merge':
                overall = detect_weight + cut_weight + progress * merge_weight
                msg = f'📦 {message}'
                update_task_progress(upload_id, overall, msg, status='processing')
            self._notify_subscribers(upload_id)
        return callback

    def _notify_subscribers(self, upload_id: str):
        with self._subscribers_lock:
            subs = self._subscribers.get(upload_id, [])
            if not subs:
                return
            task = get_task(upload_id)
            if not task:
                return
            event_data = self._format_sse_event(task)
            dead = []
            for q in subs:
                try:
                    q.put_nowait(event_data)
                except Exception:
                    dead.append(q)
            for q in dead:
                if q in subs:
                    subs.remove(q)

    def _format_sse_event(self, task: dict) -> str:
        data = {
            'upload_id': task['upload_id'],
            'status': task.get('status', 'pending'),
            'progress': float(task.get('progress') or 0.0),
            'progress_message': task.get('progress_message') or '',
            'output_count': task.get('output_count') or 0,
            'total_segments': task.get('total_segments') or 0,
        }
        if task.get('error'):
            data['error'] = task['error']
        if task.get('result_data'):
            try:
                data['result'] = json.loads(task['result_data'])
            except Exception:
                pass
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    def subscribe(self, upload_id: str):
        import queue
        q = queue.Queue(maxsize=100)
        with self._subscribers_lock:
            if upload_id not in self._subscribers:
                self._subscribers[upload_id] = []
            self._subscribers[upload_id].append(q)
        return q

    def unsubscribe(self, upload_id: str, q):
        with self._subscribers_lock:
            if upload_id in self._subscribers:
                try:
                    self._subscribers[upload_id].remove(q)
                except ValueError:
                    pass
                if not self._subscribers[upload_id]:
                    del self._subscribers[upload_id]

    def submit(self, upload_id: str, input_path: str, output_dir_abs: str,
               min_duration: Optional[float] = None,
               wechat_name: str = None, user_name: str = None, task_name: str = None):
        with self._lock:
            if upload_id in self._futures:
                future = self._futures[upload_id]
                if not future.done():
                    logger.warning('Task %s already running', upload_id)
                    return False
        update_task_started(upload_id)
        future = self.executor.submit(
            self._run_task, upload_id, input_path, output_dir_abs,
            min_duration, wechat_name, user_name, task_name
        )
        with self._lock:
            self._futures[upload_id] = future

        def _cleanup(fut):
            with self._lock:
                self._futures.pop(upload_id, None)
            with self._subscribers_lock:
                self._subscribers.pop(upload_id, None)
        future.add_done_callback(_cleanup)
        return True

    def _run_task(self, upload_id, input_path, output_dir_abs,
                  min_duration, wechat_name, user_name, task_name):
        logger.info('[Task %s] started processing', upload_id)
        output_files = []
        total_segments = 0
        try:
            from src.segmenter import VideoSegmenter
            from src.video_cutter import VideoCutter

            progress_cb = self._make_progress_callback(upload_id, {
                'detect': 0.35,
                'cut': 0.60,
                'merge': 0.05,
            })

            def segment_progress(p, msg):
                progress_cb('detect', p, msg)

            segmenter = VideoSegmenter(
                min_segment_duration=3.0,
                max_segment_duration=60.0,
                padding_before=0.5,
                padding_after=0.5
            )

            progress_cb('detect', 0.02, '视频打开中…')
            segments = segmenter.process_video(
                input_path,
                use_tracknet=False,
                progress_callback=segment_progress
            )
            stats = segmenter.get_segment_stats()
            total_segments = stats['total_segments']
            logger.info('[Task %s] segmented: %s', upload_id, stats)

            filtered_segments = segments
            cut_min_duration = None
            if min_duration is not None:
                try:
                    cut_min_duration = float(min_duration)
                    filtered_segments = [s for s in segments if (s.end_time - s.start_time) >= cut_min_duration]
                except (ValueError, TypeError):
                    pass

            video_cutter = VideoCutter(
                output_format='mp4',
                codec=None,
                quality=23
            )

            valid_segments = [s for s in filtered_segments if s.valid and s.end_time > s.start_time]
            merged = video_cutter._merge_adjacent_segments(valid_segments, max_gap=0.3)
            progress_cb('cut_total', float(len(merged)), f'共检测到 {len(merged)} 个精彩片段，开始切片…')

            cut_success_count = 0
            lock = threading.Lock()

            def cut_progress(done, total, seg):
                nonlocal cut_success_count
                with lock:
                    progress_cb('cut_done_one', 0, f'正在导出片段 {done}/{total}')

            os.makedirs(output_dir_abs, exist_ok=True)
            input_filename = os.path.splitext(os.path.basename(input_path))[0]
            cut_tasks = []
            for i, seg in enumerate(merged):
                output_filename = f"segment_{input_filename}_{i:04d}.mp4"
                output_path = os.path.join(output_dir_abs, output_filename)
                cut_tasks.append((i, seg, output_path))

            def do_cut(index, seg, out_path):
                ok = video_cutter.cut_segment(input_path, seg, out_path)
                with lock:
                    return index, ok, seg, out_path

            from concurrent.futures import ThreadPoolExecutor as CutPool
            workers = video_cutter._default_workers(len(cut_tasks))
            done_count = 0

            with CutPool(max_workers=workers) as cut_pool:
                futures_map = {cut_pool.submit(do_cut, i, s, o): (i, s, o) for i, s, o in cut_tasks}
                for fut in as_completed(futures_map):
                    i, ok, seg, out_path = fut.result()
                    done_count += 1
                    if ok:
                        cut_success_count += 1
                    progress_cb('cut', done_count / len(cut_tasks), f'已导出 {done_count}/{len(cut_tasks)} 个片段')

            progress_cb('merge', 0.0, '正在整理输出文件…')
            if os.path.exists(output_dir_abs):
                for f in sorted(os.listdir(output_dir_abs)):
                    if f.endswith('.mp4'):
                        file_path = os.path.join(output_dir_abs, f)
                        duration = self._get_video_duration(file_path)
                        output_files.append({
                            'name': f,
                            'url': f'/outputs/{upload_id}/{f}',
                            'size': os.path.getsize(file_path),
                            'duration': duration,
                            'duration_str': self._format_duration(duration)
                        })

            import sys as _sys
            if PROJECT_ROOT not in _sys.path:
                _sys.path.insert(0, PROJECT_ROOT)
            encoder_speed_ratio = float(getattr(video_cutter, 'encoder_speed_ratio', 1.0))
            encoder_label = getattr(video_cutter, 'encoder_name_label', None) or 'libx264 (CPU)'

            estimate_payload = self._estimate_plan(
                segmenter.video_duration,
                width=getattr(segmenter, 'video_width', None),
                height=getattr(segmenter, 'video_height', None),
                encoder_speed_ratio=encoder_speed_ratio,
                min_duration_filter=cut_min_duration,
            )
            estimate_payload.update({
                'best_effort_human': self._format_minutes_str(estimate_payload['best_effort_seconds']),
                'low_human': self._format_minutes_str(estimate_payload['low_seconds']),
                'high_human': self._format_minutes_str(estimate_payload['high_seconds']),
                'source_duration_human': self._format_minutes_str(segmenter.video_duration),
                'budget_human': self._format_minutes_str(180),
                'encoder_label': encoder_label,
            })

            result = {
                'upload_id': upload_id,
                'status': 'completed',
                'total_segments': total_segments,
                'success_count': cut_success_count,
                'output_files': output_files,
                'output_dir': f'/outputs/{upload_id}',
                'wechat_name': wechat_name,
                'user_name': user_name,
                'task_name': task_name,
                'processing_estimate': estimate_payload,
            }

            progress_cb('merge', 1.0, f'🎉 处理完成！共导出 {len(output_files)} 个精彩片段')
            update_task_progress(upload_id, 1.0, f'🎉 处理完成！共导出 {len(output_files)} 个精彩片段',
                                 status='completed', total_segments=len(output_files))
            update_task_completed(upload_id, 'completed', len(output_files), result_data=result)
            self._notify_subscribers(upload_id)
            logger.info('[Task %s] completed: %d segments', upload_id, len(output_files))
            return result

        except Exception as e:
            logger.exception('[Task %s] failed: %s', upload_id, e)
            import traceback
            err_msg = str(e)
            update_task_progress(upload_id, 0, f'❌ 处理失败: {err_msg}', status='failed')
            update_task_completed(upload_id, 'failed', 0, error=err_msg)
            self._notify_subscribers(upload_id)
            return None

    def _get_video_duration(self, filepath):
        try:
            import subprocess
            command = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_format', filepath
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
            info = json.loads(result.stdout)
            return float(info['format']['duration'])
        except Exception:
            return None

    def _format_duration(self, seconds):
        if seconds is None:
            return None
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}:{secs:02d}"

    def _format_minutes_str(self, seconds):
        if seconds is None:
            return None
        s = int(seconds)
        mins = s // 60
        secs = s % 60
        if mins and secs:
            return f"{mins} 分 {secs} 秒"
        if mins:
            return f"{mins} 分钟"
        return f"{secs} 秒"

    def _estimate_plan(self, source_duration_seconds, width=None, height=None,
                       encoder_speed_ratio=1.0, worker_count=None,
                       min_duration_filter=None):
        BASE_DETECT_RATIO = 0.08
        BASE_CUT_RATIO = 0.22
        BUDGET_SECONDS = 180
        duration = max(0.0, float(source_duration_seconds or 0))
        speed = max(0.1, float(encoder_speed_ratio or 1.0))
        workers = float(worker_count) if worker_count and int(worker_count) > 0 else 4.0

        def resolution_penalty(w, h):
            if not w or not h:
                return 1.0
            pixels = int(w) * int(h)
            if pixels <= 1280 * 720:
                return 0.9
            if pixels <= 1920 * 1080:
                return 1.0
            if pixels <= 2560 * 1440:
                return 1.4
            return 1.9

        resolution = resolution_penalty(width, height)
        detect_ratio = BASE_DETECT_RATIO
        cut_ratio = BASE_CUT_RATIO / speed / workers
        total_ratio = (detect_ratio + cut_ratio) * max(0.55, resolution)
        estimate = duration * total_ratio
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

        return {
            'budget_processing_seconds': BUDGET_SECONDS,
            'recommended_max_minutes': 10,
            'hard_max_seconds': 1800,
            'source_duration_seconds': round(duration, 2),
            'best_effort_seconds': round(best, 1),
            'low_seconds': round(low, 1),
            'high_seconds': round(high, 1),
            'exceeds_hard_limit': duration > 1800,
            'exceeds_recommended': duration > 600,
            'min_duration_filter_applied_seconds': float(min_duration_filter) if min_duration_filter else None,
        }


task_queue = TaskQueue()
