"""BIO-4: a fake source transport -- configurable rate, injectable dropouts,
a realistic-looking heart-rate trace. Exists so BIO-2 through BIO-8 (per
plan.md's ticket table) are all developable and testable with no real radio
involved; it implements the same `Transport` interface `ble.py`/`lsl.py`
will later, so swapping it out is a config change (`BIOSENSOR_TRANSPORTS`),
not a code change.

The "realistic" part that matters for testing the rest of the pipeline is
the dropout behaviour, not the HR number itself: this is what exercises the
"never send a stale value" rule (plan.md section 4, point 3) end to end --
during a dropout window this transport stops calling `on_sample` entirely,
the same as a real strap losing skin contact.
"""

from __future__ import annotations

import asyncio
import logging
import random

from transports.base import (
    ConnectionState,
    DeviceProfile,
    OnSample,
    Sample,
    Transport,
    TransportStatus,
)

logger = logging.getLogger("biosensor_plugin.transports.fake")


class FakeTransport(Transport):
    name = "fake"

    def __init__(self, profile: DeviceProfile) -> None:
        super().__init__(profile)
        cfg = profile.config
        self._rate_hz: float = float(cfg.get("rate_hz", 1.0))
        self._baseline_bpm: float = float(cfg.get("baseline_bpm", 70.0))
        self._variance_bpm: float = float(cfg.get("variance_bpm", 3.0))
        self._dropout_probability: float = float(cfg.get("dropout_probability", 0.0))
        lo, hi = cfg.get("dropout_duration_s", [3.0, 8.0])
        self._dropout_duration_range: tuple[float, float] = (float(lo), float(hi))
        self._rng = random.Random(cfg.get("seed"))
        self._current_bpm = self._baseline_bpm
        self._started = False
        self._in_dropout = False

    def get_status(self) -> TransportStatus:
        if not self._started:
            return TransportStatus(state=ConnectionState.NO_DEVICE)
        if self._in_dropout:
            return TransportStatus(state=ConnectionState.PRESENT_NO_CONTACT, link_quality=None)
        return TransportStatus(state=ConnectionState.CONNECTED, link_quality=1.0)

    def _next_bpm(self) -> float:
        # Bounded random walk around the baseline -- not meant to be
        # physiologically accurate, only to look like a real trace (moves
        # smoothly, doesn't teleport) so downstream aggregation/plotting
        # code has something non-trivial to chew on.
        step = self._rng.gauss(0.0, self._variance_bpm / 3.0)
        self._current_bpm = max(35.0, min(200.0, self._current_bpm + step))
        return round(self._current_bpm, 1)

    async def stream(self, on_sample: OnSample) -> None:
        period = 1.0 / self._rate_hz if self._rate_hz > 0 else 1.0
        logger.info(
            f"{self.device_id}: streaming at {self._rate_hz:.2f} Hz "
            f"(dropout probability {self._dropout_probability:.3f}/tick)"
        )
        self._started = True
        try:
            while True:
                if self._dropout_probability > 0 and self._rng.random() < self._dropout_probability:
                    duration = self._rng.uniform(*self._dropout_duration_range)
                    logger.info(f"{self.device_id}: simulated contact loss for {duration:.1f}s")
                    # Deliberately does NOT call on_sample during this
                    # window -- see the module docstring and plan.md
                    # section 4, point 3.
                    self._in_dropout = True
                    await asyncio.sleep(duration)
                    self._in_dropout = False
                    continue

                bpm = self._next_bpm()
                await on_sample(Sample(channel="heart_rate_bpm", value=bpm))
                await asyncio.sleep(period)
        except asyncio.CancelledError:
            logger.info(f"{self.device_id}: stream cancelled.")
            raise
        finally:
            self._started = False
