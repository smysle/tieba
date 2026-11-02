#!/usr/bin/env bash
# 用法：bash deploy/deploy.sh <host> [user] [key] [port]
#   bash deploy/deploy.sh 38.49.208.8 root ~/.ssh/newserver 22
set -euo pipefail
HOST="${1:?host}"
USER="${2:-root}"
KEY="${3:-}"
PORT="${4:-22}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAR="/tmp/guatan-deploy-$$.tgz"
SSH=(ssh -p "$PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
SCP=(scp -P "$PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
if [ -n "${PASSWORD:-}" ]; then
  command -v sshpass >/dev/null || { echo "需要 sshpass" >&2; exit 1; }
  SSH=(sshpass -p "$PASSWORD" "${SSH[@]}")
  SCP=(sshpass -p "$PASSWORD" "${SCP[@]}")
elif [ -n "$KEY" ]; then
  SSH+=(-i "$KEY")
  SCP+=(-i "$KEY")
fi

tar -czf "$TAR" -C "$ROOT" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='data' \
  --exclude='docs' --exclude='tests' \
  app.py gossip static tools deploy README.md requirements.txt

"${SSH[@]}" "$USER@$HOST" "mkdir -p /opt/guatan"
"${SCP[@]}" "$TAR" "$USER@$HOST:/tmp/guatan.tgz"
"${SSH[@]}" "$USER@$HOST" \
  "cd /opt/guatan && tar xzf /tmp/guatan.tgz && bash deploy/install.sh"
rm -f "$TAR"
echo "部署完成：http://$HOST:8788"
