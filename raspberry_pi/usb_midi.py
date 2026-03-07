"""
USB MIDI reader for Raspberry Pi.

Mirrors the behaviour of components/usb_midi/usb_midi.c:
  - Opens the first available ALSA MIDI input port (preferring Yamaha devices).
  - Calls a user-supplied callback with raw MIDI bytes on every incoming message.
  - SysEx messages are forwarded; timing / active-sense messages are filtered out.
  - Supports connect() / disconnect() so the main loop can handle USB hot-plug.

Requires: python-rtmidi  (pip install python-rtmidi)
"""

import logging
from typing import Callable, Optional

import rtmidi  # python-rtmidi uses ALSA on Linux

logger = logging.getLogger(__name__)

# Keywords used to auto-select a Yamaha device (port 0 is used as fallback)
_YAMAHA_KEYWORDS = ("yamaha", "ydp", "p-", "p125", "p45", "clavinova", "piano")


class USBMidi:
    """
    USB / ALSA MIDI input handler.

    Parameters
    ----------
    data_callback:
        Called from the rtmidi callback thread with the raw MIDI bytes
        (e.g. b'\\x90\\x3c\\x7f' for a note-on).  The caller is responsible
        for scheduling any asyncio work with loop.call_soon_threadsafe().
    """

    def __init__(self, data_callback: Callable[[bytes], None]) -> None:
        self._callback = data_callback
        self._midi_in: Optional[rtmidi.MidiIn] = None
        self._connected = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Scan ALSA MIDI ports and open the best match.

        Returns True on success, False when no port is available.
        """
        # Always start fresh so we see newly plugged devices
        self.disconnect()

        midi_in = rtmidi.MidiIn(rtmidi.API_LINUX_ALSA)
        ports = midi_in.get_ports()

        if not ports:
            logger.warning("No ALSA MIDI ports found – is the piano plugged in?")
            del midi_in
            return False

        logger.info("Available ALSA MIDI ports: %s", ports)

        # Prefer a Yamaha / piano device; fall back to port 0
        selected = 0
        for i, name in enumerate(ports):
            if any(kw in name.lower() for kw in _YAMAHA_KEYWORDS):
                selected = i
                logger.info("Yamaha device found on port %d: %s", i, name)
                break
        else:
            logger.info("No Yamaha device detected – using port %d: %s", selected, ports[selected])

        try:
            midi_in.open_port(selected)
            # Forward SysEx; drop timing clock and active-sense noise
            midi_in.ignore_types(sysex=False, timing=True, active_sense=True)
            midi_in.set_callback(self._on_midi_data)
            self._midi_in = midi_in
            self._connected = True
            logger.info("USB MIDI connected: %s", ports[selected])
            return True
        except Exception:
            logger.exception("Failed to open MIDI port %d", selected)
            del midi_in
            return False

    def disconnect(self) -> None:
        """Close the current MIDI port and release resources."""
        if self._midi_in is not None:
            try:
                self._midi_in.close_port()
            except Exception:
                pass
            del self._midi_in
            self._midi_in = None
        self._connected = False

    def is_connected(self) -> bool:
        """Return True when a MIDI port is currently open."""
        return self._connected and self._midi_in is not None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_midi_data(self, message: tuple, data: object = None) -> None:
        """rtmidi callback – runs in the rtmidi background thread."""
        midi_bytes, _deltatime = message
        logger.debug("USB MIDI rx: %s", " ".join(f"{b:02X}" for b in midi_bytes))
        if self._callback:
            self._callback(bytes(midi_bytes))
