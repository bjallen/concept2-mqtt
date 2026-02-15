#!/usr/bin/env bash
# Deploy concept2-mqtt to the server and Pi from this machine.
# Usage: bash deploy.sh

set -euo pipefail

SERVER="${CONCEPT2_SERVER:-mac-mini-server.local}"
PI="${CONCEPT2_PI:-raspberrypi.local}"
PI_USER="${CONCEPT2_PI_USER:-bjallen}"
SERVER_USER="${1:-$(whoami)}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Deploying to server ($SERVER) ==="
scp -r "$SCRIPT_DIR/server" "$SERVER_USER@$SERVER:/tmp/concept2-server"
ssh "$SERVER_USER@$SERVER" bash -s << 'EOF'
set -euo pipefail
eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null || true)"
mkdir -p ~/concept2-mqtt
cp -r /tmp/concept2-server/* ~/concept2-mqtt/
rm -rf /tmp/concept2-server
cd ~/concept2-mqtt

# Install and configure Mosquitto via Homebrew
brew install mosquitto 2>/dev/null || true
cp mosquitto.conf "$(brew --prefix)/etc/mosquitto/mosquitto.conf"
brew services start mosquitto
echo "Mosquitto is running."

# Install Python deps for the consumer
pip3 install -q -r requirements.txt
echo "Server deps installed."
echo "To watch live data: cd ~/concept2-mqtt && python3 consumer.py"
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
echo "  Server: ssh $SERVER_USER@$SERVER 'cd ~/concept2-mqtt && python3 consumer.py'"
echo "  Pi logs: ssh $PI_USER@$PI 'journalctl -u concept2-monitor -f'"
