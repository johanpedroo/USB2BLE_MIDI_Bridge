#!/usr/bin/env bash
# =============================================================================
# bt_setup.sh – Initialise the USB Bluetooth adapter (hci0) for BLE MIDI
#
# This script is designed to be run:
#   • Manually:       sudo ./bt_setup.sh
#   • By systemd:     ExecStartPre in midi_bridge.service
#
# It performs the following steps:
#   1. Unblock Bluetooth via rfkill (clears soft-block)
#   2. Ensure the bluetooth service is running
#   3. Bring up the HCI device (hci0) via hciconfig
#   4. Power on the adapter and configure discoverable / pairable via
#      bluetoothctl, with retries for slow-starting adapters
#
# Exit codes:
#   0 – adapter ready
#   1 – adapter could not be brought up after all retries
# =============================================================================

set -uo pipefail

# ── Configuration ──────────────────────────────────────────────────────────
HCI_DEV="${BT_HCI_DEV:-hci0}"      # override with BT_HCI_DEV env var
MAX_RETRIES=10                      # total attempts for power-on
RETRY_DELAY=2                       # seconds between retries

# ── Colour helpers ─────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[bt_setup]${NC} $*"; }
warn()  { echo -e "${YELLOW}[bt_setup]${NC} $*"; }
err()   { echo -e "${RED}[bt_setup]${NC} $*" >&2; }

# ── 1. rfkill – make sure Bluetooth is not soft-blocked ────────────────────
if command -v rfkill &>/dev/null; then
    info "Unblocking Bluetooth via rfkill…"
    rfkill unblock bluetooth 2>/dev/null || true
else
    warn "rfkill not found — skipping unblock step"
fi

# ── 2. Make sure the bluetooth service is active ───────────────────────────
if systemctl is-active --quiet bluetooth.service 2>/dev/null; then
    info "bluetooth.service is running"
else
    info "Starting bluetooth.service…"
    systemctl start bluetooth.service 2>/dev/null || true
    sleep 2
fi

# ── 3. Bring up the HCI device ────────────────────────────────────────────
if command -v hciconfig &>/dev/null; then
    info "Bringing up ${HCI_DEV} via hciconfig…"
    hciconfig "${HCI_DEV}" up 2>/dev/null || true
    sleep 1
else
    warn "hciconfig not found — relying on bluetoothctl only"
fi

# ── 4. Power on the adapter via bluetoothctl (with retries) ───────────────
powered_on=false

for attempt in $(seq 1 "${MAX_RETRIES}"); do
    # Check current state
    adapter_info=$(bluetoothctl show 2>/dev/null || true)

    if echo "${adapter_info}" | grep -q "Powered: yes"; then
        info "Bluetooth adapter is powered on (attempt ${attempt}/${MAX_RETRIES})"
        powered_on=true
        break
    fi

    info "Powering on adapter (attempt ${attempt}/${MAX_RETRIES})…"

    # Try bluetoothctl first
    bluetoothctl power on 2>/dev/null || true

    # Also try hciconfig as a fallback
    if command -v hciconfig &>/dev/null; then
        hciconfig "${HCI_DEV}" up 2>/dev/null || true
    fi

    sleep "${RETRY_DELAY}"
done

if ! "${powered_on}"; then
    # One last check — the adapter might have come up during the last sleep
    adapter_info=$(bluetoothctl show 2>/dev/null || true)
    if echo "${adapter_info}" | grep -q "Powered: yes"; then
        powered_on=true
        info "Bluetooth adapter is now powered on"
    else
        err "Could not power on the Bluetooth adapter after ${MAX_RETRIES} attempts"
        err "Check 'hciconfig -a' and 'rfkill list' for details"
        exit 1
    fi
fi

# ── 5. Discoverable & pairable ────────────────────────────────────────────
info "Setting adapter as discoverable + pairable…"

# Give the adapter a moment to settle after power-on
sleep 1

bluetoothctl discoverable on        2>/dev/null || true
sleep 0.5
bluetoothctl discoverable-timeout 0 2>/dev/null || true
sleep 0.5
bluetoothctl pairable on            2>/dev/null || true

# ── 6. Set MIDI appearance (0x0877) ───────────────────────────────────────
# Mirrors the MIDI appearance in the raw scan response data used by the
# ESP32 firmware (PR #17).  Setting the adapter appearance helps
# iOS / Android BLE MIDI-specific scanners identify this device.
if command -v btmgmt &>/dev/null; then
    info "Setting adapter appearance to MIDI (0x0877)…"
    btmgmt appearance 0x0877 2>/dev/null || true
else
    warn "btmgmt not found — skipping MIDI appearance configuration"
fi

info "Bluetooth adapter (${HCI_DEV}) is ready for BLE MIDI."
exit 0
