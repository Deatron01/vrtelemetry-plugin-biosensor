"""biosensor-plugin-01 -- entrypoint.

Wires together everything built so far:

- `hub_client.HubSessionClient` (BIO-5): read-only session lifecycle over
  `/ws/hub/telemetry`. Only tells this plugin *when* a recording is open --
  see below for why that's no longer the same thing as "when a device is
  connected."
- `devices.registry` + `transports.*` (BIO-3/BIO-4/BIO-7/BIO-8): which
  physical (or fake) devices to run.
- `core.device_worker.DeviceWorker` (BIO-6/BIO-9): one per transport, for
  the whole life of this process -- not one per recording session. Device
  connectivity and sample forwarding are two different things now: see
  `core/device_worker.py`'s module docstring for the full rationale
  (in short, the host's real, now-implemented `GET /api/biosensors/status`
  is what `NewSession.tsx`'s "Hardware handshake" panel polls *before* a
  researcher presses Start, so a device that only connects once a recording
  begins would leave that panel wrong exactly when it's being looked at).
- `ingest_client.IngestClient`: pushes what the transports produce to the
  host's biosensor ingest API (normative contract:
  `plugins/BIOSENSOR_INGEST.md` in the host repo; in development, to
  `tools/mock_ingest_server.py`, BIO-2).
- `ui_server.py`: an optional local dashboard (loopback HTTP, on by
  default) showing every configured device's live status, its most recent
  samples, and an on-demand nearby-BLE-device scan -- see that module's
  docstring. Purely a local diagnostic; the host protocol doesn't know it
  exists.

The one invariant this whole plugin exists to protect (plan.md section 1):
**never fail, slow, or stop the telemetry recording running alongside it.**
Concretely here that means every device's worker is isolated -- a crash in
one device's transport is logged and that device's worker goes quiet, but
neither crashes the process nor blocks the hub_client's own read loop, nor
any other device's worker -- and this plugin never sends anything back on
the Hub WebSocket at all (hub_client.py already enforces that; app.py never
even gets the chance to).

Run standalone for development:

    python -m venv .venv
    .venv/Scripts/pip install -r requirements.txt      # Windows
    # .venv/bin/pip install -r requirements.txt          # macOS/Linux
    python tools/mock_ingest_server.py &                # BIO-2 stub, separate terminal
    python tools/mock_hub_ws.py &                       # dev-only Hub stand-in, separate terminal
    set HUB_WS_URL=ws://127.0.0.1:8765/ws/hub/telemetry
    set BIOSENSOR_INGEST_URL=http://127.0.0.1:8100
    python app.py
    # then open http://127.0.0.1:8787/ -- the local dashboard (ui_server.py),
    # on by default, independent of everything above being wired up correctly

In production this is launched by `PluginManager`, which sets `HUB_WS_URL`
and `HUB_PLUGIN_TOKEN` for you (see the host repo's
`plugins/PLUGIN_DEVELOPER_GUIDE.md`); `BIOSENSOR_INGEST_URL` would come from
this plugin's `registry.json` `env` block until the ingest base URL has a
PluginManager-standard variable of its own.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from config import Config
from core.device_worker import DeviceWorker
from core.session_context import SessionContext
from devices.registry import TRANSPORT_REGISTRY, build_transports, load_profiles
from hub_client import HubSessionClient
from ingest_client import IngestClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s biosensor-plugin-01 %(levelname)s %(message)s"
)
logger = logging.getLogger("biosensor_plugin.app")


class BiosensorPlugin:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._ingest = IngestClient(config.ingest_base_url, config.ingest_token)
        self._session_context = SessionContext()
        self._started_at_wall_ns = time.time_ns()

        self._profiles = load_profiles(config.device_profile_dir)
        transports = build_transports(self._profiles, config.enabled_transports)
        if not transports:
            logger.warning(
                "No transports available (check BIOSENSOR_TRANSPORTS and the device "
                "profile directory) -- this plugin will connect to the Hub and report "
                "an empty device list, but stream nothing for any session."
            )
        # Built once, for the process's whole lifetime -- see
        # core/device_worker.py's module docstring for why this is no
        # longer rebuilt per session.
        self._workers = [
            DeviceWorker(
                transport=transport,
                ingest=self._ingest,
                session_context=self._session_context,
                queue_maxsize=config.queue_maxsize,
            )
            for transport in transports
        ]

    async def on_session_start(self, session_id: str, consent_id: str | None) -> None:
        current = self._session_context.session_id
        if current == session_id:
            # PROTOCOL.md section 6, rule 4: don't assume exactly one
            # session_start/session_stop pair. A duplicate session_start
            # for a session already active is a no-op.
            logger.warning(f"session_start for {session_id} but it is already active; ignoring.")
            return
        if current is not None:
            # The host is not expected to overlap sessions, but if it ever
            # does, the newer session_start wins rather than this plugin
            # silently continuing to attribute samples to a session that
            # (from this plugin's point of view) never got a session_stop.
            logger.warning(
                f"session_start for {session_id} while {current} was still active "
                "(no session_stop seen for it); switching to the new session."
            )
        self._session_context.session_id = session_id
        logger.info(f"Session {session_id} active (consent_id={consent_id}); forwarding samples.")

    async def on_session_stop(self, session_id: str) -> None:
        if self._session_context.session_id != session_id:
            # A session_stop for a session this process never saw start (or
            # already switched away from) is expected and must not raise --
            # PROTOCOL.md section 6, rule 4.
            logger.debug(f"session_stop for {session_id} but it is not the active session; ignoring.")
            return
        self._session_context.session_id = None
        logger.info(f"Session {session_id} stopped; no longer forwarding samples (devices stay connected).")

    def dashboard_state(self) -> dict[str, Any]:
        """Everything `ui_server.py`'s dashboard page shows. Read-only,
        cheap, and safe to call from any thread -- every field here is
        either an immutable config value or a `DeviceWorker.dashboard_snapshot()`
        (which locks internally around the one thing that isn't, the
        recent-samples ring buffer)."""
        active_ids = {worker.transport.profile.id for worker in self._workers}
        inactive: list[dict[str, Any]] = []
        for profile in self._profiles:
            if profile.id in active_ids:
                continue
            if profile.transport not in self._config.enabled_transports:
                reason = f"transport '{profile.transport}' not enabled (BIOSENSOR_TRANSPORTS)"
            elif profile.transport not in TRANSPORT_REGISTRY:
                reason = f"transport '{profile.transport}' not implemented in this repo yet"
            else:
                reason = "skipped at startup -- malformed profile or failed to construct (see process logs)"
            inactive.append(
                {
                    "id": profile.id,
                    "display_name": profile.display_name,
                    "transport": profile.transport,
                    "reason": reason,
                }
            )

        return {
            "plugin_id": self._config.plugin_id,
            "started_at_wall_ns": self._started_at_wall_ns,
            "uptime_s": max(0.0, (time.time_ns() - self._started_at_wall_ns) / 1e9),
            "active_session_id": self._session_context.session_id,
            "ingest_base_url": self._config.ingest_base_url,
            "enabled_transports": self._config.enabled_transports,
            "devices": [worker.dashboard_snapshot() for worker in self._workers],
            "inactive_profiles": inactive,
        }

    async def _producer_heartbeat(self) -> None:
        # One call per producer, aggregating every device -- matches
        # `plugins/BIOSENSOR_INGEST.md` #2's `{"producer_id": ..., "devices":
        # [...]}}` shape. Runs for the whole process lifetime, independent of
        # any session, so `GET /api/biosensors/status` has something real to
        # report before a researcher ever presses Start.
        while True:
            await asyncio.sleep(self._config.status_heartbeat_interval_s)
            payload = {
                "producer_id": self._config.plugin_id,
                "devices": [worker.snapshot_status() for worker in self._workers],
            }
            await self._ingest.send_status(payload)

    async def run(self) -> None:
        for worker in self._workers:
            worker.start()
        heartbeat_task = asyncio.create_task(self._producer_heartbeat(), name="producer_heartbeat")

        ui_server = None
        if self._config.ui_enabled:
            # Imported here, not at module level, so a dashboard bug can
            # never prevent `import app` from succeeding at all -- and so
            # `BIOSENSOR_UI_ENABLED=0` genuinely avoids paying for the
            # import in a context that doesn't want it.
            import ui_server as ui_server_module

            ui_server = ui_server_module.run_server(self, self._config.ui_host, self._config.ui_port)
            logger.info(
                f"Live dashboard: http://{self._config.ui_host}:{self._config.ui_port}/ "
                "(local diagnostic only -- not part of any host protocol)"
            )

        client = HubSessionClient(
            hub_ws_url=self._config.hub_ws_url,
            token=self._config.hub_token,
            plugin_id=self._config.plugin_id,
            on_session_start=self.on_session_start,
            on_session_stop=self.on_session_stop,
        )
        try:
            await client.run()
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            for worker in self._workers:
                await worker.stop()
            if ui_server is not None:
                ui_server.shutdown()


def main() -> None:
    config = Config.from_env()
    plugin = BiosensorPlugin(config)
    try:
        asyncio.run(plugin.run())
    except KeyboardInterrupt:
        logger.info("Shutting down (Ctrl+C).")


if __name__ == "__main__":
    main()
