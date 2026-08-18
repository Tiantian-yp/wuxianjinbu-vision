import os
import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager

DB_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv('DATA_DIR', DB_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.getenv('DB_PATH', os.path.join(DATA_DIR, 'badminton.db'))


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn, table, column, definition):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id TEXT UNIQUE NOT NULL,
                user_name TEXT,
                task_name TEXT,
                original_filename TEXT,
                safe_filename TEXT,
                upload_url TEXT,
                upload_size INTEGER,
                output_dir TEXT,
                min_duration REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                output_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        _ensure_column(conn, 'tasks', 'wechat_name', 'TEXT')
        _ensure_column(conn, 'tasks', 'progress', 'REAL DEFAULT 0.0')
        _ensure_column(conn, 'tasks', 'progress_message', 'TEXT DEFAULT \'\'')
        _ensure_column(conn, 'tasks', 'error', 'TEXT')
        _ensure_column(conn, 'tasks', 'started_at', 'TEXT')
        _ensure_column(conn, 'tasks', 'total_segments', 'INTEGER DEFAULT 0')
        _ensure_column(conn, 'tasks', 'result_data', 'TEXT')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_user_name ON tasks(user_name)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_task_name ON tasks(task_name)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_wechat_name ON tasks(wechat_name)')

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wechat_name TEXT NOT NULL,
                rating INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_comments_created ON comments(created_at DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_comments_wechat_name ON comments(wechat_name)')


def create_task(payload):
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks (upload_id, user_name, task_name, original_filename, safe_filename,
                               upload_url, upload_size, output_dir, min_duration, status,
                               output_count, created_at, wechat_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload['upload_id'],
                payload.get('user_name'),
                payload.get('task_name'),
                payload.get('original_filename'),
                payload.get('safe_filename'),
                payload.get('upload_url'),
                payload.get('upload_size'),
                payload.get('output_dir'),
                payload.get('min_duration'),
                payload.get('status', 'pending'),
                payload.get('output_count', 0),
                datetime.utcnow().isoformat() + 'Z',
                payload.get('wechat_name'),
            ),
        )
        return cur.lastrowid


def get_task(upload_id):
    with get_conn() as conn:
        row = conn.execute(
            'SELECT * FROM tasks WHERE upload_id = ?',
            (upload_id,),
        ).fetchone()
    return dict(row) if row else None


def delete_task(upload_id):
    with get_conn() as conn:
        conn.execute('DELETE FROM tasks WHERE upload_id = ?', (upload_id,))


def update_task_completed(upload_id, status, output_count, completed_at=None, result_data=None, error=None):
    if completed_at is None:
        completed_at = datetime.utcnow().isoformat() + 'Z'
    with get_conn() as conn:
        if result_data is not None:
            import json
            conn.execute(
                'UPDATE tasks SET status=?, output_count=?, completed_at=?, result_data=?, error=? WHERE upload_id=?',
                (status, output_count, completed_at, json.dumps(result_data, ensure_ascii=False), error, upload_id),
            )
        else:
            conn.execute(
                'UPDATE tasks SET status=?, output_count=?, completed_at=?, error=? WHERE upload_id=?',
                (status, output_count, completed_at, error, upload_id),
            )


def update_task_progress(upload_id, progress, message='', status=None, total_segments=None):
    with get_conn() as conn:
        updates = ['progress=?', 'progress_message=?']
        params = [float(progress), message]
        if status is not None:
            updates.append('status=?')
            params.append(status)
        if status == 'processing':
            updates.append('started_at=COALESCE(started_at, ?)')
            params.append(datetime.utcnow().isoformat() + 'Z')
        if total_segments is not None:
            updates.append('total_segments=?')
            params.append(int(total_segments))
        params.append(upload_id)
        conn.execute(
            f'UPDATE tasks SET {", ".join(updates)} WHERE upload_id=?',
            params,
        )


def update_task_started(upload_id):
    started_at = datetime.utcnow().isoformat() + 'Z'
    with get_conn() as conn:
        conn.execute(
            'UPDATE tasks SET status=?, started_at=?, progress=?, progress_message=? WHERE upload_id=?',
            ('processing', started_at, 0.01, '正在准备处理视频…', upload_id),
        )


def list_tasks(user_name=None, task_name=None, days=None, wechat_name=None):
    sql = 'SELECT * FROM tasks'
    where = []
    params = []
    if user_name:
        where.append('user_name = ?')
        params.append(user_name)
    if task_name:
        where.append('task_name = ?')
        params.append(task_name)
    if wechat_name:
        where.append('wechat_name = ?')
        params.append(wechat_name)
    if days:
        cutoff = (datetime.utcnow() - timedelta(days=int(days))).isoformat() + 'Z'
        where.append('created_at >= ?')
        params.append(cutoff)
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY created_at DESC LIMIT 500'
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_task_names():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT task_name FROM tasks WHERE task_name IS NOT NULL AND task_name <> '' ORDER BY task_name ASC"
        ).fetchall()
    return [r['task_name'] for r in rows]


def add_comment(wechat_name, rating, content):
    with get_conn() as conn:
        cur = conn.execute(
            'INSERT INTO comments (wechat_name, rating, content, created_at) VALUES (?, ?, ?, ?)',
            (wechat_name, int(rating or 0), content, datetime.utcnow().isoformat() + 'Z'),
        )
        row = conn.execute('SELECT * FROM comments WHERE id = ?', (cur.lastrowid,)).fetchone()
    return dict(row) if row else None


def list_comments(wechat_name=None):
    sql = 'SELECT * FROM comments'
    params = []
    if wechat_name:
        sql += ' WHERE wechat_name = ?'
        params.append(wechat_name)
    sql += ' ORDER BY created_at DESC LIMIT 500'
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def expire_old_tasks(days=7):
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + 'Z'
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT upload_id, safe_filename, output_dir FROM tasks WHERE created_at < ? AND status <> "expired"',
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_task_expired(upload_id):
    with get_conn() as conn:
        conn.execute(
            'UPDATE tasks SET status="expired" WHERE upload_id=?',
            (upload_id,),
        )
