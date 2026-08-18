#!/bin/bash
set -e

SERVER="101.96.224.241"
SSH_PORT="${SSH_PORT:-22}"
USER="root"
REMOTE_DIR="/opt/badminton"
REMOTE_DEPLOY_DIR="$REMOTE_DIR/deploy"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p $SSH_PORT"
SCP_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -P $SSH_PORT"

echo "=== 0. 本地清理脚本预演（DRY_RUN=1，确保语法/路径正确） ==="
bash deploy/local_cleanup_smoke.sh || {
  echo "本地 smoke 失败，请先解决再推送。"
  exit 1
}

echo "=== 1. 上传 Web 文件 ==="
scp web/templates/index.html $USER@$SERVER:$REMOTE_DIR/web/templates/index.html
scp web/app.py $USER@$SERVER:$REMOTE_DIR/web/app.py
scp web/models.py $USER@$SERVER:$REMOTE_DIR/web/models.py
echo "Web 文件上传完成"

echo "=== 1.5 上传 deploy/ 清理脚本 + 安装定时任务脚本 ==="
ssh $USER@$SERVER "mkdir -p $REMOTE_DEPLOY_DIR && chown root:root $REMOTE_DEPLOY_DIR && chmod 755 $REMOTE_DEPLOY_DIR"
scp deploy/cleanup_old_videos.py $USER@$SERVER:$REMOTE_DEPLOY_DIR/cleanup_old_videos.py
scp deploy/setup_cleanup_timer.sh $USER@$SERVER:$REMOTE_DEPLOY_DIR/setup_cleanup_timer.sh
echo "Deploy 文件上传完成"

echo "=== 2. 远端：chown + 重启 badminton-web + nginx reload ==="
ssh $USER@$SERVER "
set -e
chown -R badminton:badminton $REMOTE_DIR/web/templates/index.html $REMOTE_DIR/web/app.py $REMOTE_DIR/web/models.py
chown -R root:root $REMOTE_DEPLOY_DIR/cleanup_old_videos.py $REMOTE_DEPLOY_DIR/setup_cleanup_timer.sh
chmod 755 $REMOTE_DEPLOY_DIR/cleanup_old_videos.py $REMOTE_DEPLOY_DIR/setup_cleanup_timer.sh
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

echo "=== 3. 远端：部署 7 天清理定时任务（每天 03:00） + 预演 ==="
ssh $USER@$SERVER "
set -e
RETENTION_DAYS=$RETENTION_DAYS bash $REMOTE_DEPLOY_DIR/setup_cleanup_timer.sh
echo ''
echo '=== TIMER 注册结果 ==='
systemctl list-timers badminton-cleanup.timer --no-pager || true
echo '=== 立刻手动预演一次实际清理（会真的清理 7 天前的记录，请确认无异议）=== 命令：'
echo 'systemctl start badminton-cleanup.service && journalctl -u badminton-cleanup.service -n 40 --no-pager'
"

echo "=== 部署完成 ==="
echo "请刷新 https://101.126.10.54/ 查看效果"
echo "清理日志文件路径（服务器内）：$REMOTE_DIR/logs/cleanup.log"
