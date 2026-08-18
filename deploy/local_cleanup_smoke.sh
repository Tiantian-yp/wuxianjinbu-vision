#!/bin/bash
set -euo pipefail
# 本机 smoke：把清理脚本当包跑一次 DRY_RUN=1，防止语法错误直接线上翻车
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE_DIR"
export APP_DIR="$BASE_DIR"
export DATA_DIR="${DATA_DIR:-$BASE_DIR/web}"
export DB_PATH="${DB_PATH:-$DATA_DIR/badminton.db}"
export UPLOAD_DIR="${UPLOAD_DIR:-$BASE_DIR/web/uploads}"
export OUTPUT_DIR="${OUTPUT_DIR:-$BASE_DIR/web/outputs}"
export RETENTION_DAYS="${RETENTION_DAYS:-7}"
export DRY_RUN=1
PY_BIN="${PY_BIN:-$(command -v python3)}"
mkdir -p "$DATA_DIR" "$UPLOAD_DIR" "$OUTPUT_DIR" "$BASE_DIR/logs"
echo "=== Local smoke (DRY_RUN) ==="
"$PY_BIN" deploy/cleanup_old_videos.py --days="$RETENTION_DAYS" 2>&1 | tail -n 30
echo "EXIT=$?"
