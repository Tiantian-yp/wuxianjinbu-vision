#!/usr/bin/env python3
"""
清理 7 天前的视频上传与切割输出目录，并把对应任务状态改成 expired。
所有操作都写日志到 $APP_DIR/logs/cleanup.log。
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import List

APP_DIR = Path(os.getenv('APP_DIR', '/opt/badminton')).resolve()
LOG_DIR = APP_DIR / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

log_file = LOG_DIR / 'cleanup.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(str(log_file), encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger('cleanup_old_videos')

UPLOAD_DIR = Path(os.getenv('UPLOAD_DIR', str(APP_DIR / 'web' / 'uploads'))).resolve()
OUTPUT_DIR = Path(os.getenv('OUTPUT_DIR', str(APP_DIR / 'web' / 'outputs'))).resolve()
DATA_DIR = Path(os.getenv('DATA_DIR', str(APP_DIR / 'data'))).resolve()
DB_PATH = Path(os.getenv('DB_PATH', str(DATA_DIR / 'badminton.db'))).resolve()

RETENTION_DAYS = int(os.getenv('RETENTION_DAYS', '7'))
DRY_RUN = os.getenv('DRY_RUN', '0').strip().lower() in ('1', 'true', 'yes', 'on')


def ensure_runtime_paths() -> None:
    sys.path.insert(0, str(APP_DIR))
    os.environ['DATA_DIR'] = str(DATA_DIR)
    os.environ['DB_PATH'] = str(DB_PATH)


def delete_path(path: Path, description: str) -> None:
    if not path.exists():
        log.info('SKIP missing %s: %s', description, path)
        return
    if DRY_RUN:
        log.info('[DRY_RUN] would delete %s: %s (%s)', description, path, human_size(path))
        return
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        log.info('DELETED %s: %s', description, path)
    except Exception as e:
        log.exception('FAILED delete %s: %s (%s)', description, path, e)


def human_size(path: Path) -> str:
    try:
        if path.is_file():
            size = path.stat().st_size
        elif path.is_dir():
            size = sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
        else:
            size = 0
    except Exception:
        return 'unknown'
    if size < 1024:
        return f'{size} B'
    if size < 1024 * 1024:
        return f'{size / 1024:.1f} KB'
    if size < 1024 * 1024 * 1024:
        return f'{size / (1024 * 1024):.2f} MB'
    return f'{size / (1024 * 1024 * 1024):.3f} GB'


def main(argv: List[str]) -> int:
    dry_mode = DRY_RUN or ('--dry-run' in argv)
    retention = RETENTION_DAYS
    for a in argv:
        if a.startswith('--days='):
            try:
                retention = int(a.split('=', 1)[1])
            except Exception:
                pass
    log.info('=== CLEANUP START (retention_days=%s, dry_run=%s)', retention, dry_mode)
    log.info('APP_DIR=%s, DB_PATH=%s, UPLOAD_DIR=%s, OUTPUT_DIR=%s', APP_DIR, DB_PATH, UPLOAD_DIR, OUTPUT_DIR)

    ensure_runtime_paths()
    from web.models import expire_old_tasks, mark_task_expired  # noqa: WPS433

    if not DB_PATH.exists():
        log.warning('DB_PATH 不存在，没有可清理的任务记录：%s', DB_PATH)
        return 0

    try:
        expired_candidates = expire_old_tasks(days=retention)
    except Exception as e:
        log.exception('expire_old_tasks 查询失败: %s', e)
        return 2

    log.info('共发现 %s 条超过 %s 天的任务待处理', len(expired_candidates), retention)
    cleaned = 0
    for t in expired_candidates:
        upload_id = t.get('upload_id')
        if not upload_id:
            continue
        log.info('处理任务 upload_id=%s status_before=%s safe_filename=%s output_dir=%s',
                 upload_id, t.get('status'), t.get('safe_filename'), t.get('output_dir'))

        safe = t.get('safe_filename') or ''
        upload_path = UPLOAD_DIR / safe if safe else None
        if upload_path is not None:
            delete_path(upload_path, f'上传文件[{upload_id}]')

        output_rel = (t.get('output_dir') or '').strip('/')
        if output_rel:
            output_name = Path(output_rel).name or upload_id
            output_path = OUTPUT_DIR / output_name
            delete_path(output_path, f'输出目录[{upload_id}]')

        if not dry_mode:
            try:
                mark_task_expired(upload_id)
                log.info('DB 已标记为 expired: %s', upload_id)
                cleaned += 1
            except Exception as e:
                log.exception('标记任务 expired 失败: %s (%s)', upload_id, e)
        else:
            log.info('[DRY_RUN] would mark expired: %s', upload_id)

    log.info('=== CLEANUP END (cleaned_tasks=%s, dry_run=%s)', cleaned, dry_mode)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
