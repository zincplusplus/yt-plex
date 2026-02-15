#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
set -a; source "$SCRIPT_DIR/.env"; set +a

echo "Deploying to $DEPLOY_TARGET:$DEPLOY_REMOTE_DIR..."
ssh "$DEPLOY_TARGET" "find $DEPLOY_REMOTE_DIR -mindepth 1 -not -path '*/data/*' -not -path '*/downloads/*' -delete 2>/dev/null; mkdir -p $DEPLOY_REMOTE_DIR"
scp -r "$SCRIPT_DIR"/* "$DEPLOY_TARGET:$DEPLOY_REMOTE_DIR/"
scp "$SCRIPT_DIR/.env" "$DEPLOY_TARGET:$DEPLOY_REMOTE_DIR/.env"
echo "Rebuilding container..."
ssh "$DEPLOY_TARGET" "cd $DEPLOY_REMOTE_DIR && docker compose down && docker compose up -d --build"
echo "Done."
