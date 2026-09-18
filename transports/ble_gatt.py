"""Pure parsing for the Bluetooth SIG Heart Rate Measurement characteristic
(0x2A37, part of the Heart Rate service 0x180D -- plan.md section 6.2's
first row). Kept separate from ble.py so it's testable without `bleak`, a
Bluetooth adapter, or a real strap -- see tests/test_ble_gatt_parser.py,
which round-trips synthetic byte sequences this module both encodes and
decodes (there's no live radio to generate real ones against in this
environment; see ble.py's module docstring for what that means for BIO-7's
verification status).

Byte layout (Bluetooth GATT Specification Supplement, Heart Rate
Measurement):

    byte 0       : flags
      bit 0        - Heart Rate Value Format (0 = UINT8 bpm, 1 = UINT16 bpm)
      bits 1-2     - Sensor Contact Status:
                       0 or 1 = feature not supported in this connection
                       2      = supported, contact NOT detected
                       3      = supported, contact detected
      bit 3        - Energy Expended present (UINT16, kJ)
      bit 4        - RR-Interval present (one or more UINT16, units of 1/1024 s)
      bits 5-7     - reserved
    byte 1 (or 1-2): Heart Rate Value (UINT8 or UINT16 per bit 0)
    next 2 bytes  : Energy Expended, if bit 3 set
    remaining     : zero or more UINT16 RR-Interval values, if bit 4 set
"""

from __future__ import annotations

from dataclasses import dataclass


class MalformedHeartRateMeasurement(ValueError):
    pass


@dataclass(frozen=True)
class HeartRateMeasurement:
    heart_rate_bpm: float
    rr_intervals_ms: tuple[float, ...]
    energy_expended_kj: float | None
    contact_supported: bool
    contact_detected: bool


def parse_heart_rate_measurement(data: bytes) -> HeartRateMeasurement:
    if not data:
        raise MalformedHeartRateMeasurement("empty characteristic value")

    flags = data[0]
    offset = 1
    hr_format_uint16 = bool(flags & 0x01)
    contact_status = (flags >> 1) & 0x03
    energy_expended_present = bool(flags & 0x08)
    rr_present = bool(flags & 0x10)

    if hr_format_uint16:
        if len(data) < offset + 2:
            raise MalformedHeartRateMeasurement("truncated before UINT16 heart rate value")
        heart_rate = int.from_bytes(data[offset : offset + 2], "little")
        offset += 2
    else:
        if len(data) < offset + 1:
            raise MalformedHeartRateMeasurement("truncated before UINT8 heart rate value")
        heart_rate = data[offset]
        offset += 1

    energy_expended_kj: float | None = None
    if energy_expended_present:
        if len(data) < offset + 2:
            raise MalformedHeartRateMeasurement("truncated before energy expended field")
        energy_expended_kj = float(int.from_bytes(data[offset : offset + 2], "little"))
        offset += 2

    rr_intervals_ms: list[float] = []
    if rr_present:
        remaining = data[offset:]
        if len(remaining) % 2 != 0:
            raise MalformedHeartRateMeasurement("RR-interval bytes are not a whole number of UINT16s")
        for i in range(0, len(remaining), 2):
            raw = int.from_bytes(remaining[i : i + 2], "little")
            # RR-Interval is in units of 1/1024 second (Bluetooth GATT spec).
            rr_intervals_ms.append(raw / 1024.0 * 1000.0)

    contact_supported = contact_status in (2, 3)
    contact_detected = contact_status == 3

    return HeartRateMeasurement(
        heart_rate_bpm=float(heart_rate),
        rr_intervals_ms=tuple(rr_intervals_ms),
        energy_expended_kj=energy_expended_kj,
        contact_supported=contact_supported,
        contact_detected=contact_detected,
    )
