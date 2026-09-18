"""BIO-7 (partial): unit tests for transports/ble_gatt.py's byte-level
parsing, with no `bleak`, no adapter, no strap involved -- this is the part
of BIO-7 that's actually verified in this environment (see ble.py's module
docstring). Since there's no real Polar H10 here to capture a genuine
0x2A37 notification from, these round-trip synthetic measurements this file
both encodes and decodes -- a self-consistency check on the bit-shifting
logic, not a substitute for testing against a real device.

Run: python tests/test_ble_gatt_parser.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transports.ble_gatt import (  # noqa: E402
    MalformedHeartRateMeasurement,
    parse_heart_rate_measurement,
)


def encode_heart_rate_measurement(
    heart_rate_bpm: int,
    *,
    uint16_format: bool = False,
    contact_status: int = 3,  # 0/1=unsupported, 2=no contact, 3=contact
    energy_expended_kj: int | None = None,
    rr_intervals_ms: list[float] | None = None,
) -> bytes:
    """Test-only encoder -- the inverse of parse_heart_rate_measurement,
    used to build synthetic characteristic values. Not shipped in
    transports/ble_gatt.py: nothing in this plugin needs to *produce* a
    Heart Rate Measurement value, only consume one from a real strap."""
    flags = 0
    if uint16_format:
        flags |= 0x01
    flags |= (contact_status & 0x03) << 1
    if energy_expended_kj is not None:
        flags |= 0x08
    if rr_intervals_ms:
        flags |= 0x10

    out = bytearray([flags])
    if uint16_format:
        out += heart_rate_bpm.to_bytes(2, "little")
    else:
        out += bytes([heart_rate_bpm])
    if energy_expended_kj is not None:
        out += energy_expended_kj.to_bytes(2, "little")
    if rr_intervals_ms:
        for rr_ms in rr_intervals_ms:
            raw = round(rr_ms / 1000.0 * 1024.0)
            out += raw.to_bytes(2, "little")
    return bytes(out)


def check(label: str, condition: bool, failures: list[str]) -> None:
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


def close(a: float, b: float, tol: float = 0.5) -> bool:
    return abs(a - b) <= tol


def main() -> int:
    failures: list[str] = []

    # Simplest case: UINT8 bpm, contact detected, no extras.
    data = encode_heart_rate_measurement(72, contact_status=3)
    m = parse_heart_rate_measurement(data)
    check("uint8 bpm round-trips", m.heart_rate_bpm == 72.0, failures)
    check("contact_supported true", m.contact_supported is True, failures)
    check("contact_detected true", m.contact_detected is True, failures)
    check("no RR intervals when none encoded", m.rr_intervals_ms == (), failures)
    check("no energy expended when none encoded", m.energy_expended_kj is None, failures)

    # UINT16 bpm (a bpm > 255 needs it, e.g. during heavy exertion -- also
    # exercises the format bit).
    data = encode_heart_rate_measurement(210, uint16_format=True, contact_status=3)
    m = parse_heart_rate_measurement(data)
    check("uint16 bpm round-trips", m.heart_rate_bpm == 210.0, failures)

    # Contact NOT detected -- the case ble.py's notify_callback must treat
    # as "stop sending," per plan.md section 4 point 3.
    data = encode_heart_rate_measurement(70, contact_status=2)
    m = parse_heart_rate_measurement(data)
    check("contact_supported true when status=2", m.contact_supported is True, failures)
    check("contact_detected false when status=2", m.contact_detected is False, failures)

    # Contact feature not supported at all (most consumer straps).
    data = encode_heart_rate_measurement(70, contact_status=0)
    m = parse_heart_rate_measurement(data)
    check("contact_supported false when status=0", m.contact_supported is False, failures)
    check("contact_detected false (meaningless) when unsupported", m.contact_detected is False, failures)

    # Energy expended present.
    data = encode_heart_rate_measurement(80, contact_status=3, energy_expended_kj=1234)
    m = parse_heart_rate_measurement(data)
    check("energy_expended_kj round-trips", m.energy_expended_kj == 1234.0, failures)

    # RR intervals present -- one and several.
    data = encode_heart_rate_measurement(75, contact_status=3, rr_intervals_ms=[823.2])
    m = parse_heart_rate_measurement(data)
    check(
        "single RR interval round-trips (within quantisation)",
        len(m.rr_intervals_ms) == 1 and close(m.rr_intervals_ms[0], 823.2),
        failures,
    )

    data = encode_heart_rate_measurement(
        75, contact_status=3, rr_intervals_ms=[800.0, 810.5, 795.0]
    )
    m = parse_heart_rate_measurement(data)
    check(
        "multiple RR intervals round-trip in order",
        len(m.rr_intervals_ms) == 3
        and all(close(a, b) for a, b in zip(m.rr_intervals_ms, [800.0, 810.5, 795.0])),
        failures,
    )

    # Everything at once: uint16 bpm, contact detected, energy + RR.
    data = encode_heart_rate_measurement(
        188, uint16_format=True, contact_status=3, energy_expended_kj=42, rr_intervals_ms=[600.0, 610.0]
    )
    m = parse_heart_rate_measurement(data)
    check(
        "combined flags: bpm/energy/RR all round-trip together",
        m.heart_rate_bpm == 188.0
        and m.energy_expended_kj == 42.0
        and len(m.rr_intervals_ms) == 2,
        failures,
    )

    # Malformed input must raise a typed error, not crash with an IndexError
    # somewhere the caller doesn't expect (ble.py's notify_callback catches
    # exactly this type and drops the sample rather than crashing the
    # connection -- see its own comment).
    try:
        parse_heart_rate_measurement(b"")
        failures.append("empty input should raise MalformedHeartRateMeasurement")
        print("FAIL empty input should raise MalformedHeartRateMeasurement")
    except MalformedHeartRateMeasurement:
        print("ok   empty input raises MalformedHeartRateMeasurement")

    try:
        # Flags claim uint16 format but only 1 byte follows.
        parse_heart_rate_measurement(bytes([0x01, 0x50]))
        failures.append("truncated uint16 input should raise MalformedHeartRateMeasurement")
        print("FAIL truncated uint16 input should raise MalformedHeartRateMeasurement")
    except MalformedHeartRateMeasurement:
        print("ok   truncated uint16 input raises MalformedHeartRateMeasurement")

    if failures:
        print(f"\nFAIL -- {len(failures)} check(s) failed.")
        return 1
    print("\nPASS -- all BLE GATT parser checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
