"""End-to-end check for ui_server.py, the local live dashboard. Runs
`BiosensorPlugin` for real (fake transport, real mock Hub + mock ingest
server, same shape as test_smoke.py) and drives the dashboard's actual HTTP
API -- no mocking of ui_server.py itself, since the whole point is proving
a browser hitting this server would see the right thing.

Asserts:

1. `GET /` serves a real HTML page (this is what a future host "Open"
   button, or a person typing the URL by hand, would load).
2. `GET /api/state` reports the configured fake device, and it goes
   `connected: true` on its own -- confirming the dashboard reads live
   `DeviceWorker` state, not a snapshot frozen at startup.
3. `recent_samples` for that device actually grows between two polls
   (real numbers are really arriving, not just a status flag).
4. `active_session_id` is `null` before `session_start`, becomes the real
   session id once it fires, and goes back to `null` after `session_stop`
   -- exactly mirroring what test_smoke.py proves for the ingest API, but
   from the dashboard's own state instead.
5. `POST /api/scan/ble` (with a short timeout, so this test stays fast)
   starts a scan that finishes with either real results or a clean error
   (no adapter is the expected outcome in a sandboxed/no-Bluetooth
   environment; a real adapter elsewhere would instead return a device
   list) -- either way, `running` must return to `false` and the process
   must not crash.

Run: python tests/test_ui_server.py
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
logger = logging.getLogger("ui_smoke")

INGEST_PORT = 8198
UI_PORT = 8788
SESSION_ID = "ui_smoke_session"
CONSENT_ID = "consent-ui-smoke"


def _get(path: str) -> tuple[int, dict | bytes]:
    url = f"http://127.0.0.1:{UI_PORT}{path}"
    with urllib.request.urlopen(url) as r:
        body = r.read()
        content_type = r.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return r.status, json.loads(body)
        return r.status, body


def _post(path: str) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{UI_PORT}{path}"
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


async def main() -> int:
    failures: list[str] = []

    ingest_server = run_server("127.0.0.1", INGEST_PORT, token=None)
    hub = MockHub()
    hub_server = await hub.serve("127.0.0.1", 8767)

    profile_dir = Path(tempfile.mkdtemp(prefix="biosensor_ui_smoke_"))
    (profile_dir / "fake-source.json").write_text(
        json.dumps(
            {
                "id": "fake-source",
                "display_name": "Fake source (UI smoke test)",
                "transport": "fake",
                "match": {},
                "channels": {"heart_rate_bpm": {"parser": "fake.hr"}},
                "config": {"rate_hz": 10.0, "dropout_probability": 0.0},
            }
        )
    )

    config = Config(
        hub_ws_url="ws://127.0.0.1:8767/ws/hub/telemetry",
        ingest_base_url=f"http://127.0.0.1:{INGEST_PORT}",
        enabled_transports=["fake"],
        device_profile_dir=profile_dir,
        status_heartbeat_interval_s=0.5,
        ui_port=UI_PORT,
    )
    plugin = BiosensorPlugin(config)
    run_task = asyncio.create_task(plugin.run())
    await asyncio.sleep(0.5)  # let the dashboard server and device worker come up

    # 1. GET / serves real HTML.
    status, body = _get("/")
    if status != 200 or b"<html" not in body.lower() or b"biosensor-plugin-01" not in body:
        failures.append(f"GET / did not return the expected HTML page (status={status})")

    # 2/3. Device shows up, goes connected, and produces growing sample history.
    status, state = _get("/api/state")
    if status != 200:
        failures.append(f"GET /api/state returned {status}")
    if state.get("active_session_id") is not None:
        failures.append(f"active_session_id should be null before session_start, got {state.get('active_session_id')!r}")
    devices = state.get("devices", [])
    if len(devices) != 1 or devices[0].get("device_id") != "fake-source":
        failures.append(f"expected exactly one 'fake-source' device, got {devices}")
    else:
        first_count = len(devices[0].get("recent_samples", []))

    await asyncio.sleep(1.0)
    status, state2 = _get("/api/state")
    devices2 = state2.get("devices", [])
    if not devices2 or devices2[0].get("connected") is not True:
        failures.append(f"device never reported connected=True: {devices2}")
    if devices2 and len(devices2[0].get("recent_samples", [])) <= first_count:
        failures.append("recent_samples did not grow between two polls -- dashboard isn't seeing live data")
    if state.get("inactive_profiles"):
        failures.append(f"expected no inactive profiles, got {state['inactive_profiles']}")

    # 4. active_session_id tracks the real session lifecycle.
    await hub.session_start(SESSION_ID, CONSENT_ID)
    await asyncio.sleep(0.3)
    _, state3 = _get("/api/state")
    if state3.get("active_session_id") != SESSION_ID:
        failures.append(f"active_session_id should be {SESSION_ID!r} while open, got {state3.get('active_session_id')!r}")

    await hub.session_stop(SESSION_ID)
    await asyncio.sleep(0.3)
    _, state4 = _get("/api/state")
    if state4.get("active_session_id") is not None:
        failures.append(f"active_session_id should be null after session_stop, got {state4.get('active_session_id')!r}")

    # 5. BLE scan starts, runs, and always finishes cleanly (adapter or not).
    status, scan_start = _post("/api/scan/ble?timeout_s=1")
    if status not in (200, 202):
        failures.append(f"POST /api/scan/ble returned unexpected status {status}: {scan_start}")

    for _ in range(50):  # up to ~5s
        _, scan_state = _get("/api/scan/ble")
        if not scan_state.get("running"):
            break
        await asyncio.sleep(0.1)
    else:
        failures.append("BLE scan never finished (still 'running' after 5s)")

    if scan_state.get("running"):
        pass  # already recorded above
    elif scan_state.get("results") is None and scan_state.get("error") is None:
        failures.append(f"BLE scan finished with neither results nor an error: {scan_state}")
    else:
        kind = "results" if scan_state.get("results") is not None else "error"
        logger.info(f"BLE scan finished via {kind}: {scan_state}")

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

    print("\nPASS -- dashboard serves HTML, tracks live device/session state, and BLE scan completes cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
