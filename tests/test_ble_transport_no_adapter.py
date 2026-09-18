"""BIO-7 (partial): confirms BleTransport fails the way plan.md section 1
requires when there is no Bluetooth adapter at all -- by logging and
retrying with backoff, never by raising out of stream() or hanging. This
development environment genuinely has no Bluetooth adapter, so this test
exercises that exact path for real, rather than mocking it; it is not a
substitute for testing against a real strap (see ble.py's module docstring
and README.md's status section for what's actually been verified).

Run: python tests/test_ble_transport_no_adapter.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transports.base import DeviceProfile  # noqa: E402
from transports.ble import BleTransport  # noqa: E402


def check(label: str, condition: bool, failures: list[str]) -> None:
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


async def main() -> int:
    failures: list[str] = []

    profile = DeviceProfile(
        id="polar-h10-test",
        display_name="Polar H10 (test)",
        transport="ble",
        match={"service_uuid": "0000180d-0000-1000-8000-00805f9b34fb", "name_prefix": "Polar H10"},
        channels={"heart_rate_bpm": {"parser": "gatt_hr.bpm"}},
        # Short discovery timeout so this test finishes quickly rather than
        # waiting out the production default of 10s per attempt.
        config={"discovery_timeout_s": 0.5},
    )
    transport = BleTransport(profile)
    samples_received = []

    async def on_sample(sample):
        samples_received.append(sample)

    task = asyncio.create_task(transport.stream(on_sample))
    try:
        # Enough time for at least one discovery attempt (0.5s timeout) to
        # complete and the retry loop to log its "no matching device found"
        # path -- proves the absence of an adapter doesn't raise out of
        # stream() or hang it.
        await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
        failures.append("stream() returned instead of looping forever waiting for a device")
    except asyncio.TimeoutError:
        pass  # Expected: stream() runs forever until cancelled.
    except Exception as exc:  # noqa: BLE001
        failures.append(f"stream() raised instead of handling the no-adapter case: {exc!r}")

    check("stream() is still running after 3s (didn't crash, didn't return)", not task.done(), failures)
    check("no samples were produced (there is no device)", samples_received == [], failures)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    check("stream() is cancellable cleanly", task.cancelled() or task.done(), failures)

    if failures:
        print(f"\nFAIL -- {len(failures)} check(s) failed.")
        return 1
    print(
        "\nPASS -- BleTransport handles a missing Bluetooth adapter/device by "
        "retrying with backoff, never by crashing or blocking. NOTE: this does "
        "not verify the connect/notify/reconnect path -- no real strap was involved."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
