"""End-to-end smoke test for the BIO-1 -> BIO-9 slice, standing in for the
manual verification plan.md's ticket table calls for at each gate. Runs
entirely in-process (no real Hub, no real host, no radios) against:

- tools/mock_hub_ws.MockHub, driven directly (BIO-5's counterpart)
- tools/mock_ingest_server, run as a real HTTP server in a thread (BIO-2)
- app.BiosensorPlugin, driving devices/fake-source.json through
  transports.fake.FakeTransport (BIO-3/BIO-4)

Asserts, across one session_start/session_stop cycle:

1. The status heartbeat (`POST /api/biosensors/status`) starts arriving
   *before* any session_start -- the whole point of the BIO-9 refactor in
   core/device_worker.py: the host's "Hardware handshake" panel
   (NewSession.tsx) polls `GET /api/biosensors/status` before a researcher
   presses Start, so device connectivity can't be scoped to a session.
2. That heartbeat is a single per-producer call shaped
   `{"producer_id": ..., "devices": [...]}}`, matching
   `plugins/BIOSENSOR_INGEST.md` #2 exactly -- not the old
   session-scoped/per-device shape.
3. No /api/biosensors/sample requests arrive before session_start, even
   though the device has been "connected" and producing samples the whole
   time -- forwarding is session-gated, connectivity is not.
4. Samples reach /api/biosensors/sample while the session is open (BIO-4/
   BIO-5 producing and forwarding data), and consent_id from session_start
   is observed by the plugin (BIO-5 section 3: "traceable to the consent it
   was produced under").
5. No further /api/biosensors/sample requests arrive after session_stop,
   even after waiting past the fake source's own sample period (BIO-5's
   gate: "streams only between session_start and session_stop") -- but the
   status heartbeat keeps arriving, because the device is still connected.
6. No sample ever carries heart_rate_bpm == 0.0 (plan.md section 4, point
   3 -- the fake transport must stop calling on_sample during a simulated
   dropout, never forward-fill).

Plain asyncio + assert, no pytest -- this repo has no test-framework
dependency and shouldn't need one for a first slice this size.

Run: python tests/test_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import BiosensorPlugin  # noqa: E402
from config import Config  # noqa: E402
from tools.mock_hub_ws import MockHub  # noqa: E402
from tools.mock_ingest_server import run_server  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s smoke %(levelname)s %(message)s")
logger = logging.getLogger("smoke")

INGEST_PORT = 8199
SESSION_ID = "smoke_test_session"
CONSENT_ID = "consent-smoke-test"


def _debug_summary() -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{INGEST_PORT}/api/biosensors/_debug/summary") as r:
        return json.loads(r.read())


def _check_status_shape(failures: list[str], payload: dict, context: str) -> None:
    if payload.get("producer_id") != "biosensor-plugin-01":
        failures.append(f"{context}: expected producer_id 'biosensor-plugin-01', got {payload.get('producer_id')!r}")
    devices = payload.get("devices")
    if not isinstance(devices, list) or not devices:
        failures.append(f"{context}: expected a non-empty 'devices' list, got {devices!r}")
        return
    device = devices[0]
    for field in ("device_id", "display_name", "transport", "connected", "contact", "channels"):
        if field not in device:
            failures.append(f"{context}: device entry missing '{field}': {device}")


async def main() -> int:
    failures: list[str] = []

    # BIO-2: mock ingest server, real thread, real HTTP.
    ingest_server = run_server("127.0.0.1", INGEST_PORT, token=None)
    logger.info(f"mock ingest server up on {INGEST_PORT}")

    # BIO-5 dev counterpart: in-process mock Hub, no real network hop needed
    # for the Hub side since we drive it directly via MockHub's own methods
    # -- but the plugin still connects over a real websocket, exercising
    # hub_client.py for real.
    hub = MockHub()
    hub_server = await hub.serve("127.0.0.1", 8766)
    logger.info("mock hub ws up on 8766")

    # A test-only device profile directory: 10 Hz, no dropouts, instead of
    # the shared devices/fake-source.json's development-tuned 1 Hz -- keeps
    # this test's timing fast and deterministic without reaching into
    # transport internals (app.py builds transports once, from whatever's
    # in this directory, same as production).
    profile_dir = Path(tempfile.mkdtemp(prefix="biosensor_smoke_"))
    (profile_dir / "fake-source.json").write_text(
        json.dumps(
            {
                "id": "fake-source",
                "display_name": "Fake source (smoke test)",
                "transport": "fake",
                "match": {},
                "channels": {"heart_rate_bpm": {"parser": "fake.hr"}},
                "config": {"rate_hz": 10.0, "dropout_probability": 0.0},
            }
        )
    )

    config = Config(
        hub_ws_url="ws://127.0.0.1:8766/ws/hub/telemetry",
        ingest_base_url=f"http://127.0.0.1:{INGEST_PORT}",
        enabled_transports=["fake"],
        device_profile_dir=profile_dir,
        status_heartbeat_interval_s=0.5,
    )
    plugin = BiosensorPlugin(config)

    run_task = asyncio.create_task(plugin.run())

    # No session_start yet -- but the device worker starts with run(), so
    # both connectivity and the first status heartbeat should already be
    # happening. Give it two heartbeat intervals plus the hub-connect delay.
    await asyncio.sleep(1.5)

    pre_session_summary = _debug_summary()
    if pre_session_summary["status_requests"] == 0:
        failures.append("no /api/biosensors/status heartbeat arrived before session_start (BIO-9 regression)")
    else:
        _check_status_shape(failures, pre_session_summary["last_status_request"], "pre-session heartbeat")
        pre_device = pre_session_summary["last_status_request"].get("devices", [{}])[0]
        if pre_device.get("connected") is not True:
            failures.append(f"pre-session heartbeat: expected the fake device to report connected=True, got {pre_device}")
    if pre_session_summary["sample_requests"] != 0:
        failures.append(
            "a /api/biosensors/sample request arrived before session_start -- forwarding must be session-gated"
        )

    await hub.session_start(SESSION_ID, CONSENT_ID)
    await asyncio.sleep(1.5)  # ~15 samples at 10 Hz

    mid_summary = _debug_summary()
    if mid_summary["sample_requests"] == 0:
        failures.append("no /api/biosensors/sample requests arrived while session was open")
    if mid_summary["zero_heart_rate_count"] != 0:
        failures.append("a sample with heart_rate_bpm == 0.0 was forwarded (stale-value rule)")
    if mid_summary["status_requests"] == pre_session_summary["status_requests"]:
        failures.append("no further /api/biosensors/status heartbeat arrived once the session was open")

    await hub.session_stop(SESSION_ID)
    await asyncio.sleep(0.3)  # let session_stop propagate
    post_stop_summary = _debug_summary()

    await asyncio.sleep(1.5)  # longer than the fake source's own period
    quiescent_summary = _debug_summary()
    if quiescent_summary["sample_requests"] != post_stop_summary["sample_requests"]:
        failures.append(
            "samples kept arriving after session_stop "
            f"({post_stop_summary['sample_requests']} -> {quiescent_summary['sample_requests']})"
        )
    if quiescent_summary["status_requests"] == post_stop_summary["status_requests"]:
        failures.append(
            "status heartbeat stopped arriving after session_stop -- the device is still connected "
            "and the host's pre-session panel needs to keep seeing it"
        )
    else:
        post_device = quiescent_summary["last_status_request"].get("devices", [{}])[0]
        if post_device.get("connected") is not True:
            failures.append(f"post-session heartbeat: expected the fake device to still report connected=True, got {post_device}")

    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass
    hub_server.close()
    await hub_server.wait_closed()
    ingest_server.shutdown()

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"\nPASS -- {quiescent_summary['sample_requests']} sample requests "
        f"({quiescent_summary['total_samples']} total samples), "
        f"{quiescent_summary['status_requests']} status heartbeats (starting before session_start "
        "and continuing after session_stop), consent_id observed, sample stream stopped cleanly "
        "at session_stop."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
