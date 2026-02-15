#!/usr/bin/env bash
# Deploy concept2-mqtt to the server and Pi from this machine.
# Usage: bash deploy.sh

set -euo pipefail

SERVER="${CONCEPT2_SERVER:-mac-mini-server.local}"
PI="${CONCEPT2_PI:-pirow.local}"
PI_USER="${CONCEPT2_PI_USER:-bjallen}"
SERVER_USER="${1:-$(whoami)}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Deploying to server ($SERVER) ==="
rsync -az --delete \
  --exclude 'data/' \
  "$SCRIPT_DIR/server/" "$SERVER_USER@$SERVER:~/concept2-mqtt/"
ssh "$SERVER_USER@$SERVER" bash -s << 'EOF'
set -euo pipefail
cd ~/concept2-mqtt
docker compose up -d --build
echo "Containers running:"
docker compose ps
EOF
echo "Server done."

echo ""
echo "=== Deploying to Pi ($PI) ==="
scp -r "$SCRIPT_DIR/pi" "$PI_USER@$PI:/tmp/concept2-pi"
ssh "$PI_USER@$PI" MQTT_BROKER="$SERVER" bash -s << 'DEPLOY_EOF'
set -euo pipefail
mkdir -p ~/concept2-monitor
cp -r /tmp/concept2-pi/* ~/concept2-monitor/
rm -rf /tmp/concept2-pi

# Write env file for the systemd service
sudo tee /etc/concept2-monitor.env > /dev/null << EOF
MQTT_BROKER=${MQTT_BROKER}
EOF

cd ~/concept2-monitor
sudo bash install.sh
DEPLOY_EOF
echo "Pi done."

echo ""
echo "=== All deployed ==="
echo "  Dashboard: http://$SERVER/"
echo "  Pi logs:   ssh $PI_USER@$PI 'journalctl -u concept2-monitor -f'"
