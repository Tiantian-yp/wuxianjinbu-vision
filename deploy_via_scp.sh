#!/bin/bash
set -e

SERVER="101.126.10.54"
USER="root"
REMOTE_DIR="/opt/badminton"

echo "=== 1. 上传文件 ==="
scp web/templates/index.html $USER@$SERVER:$REMOTE_DIR/web/templates/index.html
scp web/app.py $USER@$SERVER:$REMOTE_DIR/web/app.py
scp web/models.py $USER@$SERVER:$REMOTE_DIR/web/models.py
echo "上传完成"

echo "=== 2. 远端重启服务 ==="
ssh $USER@$SERVER "
chown -R badminton:badminton $REMOTE_DIR/web/templates/index.html $REMOTE_DIR/web/app.py $REMOTE_DIR/web/models.py
systemctl restart badminton-web
nginx -t
systemctl reload nginx
sleep 3
echo '=== 服务状态 ==='
systemctl is-active badminton-web nginx
echo '=== 健康检查 ==='
curl -sS http://127.0.0.1/health
echo ''
"

echo "=== 部署完成 ==="
echo "请刷新 https://101.126.10.54/ 查看效果"
