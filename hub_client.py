"""BIO-5: session lifecycle over `/ws/hub/telemetry`.

This plugin subscribes to the same read-only Event Router every VRTelemetry
plugin does (see `plugins/PROTOCOL.md` in the host repo), but only cares
about two of its message types: `session_start` and `session_stop`. It has
no use for `telemetry_frame` (that's the host's own recording path, not
this plugin's concern) and, per plan.md section 3 and the host protocol's
compatibility rule 6, it **never sends anything back on this socket** --
v1 is Hub -> plugin only, and this plugin doesn't even have a response to
give.

Connection pattern (reconnect with exponential backoff, protocol_version
check, ignore-unknown-type) follows `plugins/medical-plugin-01/app.py` and
`plugins/ros-unity-bridge/bridge_node.py` in the host repo, which are the
two reference implementations of the host's compatibility rules
(`plugins/PROTOCOL.md` section 6).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

logger = logging.getLogger("biosensor_plugin.hub_client")

# The protocol_version this plugin was written against (plugin.json's own
# protocol_version must match). A message carrying a *higher* version than
# this is refused rather than guessed at -- see PROTOCOL.md section 2 and
# section 6, rule 2.
SUPPORTED_PROTOCOL_VERSION = 1

OnSessionStart = Callable[[str, str | None], Awaitable[None]]
OnSessionStop = Callable[[str], Awaitable[None]]


class HubSessionClient:
    """Owns the WebSocket connection to the Hub's Event Router. Calls
    `on_session_start(session_id, consent_id)` and `on_session_stop(session_id)`
    -- nothing else reaches the caller. `telemetry_frame`,
    `questionnaire_submitted`, and any message type this plugin doesn't
    recognise are ignored, per PROTOCOL.md section 6, rule 1."""

    def __init__(
        self,
        hub_ws_url: str,
        token: str | None,
        plugin_id: str,
        on_session_start: OnSessionStart,
        on_session_stop: OnSessionStop,
    ) -> None:
        self._hub_ws_url = hub_ws_url
        self._token = token
        self._plugin_id = plugin_id
        self._on_session_start = on_session_start
        self._on_session_stop = on_session_stop

    def _connect_url(self) -> str:
        url = self._hub_ws_url
        params = []
        if self._token:
            params.append(f"token={self._token}")
        params.append(f"plugin_id={self._plugin_id}")
        sep = "&" if "?" in url else "?"
        return url + sep + "&".join(params)

    async def run(self) -> None:
        """Run forever, reconnecting with backoff on any connection loss
        (PROTOCOL.md section 6, rule 4). Never raises on a connection
        problem -- only cancellation from the caller stops this."""
        url = self._connect_url()
        backoff = 1.0
        while True:
            try:
                logger.info(f"Connecting to Hub Event Router at {self._hub_ws_url}...")
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("Connected. Waiting for session_start/session_stop.")
                    backoff = 1.0
                    async for raw in ws:
                        await self._handle(raw)
            except (websockets.WebSocketException, OSError) as exc:
                logger.warning(f"Hub connection lost ({exc}); reconnecting in {backoff:.0f}s.")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    async def _handle(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.warning(f"Ignoring malformed message from Hub: {exc}")
            return

        version = message.get("protocol_version")
        if isinstance(version, int) and version > SUPPORTED_PROTOCOL_VERSION:
            logger.error(
                f"Hub is speaking protocol_version {version}, this plugin was written "
                f"against {SUPPORTED_PROTOCOL_VERSION}. Ignoring message rather than "
                "guessing at its shape -- see PROTOCOL.md section 2."
            )
            return

        msg_type = message.get("type")
        if msg_type == "session_start":
            session_id = message.get("session_id")
            if not session_id:
                logger.warning("session_start with no session_id; ignoring.")
                return
            consent_id = message.get("consent_id")
            if consent_id is None:
                # Known gap in the host's current wiring, not an error on
                # our end -- see PROTOCOL.md section 3's note under
                # session_start. Absence here says nothing about whether
                # consent exists; the Hub has already gated recording on it.
                logger.info(
                    f"Session {session_id} started (no consent_id on this event yet)."
                )
            else:
                logger.info(f"Session {session_id} started (consent {consent_id}).")
            await self._on_session_start(session_id, consent_id)
        elif msg_type == "session_stop":
            session_id = message.get("session_id")
            if not session_id:
                logger.warning("session_stop with no session_id; ignoring.")
                return
            logger.info(f"Session {session_id} stopped.")
            await self._on_session_stop(session_id)
        else:
            # telemetry_frame, questionnaire_submitted, or anything future
            # -- not this plugin's concern. Ignore rather than error, per
            # PROTOCOL.md section 6, rule 1.
            logger.debug(f"Ignoring event type {msg_type!r}.")
