"""BIO-7: BLE GATT Heart Rate service (0x180D) transport, via `bleak`
(plan.md section 6.2 -- "the consumer-strap default, no extra hardware").

**Not verified against real hardware.** There is no Bluetooth adapter or
Polar H10 in this development environment -- see plan.md's own
"Verification status" note at the top of the file. What *is* verified here:

- The GATT byte-level parsing (transports/ble_gatt.py) against synthetic
  round-tripped measurements -- tests/test_ble_gatt_parser.py.
- That this class uses `bleak`'s documented API correctly and fails the way
  plan.md section 1 requires when there's no adapter at all: by logging and
  retrying with backoff, never by raising out of `stream()` or blocking the
  caller -- tests/test_ble_transport_no_adapter.py runs this against a real,
  adapter-less `BleakScanner` in this environment (there genuinely is no
  Bluetooth adapter here, so this exercises the exact failure path a lab
  machine would hit if its adapter were off or missing).

The connect / notify / reconnect loop itself -- the part that only a real
strap can exercise -- has not been run. Don't read this docstring as "BIO-7
is done"; read the README's own status section, which says the same thing
in one place instead of two.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from transports.base import (
    ConnectionState,
    DeviceProfile,
    OnSample,
    Sample,
    Transport,
    TransportStatus,
)
from transports.ble_gatt import MalformedHeartRateMeasurement, parse_heart_rate_measurement

logger = logging.getLogger("biosensor_plugin.transports.ble")

HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
DEFAULT_DISCOVERY_TIMEOUT_S = 10.0


class BleTransport(Transport):
    name = "ble"

    def __init__(self, profile: DeviceProfile) -> None:
        super().__init__(profile)
        self._service_uuid: str | None = profile.match.get("service_uuid")
        self._name_prefix: str | None = profile.match.get("name_prefix")
        self._discovery_timeout_s: float = float(
            profile.config.get("discovery_timeout_s", DEFAULT_DISCOVERY_TIMEOUT_S)
        )
        self._state = ConnectionState.NO_DEVICE
        self._contact_detected: bool | None = None

    def get_status(self) -> TransportStatus:
        if self._state == ConnectionState.CONNECTED and self._contact_detected is False:
            return TransportStatus(state=ConnectionState.PRESENT_NO_CONTACT)
        return TransportStatus(state=self._state)

    def _matches(self, device: BLEDevice, advertisement_data: Any) -> bool:
        if self._name_prefix and not (device.name or "").startswith(self._name_prefix):
            return False
        if self._service_uuid:
            uuids = [u.lower() for u in (advertisement_data.service_uuids or [])]
            if self._service_uuid.lower() not in uuids:
                return False
        return True

    async def _discover(self) -> BLEDevice | None:
        return await BleakScanner.find_device_by_filter(
            self._matches, timeout=self._discovery_timeout_s
        )

    async def stream(self, on_sample: OnSample) -> None:
        backoff = 1.0
        try:
            while True:
                self._state = ConnectionState.NO_DEVICE
                self._contact_detected = None

                try:
                    device = await self._discover()
                except (BleakError, OSError, TimeoutError) as exc:
                    # OSError (e.g. FileNotFoundError) is what bleak's BlueZ
                    # backend actually raises when there's no D-Bus/BlueZ to
                    # talk to at all -- not a BleakError, discovered by
                    # running this against a real adapter-less machine (see
                    # tests/test_ble_transport_no_adapter.py). Caught here
                    # for the same reason it's caught in
                    # _connect_and_stream: this must never escape stream().
                    logger.warning(
                        f"{self.device_id}: BLE discovery failed ({exc!r}) -- is a Bluetooth "
                        "adapter present and powered on?"
                    )
                    device = None

                if device is None:
                    logger.info(
                        f"{self.device_id}: no matching device found; retrying in {backoff:.0f}s."
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(30.0, backoff * 2)
                    continue

                connected = await self._connect_and_stream(device, on_sample)

                self._state = ConnectionState.NO_DEVICE
                self._contact_detected = None
                if connected:
                    # We did reach a working connection -- the disconnect
                    # that just happened isn't evidence the device or
                    # adapter is having trouble, so don't punish the next
                    # attempt with a backoff that grew across a working
                    # session.
                    backoff = 1.0
                else:
                    await asyncio.sleep(backoff)
                    backoff = min(30.0, backoff * 2)
        except asyncio.CancelledError:
            logger.info(f"{self.device_id}: stream cancelled.")
            raise

    async def _connect_and_stream(self, device: BLEDevice, on_sample: OnSample) -> bool:
        """One connection attempt: connect, subscribe, wait until
        disconnected. Returns whether a connection was ever established
        (never raises, except CancelledError) -- the caller's backoff/retry
        loop takes it from there either way."""
        disconnected = asyncio.Event()
        loop = asyncio.get_running_loop()
        reached_connected = False

        def on_disconnect(_client: BleakClient) -> None:
            # bleak documents this callback as possibly invoked from a
            # different thread depending on backend -- call_soon_threadsafe
            # is correct regardless of which.
            loop.call_soon_threadsafe(disconnected.set)

        async def notify_callback(_char: Any, data: bytearray) -> None:
            try:
                measurement = parse_heart_rate_measurement(bytes(data))
            except MalformedHeartRateMeasurement as exc:
                logger.warning(f"{self.device_id}: malformed HR measurement ({exc}); dropping.")
                return

            self._contact_detected = (
                measurement.contact_detected if measurement.contact_supported else None
            )
            if measurement.contact_supported and not measurement.contact_detected:
                # Contact lost -- per plan.md section 4, point 3: stop
                # sending rather than forward a stale value.
                # self._contact_detected above still lets get_status()
                # report PRESENT_NO_CONTACT correctly during this gap.
                return

            await on_sample(Sample(channel="heart_rate_bpm", value=measurement.heart_rate_bpm))
            for rr_ms in measurement.rr_intervals_ms:
                await on_sample(Sample(channel="rr_interval_ms", value=rr_ms))

        try:
            async with BleakClient(device, disconnected_callback=on_disconnect) as client:
                await client.start_notify(HEART_RATE_MEASUREMENT_UUID, notify_callback)
                self._state = ConnectionState.CONNECTED
                reached_connected = True
                logger.info(
                    f"{self.device_id}: connected to {device.name or device.address}, streaming."
                )
                await disconnected.wait()
                logger.warning(f"{self.device_id}: device disconnected.")
        except (BleakError, OSError, TimeoutError) as exc:
            logger.warning(f"{self.device_id}: BLE connection error ({exc}); reconnecting.")

        return reached_connected
