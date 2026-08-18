#!/usr/bin/env bash
#
# 跨发行版远程服务器初始化脚本（Ubuntu 22.04+ / Debian 12+ / CentOS Stream 9 / Rocky 9 / Alma 9 / RHEL 9 / veLinux）
# 职责：
#   1) 安装系统依赖（python3+venv/pip、ffmpeg、nginx、编译工具、opencv系统依赖）
#   2) 创建独立应用用户 + 目录 + Python venv
#   3) 部署 systemd 服务 + nginx 站点
#   4) 防火墙放行 22/80/443
#
# 用法（root）：bash remote_bootstrap.sh <app_user> <app_dir> <domain_or_ip>
#
set -euo pipefail

APP_USER="${1:-badminton}"
APP_DIR="${2:-/opt/badminton}"
DOMAIN_OR_IP="${3:?Usage: $0 <app_user> <app_dir> <domain_or_ip>}"
APP_GROUP="$APP_USER"

log() { echo "[$(date +%H:%M:%S)] $*"; }

if [[ "$(whoami)" != "root" ]]; then
    echo "Error: please run as root (sudo)" >&2; exit 1
fi

# ------------------------------
# 检测发行版 -> 选择包管理器
# ------------------------------
. /etc/os-release 2>/dev/null || true
OS_ID="${ID:-}"
OS_LIKE="${ID_LIKE:-}"
OS_VER="${VERSION_ID:-}"

PKG=""
PKG_INSTALL=""
PKG_UPDATE=""
IS_DEBIAN="no"
IS_RHEL="no"
case " $OS_ID $OS_LIKE " in
    *ubuntu*|*debian* )
        IS_DEBIAN="yes"
        PKG="apt-get"
        PKG_UPDATE="apt-get update -y"
        PKG_INSTALL="apt-get install -y --no-install-recommends"
        ;;
    *centos*|*rhel*|*fedora*|*rocky*|*almalinux*|*velinux*|*ol* )
        IS_RHEL="yes"
        PKG="dnf"
        PKG_UPDATE="dnf makecache -y"
        PKG_INSTALL="dnf install -y"
        ;;
    *)
        echo "Unsupported OS: ID=$OS_ID ID_LIKE=$OS_LIKE" >&2
        echo "Trying RHEL-compatible (dnf) path as fallback..."
        IS_RHEL="yes"
        PKG="dnf"
        PKG_UPDATE="dnf makecache -y || true"
        PKG_INSTALL="dnf install -y"
        ;;
esac
log "Detected OS: ID=$OS_ID VER=$OS_VER PKG=$PKG"

# ------------------------------------------------------------------
# 1. 系统依赖
# ------------------------------------------------------------------
log "[1/6] Installing system packages via $PKG ..."
export DEBIAN_FRONTEND=noninteractive 2>/dev/null || true
$PKG_UPDATE

if [[ "$IS_DEBIAN" == "yes" ]]; then
    $PKG_INSTALL \
        ca-certificates curl gnupg lsb-release software-properties-common \
        python3 python3-venv python3-dev python3-pip python3-setuptools python3-wheel \
        nginx \
        build-essential pkg-config \
        libsm6 libxext6 libxrender1 libgl1-mesa-glx libglib2.0-0 \
        ffmpeg
else
    # CentOS/RHEL 9+
    # 先装 EPEL（ffmpeg 在 RPM Fusion Free；opencv 运行库在 EPEL/CRB）
    $PKG_INSTALL epel-release 2>/dev/null || $PKG_INSTALL 'https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm' 2>/dev/null || true

    # Enable CRB (CodeReady Builder for -devel packages)
    crb_enable="crb"
    (dnf config-manager --set-enabled "$crb_enable" 2>/dev/null) || \
    (dnf config-manager --set-enabled codeready-builder-for-rhel-9-$(uname -m)-rpms 2>/dev/null) || true

    # RPM Fusion Free (for ffmpeg)
    $PKG_INSTALL --nogpgcheck 'https://download1.rpmfusion.org/free/el/rpmfusion-free-release-9.noarch.rpm' 2>/dev/null || true

    $PKG_UPDATE || true

    $PKG_INSTALL \
        ca-certificates curl gnupg findutils tar xz \
        python3 python3-pip python3-devel python3-setuptools python3-wheel \
        nginx \
        gcc gcc-c++ make pkgconf-pkg-config \
        mesa-libGL libSM libXext libXrender glib2 \
        openssh-clients procps-ng

    # 尝试 dnf 装 ffmpeg，如果仓库没装成功就用静态二进制
    if ! command -v ffmpeg >/dev/null 2>&1; then
        log "ffmpeg not in repos, installing static binary..."
        FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
        FFMPEG_TMP="/tmp/ffmpeg-static.tar.xz"
        curl -fSL --retry 3 --connect-timeout 15 "$FFMPEG_URL" -o "$FFMPEG_TMP" 2>/dev/null || {
            # mirror fallback (国内源镜像)
            FFMPEG_URL="https://mirrors.tuna.tsinghua.edu.cn/github-release/BtbN/FFmpeg-Builds/LatestRelease/ffmpeg-master-latest-linux64-gpl.tar.xz"
            curl -fSL --retry 3 --connect-timeout 15 "$FFMPEG_URL" -o "$FFMPEG_TMP" || {
                echo "ERROR: cannot download ffmpeg. Install it manually then re-run." >&2
                exit 1
            }
        }
        FFMPEG_DIR="/tmp/ffmpeg-extract"
        rm -rf "$FFMPEG_DIR" && mkdir -p "$FFMPEG_DIR"
        tar -xJf "$FFMPEG_TMP" -C "$FFMPEG_DIR" --strip-components=1 2>/dev/null || {
            # BtbN build uses one level of nested directory
            tar -xJf "$FFMPEG_TMP" -C "$FFMPEG_DIR"
        }
        find "$FFMPEG_DIR" -maxdepth 3 -type f \( -name ffmpeg -o -name ffprobe \) -executable \
            -exec install -m 755 {} /usr/local/bin/ \;
        rm -rf "$FFMPEG_DIR" "$FFMPEG_TMP"
        hash -r
    fi
fi

command -v ffmpeg >/dev/null 2>&1 || { echo "FATAL: ffmpeg not available"; exit 1; }
log "ffmpeg: $(ffmpeg -version | head -1 | cut -c1-80)"
command -v nginx -v 2>&1 | head -1 || true

# ------------------------------------------------------------------
# 2. 应用用户 + 目录
# ------------------------------------------------------------------
log "[2/6] Creating app user '$APP_USER' and dir '$APP_DIR'..."
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd -r -s /usr/sbin/nologin -U -m "$APP_USER"
fi

mkdir -p "$APP_DIR"
mkdir -p "$APP_DIR/venv"
mkdir -p "$APP_DIR/web/uploads" "$APP_DIR/web/outputs"
mkdir -p "$APP_DIR/logs" "$APP_DIR/run"
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
chmod 755 "$APP_DIR"

# ------------------------------------------------------------------
# 3. Python venv + base pip
# ------------------------------------------------------------------
log "[3/6] Creating python venv..."
PY_BIN="$(command -v python3 || true)"
if [[ -z "$PY_BIN" ]]; then
    echo "FATAL: python3 not found after install" >&2; exit 1
fi
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
    sudo -u "$APP_USER" "$PY_BIN" -m venv "$APP_DIR/venv"
fi
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip setuptools wheel
log "python version in venv: $($APP_DIR/venv/bin/python --version)"

# ------------------------------------------------------------------
# 4. Nginx
# ------------------------------------------------------------------
log "[4/6] Configuring nginx..."
mkdir -p /var/log/nginx
TEMPLATE_FILE="$APP_DIR/deploy/nginx.conf.template"

if [[ -f "$TEMPLATE_FILE" ]]; then
    RENDERED="$(sed -e "s|{{DOMAIN_OR_IP}}|${DOMAIN_OR_IP}|g" "$TEMPLATE_FILE")"
else
    RENDERED="server {
    listen 80;
    server_name ${DOMAIN_OR_IP};
    client_max_body_size 600M;
    proxy_connect_timeout 60s;
    proxy_send_timeout 900s;
    proxy_read_timeout 900s;
    send_timeout 900s;
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}"
fi

if [[ "$IS_DEBIAN" == "yes" ]]; then
    NGINX_SITE="/etc/nginx/sites-available/badminton"
    echo "$RENDERED" > "$NGINX_SITE"
    ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/badminton
    rm -f /etc/nginx/sites-enabled/default
else
    # RHEL/CentOS 9 uses conf.d/*.conf
    NGINX_SITE="/etc/nginx/conf.d/badminton.conf"
    echo "$RENDERED" > "$NGINX_SITE"
    # remove default server block if present (it's in /etc/nginx/nginx.conf usually, so leave it)
    :
fi
nginx -t

# ------------------------------------------------------------------
# 5. Systemd service
# ------------------------------------------------------------------
log "[5/6] Rendering systemd badminton-web.service..."
SERVICE_TEMPLATE="$APP_DIR/deploy/badminton-web.service.template"
SERVICE_OUT="/etc/systemd/system/badminton-web.service"
if [[ -f "$SERVICE_TEMPLATE" ]]; then
    sed -e "s|{{APP_USER}}|${APP_USER}|g" \
        -e "s|{{APP_GROUP}}|${APP_GROUP}|g" \
        -e "s|{{APP_DIR}}|${APP_DIR}|g" \
        "$SERVICE_TEMPLATE" > "$SERVICE_OUT"
else
    cat > "$SERVICE_OUT" <<EOF
[Unit]
Description=Badminton Video Segment Web Service
After=network.target
[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${APP_DIR}
Environment="PATH=${APP_DIR}/venv/bin"
EnvironmentFile=-${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/gunicorn -c ${APP_DIR}/deploy/gunicorn.conf.py web.app:app
KillMode=mixed
TimeoutStopSec=300
Restart=always
RestartSec=5
StandardOutput=append:${APP_DIR}/logs/stdout.log
StandardError=append:${APP_DIR}/logs/stderr.log
[Install]
WantedBy=multi-user.target
EOF
fi
systemctl daemon-reload
systemctl enable badminton-web.service
systemctl enable nginx.service

# ------------------------------------------------------------------
# 6. Firewall (firewalld/ufw/iptables) - best-effort
# ------------------------------------------------------------------
log "[6/6] Firewall best-effort (non-fatal)..."
if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active firewalld >/dev/null 2>&1; then
    firewall-cmd --permanent --add-service=ssh >/dev/null 2>&1 || true
    firewall-cmd --permanent --add-service=http >/dev/null 2>&1 || true
    firewall-cmd --permanent --add-service=https >/dev/null 2>&1 || true
    firewall-cmd --permanent --add-port=443/tcp >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    log "  -> firewalld rules added"
fi
if command -v ufw >/dev/null 2>&1; then
    ufw allow 22/tcp comment SSH || true
    ufw allow 80/tcp comment HTTP || true
    ufw allow 443/tcp comment HTTPS || true
    log "  -> ufw rules added (not enabling automatically)"
fi
setenforce 0 >/dev/null 2>&1 || true
if command -v semanage >/dev/null 2>&1; then
    semanage port -a -t ssh_port_t -p tcp 443 2>/dev/null || true
    # SELinux: allow nginx to network-connect and write logs (httpd_can_network_connect=1)
    setsebool -P httpd_can_network_connect 1 2>/dev/null || true
fi

log "Remote bootstrap complete."
echo ""
echo "===================================="
echo " OS_ID        : $OS_ID $OS_VER ($PKG)"
echo " APP_USER/DIR : $APP_USER @ $APP_DIR"
echo " DOMAIN_OR_IP : $DOMAIN_OR_IP"
echo " ffmpeg       : $(command -v ffmpeg)"
echo " nginx conf   : ${NGINX_SITE}"
echo " systemd      : /etc/systemd/system/badminton-web.service (enabled)"
echo ""
echo " Next steps (run via deploy.sh/runner):"
echo "  sudo -u $APP_USER $APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt"
echo "  systemctl reload nginx || systemctl start nginx"
echo "  systemctl start badminton-web.service"
echo "  curl http://127.0.0.1/health"
echo "===================================="
