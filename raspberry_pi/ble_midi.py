"""
BLE MIDI GATT server for Raspberry Pi.

Mirrors the behaviour of components/ble_midi/ble_midi.c:
  - Advertises as "USB2BLE MIDI Bridge" using the standard BLE MIDI UUIDs.
  - Accepts connections from any BLE MIDI client (iOS, Android, macOS …).
  - Adds a 13-bit timestamp to every outgoing packet (320 µs resolution),
    identical to the timestamp logic in main.c / ble_midi.c.
  - Exposes ble_midi_send_data() and ble_midi_set_callback() equivalents.

Requires: bless  (pip install bless)
BlueZ must be installed and the Bluetooth adapter must be powered on.
"""

import asyncio
import logging
import time
from typing import Callable, Optional

from bless import (  # type: ignore[import]
    BlessGATTCharacteristic,
    BlessServer,
    GATTAttributePermissions,
    GATTCharacteristicProperties,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Standard BLE MIDI UUIDs (same as ble_midi.c)
# ---------------------------------------------------------------------------
MIDI_SERVICE_UUID = "03B80E5A-EDE8-4B33-A751-6CE34EC4C700"
MIDI_CHAR_UUID = "7772E5DB-3868-4112-A1A9-F2669D106BF3"

# BLE MIDI timestamp resolution – one tick every 320 µs (= 1/3125 s)
_TIMESTAMP_TICK_US: int = 320
# 13-bit counter wraps at 8192
_TIMESTAMP_MASK: int = 0x1FFF


class BLEMidi:
    """
    BLE MIDI GATT server.

    Parameters
    ----------
    loop:
        The running asyncio event loop.  Required by bless on Linux.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._server: Optional[BlessServer] = None
        self._receive_callback: Optional[Callable[[bytes], None]] = None
        # Timestamp origin – microseconds since monotonic clock start
        self._ts_origin_us: int = time.monotonic_ns() // 1_000

    # ------------------------------------------------------------------
    # Public API (mirrors ble_midi.h)
    # ------------------------------------------------------------------

    async def init(
        self, max_retries: int = 10, retry_delay: float = 2.0
    ) -> None:
        """
        Initialise the BLE stack, register the MIDI GATT service, and start
        advertising.  Equivalent to ble_midi_init() in ble_midi.c.

        If BlueZ refuses to register the advertisement (e.g. a stale
        advertisement from a previous crash is still registered, or the
        adapter is not yet ready), retry up to *max_retries* times with
        exponential back-off (capped at 10 s) between attempts.

        On the first attempt the helper *_ensure_adapter_ready()* verifies
        (and if necessary powers on) the Bluetooth adapter.  After three
        consecutive failures the adapter is power-cycled via
        *_reset_adapter()* to clear any stale BlueZ state.
        """
        # Make sure the adapter is powered on before the first attempt.
        await self._ensure_adapter_ready()

        for attempt in range(1, max_retries + 1):
            server = BlessServer(name="USB2BLE MIDI Bridge", loop=self._loop)
            server.read_request_func = self._handle_read
            server.write_request_func = self._handle_write

            # ---- MIDI service --------------------------------------------
            await server.add_new_service(MIDI_SERVICE_UUID)

            # MIDI I/O characteristic: read + write-without-response + notify
            char_props = (
                GATTCharacteristicProperties.read
                | GATTCharacteristicProperties.write_without_response
                | GATTCharacteristicProperties.notify
            )
            char_perms = (
                GATTAttributePermissions.readable
                | GATTAttributePermissions.writeable
            )
            await server.add_new_characteristic(
                MIDI_SERVICE_UUID,
                MIDI_CHAR_UUID,
                char_props,
                None,  # initial value
                char_perms,
            )

            try:
                await server.start()
                self._server = server
                logger.info("BLE MIDI advertising as 'USB2BLE MIDI Bridge'")
                return
            except Exception as exc:
                logger.warning(
                    "Failed to register BLE advertisement (attempt %d/%d): %s",
                    attempt,
                    max_retries,
                    exc,
                )
                # Clean up the failed server before retrying
                try:
                    await server.stop()
                except Exception:
                    logger.debug("Ignoring error during server cleanup", exc_info=True)

                if attempt < max_retries:
                    # After 3 consecutive failures, power-cycle the adapter
                    # to clear any orphaned advertisements left by a
                    # previous crash.
                    if attempt == 3:
                        await self._reset_adapter()

                    delay = min(retry_delay * (2 ** (attempt - 1)), 10.0)
                    logger.info("Retrying in %.1f s…", delay)
                    await asyncio.sleep(delay)
                else:
                    raise RuntimeError(
                        f"Could not register BLE advertisement after "
                        f"{max_retries} attempts"
                    ) from exc

    def set_callback(self, callback: Callable[[bytes], None]) -> None:
        """Register a callback for MIDI data received from a BLE client."""
        self._receive_callback = callback

    def send_data(self, midi_data: bytes) -> None:
        """
        Wrap *midi_data* in a BLE MIDI packet (header + timestamp + bytes)
        and send it as a GATT notification to all subscribed clients.

        This method is safe to call from a non-asyncio thread via
        loop.call_soon_threadsafe().

        Equivalent to ble_midi_send_data() in ble_midi.c.
        """
        if self._server is None:
            return

        ts = self._get_timestamp()
        header, ts_byte = self._pack_timestamp(ts)
        packet = bytes([header, ts_byte]) + midi_data

        try:
            char = self._server.get_characteristic(MIDI_CHAR_UUID)
            if char is not None:
                char.value = bytearray(packet)
                self._server.update_value(MIDI_SERVICE_UUID, MIDI_CHAR_UUID)
                logger.debug("BLE MIDI tx: %s", packet.hex())
        except Exception:
            logger.exception("Failed to send BLE MIDI packet")

    async def stop(self) -> None:
        """Stop advertising and release BLE resources."""
        if self._server is not None:
            await self._server.stop()
            self._server = None
            logger.info("BLE MIDI server stopped")

    # ------------------------------------------------------------------
    # Bluetooth adapter helpers
    # ------------------------------------------------------------------

    async def _run_cmd(
        self, *args: str, timeout: float = 10
    ) -> tuple[int, str, str]:
        """Run a command and return (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )

    async def _ensure_adapter_ready(self) -> None:
        """Verify the Bluetooth adapter is powered on and discoverable.

        The method performs a multi-step initialisation that mirrors what
        ``bt_setup.sh`` does, so the bridge can self-heal even when the
        external script was not run (e.g. manual invocation).

        Steps:
          1. ``rfkill unblock bluetooth`` – clear any soft-block.
          2. ``hciconfig hci0 up``        – bring the HCI device up.
          3. ``bluetoothctl power on``     – power on via BlueZ (with retries).
          4. Set discoverable / pairable.
        """
        try:
            # ── Step 1: rfkill unblock ─────────────────────────────────
            try:
                rc, _, err = await self._run_cmd("rfkill", "unblock", "bluetooth")
                if rc == 0:
                    logger.debug("rfkill unblock bluetooth succeeded")
                else:
                    logger.debug("rfkill unblock bluetooth returned %d: %s", rc, err)
            except FileNotFoundError:
                logger.debug("rfkill not found — skipping unblock")

            # ── Step 2: hciconfig up ───────────────────────────────────
            try:
                rc, _, err = await self._run_cmd("hciconfig", "hci0", "up")
                if rc == 0:
                    logger.debug("hciconfig hci0 up succeeded")
                else:
                    logger.debug("hciconfig hci0 up returned %d: %s", rc, err)
                await asyncio.sleep(1)
            except FileNotFoundError:
                logger.debug("hciconfig not found — skipping HCI device up")

            # ── Step 3: bluetoothctl power on (with retries) ───────────
            powered = False
            for attempt in range(1, 6):
                rc, out, _ = await self._run_cmd("bluetoothctl", "show")
                if "Powered: yes" in out:
                    logger.debug("Bluetooth adapter is powered on")
                    powered = True
                    break

                logger.info(
                    "Bluetooth adapter is not powered on — "
                    "attempting to power on (attempt %d/5)…",
                    attempt,
                )
                await self._run_cmd("bluetoothctl", "power", "on")

                # Also retry hciconfig as a fallback
                try:
                    await self._run_cmd("hciconfig", "hci0", "up")
                except FileNotFoundError:
                    pass

                await asyncio.sleep(2)

            if not powered:
                # Final check
                rc, out, _ = await self._run_cmd("bluetoothctl", "show")
                if "Powered: yes" in out:
                    powered = True

            if not powered:
                logger.warning(
                    "Could not power on the Bluetooth adapter after 5 attempts. "
                    "Run 'sudo bt_setup.sh' or check 'rfkill list' / 'hciconfig -a'."
                )

            # ── Step 4: discoverable + pairable ────────────────────────
            await asyncio.sleep(1)

            # Register a "NoInputNoOutput" agent so BlueZ uses "Just Works"
            # pairing (no PIN dialog on Android or other clients).
            for cmd, label in (
                (["bluetoothctl", "agent", "NoInputNoOutput"], "agent NoInputNoOutput"),
                (["bluetoothctl", "default-agent"], "default-agent"),
                (["bluetoothctl", "discoverable", "on"], "discoverable on"),
                (["bluetoothctl", "discoverable-timeout", "0"], "discoverable-timeout 0"),
                (["bluetoothctl", "pairable", "on"], "pairable on"),
            ):
                rc, _, err = await self._run_cmd(*cmd)
                if rc != 0:
                    logger.debug("bluetoothctl %s returned %d: %s", label, rc, err)
                await asyncio.sleep(0.5)

            logger.debug("Adapter set to discoverable + pairable")

        except FileNotFoundError:
            logger.debug("bluetoothctl not found — skipping adapter readiness check")
        except Exception as exc:
            logger.warning("Could not verify Bluetooth adapter state: %s", exc)

    async def _reset_adapter(self) -> None:
        """Power-cycle the Bluetooth adapter to clear stale BlueZ state."""
        logger.info("Resetting Bluetooth adapter to clear stale state…")
        try:
            # Power off via bluetoothctl
            rc, _, err = await self._run_cmd("bluetoothctl", "power", "off")
            if rc != 0:
                logger.debug("bluetoothctl power off returned %d: %s", rc, err)

            # Also bring HCI device down/up as a more forceful reset
            try:
                await self._run_cmd("hciconfig", "hci0", "down")
            except FileNotFoundError:
                pass

            await asyncio.sleep(1)

            # rfkill unblock in case the power-off left a soft-block
            try:
                await self._run_cmd("rfkill", "unblock", "bluetooth")
            except FileNotFoundError:
                pass

            # Bring HCI device back up
            try:
                await self._run_cmd("hciconfig", "hci0", "up")
            except FileNotFoundError:
                pass

            await asyncio.sleep(1)

            # Power on via bluetoothctl
            rc, _, err = await self._run_cmd("bluetoothctl", "power", "on")
            if rc != 0:
                logger.debug("bluetoothctl power on returned %d: %s", rc, err)

            await asyncio.sleep(2)
        except FileNotFoundError:
            logger.debug("bluetoothctl not found — skipping adapter reset")
        except Exception as exc:
            logger.warning("Adapter reset failed: %s", exc)

    # ------------------------------------------------------------------
    # Timestamp helpers (mirrors get_current_timestamp / pack_timestamp
    # in main.c)
    # ------------------------------------------------------------------

    def _get_timestamp(self) -> int:
        """Return the current 13-bit BLE MIDI timestamp."""
        elapsed_us = (time.monotonic_ns() // 1_000) - self._ts_origin_us
        return (elapsed_us // _TIMESTAMP_TICK_US) & _TIMESTAMP_MASK

    @staticmethod
    def _pack_timestamp(ts: int) -> tuple[int, int]:
        """
        Encode a 13-bit timestamp into the two BLE MIDI header bytes.

        Header byte   : bit7=1 | ts[12:7] in bits 5:0
        Timestamp byte: bit7=1 | ts[6:0]  in bits 6:0
        """
        header = 0x80 | ((ts >> 7) & 0x3F)
        ts_byte = 0x80 | (ts & 0x7F)
        return header, ts_byte

    # ------------------------------------------------------------------
    # bless callbacks
    # ------------------------------------------------------------------

    def _handle_read(
        self, characteristic: BlessGATTCharacteristic, **kwargs: object
    ) -> bytearray:
        logger.debug("BLE MIDI read request")
        return characteristic.value or bytearray()

    def _handle_write(
        self,
        characteristic: BlessGATTCharacteristic,
        value: bytearray,
        **kwargs: object,
    ) -> None:
        logger.debug("BLE MIDI write rx: %s", value.hex())
        if self._receive_callback and len(value) >= 2:
            # Strip the two BLE MIDI header/timestamp bytes before forwarding
            self._receive_callback(bytes(value[2:]))
