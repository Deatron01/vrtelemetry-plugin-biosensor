"""Development-only stand-in for the Hub's `/ws/hub/telemetry` endpoint.

Unlike the ingest API (BIO-2's mock_ingest_server.py), this endpoint
*already exists* on the real VRTelemetry host (see `plugins/PROTOCOL.md`:
"`/ws/hub/telemetry` is live on this checkout"). This file exists only
because this plugin's repository is developed and tested standalone, without
a running VRTelemetry checkout alongside it -- it is not a BIO ticket, and
it is not shipped as part of the plugin (`plugin.json`'s entrypoint is
`app.py`, never this). Point `HUB_WS_URL` at the real Hub instead of this
script the moment one is available.

Speaks the same envelope as the real Event Router
(`{"protocol_version": 1, "type": ..., ...}`), accepts the same
`?plugin_id=&token=` query string (and ignores both), and exposes
`session_start`/`session_stop`/`telemetry_frame` so hub_client.py's
"ignore what you don't need" behaviour has something to actually ignore
during a manual test.

Run standalone and drive it from stdin:

    python tools/mock_hub_ws.py
    # then, in the same terminal:
    start 20260918_120000 consent-abc
    stop 20260918_120000
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

import websockets

logger = logging.getLogger("mock_hub_ws")


class MockHub:
    """Importable half of this module -- tests/test_smoke.py drives this
    directly instead of shelling out, so it can await delivery rather than
    guessing at a sleep duration."""

    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._lock = asyncio.Lock()

    async def _register(self, ws: Any) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def _unregister(self, ws: Any) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message)
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send(payload)
            except websockets.WebSocketException:
                pass

    async def session_start(self, session_id: str, consent_id: str | None) -> None:
        msg: dict[str, Any] = {
            "protocol_version": 1,
            "type": "session_start",
            "session_id": session_id,
        }
        if consent_id is not None:
            msg["consent_id"] = consent_id
        await self._broadcast(msg)

    async def session_stop(self, session_id: str) -> None:
        await self._broadcast(
            {"protocol_version": 1, "type": "session_stop", "session_id": session_id}
        )

    async def telemetry_frame(self, session_id: str, frame: dict[str, Any]) -> None:
        await self._broadcast(
            {
                "protocol_version": 1,
                "type": "telemetry_frame",
                "session_id": session_id,
                "frame": frame,
            }
        )

    async def _handler(self, ws: Any) -> None:
        await self._register(ws)
        logger.info(f"Plugin connected ({ws.request.path if hasattr(ws, 'request') else ''})")
        try:
            async for raw in ws:
                # PROTOCOL.md section 6, rule 6: the real Event Router
                # reads and discards anything a plugin sends. Match that.
                logger.debug(f"Ignoring message from plugin: {raw!r}")
        finally:
            await self._unregister(ws)

    async def serve(self, host: str, port: int) -> websockets.WebSocketServer:
        return await websockets.serve(self._handler, host, port)


async def _stdin_driver(hub: MockHub) -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await asyncio.sleep(0.5)
            continue
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == "start" and len(parts) >= 2:
            consent_id = parts[2] if len(parts) > 2 else None
            await hub.session_start(parts[1], consent_id)
            print(f"sent session_start {parts[1]}")
        elif parts[0] == "stop" and len(parts) >= 2:
            await hub.session_stop(parts[1])
            print(f"sent session_stop {parts[1]}")
        else:
            print("commands: 'start <session_id> [consent_id]' | 'stop <session_id>'")


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    hub = MockHub()
    server = await hub.serve("127.0.0.1", 8765)
    logger.info("Mock Hub Event Router listening on ws://127.0.0.1:8765/ws/hub/telemetry")
    async with server:
        await _stdin_driver(hub)


if __name__ == "__main__":
    asyncio.run(_main())
