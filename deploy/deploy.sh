#!/bin/bash
set -e

ENV="${1:-test}"

case "$ENV" in
    test)
        SERVER="101.126.10.54"
        SSH_PORT="2222"
        ENV_NAME="测试环境"
        BASE_URL="http://101.126.10.54"
        ;;
    prod)
        SERVER="101.96.224.241"
        SSH_PORT="22"
        ENV_NAME="线上环境"
        BASE_URL="https://wuxianjinbu.fun"
        ;;
    *)
        echo "Usage: $0 [test|prod]"
        echo "  test  - deploy to test server (101.126.10.54:2222, ByteDance internal)"
        echo "  prod  - deploy to production server (101.96.224.241:22, public)"
        exit 1
        ;;
esac

USER="root"
PASS="Root@123"
REMOTE_DIR="/opt/badminton"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "============================================"
echo " Deploying to ${ENV_NAME}"
echo " Server: ${SERVER}:${SSH_PORT}"
echo "============================================"

echo ""
echo "[1/5] Packaging code..."
cd "$PROJECT_DIR"
TMP_PKG=$(mktemp -d)
mkdir -p "$TMP_PKG/web/templates" "$TMP_PKG/src" "$TMP_PKG/deploy"

cp web/app.py "$TMP_PKG/web/"
cp web/models.py "$TMP_PKG/web/"
cp web/task_queue.py "$TMP_PKG/web/"
cp web/templates/index.html "$TMP_PKG/web/templates/"
cp web/templates/guide.html "$TMP_PKG/web/templates/" 2>/dev/null || true
cp src/*.py "$TMP_PKG/src/"
cp deploy/gunicorn.conf.py "$TMP_PKG/deploy/" 2>/dev/null || true
cp deploy/cleanup_old_videos.py "$TMP_PKG/deploy/" 2>/dev/null || true
cp deploy/setup_cleanup_timer.sh "$TMP_PKG/deploy/" 2>/dev/null || true

PKG_FILE="/tmp/_deploy_${ENV}.tar.gz"
COPYFILE_DISABLE=1 tar -czf "$PKG_FILE" -C "$TMP_PKG" .
rm -rf "$TMP_PKG"
echo "  Package: $(du -h "$PKG_FILE" | cut -f1)"

cat > /tmp/_remote_deploy.sh <<'REMOTE'
#!/bin/bash
set -e
cd /opt/badminton
tar -xzf /tmp/_deploy_pkg.tar.gz
chown -R badminton:badminton /opt/badminton
chmod 755 /opt/badminton /opt/badminton/web /opt/badminton/src /opt/badminton/deploy 2>/dev/null || true
find /opt/badminton -name '._*' -delete 2>/dev/null || true
find /opt/badminton -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find /opt/badminton -name '*.pyc' -delete 2>/dev/null || true
if [ -f deploy/gunicorn.conf.py ]; then
    sed -i 's/MAX_CONTENT_MB=500/MAX_CONTENT_MB=600/g' deploy/gunicorn.conf.py
fi
systemctl restart badminton-web
for i in $(seq 1 15); do
    if curl -sS -m 3 http://127.0.0.1:5000/health >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
echo "=== Service Status ==="
systemctl is-active badminton-web nginx
echo ""
echo "=== Health Check ==="
curl -sS -m 10 http://127.0.0.1/health
echo ""
echo "=== Config ==="
curl -sS -m 10 http://127.0.0.1/config | python3 -c 'import sys,json; d=json.load(sys.stdin); print("max_upload=%dMB hard_max=%dmin recommended=%dmin" % (d["max_upload_mb"], d["hard_max_duration_seconds"]//60, d["recommended_max_duration_minutes"]))'
rm -f /tmp/_deploy_pkg.tar.gz /tmp/_remote_deploy.sh
REMOTE

echo ""
echo "[2/5] Uploading package and deploy script..."
expect <<EXPECT_EOF
set timeout 60
spawn scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -P ${SSH_PORT} "${PKG_FILE}" ${USER}@${SERVER}:/tmp/_deploy_pkg.tar.gz
expect {
    -re "(?i)password:" { send -- "${PASS}\r"; exp_continue }
    eof { catch wait result; exit [lindex \$result 3] }
    timeout { exit 1 }
}
EXPECT_EOF

expect <<EXPECT_EOF
set timeout 30
spawn scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -P ${SSH_PORT} /tmp/_remote_deploy.sh ${USER}@${SERVER}:/tmp/_remote_deploy.sh
expect {
    -re "(?i)password:" { send -- "${PASS}\r"; exp_continue }
    eof { catch wait result; exit [lindex \$result 3] }
    timeout { exit 1 }
}
EXPECT_EOF

echo ""
echo "[3/5] Running remote deploy..."
expect <<EXPECT_EOF
set timeout 120
spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=30 -p ${SSH_PORT} ${USER}@${SERVER} "bash /tmp/_remote_deploy.sh"
expect {
    -re "(?i)password:" { send -- "${PASS}\r"; exp_continue }
    eof { catch wait result; exit [lindex \$result 3] }
    timeout { exit 1 }
}
EXPECT_EOF

echo ""
echo "[4/5] External verification..."
sleep 2
HTTP_CODE=$(curl -sS -o /dev/null -w "%{http_code}" -m 15 "$BASE_URL/health" 2>/dev/null || echo "000")
echo "  $BASE_URL/health -> HTTP $HTTP_CODE"
if [ "$HTTP_CODE" = "200" ]; then
    echo "  ✅ Service is accessible"
else
    echo "  ⚠️  Cannot access externally (test server requires ByteDance network)"
fi

echo ""
echo "[5/5] Cleanup..."
rm -f "$PKG_FILE" /tmp/_remote_deploy.sh

echo ""
echo "============================================"
echo " ✅ Deploy to ${ENV_NAME} Complete!"
echo "    URL: ${BASE_URL}/"
echo "============================================"
