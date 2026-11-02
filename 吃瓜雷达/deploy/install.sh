#!/usr/bin/env bash
# 在目标服务器上执行：bash deploy/install.sh
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/guatan}"
PY="${PY:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y python3 curl
  else
    echo "请先安装 python3 和 curl" >&2
    exit 1
  fi
fi

cd "$APP_DIR"
mkdir -p data

if command -v systemctl >/dev/null 2>&1; then
  install -m 0644 deploy/guatan.service /etc/systemd/system/guatan.service
  systemctl daemon-reload
  systemctl enable guatan
  systemctl restart guatan
  sleep 2
  systemctl --no-pager --lines=5 status guatan || true
else
  nohup "$PY" app.py --host 0.0.0.0 --port 8788 > data/server.out 2>&1 &
  echo "systemd 不可用，已用 nohup 启动"
fi

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 8788/tcp >/dev/null || true
  echo "ufw 已放行 8788/tcp"
fi

sleep 1
curl -fsS "http://127.0.0.1:8788/api/health" && echo
