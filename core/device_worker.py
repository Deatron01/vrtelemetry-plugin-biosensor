"""BIO-6 + BIO-9, revised: owns one transport for the *plugin process's*
whole lifetime, not one recording session's.

This replaces the original `core/device_session.py` (`DeviceSession`), which
connected each transport only between `session_start` and `session_stop`.
That matched `plan.md` section 4's ticket description at the time it was
written, but the host's now-implemented contract
(`plugins/BIOSENSOR_INGEST.md` in the host repo) makes a different
requirement explicit: `GET /api/biosensors/status` is what the Electron
frontend's `NewSession.tsx` "Hardware handshake" panel polls to show a
researcher "no device" vs. "device present, no skin contact" *before* they
press Start -- and BLE discovery/connect can take several seconds, so
waiting for `session_start` to begin connecting would leave that panel
blank/stale right when it matters most. See `app.py`'s module docstring for
the resulting shape.

Two things a DeviceWorker keeps deliberately separate:

- **Connectivity** (`_produce`, `get_status()`/`snapshot_status()`): runs
  from `start()` to `stop()`, i.e. for as long as the plugin process itself
  runs. A device can be "connected" with samples flowing into the queue
  whether or not any recording is currently open.
- **Forwarding** (`_consume`): only actually POSTs a drained batch to
  `/api/biosensors/sample` when `session_context.session_id` is set. With no
  active session, batches are drained and discarded, not buffered -- there
  is no `session_id` to send them under, and holding them would mean a
  session that starts later gets a burst of now-stale pre-session readings
  instead of genuinely live ones (plan.md section 4, point 2: never forward
  a value across a gap that makes it look more current than it is).

The status *heartbeat* itself (`POST /api/biosensors/status`) is no longer
each worker's own task -- one worker's status is only half of what the host
wants in a single call (`{"producer_id": ..., "devices": [...]}}`, see
`plugins/BIOSENSOR_INGEST.md` #2), so `BiosensorPlugin` now owns one shared
heartbeat task that polls every worker's `snapshot_status()`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any

from core.sample_queue import BoundedSampleQueue
from core.session_context import SessionContext
from ingest_client import IngestClient
from transports.base import ConnectionState, Sample, Transport

logger = logging.getLogger("biosensor_plugin.device_worker")

# How many of the most recent samples ui_server.py's dashboard can show per
# device. Separate from BoundedSampleQueue's maxsize (which bounds what's
# *waiting to be sent*) -- this one only exists so a human looking at the
# dashboard can see "yes, real numbers are actually arriving," and is small
# on purpose: it's a debugging aid, not a data path.
RECENT_SAMPLES_MAXLEN = 200

# Cap on how many samples one POST carries, even if the queue holds more --
# keeps a single request's payload (and its retry cost) bounded regardless
# of queue_maxsize. Plan.md section 12, open question 1 leaves the real
# number as something to settle with the host; this is a conservative
# starting point for a slow (~1 Hz) channel and is revisited once a
# higher-rate transport (raw ECG at 130 Hz) is actually in use.
MAX_BATCH_SIZE = 50


def _connected_and_contact(state: ConnectionState) -> tuple[bool, bool | None]:
    """Map this repo's three-value ConnectionState onto the host's two
    separate booleans (`plugins/BIOSENSOR_INGEST.md` #2: `connected`,
    `contact`). `contact` is `null` whenever the device's own contact state
    isn't knowable -- either because there's no device at all, or because
    this transport never implemented status reporting (UNKNOWN, the
    Transport base class default) and reporting `False` would claim a
    negative we don't actually have evidence for."""
    if state == ConnectionState.CONNECTED:
        return True, True
    if state == ConnectionState.PRESENT_NO_CONTACT:
        return True, False
    if state == ConnectionState.NO_DEVICE:
        return False, None
    return False, None  # UNKNOWN


class DeviceWorker:
    def __init__(
        self,
        transport: Transport,
        ingest: IngestClient,
        session_context: SessionContext,
        queue_maxsize: int,
    ) -> None:
        self.transport = transport
        self._ingest = ingest
        self._session_context = session_context
        self._queue = BoundedSampleQueue(queue_maxsize)
        self._last_sample_wall_ns: int | None = None
        self._samples_sent = 0
        self._samples_discarded_no_session = 0
        self._batches_failed = 0
        self._tasks: list[asyncio.Task[None]] = []

        # Read by ui_server.py's HTTP handler threads while _on_sample
        # appends from the asyncio loop thread -- the lock is the only
        # thing making that safe across threads, not the GIL alone (a
        # dashboard reader mid-`list(deque)` while an appendleft-eviction
        # happens is exactly the kind of thing a lock exists to rule out).
        self._recent_lock = threading.Lock()
        self._recent_samples: deque[dict[str, Any]] = deque(maxlen=RECENT_SAMPLES_MAXLEN)

    @property
    def device_id(self) -> str:
        return self.transport.device_id

    def _on_sample(self, sample: Sample) -> None:
        self._last_sample_wall_ns = sample.t_wall_ns
        self._queue.put(sample)
        with self._recent_lock:
            self._recent_samples.append(sample.to_wire())

    async def _produce(self) -> None:
        async def on_sample(sample: Sample) -> None:
            # Synchronous body, no await -- see the module docstring.
            # Wrapped as async only because Transport.stream's OnSample
            # callback type is async (transports/base.py), so a future
            # transport that genuinely needs to await something in its
            # callback isn't blocked from doing so.
            self._on_sample(sample)

        await self.transport.stream(on_sample)

    async def _consume(self) -> None:
        while True:
            batch = await self._queue.get_batch(MAX_BATCH_SIZE)
            session_id = self._session_context.session_id
            if session_id is None:
                # No recording open right now -- nothing to attach this
                # batch to. Drop it rather than buffer it; see the module
                # docstring for why holding it for a later session is worse
                # than just losing it.
                self._samples_discarded_no_session += len(batch)
                continue
            ok = await self._ingest.send_sample(session_id, self.device_id, batch)
            if ok:
                self._samples_sent += len(batch)
            else:
                self._batches_failed += 1

    def snapshot_status(self) -> dict[str, Any]:
        """One entry of the `devices` array in the host's
        `POST /api/biosensors/status` (`plugins/BIOSENSOR_INGEST.md` #2).
        Safe to call at any time, session open or not -- that's the whole
        point of this refactor."""
        status = self.transport.get_status()
        connected, contact = _connected_and_contact(status.state)

        last_sample_age_ms: float | None = None
        if self._last_sample_wall_ns is not None:
            last_sample_age_ms = max(0.0, (time.time_ns() - self._last_sample_wall_ns) / 1e6)

        return {
            "device_id": self.device_id,
            "display_name": self.transport.profile.display_name,
            "transport": self.transport.name,
            "connected": connected,
            "contact": contact,
            "link_quality": status.link_quality,
            "last_sample_age_ms": last_sample_age_ms,
            "channels": sorted(self.transport.profile.channels.keys()),
        }

    def dashboard_snapshot(self, sample_limit: int = 50) -> dict[str, Any]:
        """Everything `snapshot_status()` has, plus the diagnostics only the
        local dashboard needs (`ui_server.py`) and the host's real
        `POST /api/biosensors/status` contract has no field for --
        `plugins/BIOSENSOR_INGEST.md` #2 defines that payload's shape
        exactly, and this is intentionally a superset kept out of it."""
        base = self.snapshot_status()
        with self._recent_lock:
            recent = list(self._recent_samples)[-sample_limit:]
        base.update(
            {
                "profile_id": self.transport.profile.id,
                "match": self.transport.profile.match,
                "samples_sent": self._samples_sent,
                "samples_dropped_by_queue": self._queue.dropped_count,
                "samples_discarded_no_session": self._samples_discarded_no_session,
                "batches_failed": self._batches_failed,
                "queue_depth": len(self._queue),
                "recent_samples": recent,
            }
        )
        return base

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._guarded(self._produce), name=f"{self.device_id}:produce"),
            asyncio.create_task(self._consume(), name=f"{self.device_id}:consume"),
        ]

    async def _guarded(self, coro_fn: Any) -> None:
        try:
            await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            # One device's transport crashing must never take down the
            # whole plugin process (plan.md section 1). This device's
            # `_consume` task keeps running (harmlessly idle -- `_produce`
            # is what feeds its queue) and its `snapshot_status()` keeps
            # being polled, so the host still sees this device go quiet
            # rather than just silently vanishing from the payload.
            logger.exception(
                f"Transport for device '{self.device_id}' crashed; "
                "this device stops streaming for the rest of the process."
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
