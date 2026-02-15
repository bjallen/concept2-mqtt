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
scp -r "$SCRIPT_DIR/server" "$SERVER_USER@$SERVER:/tmp/concept2-server"
ssh "$SERVER_USER@$SERVER" bash -s << 'EOF'
set -euo pipefail
eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null || true)"

DEPLOY_DIR=~/Sites/concept2-mqtt
mkdir -p "$DEPLOY_DIR"
cp -r /tmp/concept2-server/* "$DEPLOY_DIR/"
rm -rf /tmp/concept2-server
cd "$DEPLOY_DIR"

# Install and configure Mosquitto via Homebrew
brew install mosquitto 2>/dev/null || true
cp mosquitto.conf "$(brew --prefix)/etc/mosquitto/mosquitto.conf"
brew services start mosquitto
echo "Mosquitto is running."

# Install Caddy via Homebrew
brew install caddy 2>/dev/null || true

# Install Python deps
pip3 install -q -r requirements.txt
echo "Python deps installed."

# Create launchd plist for the dashboard
PYTHON3=$(which python3)
PLIST=~/Library/LaunchAgents/com.concept2.dashboard.plist
cat > "$PLIST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.concept2.dashboard</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON3}</string>
        <string>${DEPLOY_DIR}/dashboard.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${DEPLOY_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${DEPLOY_DIR}/dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>${DEPLOY_DIR}/dashboard.log</string>
</dict>
</plist>
PLIST_EOF

# (Re)load the dashboard service
launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$PLIST"
echo "Dashboard service running on :8080"

# Start Caddy with the Caddyfile
caddy stop 2>/dev/null || true
cd "$DEPLOY_DIR"
caddy start --config Caddyfile >/dev/null 2>&1
echo "Caddy reverse proxy running on :80"

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
