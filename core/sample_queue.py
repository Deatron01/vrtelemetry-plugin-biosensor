"""BIO-6: the bounded, drop-oldest sample queue described in plan.md
section 3 ("The sample queue is bounded and drops oldest") and required by
section 6, rule 3 of the host's compatibility rules ("Never blocks").

`put()` is synchronous and O(1) -- it is called directly from a transport's
`on_sample` callback (see transports/base.py), which may itself be called
from a BLE notification callback or an LSL pull loop. It must never await,
sleep, or raise for backpressure; the only allowed response to a full queue
is to silently evict the oldest sample and count the drop.
"""

from __future__ import annotations

import asyncio
from collections import deque

from transports.base import Sample


class BoundedSampleQueue:
    def __init__(self, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._deque: deque[Sample] = deque(maxlen=maxsize)
        self._dropped = 0
        self._not_empty = asyncio.Event()

    def put(self, sample: Sample) -> None:
        """Synchronous, O(1), never blocks. If the queue is already at
        capacity, `deque(maxlen=...)` silently evicts the oldest entry on
        append -- we only need to notice that it happened and count it."""
        if len(self._deque) == self._deque.maxlen:
            self._dropped += 1
        self._deque.append(sample)
        self._not_empty.set()

    async def get_batch(self, max_batch: int) -> list[Sample]:
        """Wait for at least one sample, then drain up to `max_batch` of
        whatever is queued right now. Batching (rather than one POST per
        sample) is what keeps a high-rate source, e.g. a 130 Hz raw ECG
        channel (plan.md section 12, open question 1), from turning into a
        POST-per-sample flood."""
        while not self._deque:
            self._not_empty.clear()
            await self._not_empty.wait()
        batch: list[Sample] = []
        while self._deque and len(batch) < max_batch:
            batch.append(self._deque.popleft())
        return batch

    @property
    def dropped_count(self) -> int:
        return self._dropped

    def __len__(self) -> int:
        return len(self._deque)
