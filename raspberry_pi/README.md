# USB2BLE MIDI Bridge – Raspberry Pi 3B

This folder contains a **Python port** of the ESP32-S3 firmware.  
It runs natively on a **Raspberry Pi 3B** (or any Raspberry Pi with Bluetooth LE and a USB port) under **Raspberry Pi OS** (Bullseye / Bookworm).

The bridge connects your **Yamaha Digital Piano** (or any USB-MIDI device) via USB and re-transmits all MIDI data over **Bluetooth LE MIDI**, so you can play wirelessly with any BLE MIDI client (GarageBand, Pianoteq, forScore, etc.).

---

## Hardware required

| Item | Notes |
|------|-------|
| Raspberry Pi 3B (or newer) | Built-in Bluetooth 4.1 (BLE) |
| USB-A cable | Piano → Raspberry Pi USB port |
| Yamaha Digital Piano | Tested: YDP-144 – any Yamaha with USB MIDI works |
| 5V power supply | For the Raspberry Pi |

No additional USB-to-Bluetooth dongle or wiring is needed.

---

## Architecture

```
┌──────────────────────┐
│   Yamaha Piano        │
│  (USB MIDI device)    │
└──────────┬───────────┘
           │ USB-A cable
           ▼
┌──────────────────────────────────────────┐
│           Raspberry Pi 3B                │
│                                          │
│  usb_midi.py                             │
│  ┌───────────────────────────────────┐  │
│  │ python-rtmidi  (ALSA backend)     │  │
│  │  • scans ALSA MIDI ports          │  │
│  │  • auto-selects Yamaha device     │  │
│  │  • hot-plug retry every 5 s       │  │
│  └──────────────┬────────────────────┘  │
│                 │ raw MIDI bytes         │
│  midi_bridge.py │ (thread-safe)          │
│                 ▼                        │
│  ble_midi.py                             │
│  ┌───────────────────────────────────┐  │
│  │ bless  (BlueZ / D-Bus backend)    │  │
│  │  • GATT server                    │  │
│  │  • 13-bit BLE MIDI timestamps     │  │
│  │  • Advertises "USB2BLE MIDI Bridge│  │
│  └──────────────┬────────────────────┘  │
│                 │ Bluetooth LE           │
└─────────────────┼────────────────────────┘
                  ▼
        ┌──────────────────┐
        │  BLE MIDI client  │
        │ (iOS/Android/Mac) │
        └──────────────────┘
```

| Python module | ESP-IDF equivalent |
|---|---|
| `usb_midi.py` | `components/usb_midi/usb_midi.c` |
| `ble_midi.py` | `components/ble_midi/ble_midi.c` |
| `midi_bridge.py` | `main/main.c` |

---

## Quick start

### 1. Install (automated)

```bash
git clone https://github.com/johanpedroo/USB2BLE_MIDI_Bridge.git
cd USB2BLE_MIDI_Bridge/raspberry_pi
chmod +x setup.sh
sudo ./setup.sh
```

`setup.sh` will:
- Install BlueZ, ALSA headers, and Python 3 packages
- Configure BlueZ for BLE peripheral mode (`/etc/bluetooth/main.conf` + `--experimental` flag)
- Create a virtual-env at `/opt/midi_bridge/venv`
- Copy the application to `/opt/midi_bridge/`
- Register and start the `midi_bridge` **systemd service** (auto-start on boot)

### 2. Verify

```bash
# Check the service is running
sudo systemctl status midi_bridge

# Watch live logs
sudo journalctl -fu midi_bridge
```

Expected output when the piano is plugged in:

```
[INFO]  BLE MIDI advertising as 'USB2BLE MIDI Bridge'
[INFO]  Scanning for USB MIDI device…
[INFO]  Yamaha device found on port 0: Yamaha Corporation YDP-144
[INFO]  USB MIDI device ready — bridging to BLE MIDI.
```

### 3. Connect from your device

1. Open a BLE MIDI app (e.g. GarageBand on iOS → Settings → Bluetooth MIDI Devices).
2. Tap **"USB2BLE MIDI Bridge"** — the device will connect within a few seconds.
3. Play the piano — MIDI notes will arrive wirelessly.

> **No Bluetooth password or PIN is required.**
> BLE MIDI uses the "Just Works" pairing model — you will not be asked for any password or PIN code when connecting.

---

## Manual run (without systemd)

```bash
cd /opt/midi_bridge          # or wherever you cloned the repo
source venv/bin/activate     # if you created a venv manually
python3 midi_bridge.py

# Verbose debug output:
python3 midi_bridge.py --log-level DEBUG
```

---

## Troubleshooting

### "The device doesn't appear in the iPhone / iPad Bluetooth settings"

BLE MIDI devices do **not** appear in the iOS *Settings → Bluetooth* list.
This is normal — iOS only shows classic Bluetooth devices there.

To find a BLE MIDI device you must look inside a **MIDI-enabled app**:
- **GarageBand** → Settings (⚙) → *Bluetooth MIDI Devices* → tap **"USB2BLE MIDI Bridge"**
- **Piano – Play & Learn Music** or similar apps may have their own Bluetooth MIDI scanner.

If the device still does not show up inside the app, see the "BLE advertising fails" section below.

### "Do I need a password or PIN to connect?"

No. BLE MIDI uses the Bluetooth "Just Works" pairing model. You should **not** be asked for any
password, PIN, or passkey when connecting. Simply tap **"USB2BLE MIDI Bridge"** in your BLE MIDI
app's device list and it will connect automatically.

If your device unexpectedly asks for a PIN, try the following:
- Remove the device from your system's Bluetooth paired-devices list and reconnect from your BLE MIDI app.
- On iOS, go to **Settings → Bluetooth**, tap the ⓘ next to the device, choose **Forget This Device**, then reconnect via your BLE MIDI app (not the system Bluetooth settings).

### "No ALSA MIDI ports found"
- Make sure the piano is powered on and the USB cable is firmly connected.
- Run `aconnect -l` to list ALSA MIDI ports.  The bridge rescans automatically every 5 seconds.
- If the piano appears as a USB audio device only, check `lsusb` and `amidi -l`.

### "BLE advertising fails / adapter not found"
```bash
sudo rfkill unblock bluetooth  # unblock if soft-blocked
sudo hciconfig hci1 up         # bring adapter up
sudo systemctl restart bluetooth
```

If re-running `setup.sh` doesn't help, verify that BlueZ is configured for BLE:
```bash
# Check /etc/bluetooth/main.conf contains:
#   ControllerMode = le
#   AutoEnable = true
#   DisablePlugins = sap,avrcp,bap
cat /etc/bluetooth/main.conf

# Check the --experimental flag is present
systemctl cat bluetooth | grep experimental

# Confirm the adapter is discoverable
bluetoothctl show  # should show: Discoverable: yes, Powered: yes
```

### bluetoothd logs errors about SAP, AVRCP, or BAP plugins

If you see messages like these in `sudo journalctl -fu bluetooth`:
```
profiles/audio/bap.c:bap_adapter_probe() BAP requires ISO Socket which is not enabled
profiles/audio/avrcp.c:avrcp_controller_server_probe() Unable to register AVRCP service record
profiles/sap/server.c:sap_server_register() Adding SAP SDP record to the SDP server failed.
```

These errors come from classic-Bluetooth plugins that are not needed in BLE-only mode.
The `setup.sh` script disables them via `DisablePlugins = sap,avrcp,bap` in `/etc/bluetooth/main.conf`.
If you upgraded BlueZ or reinstalled the system, re-run `sudo ./setup.sh` to restore the setting.

### Permission errors with BlueZ
Running as `root` (the default in `midi_bridge.service`) avoids most permission issues.  
To run as a non-root user, add the user to the `bluetooth` group and grant D-Bus policy access:

```bash
sudo usermod -aG bluetooth $USER
```

### Check Bluetooth adapter
```bash
hciconfig          # should show: hci1 ... UP RUNNING
bluetoothctl show  # should show: Powered: yes
```

---

## Service management

```bash
# Start / stop / restart
sudo systemctl start   midi_bridge
sudo systemctl stop    midi_bridge
sudo systemctl restart midi_bridge

# Enable / disable auto-start on boot
sudo systemctl enable  midi_bridge
sudo systemctl disable midi_bridge

# Logs
sudo journalctl -fu midi_bridge        # follow live
sudo journalctl -u  midi_bridge --since "1 hour ago"
```

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `python-rtmidi` | ≥ 1.5.0 | USB MIDI via ALSA |
| `bless` | ≥ 0.2.7 | BLE GATT server via BlueZ/D-Bus |

System packages: `bluez`, `bluetooth`, `libasound2-dev`, `libdbus-1-dev`, `python3-dev`
