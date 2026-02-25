#!/usr/bin/env bash
# Deploy concept2-mqtt to the Mac Mini.
# Usage: bash deploy.sh

set -euo pipefail

SERVER="${CONCEPT2_SERVER:-mac-mini-server.local}"
SERVER_USER="${1:-$(whoami)}"
DEPLOY_DIR="~/Sites/concept2-mqtt"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Deploying to $SERVER ==="

# Sync server files (dashboard, Docker)
echo "Syncing server files..."
rsync -az --delete \
  --exclude 'data/' \
  "$SCRIPT_DIR/server/" "$SERVER_USER@$SERVER:$DEPLOY_DIR/"

# Sync monitor files
echo "Syncing monitor files..."
rsync -az \
  --exclude '__pycache__/' \
  "$SCRIPT_DIR/pi/monitor.py" \
  "$SCRIPT_DIR/pi/requirements.txt" \
  "$SCRIPT_DIR/pi/test_polar.py" \
  "$SERVER_USER@$SERVER:$DEPLOY_DIR/"

# Install/update launchd plist
echo "Updating launchd plist..."
scp "$SCRIPT_DIR/pi/com.concept2.monitor.plist" \
  "$SERVER_USER@$SERVER:~/Library/LaunchAgents/com.concept2.monitor.plist"

# Rebuild containers and restart monitor
ssh "$SERVER_USER@$SERVER" bash -s << 'EOF'
set -euo pipefail
cd ~/Sites/concept2-mqtt

# Rebuild dashboard container
docker compose up -d --build
echo "Containers running:"
docker compose ps

# Restart monitor
launchctl stop com.concept2.monitor 2>/dev/null || true
sleep 1
echo "Monitor restarted (PID: $(launchctl list | grep concept2.monitor | awk '{print $1}'))"
EOF

echo ""
echo "=== Deployed ==="
echo "  Dashboard: http://$SERVER/"
echo "  Monitor:   ssh $SERVER_USER@$SERVER 'tail -f $DEPLOY_DIR/monitor.log'"
