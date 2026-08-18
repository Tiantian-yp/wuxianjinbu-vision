"""
Gunicorn 配置文件 - 生产环境使用
用法: gunicorn -c deploy/gunicorn.conf.py web.app:app
"""
import multiprocessing
import os

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

bind = os.getenv('GUNICORN_BIND', '127.0.0.1:5000')
workers = int(os.getenv('GUNICORN_WORKERS', max(2, multiprocessing.cpu_count())))
worker_class = 'sync'
worker_connections = 1000
timeout = int(os.getenv('GUNICORN_TIMEOUT', 600))
graceful_timeout = 30
keepalive = 5

# 视频处理是阻塞型 + 耗时长，单 worker 一次只处理 1 个请求即可
threads = 1

# 日志
accesslog = os.path.join(_BASE_DIR, 'logs', 'access.log')
errorlog = os.path.join(_BASE_DIR, 'logs', 'error.log')
loglevel = os.getenv('GUNICORN_LOG_LEVEL', 'info')
capture_output = True

# 进程
daemon = False
pidfile = os.path.join(_BASE_DIR, 'run', 'gunicorn.pid')
user = None
group = None

# 环境变量
raw_env = [
    f'FLASK_DEBUG=false',
    f'LOG_LEVEL=INFO',
    f'MAX_CONTENT_MB=600',
]

os.makedirs(os.path.join(_BASE_DIR, 'logs'), exist_ok=True)
os.makedirs(os.path.join(_BASE_DIR, 'run'), exist_ok=True)
