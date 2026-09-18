"""BIO-8: end-to-end test of LslTransport against a real `pylsl.StreamOutlet`
in this same process -- genuine LSL discovery and push/pull, no mocking.
Unlike BIO-7's BLE transport, LSL needs no special hardware, so this is a
real verification, not just a parser/no-adapter check.

Covers:
1. Discovery + streaming: a real outlet pushing heart-rate-shaped values,
   received via LslTransport and forwarded as Sample(channel="heart_rate_bpm").
2. Stream-loss detection: destroying the outlet mid-stream is picked up via
   consecutive pull timeouts (see lsl.py's module docstring -- pylsl doesn't
   raise on source loss in this version) and status flips out of CONNECTED.
3. No stream found at all: LslTransport retries with backoff rather than
   raising or hanging.

Run: python tests/test_lsl_transport.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pylsl  # noqa: E402

from transports.base import ConnectionState, DeviceProfile  # noqa: E402
from transports.lsl import LslTransport  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s test %(levelname)s %(message)s")


def check(label: str, condition: bool, failures: list[str]) -> None:
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


async def test_discover_and_stream(failures: list[str]) -> None:
    stream_name = "SmokeTestHR"
    info = pylsl.StreamInfo(stream_name, "HeartRate", 1, 20.0, "float32", "smoke_uid_1")
    outlet = pylsl.StreamOutlet(info)
    stop = threading.Event()

    def pusher() -> None:
        i = 0
        while not stop.is_set():
            outlet.push_sample([70.0 + (i % 10)])
            i += 1
            time.sleep(0.05)

    pusher_thread = threading.Thread(target=pusher, daemon=True)
    pusher_thread.start()

    profile = DeviceProfile(
        id="lsl-smoke-test",
        display_name="LSL smoke test",
        transport="lsl",
        match={"stream_name": stream_name},
        channels={"heart_rate_bpm": {"parser": "lsl.channel0"}},
        config={"resolve_timeout_s": 5.0, "pull_timeout_s": 0.5, "max_consecutive_timeouts": 4},
    )
    transport = LslTransport(profile)
    received: list = []

    async def on_sample(sample):
        received.append(sample)

    task = asyncio.create_task(transport.stream(on_sample))
    await asyncio.sleep(2.0)

    check(
        "received samples from a real LSL outlet",
        len(received) > 5,
        failures,
    )
    check(
        "every sample landed on the heart_rate_bpm channel",
        all(s.channel == "heart_rate_bpm" for s in received),
        failures,
    )
    check(
        "status reports CONNECTED while streaming",
        transport.get_status().state == ConnectionState.CONNECTED,
        failures,
    )

    # Now kill the source and confirm loss is detected (status leaves
    # CONNECTED) within a bounded time, without the transport crashing.
    stop.set()
    pusher_thread.join()
    del outlet
    count_at_kill = len(received)

    # max_consecutive_timeouts=4 * pull_timeout_s=0.5s -> ~2s to detect loss,
    # give it margin.
    await asyncio.sleep(3.5)
    check(
        "no new samples after the outlet was destroyed",
        len(received) == count_at_kill,
        failures,
    )
    check(
        "status leaves CONNECTED after the stream is detected lost",
        transport.get_status().state != ConnectionState.CONNECTED,
        failures,
    )

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_no_stream_found(failures: list[str]) -> None:
    profile = DeviceProfile(
        id="lsl-nonexistent",
        display_name="LSL stream that doesn't exist",
        transport="lsl",
        match={"stream_name": "ThisStreamDoesNotExist_" + str(time.time())},
        channels={"heart_rate_bpm": {"parser": "lsl.channel0"}},
        config={"resolve_timeout_s": 0.5},
    )
    transport = LslTransport(profile)
    received: list = []

    async def on_sample(sample):
        received.append(sample)

    task = asyncio.create_task(transport.stream(on_sample))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        failures.append("stream() returned instead of retrying forever with no stream found")
    except asyncio.TimeoutError:
        pass
    except Exception as exc:  # noqa: BLE001
        failures.append(f"stream() raised instead of handling 'no stream found': {exc!r}")

    check("stream() keeps running when no matching LSL stream exists", not task.done(), failures)
    check("no samples produced (there is no stream)", received == [], failures)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_bad_profile_raises_clearly(failures: list[str]) -> None:
    profile = DeviceProfile(
        id="lsl-bad-profile",
        display_name="Missing match criteria",
        transport="lsl",
        match={},  # neither stream_name nor stream_type -- must be rejected early
        channels={},
        config={},
    )
    try:
        LslTransport(profile)
        failures.append("LslTransport should reject a profile with no stream_name/stream_type")
        print("FAIL LslTransport should reject a profile with no stream_name/stream_type")
    except ValueError:
        print("ok   LslTransport rejects a profile with no stream_name/stream_type")


async def main() -> int:
    failures: list[str] = []
    test_bad_profile_raises_clearly(failures)
    await test_no_stream_found(failures)
    await test_discover_and_stream(failures)

    if failures:
        print(f"\nFAIL -- {len(failures)} check(s) failed.")
        return 1
    print("\nPASS -- LslTransport verified end to end against a real pylsl outlet.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
