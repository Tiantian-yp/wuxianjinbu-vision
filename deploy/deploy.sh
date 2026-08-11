#!/usr/bin/env bash
#
# 本地 Mac 执行的一键部署脚本
# 功能：
#   1) Rsync 同步本地代码到远程服务器（保留 uploads/outputs/logs/run）
#   2) 远程执行：pip 安装依赖、刷新 systemd+nginx 配置、重启服务
#   3) 最后调用 /health 验证
#
# 用法：
#   # 最简：服务器 root + 默认目标路径 + 公网IP
#   bash deploy/deploy.sh root@47.x.x.x
#
#   # 指定用户、目录、域名/IP（用于 nginx + systemd 模板渲染）
#   bash deploy/deploy.sh -u badminton -d /opt/badminton -i your.domain.com root@47.x.x.x
#
#   # 使用指定 SSH 私钥
#   bash deploy/deploy.sh -i ~/.ssh/id_rsa_cloud -d /opt/badminton ubuntu@47.x.x.x
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

APP_USER="badminton"
APP_DIR="/opt/badminton"
DOMAIN_OR_IP=""
SSH_IDENTITY=""
SSH_EXTRA_OPTS=()
RSYNC_EXCLUDE=(
    '--exclude=venv'
    '--exclude=.venv'
    '--exclude=__pycache__'
    '--exclude=*.pyc'
    '--exclude=web/uploads/*'
    '--exclude=web/outputs/*'
    '--exclude=logs/*'
    '--exclude=run/*'
    '--exclude=.env'
    '--exclude=.DS_Store'
    '--exclude=models/TrackNetV3/TrackNetV3_ckpts.zip'
    '--exclude=output/*'
)

usage() {
    cat <<EOF
Usage: $0 [OPTIONS] <[user@]host>

Options:
  -u APP_USER     Remote unprivileged app user (default: badminton)
  -d APP_DIR      Remote deployment directory (default: /opt/badminton)
  -i IDENTITY     SSH identity file path (e.g. ~/.ssh/id_rsa_cloud)
  -n DOMAIN       Domain or public IP used in nginx (default: <host>)
  -h              Show this help
EOF
}

while getopts "u:d:i:n:h" opt; do
    case "$opt" in
        u) APP_USER="$OPTARG" ;;
        d) APP_DIR="$OPTARG" ;;
        i) SSH_IDENTITY="$OPTARG" ;;
        n) DOMAIN_OR_IP="$OPTARG" ;;
        h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done
shift $((OPTIND - 1))

SSH_HOST="${1:-}"
if [[ -z "$SSH_HOST" ]]; then
    echo "Error: missing SSH host argument (e.g. root@47.xx.xx.xx)"
    usage
    exit 1
fi

# -------- SSH 公共参数：避免 host key 交互、避免写入用户 known_hosts --------
TMP_KNOWN_HOSTS="$(mktemp)"
trap 'rm -f "$TMP_KNOWN_HOSTS"' EXIT
SSH_EXTRA_OPTS+=(
    -o "UserKnownHostsFile=$TMP_KNOWN_HOSTS"
    -o "StrictHostKeyChecking=accept-new"
    -o "ConnectTimeout=15"
    -o "ServerAliveInterval=30"
)
if [[ -n "$SSH_IDENTITY" ]]; then
    SSH_EXTRA_OPTS+=( -i "$SSH_IDENTITY" )
fi

# 从 SSH_HOST 提取 IP，用于 nginx 默认值
HOST_PART="${SSH_HOST##*@}"
if [[ -z "$DOMAIN_OR_IP" ]]; then
    DOMAIN_OR_IP="$HOST_PART"
fi

log() { echo "[$(date +%H:%M:%S)] $*"; }
run_remote() {
    ssh "${SSH_EXTRA_OPTS[@]}" "$SSH_HOST" "$@"
}

# -------------------- 1. 连通性测试 --------------------
log "[1/6] Testing SSH connection to $SSH_HOST ..."
run_remote "uname -a; echo '--- whoami:'; whoami; echo '--- os release:'; cat /etc/os-release | head -5" || {
    echo "ERROR: cannot SSH to $SSH_HOST"
    echo "Please verify: network reachable, security group port 22 open, SSH key/password correct."
    exit 1
}

# 确认是 root 登录（否则后面 sudo 需要免交互）
REMOTE_USER="$(run_remote whoami | tr -d '\r')"
SUDO=""
if [[ "$REMOTE_USER" != "root" ]]; then
    log "Remote user is '$REMOTE_USER', will use 'sudo -S' for priv ops."
    SUDO="sudo -S"
fi

# -------------------- 2. Rsync 代码 --------------------
log "[2/6] Rsync code to ${SSH_HOST}:${APP_DIR} ..."
# 先确保远程目录存在（父目录可能需要 root 权限）
run_remote "$SUDO mkdir -p '$APP_DIR' && $SUDO chown -R '${APP_USER}':'${APP_USER}' '$APP_DIR' && $SUDO chmod 755 '$APP_DIR' || true"

RSYNC_RSH="ssh ${SSH_EXTRA_OPTS[*]}"
# 如果是 root 登录，直接 root rsync 后再改权限
rsync -avz --no-perms --no-owner --no-group \
    -e "$RSYNC_RSH" \
    "${RSYNC_EXCLUDE[@]}" \
    "$PROJECT_ROOT/" \
    "${SSH_HOST}:${APP_DIR}/"

run_remote "$SUDO chown -R '${APP_USER}':'${APP_USER}' '${APP_DIR}'"

# -------------------- 3. （首次部署）执行远程 bootstrap --------------------
log "[3/6] Run remote bootstrap (install apt packages: python3/ffmpeg/nginx, create user/dirs, nginx+systemd)..."
BOOTSTRAP_DONE_FLAG="${APP_DIR}/.deploy_bootstrap_done"
BOOTSTRAP_NEED="$(run_remote "if [ -f '$BOOTSTRAP_DONE_FLAG' ]; then echo no; else echo yes; fi" | tr -d '\r')"
if [[ "$BOOTSTRAP_NEED" == "yes" ]]; then
    run_remote "$SUDO bash '$APP_DIR/deploy/remote_bootstrap.sh' '$APP_USER' '$APP_DIR' '$DOMAIN_OR_IP'"
    run_remote "$SUDO touch '$BOOTSTRAP_DONE_FLAG'"
else
    log "  (bootstrap already done, skipping. To re-run: rm $BOOTSTRAP_DONE_FLAG on remote)"
fi

# -------------------- 4. 安装/更新 Python 依赖 --------------------
log "[4/6] Install Python dependencies into venv..."
run_remote "sudo -u '$APP_USER' '$APP_DIR/venv/bin/pip' install --no-cache-dir -r '$APP_DIR/requirements.txt'"

# 创建必须存在的子目录并授权
run_remote "
sudo -u '$APP_USER' mkdir -p '$APP_DIR/web/uploads' '$APP_DIR/web/outputs' '$APP_DIR/logs' '$APP_DIR/run' &&
$SUDO chown -R '${APP_USER}':'${APP_USER}' '$APP_DIR/web/uploads' '$APP_DIR/web/outputs' '$APP_DIR/logs' '$APP_DIR/run'
"

# -------------------- 5. 刷新 nginx + systemd 并重启服务 --------------------
log "[5/6] Render nginx + systemd configs and restart services..."
run_remote "
set -e
# 渲染 nginx 配置（代码上传后模板已存在）
sed -e \"s|{{DOMAIN_OR_IP}}|${DOMAIN_OR_IP}|g\" '$APP_DIR/deploy/nginx.conf.template' | $SUDO tee /etc/nginx/sites-available/badminton >/dev/null
$SUDO ln -sf /etc/nginx/sites-available/badminton /etc/nginx/sites-enabled/badminton
$SUDO rm -f /etc/nginx/sites-enabled/default
$SUDO nginx -t
$SUDO systemctl reload nginx || $SUDO systemctl start nginx

# 渲染 systemd 服务
sed -e \"s|{{APP_USER}}|${APP_USER}|g\" \
    -e \"s|{{APP_GROUP}}|${APP_USER}|g\" \
    -e \"s|{{APP_DIR}}|${APP_DIR}|g\" \
    '$APP_DIR/deploy/badminton-web.service.template' | $SUDO tee /etc/systemd/system/badminton-web.service >/dev/null
$SUDO systemctl daemon-reload
$SUDO systemctl enable badminton-web.service
$SUDO systemctl restart badminton-web.service
sleep 2
$SUDO systemctl --no-pager status badminton-web.service | head -20
"

# -------------------- 6. 健康检查验证 --------------------
log "[6/6] Verify /health endpoint via nginx public endpoint..."
# server 端本地请求（避免客户端还没配 DNS / 安全组问题影响）
HEALTH_CHECK="$(run_remote "set +e; sleep 2; curl -sS -o /dev/null -w 'HTTP_%{http_code}' --max-time 10 http://127.0.0.1/health; echo" | tr -d '\r')"
log "  local curl from server -> 127.0.0.1/health: $HEALTH_CHECK"
if [[ "$HEALTH_CHECK" != *HTTP_200* ]]; then
    echo "ERROR: service health check failed ($HEALTH_CHECK). Check journalctl -u badminton-web.service on server."
    exit 1
fi

PUBLIC_CHECK="N/A (skipped from client)"
if command -v curl >/dev/null 2>&1; then
    set +e
    PUBLIC_CHECK="$(curl -sS -o /dev/null -w 'HTTP_%{http_code}' --max-time 15 "http://${DOMAIN_OR_IP}/health" 2>&1)" || true
    set -e
fi

echo ""
echo "================================================"
echo "  ✅  Deployment completed successfully!"
echo "================================================"
echo "  Local /health (server-side): $HEALTH_CHECK"
echo "  Public /health:              $PUBLIC_CHECK"
echo ""
echo "  🌐 Site URL:           http://${DOMAIN_OR_IP}/"
echo "  🏥 Health:             http://${DOMAIN_OR_IP}/health"
echo "  📁 Deploy dir:         $SSH_HOST:$APP_DIR"
echo "  📜 App logs (server):  journalctl -u badminton-web.service -f"
echo "  📜 Nginx access:       /var/log/nginx/badminton.access.log"
echo ""
echo "Next step (optional HTTPS, once DNS points to this IP):"
echo "  $ $SUDO apt-get install -y certbot python3-certbot-nginx"
echo "  $ $SUDO certbot --nginx -d ${DOMAIN_OR_IP}"
echo "================================================"
