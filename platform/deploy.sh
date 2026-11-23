#!/usr/bin/env bash
# Выкладка платформы Вершины: код приложения + формулы сборщика → /opt/platform/src,
# сборка и перезапуск. Данные на сервере (./data, .env) не трогаются.
#   platform/deploy.sh            сервер из DEPLOY_HOST (по умолчанию vershina)
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${DEPLOY_HOST:-vershina}"
DIR="${DEPLOY_DIR:-/opt/platform}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -r app mentors_import.py "$STAGE"/
cp platform/Dockerfile "$STAGE"/
cp -r ../vershina-kb/kb "$STAGE"/kb
find "$STAGE" -name '__pycache__' -prune -exec rm -rf {} + -o -name '*.bak-*' -exec rm -f {} +
echo "→ $HOST:$DIR"
ssh "$HOST" "mkdir -p '$DIR/src' '$DIR/data' && rm -rf '$DIR/src'/* && touch '$DIR/.env'"
tar -czf - -C "$STAGE" . | ssh "$HOST" "tar -xzf - -C '$DIR/src'"
scp -q platform/docker-compose.yml "$HOST:$DIR/"
ssh "$HOST" "cd '$DIR' && docker compose build -q && docker compose up -d && echo DEPLOY OK"
