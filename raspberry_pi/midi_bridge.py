#!/usr/bin/env python3
"""
USB2BLE MIDI Bridge — Raspberry Pi 3B entry point.

Mirrors the logic of main/main.c:
  1. Initialise the BLE MIDI GATT server (ble_midi_init).
  2. Open the USB MIDI port (usb_midi_init).
  3. Forward every USB MIDI message to BLE MIDI with a timestamp
     (usb_midi_data_callback).
  4. Poll for USB hot-plug: if the piano is disconnected, keep retrying
     every RECONNECT_INTERVAL_S seconds until it comes back.

Usage
-----
    python3 midi_bridge.py [--log-level DEBUG|INFO|WARNING|ERROR]

Run as a service
----------------
    See midi_bridge.service for a ready-made systemd unit.
"""

import argparse
import asyncio
import logging
import signal
import sys

from ble_midi import BLEMidi
from usb_midi import USBMidi

# How often (seconds) to retry when no USB MIDI device is found
RECONNECT_INTERVAL_S: float = 5.0


# ---------------------------------------------------------------------------
# Argument parsing & logging
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="USB2BLE MIDI Bridge for Raspberry Pi 3B",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 midi_bridge.py
  python3 midi_bridge.py --log-level DEBUG
""",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return parser.parse_args()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------


async def run() -> None:
    log = logging.getLogger("midi_bridge")
    loop = asyncio.get_running_loop()

    log.info("=== USB2BLE MIDI Bridge for Raspberry Pi 3B ===")
    log.info("Target: Yamaha Digital Piano  →  BLE MIDI")

    # ------------------------------------------------------------------ BLE
    ble = BLEMidi(loop=loop)
    log.info("Initialising BLE MIDI server…")
    await ble.init()

    # ------------------------------------------------------------------ USB MIDI callback
    # The rtmidi callback runs in a C++ thread; we must schedule the BLE send
    # on the asyncio event loop with call_soon_threadsafe() to stay
    # thread-safe (mirrors the FreeRTOS task model in main.c).
    def on_usb_midi(midi_data: bytes) -> None:
        loop.call_soon_threadsafe(ble.send_data, midi_data)

    # ------------------------------------------------------------------ USB
    usb = USBMidi(data_callback=on_usb_midi)

    # ------------------------------------------------------------------ Shutdown
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    # ------------------------------------------------------------------ Main loop
    log.info("BLE advertising started.  Plug in your Yamaha piano via USB.")
    log.info("Press Ctrl+C to stop.")

    while not stop_event.is_set():
        if not usb.is_connected():
            log.info("Scanning for USB MIDI device…")
            connected = usb.connect()
            if connected:
                log.info("USB MIDI device ready — bridging to BLE MIDI.")
            else:
                log.info(
                    "No USB MIDI device found.  Retrying in %.0f s…",
                    RECONNECT_INTERVAL_S,
                )
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=RECONNECT_INTERVAL_S
                    )
                except asyncio.TimeoutError:
                    pass
                continue

        # Device is connected — wait until shutdown or disconnect
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass  # still running, loop back to check is_connected()

    # ------------------------------------------------------------------ Cleanup
    usb.disconnect()
    await ble.stop()
    log.info("Bridge stopped.  Goodbye.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    args = _parse_args()
    _setup_logging(args.log_level)
    asyncio.run(run())


if __name__ == "__main__":
    main()
