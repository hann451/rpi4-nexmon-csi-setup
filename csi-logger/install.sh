#!/usr/bin/env bash
set -euo pipefail

# Install csi-logger daemon, rclone, and Tailscale on Raspberry Pi 4B.
# Run as root or with sudo: sudo bash install.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[1/6] Setting timezone to Asia/Tokyo..."
timedatectl set-timezone Asia/Tokyo

echo "[2/6] Ensuring dhcpcd ignores wlan0 and mon0..."
if ! grep -q "^denyinterfaces wlan0" /etc/dhcpcd.conf; then
  echo "denyinterfaces wlan0 mon0" >> /etc/dhcpcd.conf
fi

echo "[3/6] Installing rclone and curl..."
apt-get update
apt-get install -y rclone curl

echo "[4/6] Installing Tailscale if missing and setting iptables-legacy..."
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
else
  echo "Tailscale is already installed."
fi
if [[ -x /usr/sbin/iptables-legacy ]]; then
  update-alternatives --set iptables /usr/sbin/iptables-legacy || true
fi
if [[ -x /usr/sbin/ip6tables-legacy ]]; then
  update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy || true
fi
systemctl restart tailscaled || true

echo "[5/6] Installing csi-logger files to /opt/csi-logger..."
mkdir -p /opt/csi-logger
cp "${SCRIPT_DIR}/app.py" /opt/csi-logger/app.py
chmod +x /opt/csi-logger/app.py

if [[ -f "${SCRIPT_DIR}/../scripts/start_csi.sh" ]]; then
  cp "${SCRIPT_DIR}/../scripts/start_csi.sh" /opt/csi-logger/start_csi.sh
  chmod +x /opt/csi-logger/start_csi.sh
fi

mkdir -p /home/pi/csi_data /home/pi/.config/rclone
chown -R pi:pi /home/pi/csi_data /home/pi/.config/rclone

echo "[6/6] Enabling csi-logger.service..."
cp "${SCRIPT_DIR}/csi-logger.service" /etc/systemd/system/csi-logger.service
systemctl daemon-reload
systemctl enable csi-logger.service
systemctl restart csi-logger.service

echo ""
echo "Installation complete!"
systemctl status csi-logger.service --no-pager -l | head -n 15 || true
