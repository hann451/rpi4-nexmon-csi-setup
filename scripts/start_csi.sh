#!/usr/bin/env bash
set -euo pipefail

# Start Nexmon CSI capture on Raspberry Pi 4B (wlan0 + mon0).
# Usage: ./start_csi.sh [channel/bandwidth] [core_mask] [nss_mask]
# Example: ./start_csi.sh 36/80 1 1

CHAN_SPEC="${1:-36/80}"
CORE_MASK="${2:-1}"
NSS_MASK="${3:-1}"

if ! command -v makecsiparams >/dev/null 2>&1; then
  echo "Error: makecsiparams not found in PATH" >&2
  exit 1
fi

if ! command -v nexutil >/dev/null 2>&1; then
  echo "Error: nexutil not found in PATH" >&2
  exit 1
fi

sudo systemctl stop wpa_supplicant 2>/dev/null || true
sudo pkill wpa_supplicant 2>/dev/null || true
sleep 1

CSIPARAMS="$(makecsiparams -c "${CHAN_SPEC}" -C "${CORE_MASK}" -N "${NSS_MASK}")"
echo "CSIPARAMS=${CSIPARAMS}"

sudo ip link set wlan0 up
sudo nexutil -Iwlan0 -s500 -b -l34 -v"${CSIPARAMS}"

WIPHY="$(iw dev wlan0 info | awk '/wiphy/ {printf "phy" $2}')"
sudo iw phy "${WIPHY}" interface add mon0 type monitor 2>/dev/null || true
sudo ip link set mon0 up

sudo nexutil -Iwlan0 -k

echo ""
echo "CSI extractor is active on ${CHAN_SPEC} (core=${CORE_MASK}, nss=${NSS_MASK})."
echo "Run tcpdump on wlan0 to verify or record CSI packets (UDP port 5500):"
echo "  sudo tcpdump -i wlan0 dst port 5500 -vv -c 5"
echo "  sudo tcpdump -i wlan0 dst port 5500 -w csi_capture.pcap"
