"""BIO-8: LSL inbound transport (plan.md section 6.1 -- "build first" for
the reach it gets from existing LSL bridges for OpenBCI, Shimmer, Empatica,
Polar straps and others, and for LSL's own built-in clock synchronisation
across processes/machines).

Unlike BIO-7's BLE transport, this one is verified end to end in this
environment: `pylsl` needs no special hardware, so
tests/test_lsl_transport.py runs this class against a real
`pylsl.StreamOutlet` in the same process -- genuine discovery, genuine
push/pull, not a mock.

**Stream-loss detection is timeout-based, not exception-based** -- confirmed
empirically while building this: `pylsl.StreamInlet.pull_sample` does not
raise when its source disappears (this pylsl version has no `LostError` at
all). Destroying the outlet mid-stream just makes every subsequent
`pull_sample(timeout=X)` return `(None, None)` after waiting out the
timeout, indefinitely. So "lost" here means N consecutive timeouts in a
row, not a caught exception -- see `_pull_until_lost` below.

`t_wall_ns` uses `time.time_ns()` at the moment a sample is pulled, not a
conversion from LSL's own clock (`pylsl.local_clock()`). That satisfies
plan.md section 4 point 2's actual requirement (a real wall-clock reading,
not a monotonic counter) but leaves LSL's clock-sync feature -- the whole
reason section 6.1 calls this transport "build first" -- unused; a more
precise implementation would offset using `local_clock()` at both ends.
Noted as a follow-up, not a defect in what's here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pylsl

from transports.base import (
    ConnectionState,
    DeviceProfile,
    OnSample,
    Sample,
    Transport,
    TransportStatus,
)

logger = logging.getLogger("biosensor_plugin.transports.lsl")

DEFAULT_RESOLVE_TIMEOUT_S = 5.0
DEFAULT_PULL_TIMEOUT_S = 1.0
DEFAULT_MAX_CONSECUTIVE_TIMEOUTS = 5  # at pull_timeout_s=1.0s -> ~5s of silence


class LslTransport(Transport):
    name = "lsl"

    def __init__(self, profile: DeviceProfile) -> None:
        super().__init__(profile)
        self._stream_name: str | None = profile.match.get("stream_name")
        self._stream_type: str | None = profile.match.get("stream_type")
        if not self._stream_name and not self._stream_type:
            raise ValueError(
                f"device profile '{profile.id}': lsl transport needs "
                "match.stream_name or match.stream_type"
            )

        cfg = profile.config
        # LSL channel index (string, since it comes from JSON) -> our
        # channel name. Default assumes a single-channel stream whose one
        # channel is heart rate.
        raw_channel_map = cfg.get("channel_map", {"0": "heart_rate_bpm"})
        try:
            self._channel_map: dict[int, str] = {int(k): v for k, v in raw_channel_map.items()}
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"device profile '{profile.id}': channel_map keys must be channel "
                f"indices (e.g. \"0\"): {exc}"
            ) from exc

        self._resolve_timeout_s = float(cfg.get("resolve_timeout_s", DEFAULT_RESOLVE_TIMEOUT_S))
        self._pull_timeout_s = float(cfg.get("pull_timeout_s", DEFAULT_PULL_TIMEOUT_S))
        self._max_consecutive_timeouts = int(
            cfg.get("max_consecutive_timeouts", DEFAULT_MAX_CONSECUTIVE_TIMEOUTS)
        )
        self._state = ConnectionState.NO_DEVICE

    def get_status(self) -> TransportStatus:
        return TransportStatus(state=self._state)

    def _resolve(self) -> Any | None:
        # Blocking call -- only ever run via asyncio.to_thread (see
        # stream() below), never awaited directly.
        prop, value = (
            ("name", self._stream_name) if self._stream_name else ("type", self._stream_type)
        )
        streams = pylsl.resolve_byprop(prop, value, timeout=self._resolve_timeout_s)
        return streams[0] if streams else None

    async def stream(self, on_sample: OnSample) -> None:
        backoff = 1.0
        match_desc = (
            f"name={self._stream_name}" if self._stream_name else f"type={self._stream_type}"
        )
        try:
            while True:
                self._state = ConnectionState.NO_DEVICE
                stream_info = await asyncio.to_thread(self._resolve)
                if stream_info is None:
                    logger.info(
                        f"{self.device_id}: no LSL stream found matching {match_desc}; "
                        f"retrying in {backoff:.0f}s."
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(30.0, backoff * 2)
                    continue

                try:
                    await self._pull_until_lost(stream_info, on_sample)
                    # Reaching here means we connected and later detected
                    # the stream going quiet -- not a sign of trouble with
                    # this transport or the network, so don't carry a grown
                    # backoff into the next resolve attempt.
                    backoff = 1.0
                except (RuntimeError, OSError) as exc:
                    logger.warning(f"{self.device_id}: LSL error ({exc!r}); reconnecting.")
                    await asyncio.sleep(backoff)
                    backoff = min(30.0, backoff * 2)
                finally:
                    self._state = ConnectionState.NO_DEVICE
        except asyncio.CancelledError:
            logger.info(f"{self.device_id}: stream cancelled.")
            raise

    async def _pull_until_lost(self, stream_info: Any, on_sample: OnSample) -> None:
        inlet = await asyncio.to_thread(pylsl.StreamInlet, stream_info)
        self._state = ConnectionState.CONNECTED
        logger.info(
            f"{self.device_id}: connected to LSL stream '{stream_info.name()}' "
            f"({stream_info.channel_count()} channel(s)), streaming."
        )

        consecutive_timeouts = 0
        try:
            while True:
                sample, _lsl_timestamp = await asyncio.to_thread(
                    inlet.pull_sample, self._pull_timeout_s
                )
                if sample is None:
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= self._max_consecutive_timeouts:
                        logger.warning(
                            f"{self.device_id}: no data for "
                            f"{consecutive_timeouts * self._pull_timeout_s:.0f}s; "
                            "treating stream as lost."
                        )
                        return
                    # A quiet stream within the timeout window is not
                    # forward-filled -- we simply don't call on_sample this
                    # tick, which already satisfies plan.md section 4,
                    # point 3 by construction (we only ever send what we
                    # actually just received).
                    continue

                consecutive_timeouts = 0
                wall_ns = time.time_ns()
                for index, channel_name in self._channel_map.items():
                    if index < len(sample):
                        await on_sample(
                            Sample(channel=channel_name, value=float(sample[index]), t_wall_ns=wall_ns)
                        )
        finally:
            del inlet
