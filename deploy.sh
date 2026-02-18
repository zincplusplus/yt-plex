#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
set -a; source "$SCRIPT_DIR/.env"; set +a

echo "Deploying to $DEPLOY_TARGET:$DEPLOY_REMOTE_DIR..."
ssh "$DEPLOY_TARGET" "find $DEPLOY_REMOTE_DIR -mindepth 1 -not -path '*/data/*' -not -path '*/downloads/*' -delete 2>/dev/null; mkdir -p $DEPLOY_REMOTE_DIR"
scp -r "$SCRIPT_DIR"/* "$DEPLOY_TARGET:$DEPLOY_REMOTE_DIR/"
scp "$SCRIPT_DIR/.env" "$DEPLOY_TARGET:$DEPLOY_REMOTE_DIR/.env"
echo "Creating data backups on remote..."
ssh "$DEPLOY_TARGET" "cd $DEPLOY_REMOTE_DIR && mkdir -p data/backups && ts=\$(date +%Y%m%d_%H%M%S); [ -f data/queue.db ] && cp data/queue.db data/backups/queue.db.\$ts.bak || true; [ -f data/settings.json ] && cp data/settings.json data/backups/settings.json.\$ts.bak || true; [ -f data/sources.json ] && cp data/sources.json data/backups/sources.json.\$ts.bak || true"
echo "Rebuilding container..."
ssh "$DEPLOY_TARGET" "cd $DEPLOY_REMOTE_DIR && docker compose down && docker compose build --no-cache && docker compose up -d"
echo "Done."
