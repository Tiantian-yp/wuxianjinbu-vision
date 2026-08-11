#!/usr/bin/env bash
# 一键部署（expect 密码登录版）：走 SSH 2222 端口（443 已让给 Nginx HTTPS）
# 适配：Ubuntu 22.04 / CentOS Stream 9 / Rocky / Alma / veLinux
set -euo pipefail

HOST="101.96.224.241"
PORT="${SSH_PORT:-22}"
USER="root"
PASS='Root@123'
APP_DIR="/opt/badminton"
APP_USER="badminton"
DOMAIN_OR_IP="$HOST"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP_TAR="/tmp/record_flow_deploy.tar.gz"
REMOTE_TAR="/tmp/record_flow_deploy.tar.gz"

echo "======================================================"
echo " Badminton video web — deploy to $USER@$HOST:$PORT"
echo "======================================================"

# -------- expect wrapper --------
EXP_RUNNER="/tmp/_badminton_exp.sh"
cat > "$EXP_RUNNER" <<'EXP'
#!/usr/bin/expect -f
set timeout 1800
set mode [lindex $argv 0]
set user [lindex $argv 1]
set host [lindex $argv 2]
set port [lindex $argv 3]
set pass [lindex $argv 4]
if {$mode eq "ssh"} {
    set cmd [lindex $argv 5]
    spawn -noecho ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o ServerAliveInterval=30 -p $port $user@$host "bash -lc $cmd"
} elseif {$mode eq "scp"} {
    set src [lindex $argv 5]
    set dst [lindex $argv 6]
    spawn -noecho scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -P $port $src $user@$host:$dst
} else {
    puts stderr "unknown mode: $mode"
    exit 2
}
expect {
    -re "(?i)yes/no" { send "yes\r"; exp_continue }
    -re "(?i)password:" { send -- "$pass\r"; exp_continue }
    -re "(?i)Permission denied" { puts stderr "SSH AUTH FAILED (permission denied)"; exit 5 }
    -re "Connection refused|Connection timed out|No route to host|Could not resolve" { puts stderr "SSH CONNECT FAILED ($host:$port)"; exit 6 }
    timeout { puts stderr "SSH TIMEOUT ($host:$port)"; exit 7 }
    eof {
        set exit_code [lindex [wait] 3]
        if {$exit_code != 0} { puts stderr "REMOTE EXIT CODE=$exit_code"; exit $exit_code }
        exit 0
    }
}
EXP
chmod +x "$EXP_RUNNER"

do_ssh() {
    local cmd="$1"
    local short
    short="$(echo "$cmd" | cut -c1-100)$( [ $(echo "$cmd" | wc -c) -gt 100 ] && echo '...' )"
    echo "  • [ssh] $short"
    "$EXP_RUNNER" ssh "$USER" "$HOST" "$PORT" "$PASS" "$(printf '%q' "$cmd")"
}
do_scp() {
    local src="$1" dst="$2"
    echo "  • [scp] $(basename "$src") -> $dst"
    "$EXP_RUNNER" scp "$USER" "$HOST" "$PORT" "$PASS" "$src" "$dst"
}

# -------- 0. 打包 --------
echo ""
echo "[0/6] Packaging project code..."
cd "$PROJECT_ROOT"
rm -f "$TMP_TAR"
tar -czf "$TMP_TAR" \
  --exclude='venv' --exclude='.venv' \
  --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='web/uploads/*' --exclude='web/outputs/*' \
  --exclude='logs/*' --exclude='run/*' \
  --exclude='.env' --exclude='.DS_Store' \
  --exclude='models/TrackNetV3/TrackNetV3_ckpts.zip' \
  --exclude='output/*' --exclude='.git' \
  .
echo "    size: $(du -h "$TMP_TAR" | cut -f1)"

# -------- 1. 连通性 + 快速端口 banner --------
echo ""
echo "[1/6] SSH connect to $USER@$HOST:$PORT ..."
do_ssh "echo '===' && whoami && uname -a && echo 'os:' && cat /etc/os-release | head -4 && echo '===' && echo python3: \$(python3 --version 2>&1)"

# -------- 2. 上传 --------
echo ""
echo "[2/6] Upload code tar + bootstrap script..."
do_scp "$TMP_TAR" "$REMOTE_TAR"
do_scp "$PROJECT_ROOT/deploy/remote_bootstrap.sh" "/tmp/remote_bootstrap.sh"
do_ssh "chmod +x /tmp/remote_bootstrap.sh"

# -------- 3. 远程初始化 --------
echo ""
echo "[3/6] Run remote bootstrap (install packages: python3/nginx/ffmpeg + user/dirs/venv/firewalld)..."
echo "    (首次安装 dnf/apt 较慢，预计 2-5 分钟，请耐心等待...)"
do_ssh "bash /tmp/remote_bootstrap.sh '$APP_USER' '$APP_DIR' '$DOMAIN_OR_IP' 2>&1 | tail -40"

# -------- 4. 解压代码 + pip 依赖 --------
echo ""
echo "[4/6] Extract code and install pip dependencies..."
do_ssh "
set -e
cd '$APP_DIR'
# 持久目录备份（保持 uploads/outputs/logs/run/venv）
for d in web/uploads web/outputs logs run venv; do
  [ -d \"\$d\" ] && rm -rf /tmp/_badm_keep_\$(basename \"\$d\") && mv \"\$d\" /tmp/_badm_keep_\$(basename \"\$d\")
done
# 清老代码文件，保留目录骨架
rm -rf deploy src web requirements.txt README.md config 2>/dev/null || true
tar -xzf '$REMOTE_TAR' -C '$APP_DIR'
# 恢复持久目录
for d in web/uploads web/outputs logs run venv; do
  bn=\$(basename \"\$d\")
  if [ -d /tmp/_badm_keep_\$bn ]; then
    mkdir -p \"\$(dirname \"\$d\")\" 2>/dev/null || true
    if [ -d \"\$d\" ]; then cp -rn /tmp/_badm_keep_\$bn/. \"\$d/.\" 2>/dev/null || true
    else mv /tmp/_badm_keep_\$bn \"\$d\"; fi
    rm -rf /tmp/_badm_keep_\$bn
  fi
done
chown -R '${APP_USER}':'${APP_USER}' '$APP_DIR'
# 安装依赖（比较慢：torch/opencv/mediapipe wheel 很大）
echo '--- installing pip deps (may take 3~8 min) ---'
sudo -u '${APP_USER}' '$APP_DIR/venv/bin/pip' install --no-cache-dir -r '$APP_DIR/requirements.txt' 2>&1 | tail -20
# 基础目录
sudo -u '${APP_USER}' mkdir -p '$APP_DIR/web/uploads' '$APP_DIR/web/outputs' '$APP_DIR/logs' '$APP_DIR/run'
chown -R '${APP_USER}':'${APP_USER}' '$APP_DIR/web/uploads' '$APP_DIR/web/outputs' '$APP_DIR/logs' '$APP_DIR/run'
# 再次确认 badminton 用户存在 + 可执行
id '${APP_USER}' || true
sudo -u '${APP_USER}' '$APP_DIR/venv/bin/python' -c 'import flask, flask_cors, cv2, numpy, ffmpeg; print(\"deps OK, flask=\", flask.__version__)' 2>&1 | tail -5
"

# -------- 5. 渲染 systemd/nginx 并启动 --------
echo ""
echo "[5/6] Render systemd/nginx and restart services..."
do_ssh "
set -e
# 先看发行版判断 nginx 配置路径
. /etc/os-release 2>/dev/null || true
case \" \$ID \$ID_LIKE \" in
  *ubuntu*|*debian*)
    NGINX_SITE='/etc/nginx/sites-available/badminton'
    sed -e 's|{{DOMAIN_OR_IP}}|${DOMAIN_OR_IP}|g' '$APP_DIR/deploy/nginx.conf.template' > \"\$NGINX_SITE\"
    ln -sf \"\$NGINX_SITE\" /etc/nginx/sites-enabled/badminton
    rm -f /etc/nginx/sites-enabled/default
    ;;
  *)
    NGINX_SITE='/etc/nginx/conf.d/badminton.conf'
    sed -e 's|{{DOMAIN_OR_IP}}|${DOMAIN_OR_IP}|g' '$APP_DIR/deploy/nginx.conf.template' > \"\$NGINX_SITE\"
    ;;
esac
nginx -t || (nginx -T 2>&1 | tail -40; exit 1)

sed -e \"s|{{APP_USER}}|${APP_USER}|g\" \
    -e \"s|{{APP_GROUP}}|${APP_USER}|g\" \
    -e \"s|{{APP_DIR}}|${APP_DIR}|g\" \
    '$APP_DIR/deploy/badminton-web.service.template' > /etc/systemd/system/badminton-web.service

systemctl daemon-reload
systemctl enable badminton-web.service
systemctl restart badminton-web.service
sleep 4
systemctl --no-pager status badminton-web.service | head -15 || true

# 确认 nginx 启动
systemctl enable nginx
systemctl start nginx 2>/dev/null || systemctl reload nginx
systemctl --no-pager status nginx | head -8 || true

# 服务器本地探活 /health（经过 nginx :80）
for i in 1 2 3 4 5 6 7 8; do
  code=\$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1/health 2>&1) || code=000
  body=\$(curl -sS --max-time 15 http://127.0.0.1/health 2>&1 | head -c 200) || true
  echo \"  probe \$i code=\$code\"
  if [ \"\$code\" = '200' ]; then echo '  ✅ SERVER-SIDE /health 200:' \"\$body\"; break; fi
  sleep 4
done
if [ \"\$code\" != '200' ]; then
  echo '--- last 40 journal of badminton-web ---'; journalctl -u badminton-web.service -n 40 --no-pager || true
  echo '--- last 20 app stderr.log ---'; tail -n 20 '$APP_DIR/logs/stderr.log' 2>/dev/null || true
  exit 1
fi
"

# -------- 6. 公网探测 --------
echo ""
echo "[6/6] Public endpoint verification..."
for i in 1 2 3 4 5; do
  HC="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "http://${DOMAIN_OR_IP}/health" 2>&1 || true)"
  echo "  attempt $i: /health HTTP_$HC"
  [ "$HC" = "200" ] && break
  sleep 4
done
HP="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "http://${DOMAIN_OR_IP}/" 2>&1 || true)"
echo "  homepage: / HTTP_$HP"
HBODY="$(curl -sS --max-time 20 "http://${DOMAIN_OR_IP}/health" 2>&1 || true)"
echo "  /health body: $HBODY"

echo ""
echo "===================================================================="
echo "  🎉 BADMINTON VIDEO WEB 部署完成 🎉"
echo "===================================================================="
echo "  🌐 朋友直接打开：     http://${DOMAIN_OR_IP}/"
echo "  🏥 健康检查：         http://${DOMAIN_OR_IP}/health"
echo "  🖥  SSH 管理：         ssh root@${DOMAIN_OR_IP} -p $PORT"
echo "  📁 代码目录：         ${DOMAIN_OR_IP}:${APP_DIR}"
echo "  📜 应用日志：         journalctl -u badminton-web.service -f (SSH执行)"
echo "  📜 Nginx 访问：       tail -f /var/log/nginx/badminton.access.log"
echo "  📜 Nginx 错误：       tail -f /var/log/nginx/badminton.error.log"
echo ""
echo "  后续可选（绑定域名 + HTTPS）："
echo "    1) 买域名后 A 记录 -> ${DOMAIN_OR_IP}"
echo "    2) SSH 执行：dnf install -y certbot python3-certbot-nginx"
echo "    3) SSH 执行：certbot --nginx -d your.domain.com (邮箱确认同意条款即可)"
echo "===================================================================="

rm -f "$EXP_RUNNER"
