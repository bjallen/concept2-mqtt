#!/usr/bin/env bash
# Sets up the concept2 monitor as a systemd service on the Pi.
# Run: sudo bash install.sh

set -euo pipefail

SERVICE_NAME="concept2-monitor"
INSTALL_DIR="/opt/concept2-monitor"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing concept2 monitor..."

# System deps for pyrow (USB HID access)
apt-get update -qq
apt-get install -y -qq python3-venv python3-dev libusb-1.0-0-dev libudev-dev git

# Install app
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/monitor.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# udev rule so pyrow can access the PM5 without root
cat > /etc/udev/rules.d/99-concept2.rules << 'EOF'
# Concept2 Performance Monitor
SUBSYSTEM=="usb", ATTR{idVendor}=="17a4", MODE="0666"
EOF
udevadm control --reload-rules

# systemd service
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=Concept2 MQTT Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=-/etc/concept2-monitor.env
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/monitor.py
Restart=always
RestartSec=5
User=${SUDO_USER:-$(whoami)}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME"

echo "Done! Service is running."
echo "  Logs:    journalctl -u $SERVICE_NAME -f"
echo "  Stop:    sudo systemctl stop $SERVICE_NAME"
echo "  Restart: sudo systemctl restart $SERVICE_NAME"
