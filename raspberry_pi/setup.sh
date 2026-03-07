#!/usr/bin/env bash
# =============================================================================
# setup.sh – One-shot installer for USB2BLE MIDI Bridge on Raspberry Pi 3B
#
# Tested on: Raspberry Pi OS Lite / Desktop (Bullseye & Bookworm), 32-bit & 64-bit
#
# What this script does
# ---------------------
#   1. Installs system dependencies (BlueZ, ALSA dev headers, Python 3, DBus)
#   2. Creates a Python virtual-env at /opt/midi_bridge/venv
#   3. Installs Python packages (python-rtmidi, bless)
#   4. Copies the application files to /opt/midi_bridge/
#   5. Installs and enables the systemd service (auto-start on boot)
#   6. Configures BlueZ for BLE peripheral / GATT server operation
#
# Usage
# -----
#   cd raspberry_pi/
#   chmod +x setup.sh
#   sudo ./setup.sh
# =============================================================================

set -euo pipefail

APP_DIR="/opt/midi_bridge"
VENV_DIR="${APP_DIR}/venv"
SERVICE_FILE="/etc/systemd/system/midi_bridge.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colour helpers ─────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Root check ─────────────────────────────────────────────────────────────
[[ "$EUID" -eq 0 ]] || error "Please run as root:  sudo ./setup.sh"

# ── 1. System packages ─────────────────────────────────────────────────────
info "Updating package lists…"
apt-get update -qq

info "Installing system dependencies…"
apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    libasound2-dev \
    libdbus-1-dev \
    bluez \
    bluetooth \
    dbus

# ── 2. Enable & start Bluetooth ────────────────────────────────────────────
info "Enabling Bluetooth service…"
systemctl enable bluetooth
systemctl start  bluetooth

# Give the adapter time to come up
sleep 2

if hciconfig hci0 &>/dev/null; then
    info "Powering on Bluetooth adapter (hci0)…"
    hciconfig hci0 up || warn "hciconfig hci0 up failed (may already be up)"
else
    warn "hci0 not found – make sure the Raspberry Pi Bluetooth is not blocked."
    warn "Run:  sudo rfkill unblock bluetooth"
fi

# ── 3. Application directory ───────────────────────────────────────────────
info "Creating application directory at ${APP_DIR}…"
mkdir -p "${APP_DIR}"

info "Copying application files…"
for f in midi_bridge.py ble_midi.py usb_midi.py requirements.txt; do
    cp "${SCRIPT_DIR}/${f}" "${APP_DIR}/"
done

# ── 4. Python virtual environment ──────────────────────────────────────────
info "Creating Python virtual environment at ${VENV_DIR}…"
python3 -m venv "${VENV_DIR}"

info "Installing Python packages (this may take a few minutes)…"
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements.txt" --quiet

# ── 5. systemd service ─────────────────────────────────────────────────────
info "Installing systemd service…"
cp "${SCRIPT_DIR}/midi_bridge.service" "${SERVICE_FILE}"

# Patch ExecStart to use the venv Python
sed -i "s|^ExecStart=.*|ExecStart=${VENV_DIR}/bin/python3 ${APP_DIR}/midi_bridge.py|" \
    "${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable midi_bridge
systemctl restart midi_bridge

# ── 6. Status ──────────────────────────────────────────────────────────────
echo ""
info "=== Setup complete! ==="
echo ""
echo "  Service status : sudo systemctl status midi_bridge"
echo "  Live logs      : sudo journalctl -fu midi_bridge"
echo "  Stop bridge    : sudo systemctl stop midi_bridge"
echo "  Restart bridge : sudo systemctl restart midi_bridge"
echo ""
info "Plug in your Yamaha piano via USB and pair with 'USB2BLE MIDI Bridge' over BLE."
