import os
import json
import uuid
import shutil
import logging
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)

UPLOAD_DIR = os.getenv('UPLOAD_DIR', os.path.join(BASE_DIR, 'uploads'))
OUTPUT_DIR = os.getenv('OUTPUT_DIR', os.path.join(BASE_DIR, 'outputs'))
MAX_CONTENT_MB = int(os.getenv('MAX_CONTENT_MB', '600'))
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '5000'))
DEBUG = os.getenv('FLASK_DEBUG', 'false').lower() in ('1', 'true', 'yes')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_DIR
app.config['OUTPUT_FOLDER'] = OUTPUT_DIR
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_MB * 1024 * 1024

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'qt', 'm4v', '3gp', 'avi', 'mkv', 'flv', 'wmv', 'webm', 'mts', 'm2ts'}
RECOMMENDED_MAX_DURATION_MINUTES = 10
HARD_MAX_DURATION_SECONDS = 30 * 60
BUDGET_SECONDS_FOR_PROCESS = 180
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

from web.models import (
    init_db,
    create_task,
    update_task_completed,
    list_tasks,
    list_task_names,
    get_task,
    delete_task,
    add_comment,
    list_comments,
)

init_db()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger('badminton-web')


def _require_wechat_name():
    header_value = request.headers.get('X-Wechat-Name') or ''
    try:
        from urllib.parse import unquote
        header_value = unquote(header_value or '').strip()
    except Exception:
        header_value = header_value.strip()
    value = header_value or (request.args.get('wechat_name') or '').strip()
    if not value and request.is_json:
        body = request.get_json(silent=True) or {}
        value = (body.get('wechat_name') or '').strip()
    if not value and request.method == 'POST' and request.form:
        value = (request.form.get('wechat_name') or '').strip()
    return value or None


def _format_comment_time(created_at):
    try:
        if created_at.endswith('Z'):
            dt = datetime.fromisoformat(created_at[:-1])
        else:
            dt = datetime.fromisoformat(created_at)
    except Exception:
        return created_at
    return dt.strftime('%Y-%m-%d %H:%M')


def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_file_info(filepath):
    if os.path.exists(filepath):
        stat = os.stat(filepath)
        return {
            'name': os.path.basename(filepath),
            'size': stat.st_size,
            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat()
        }
    return None


def get_video_duration(filepath):
    try:
        import subprocess
        command = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', filepath
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        return float(info['format']['duration'])
    except Exception as e:
        logger.warning(f'Failed to get duration for {filepath}: {e}')
        return None


def format_duration(seconds):
    if seconds is None:
        return None
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins}:{secs:02d}"


def format_minutes_str(seconds):
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


def format_clock_minutes(seconds):
    total = max(0, int(round(float(seconds or 0))))
    hours = total // 3600
    mins = (total % 3600) // 60
    secs = total % 60
    if hours:
        return f"{hours:01d}:{mins:02d}:{secs:02d}"
    return f"{mins:01d}:{secs:02d}"


def get_video_meta(filepath):
    try:
        import subprocess
        command = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', filepath
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        duration = float(info['format']['duration'])
        size = int(info['format']['size'])
        width = height = fps = None
        for stream in info.get('streams', []):
            if stream.get('codec_type') != 'video':
                continue
            width = int(stream['width']) if stream.get('width') else None
            height = int(stream['height']) if stream.get('height') else None
            rate = stream.get('r_frame_rate') or stream.get('avg_frame_rate')
            if rate:
                from fractions import Fraction
                try:
                    fps = float(Fraction(rate))
                except Exception:
                    fps = None
        return {
            'duration': duration,
            'size': size,
            'width': width,
            'height': height,
            'fps': fps,
        }
    except Exception as e:
        logger.warning(f'Failed to get video meta for {filepath}: {e}')
        return None


def _resolution_penalty(width, height):
    if not width or not height:
        return 1.0
    pixels = int(width) * int(height)
    if pixels <= 1280 * 720:
        return 0.9
    if pixels <= 1920 * 1080:
        return 1.0
    if pixels <= 2560 * 1440:
        return 1.4
    return 1.9


def estimate_process_plan(source_duration_seconds, width=None, height=None,
                          encoder_speed_ratio=1.0, worker_count=None,
                          min_duration_filter=None):
    import sys
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    try:
        from src.video_cutter import VideoCutter
    except Exception:
        VideoCutter = None

    duration = max(0.0, float(source_duration_seconds or 0))
    speed = max(0.1, float(encoder_speed_ratio or 1.0))
    workers = float(worker_count) if worker_count and int(worker_count) > 0 else 4.0
    resolution = _resolution_penalty(width, height)
    detect_ratio = BASE_DETECT_RATIO
    cut_ratio = BASE_CUT_RATIO / speed / workers
    total_ratio = (detect_ratio + cut_ratio) * max(0.55, resolution)
    estimate = duration * total_ratio
    low, best_effort, high = _estimate_interval(estimate)
    budget = float(BUDGET_SECONDS_FOR_PROCESS)

    def recommended_max_minutes(speed_ratio, reso):
        baseline_workers = 4.0
        best_ratio = (BASE_DETECT_RATIO + BASE_CUT_RATIO / max(0.1, speed_ratio) / baseline_workers) * max(0.55, reso)
        safe_seconds = int(budget / max(0.001, best_ratio))
        if safe_seconds <= 0:
            return RECOMMENDED_MAX_DURATION_MINUTES
        minutes = max(4, safe_seconds // 60)
        if minutes < RECOMMENDED_MAX_DURATION_MINUTES:
            return minutes
        return RECOMMENDED_MAX_DURATION_MINUTES

    recommended_minutes = recommended_max_minutes(speed, resolution)
    exceed_msg = None
    if duration > recommended_minutes * 60:
        exceed_msg = f"建议上传不超过 {recommended_minutes} 分钟的视频，以确保整体处理在 3 分钟内完成"
    hard_exceeded = duration > HARD_MAX_DURATION_SECONDS

    if VideoCutter is not None:
        try:
            base = VideoCutter.estimate_process_seconds(
                duration,
                encoder_speed_ratio=speed,
                worker_count=int(workers),
                resolution_penalty=resolution,
            )
            low = max(low, base['low_seconds'])
            best_effort = max(best_effort, base['best_effort_seconds'])
            high = max(high, base['high_seconds'])
        except Exception:
            pass
    min_dur_filter = None
    if min_duration_filter is not None:
        try:
            min_dur_filter = float(min_duration_filter)
        except Exception:
            min_dur_filter = None
    return {
        'budget_processing_seconds': budget,
        'recommended_max_minutes': recommended_minutes,
        'hard_max_seconds': HARD_MAX_DURATION_SECONDS,
        'source_duration_seconds': round(duration, 2),
        'best_effort_seconds': round(best_effort, 1),
        'low_seconds': round(low, 1),
        'high_seconds': round(high, 1),
        'exceeds_hard_limit': hard_exceeded,
        'exceeds_recommended': duration > recommended_minutes * 60,
        'recommendation_message': exceed_msg,
        'min_duration_filter_applied_seconds': min_dur_filter,
    }


@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'time': datetime.utcnow().isoformat() + 'Z',
        'upload_dir': UPLOAD_DIR,
        'output_dir': OUTPUT_DIR
    }), 200


@app.route('/config')
def app_config():
    import sys
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    encoder_label = 'libx264 (CPU)'
    try:
        from src.video_cutter import VideoCutter
        try:
            cutter = VideoCutter(output_format='mp4', quality=23)
            encoder_label = cutter.encoder_name_label
        except Exception:
            encoder_label = '待启动处理后确认'
    except Exception:
        pass
    return jsonify({
        'recommended_max_duration_minutes': RECOMMENDED_MAX_DURATION_MINUTES,
        'hard_max_duration_seconds': HARD_MAX_DURATION_SECONDS,
        'budget_processing_seconds': BUDGET_SECONDS_FOR_PROCESS,
        'allowed_extensions': sorted(list(ALLOWED_EXTENSIONS)),
        'max_upload_mb': MAX_CONTENT_MB,
        'encoder_label': encoder_label,
    })


@app.route('/')
def index():
    return render_template('index.html',
                           recommended_max_duration_minutes=RECOMMENDED_MAX_DURATION_MINUTES,
                           budget_processing_seconds=BUDGET_SECONDS_FOR_PROCESS)


@app.route('/guide')
def guide():
    site_url = request.host_url.rstrip('/')
    return render_template('guide.html', site_url=site_url)


@app.route('/api/estimate', methods=['GET', 'POST'])
def estimate_process():
    import sys
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    try:
        from src.video_cutter import VideoCutter
    except Exception:
        VideoCutter = None

    payload = {}
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    safe_filename = request.args.get('safe_filename') or payload.get('safe_filename')
    upload_id = request.args.get('upload_id') or payload.get('upload_id')
    min_duration = request.args.get('min_duration') if request.method == 'GET' else payload.get('min_duration')
    duration_input = request.args.get('duration_seconds') if request.method == 'GET' else payload.get('duration_seconds')
    width_input = request.args.get('width') if request.method == 'GET' else payload.get('width')
    height_input = request.args.get('height') if request.method == 'GET' else payload.get('height')
    meta = None
    if safe_filename:
        input_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)
        if os.path.exists(input_path):
            meta = get_video_meta(input_path)
    elif upload_id:
        for f in os.listdir(app.config['UPLOAD_FOLDER']):
            if f.startswith(f'{upload_id}_'):
                input_path = os.path.join(app.config['UPLOAD_FOLDER'], f)
                meta = get_video_meta(input_path)
                break
    duration_seconds = None
    width = height = None
    size_bytes = None
    if meta:
        duration_seconds = meta.get('duration')
        width = meta.get('width')
        height = meta.get('height')
        size_bytes = meta.get('size')
    if duration_input is not None:
        try:
            duration_seconds = float(duration_input)
        except Exception:
            pass
    if width_input is not None:
        try:
            width = int(width_input)
        except Exception:
            pass
    if height_input is not None:
        try:
            height = int(height_input)
        except Exception:
            pass
    if duration_seconds is None:
        return jsonify({'error': '请先完成视频上传，再获取预估耗时信息'}), 400

    encoder_speed_ratio = 1.0
    encoder_label = 'libx264 (CPU)'
    if VideoCutter is not None:
        try:
            cutter = VideoCutter(output_format='mp4', quality=23)
            encoder_speed_ratio = cutter.encoder_speed_ratio
            encoder_label = cutter.encoder_name_label
        except Exception:
            pass
    plan = estimate_process_plan(duration_seconds, width=width, height=height,
                                 encoder_speed_ratio=encoder_speed_ratio,
                                 min_duration_filter=min_duration)
    plan.update({
        'source_size_bytes': size_bytes,
        'source_width': width,
        'source_height': height,
        'encoder_label': encoder_label,
        'best_effort_human': format_minutes_str(plan['best_effort_seconds']),
        'low_human': format_minutes_str(plan['low_seconds']),
        'high_human': format_minutes_str(plan['high_seconds']),
        'source_duration_human': format_minutes_str(duration_seconds),
        'budget_human': format_minutes_str(BUDGET_SECONDS_FOR_PROCESS),
    })
    return jsonify(plan), 200


@app.route('/api/upload', methods=['POST'])
def upload_video():
    if 'file' not in request.files:
        logger.warning('upload request missing file part')
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    wechat_name = _require_wechat_name()

    if not wechat_name:
        return jsonify({'error': '请先填写可爱的微信名再来上传哦 ✨'}), 400

    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'File type not allowed'}), 400

    upload_id = str(uuid.uuid4())
    filename = file.filename
    safe_filename = f"{upload_id}_{filename}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)
    file.save(filepath)

    upload_url = f'/uploads/{safe_filename}'
    try:
        upload_size = os.path.getsize(filepath)
    except OSError:
        upload_size = 0

    meta = get_video_meta(filepath)
    duration_seconds = meta.get('duration') if meta else None
    width = meta.get('width') if meta else None
    height = meta.get('height') if meta else None

    error = None
    hard_blocked = False
    if duration_seconds is not None:
        if duration_seconds > HARD_MAX_DURATION_SECONDS:
            error = f"视频时长 {format_minutes_str(duration_seconds)} 超过了硬上限 {HARD_MAX_DURATION_SECONDS // 60} 分钟，暂不支持"
            hard_blocked = True

    if hard_blocked:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except OSError:
            pass
        return jsonify({
            'error': error,
            'recommended_max_duration_minutes': RECOMMENDED_MAX_DURATION_MINUTES,
            'hard_max_duration_minutes': HARD_MAX_DURATION_SECONDS // 60,
            'duration_seconds': round(duration_seconds, 2),
            'duration_human': format_minutes_str(duration_seconds),
        }), 413

    create_task({
        'upload_id': upload_id,
        'wechat_name': wechat_name,
        'task_name': None,
        'user_name': None,
        'original_filename': filename,
        'safe_filename': safe_filename,
        'upload_url': upload_url,
        'upload_size': upload_size,
        'output_dir': f'/outputs/{upload_id}',
        'min_duration': None,
        'status': 'uploaded',
        'output_count': 0,
    })

    import sys
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    encoder_speed_ratio = 1.0
    try:
        from src.video_cutter import VideoCutter
        try:
            cutter = VideoCutter(output_format='mp4', quality=23)
            encoder_speed_ratio = cutter.encoder_speed_ratio
        except Exception:
            pass
    except Exception:
        pass

    estimate = None
    warning = None
    if duration_seconds is not None:
        estimate = estimate_process_plan(duration_seconds, width=width, height=height,
                                         encoder_speed_ratio=encoder_speed_ratio)
        if estimate.get('exceeds_recommended'):
            warning = estimate.get('recommendation_message')

    logger.info(f'uploaded file: {safe_filename} size={upload_size} duration={duration_seconds}s wechat_name={wechat_name}')

    response = {
        'upload_id': upload_id,
        'filename': filename,
        'safe_filename': safe_filename,
        'url': upload_url,
        'size': upload_size,
        'wechat_name': wechat_name,
        'duration_seconds': round(duration_seconds, 2) if duration_seconds is not None else None,
        'duration_human': format_minutes_str(duration_seconds) if duration_seconds is not None else None,
        'width': width,
        'height': height,
        'recommended_max_duration_minutes': RECOMMENDED_MAX_DURATION_MINUTES,
        'hard_max_duration_minutes': HARD_MAX_DURATION_SECONDS // 60,
        'warning': warning,
        'estimate': None,
    }
    if estimate:
        response['estimate'] = {
            **estimate,
            'best_effort_human': format_minutes_str(estimate['best_effort_seconds']),
            'low_human': format_minutes_str(estimate['low_seconds']),
            'high_human': format_minutes_str(estimate['high_seconds']),
            'source_duration_human': format_minutes_str(duration_seconds),
            'budget_human': format_minutes_str(BUDGET_SECONDS_FOR_PROCESS),
        }
    return jsonify(response), 200


@app.route('/api/process', methods=['POST'])
def process_video():
    data = request.get_json() or {}
    upload_id = data.get('upload_id')
    safe_filename = data.get('safe_filename')
    min_duration = data.get('min_duration')
    user_name = (data.get('user_name') or data.get('task_name') or '').strip() or None
    task_name = (data.get('task_name') or data.get('user_name') or '').strip() or None
    wechat_name = _require_wechat_name()

    if not wechat_name:
        return jsonify({'error': '请先填写可爱的微信名再来处理视频哦 🏸'}), 400

    if not upload_id or not safe_filename:
        return jsonify({'error': 'Missing upload_id or safe_filename'}), 400

    input_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)

    if not os.path.exists(input_path):
        return jsonify({'error': 'File not found'}), 404

    task = get_task(upload_id)
    if task and task.get('wechat_name') and task['wechat_name'] != wechat_name:
        return jsonify({'error': '这个视频不属于你哦，处理权限不足 🙅'}), 403

    output_dir_abs = os.path.join(app.config['OUTPUT_FOLDER'], upload_id)
    os.makedirs(output_dir_abs, exist_ok=True)

    upload_url = f'/uploads/{safe_filename}'
    output_dir_rel = f'/outputs/{upload_id}'

    if task is None:
        create_task({
            'upload_id': upload_id,
            'wechat_name': wechat_name,
            'user_name': user_name,
            'task_name': task_name,
            'original_filename': safe_filename[len(upload_id) + 1:] if safe_filename.startswith(upload_id + '_') else safe_filename,
            'safe_filename': safe_filename,
            'upload_url': upload_url,
            'upload_size': os.path.getsize(input_path) if os.path.exists(input_path) else None,
            'output_dir': output_dir_rel,
            'min_duration': min_duration,
            'status': 'processing',
            'output_count': 0,
        })

    try:
        import sys
        if PROJECT_ROOT not in sys.path:
            sys.path.insert(0, PROJECT_ROOT)

        from src.segmenter import VideoSegmenter
        from src.video_cutter import VideoCutter

        segmenter = VideoSegmenter(
            min_segment_duration=3.0,
            max_segment_duration=60.0,
            padding_before=0.5,
            padding_after=0.5
        )

        logger.info(f'[{upload_id}] start segmenting video: {input_path} wechat_name={wechat_name}')
        segments = segmenter.process_video(input_path, use_tracknet=False)
        stats = segmenter.get_segment_stats()
        logger.info(f'[{upload_id}] segmented: {stats}')

        video_cutter = VideoCutter(
            output_format='mp4',
            codec=None,
            quality=23
        )
        encoder_speed_ratio = float(getattr(video_cutter, 'encoder_speed_ratio', 1.0))
        encoder_label = getattr(video_cutter, 'encoder_name_label', video_cutter.video_encoder)

        cut_min_duration = None
        if min_duration is not None:
            try:
                cut_min_duration = float(min_duration)
            except (ValueError, TypeError):
                pass

        estimate_payload = estimate_process_plan(
            segmenter.video_duration,
            width=getattr(segmenter, 'video_width', None),
            height=getattr(segmenter, 'video_height', None),
            encoder_speed_ratio=encoder_speed_ratio,
            min_duration_filter=cut_min_duration,
        )
        process_hint = (f"预估 {format_minutes_str(estimate_payload['low_seconds'])} ～ "
                        f"{format_minutes_str(estimate_payload['high_seconds'])}，编码器：{encoder_label}")
        logger.info(f'[{upload_id}] cutting: {process_hint}')

        success_count = video_cutter.cut_segments_with_filter(
            input_path=input_path,
            segments=segmenter.segments,
            output_dir=output_dir_abs,
            min_duration=cut_min_duration,
            prefix='segment'
        )
        logger.info(f'[{upload_id}] cut segments: success={success_count}')

        output_files = []
        if os.path.exists(output_dir_abs):
            for f in sorted(os.listdir(output_dir_abs)):
                if f.endswith('.mp4'):
                    file_path = os.path.join(output_dir_abs, f)
                    duration = get_video_duration(file_path)
                    output_files.append({
                        'name': f,
                        'url': f'/outputs/{upload_id}/{f}',
                        'size': os.path.getsize(file_path),
                        'duration': duration,
                        'duration_str': format_duration(duration)
                    })

        update_task_completed(upload_id, 'completed', len(output_files))

        return jsonify({
            'upload_id': upload_id,
            'status': 'completed',
            'total_segments': stats['total_segments'],
            'success_count': success_count,
            'output_files': output_files,
            'output_dir': output_dir_rel,
            'wechat_name': wechat_name,
            'user_name': user_name,
            'task_name': task_name,
            'processing_estimate': {
                **estimate_payload,
                'best_effort_human': format_minutes_str(estimate_payload['best_effort_seconds']),
                'low_human': format_minutes_str(estimate_payload['low_seconds']),
                'high_human': format_minutes_str(estimate_payload['high_seconds']),
                'budget_human': format_minutes_str(BUDGET_SECONDS_FOR_PROCESS),
                'encoder_label': encoder_label,
            },
        }), 200

    except Exception as e:
        logger.exception(f'[{upload_id}] process failed: {e}')
        try:
            update_task_completed(upload_id, 'failed', 0)
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/list', methods=['GET'])
def list_files():
    user_name = request.args.get('user_name')
    task_name = request.args.get('task_name')
    wechat_name = _require_wechat_name()
    raw_tasks = list_tasks(user_name=user_name, task_name=task_name, days=None, wechat_name=wechat_name)
    tasks = []
    for t in raw_tasks:
        output_dir_abs = os.path.join(app.config['OUTPUT_FOLDER'], t['upload_id'])
        output_files = []
        if os.path.isdir(output_dir_abs):
            for f in sorted(os.listdir(output_dir_abs)):
                fpath = os.path.join(output_dir_abs, f)
                if os.path.isfile(fpath) and f.endswith('.mp4'):
                    duration = get_video_duration(fpath)
                    output_files.append({
                        'name': f,
                        'url': f"/outputs/{t['upload_id']}/{f}",
                        'size': os.path.getsize(fpath),
                        'duration': duration,
                        'duration_str': format_duration(duration),
                    })
        tasks.append({
            'upload_id': t['upload_id'],
            'wechat_name': t.get('wechat_name'),
            'user_name': t.get('user_name'),
            'task_name': t.get('task_name'),
            'original_filename': t.get('original_filename'),
            'safe_filename': t.get('safe_filename'),
            'upload_url': t.get('upload_url'),
            'upload_size': t.get('upload_size'),
            'output_dir': t.get('output_dir'),
            'output_files': output_files,
            'output_count': len(output_files),
            'min_duration': t.get('min_duration'),
            'status': t.get('status'),
            'created_at': t.get('created_at'),
            'completed_at': t.get('completed_at'),
        })

    return jsonify({
        'tasks': tasks,
        'task_names': list_task_names(),
    })


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/outputs/<upload_id>/<filename>')
def output_file(upload_id, filename):
    output_dir = os.path.join(app.config['OUTPUT_FOLDER'], upload_id)
    return send_from_directory(output_dir, filename)


@app.route('/api/delete/<upload_id>', methods=['DELETE'])
def delete_upload(upload_id):
    wechat_name = _require_wechat_name()
    if not wechat_name:
        return jsonify({'error': '请先填写可爱的微信名再来操作哦 🧸'}), 400

    task = get_task(upload_id)
    if not task:
        return jsonify({'error': 'Not found'}), 404
    if task.get('wechat_name') and task['wechat_name'] != wechat_name:
        return jsonify({'error': '这个任务不属于你哦，删除权限不足 🙅'}), 403

    upload_path = os.path.join(app.config['UPLOAD_FOLDER'])
    output_path = os.path.join(app.config['OUTPUT_FOLDER'], upload_id)

    deleted = False

    if os.path.isdir(upload_path):
        for f in os.listdir(upload_path):
            if f.startswith(upload_id):
                try:
                    os.remove(os.path.join(upload_path, f))
                    deleted = True
                except OSError:
                    pass

    if os.path.exists(output_path):
        shutil.rmtree(output_path, ignore_errors=True)
        deleted = True

    delete_task(upload_id)

    if deleted or task:
        logger.info(f'deleted upload_id={upload_id} wechat_name={wechat_name}')
        return jsonify({'message': 'Deleted successfully'}), 200
    else:
        return jsonify({'error': 'Not found'}), 404


@app.route('/api/comments', methods=['GET'])
def get_comments():
    wechat_name = request.args.get('wechat_name') or None
    rows = list_comments(wechat_name=wechat_name)
    items = []
    for row in rows:
        items.append({
            'id': row['id'],
            'wechat_name': row['wechat_name'],
            'rating': int(row.get('rating') or 0),
            'content': row['content'],
            'created_at': row['created_at'],
            'created_at_str': _format_comment_time(row['created_at']),
        })
    stats = {
        'total': len(items),
        'average_rating': (sum(i['rating'] for i in items) / len(items)) if items else 0,
    }
    return jsonify({
        'comments': items,
        'stats': stats,
    })


@app.route('/api/comments', methods=['POST'])
def create_comment():
    wechat_name = _require_wechat_name()
    if not wechat_name:
        return jsonify({'error': '请先填写可爱的微信名再来评论哦 🫶'}), 400

    data = request.get_json(silent=True) or {}
    rating = data.get('rating', 0)
    content = (data.get('content') or '').strip()

    try:
        rating = int(rating)
    except (TypeError, ValueError):
        rating = 0
    if rating < 0:
        rating = 0
    if rating > 5:
        rating = 5
    if len(wechat_name) > 32:
        return jsonify({'error': '微信名太长啦，最多 32 个字符 🎀'}), 400
    if not content:
        return jsonify({'error': '评论内容不能为空哦 ✍️'}), 400
    if len(content) > 500:
        return jsonify({'error': '评论内容太长啦，最多 500 字 📝'}), 400

    comment = add_comment(wechat_name, rating, content)
    logger.info(f'new comment id={comment["id"]} wechat={wechat_name} rating={rating}')
    return jsonify({
        'comment': {
            'id': comment['id'],
            'wechat_name': comment['wechat_name'],
            'rating': int(comment.get('rating') or 0),
            'content': comment['content'],
            'created_at': comment['created_at'],
            'created_at_str': _format_comment_time(comment['created_at']),
        }
    }), 200


if __name__ == '__main__':
    logger.info(f'Starting app on {HOST}:{PORT} debug={DEBUG}')
    app.run(host=HOST, port=PORT, debug=DEBUG)
