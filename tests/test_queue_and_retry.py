"""BIO-6 unit tests that don't need any network or mock server: the bounded
queue's drop-oldest behaviour, and the ingest client's retry/backoff and
non-retryable-error short-circuit. tests/test_smoke.py's happy path never
exercises a failure, so this is where that logic actually gets checked.

Run: python tests/test_queue_and_retry.py
"""

from __future__ import annotations

import asyncio
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sample_queue import BoundedSampleQueue  # noqa: E402
from ingest_client import IngestClient  # noqa: E402
from transports.base import Sample  # noqa: E402


def check(label: str, condition: bool, failures: list[str]) -> None:
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


async def test_queue_drop_oldest(failures: list[str]) -> None:
    q = BoundedSampleQueue(maxsize=3)
    for i in range(5):
        q.put(Sample(channel="heart_rate_bpm", value=float(i), t_wall_ns=i))

    check("queue length capped at maxsize", len(q) == 3, failures)
    check("dropped_count counts the 2 evicted", q.dropped_count == 2, failures)

    batch = await q.get_batch(max_batch=10)
    values = [s.value for s in batch]
    check(
        "surviving samples are the 3 newest, oldest-first (FIFO within the window)",
        values == [2.0, 3.0, 4.0],
        failures,
    )
    check("queue drained after get_batch", len(q) == 0, failures)


async def test_queue_get_batch_respects_max_batch(failures: list[str]) -> None:
    q = BoundedSampleQueue(maxsize=100)
    for i in range(10):
        q.put(Sample(channel="heart_rate_bpm", value=float(i)))
    batch = await q.get_batch(max_batch=4)
    check("get_batch caps at max_batch even with more queued", len(batch) == 4, failures)
    check("remaining samples stay queued", len(q) == 6, failures)


async def test_queue_get_batch_waits_for_data(failures: list[str]) -> None:
    q = BoundedSampleQueue(maxsize=10)

    async def producer() -> None:
        await asyncio.sleep(0.2)
        q.put(Sample(channel="heart_rate_bpm", value=1.0))

    task = asyncio.create_task(producer())
    batch = await asyncio.wait_for(q.get_batch(max_batch=10), timeout=1.0)
    await task
    check("get_batch blocks until a sample arrives, then returns it", len(batch) == 1, failures)


async def test_retry_succeeds_after_transient_failures(failures: list[str]) -> None:
    client = IngestClient(base_url="http://example.invalid", token=None)
    attempts = {"n": 0}

    def fake_post(path: str, payload: dict) -> tuple[bool, str | None]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return False, "simulated transient failure"
        return True, None

    with patch.object(client, "_post", side_effect=fake_post):
        ok = await client.send_sample(
            "session-1", "device-1", [Sample(channel="heart_rate_bpm", value=70.0)],
            max_attempts=5, base_backoff_s=0.01,
        )
    check("send_sample eventually succeeds after transient failures", ok is True, failures)
    check("took exactly 3 attempts (2 failures + 1 success)", attempts["n"] == 3, failures)


async def test_retry_gives_up_after_max_attempts(failures: list[str]) -> None:
    client = IngestClient(base_url="http://example.invalid", token=None)
    attempts = {"n": 0}

    def always_fail(path: str, payload: dict) -> tuple[bool, str | None]:
        attempts["n"] += 1
        return False, "simulated permanent failure"

    with patch.object(client, "_post", side_effect=always_fail):
        ok = await client.send_sample(
            "session-1", "device-1", [Sample(channel="heart_rate_bpm", value=70.0)],
            max_attempts=3, base_backoff_s=0.01,
        )
    check("send_sample gives up and returns False", ok is False, failures)
    check("never exceeds max_attempts", attempts["n"] == 3, failures)


async def test_retry_short_circuits_on_http_error(failures: list[str]) -> None:
    client = IngestClient(base_url="http://example.invalid", token=None)
    attempts = {"n": 0}

    def raise_http_error(request, timeout):
        attempts["n"] += 1
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

    with patch("urllib.request.urlopen", side_effect=raise_http_error):
        ok = await client.send_sample(
            "session-1", "device-1", [Sample(channel="heart_rate_bpm", value=70.0)],
            max_attempts=5, base_backoff_s=0.01,
        )
    check("send_sample fails on a 401", ok is False, failures)
    check(
        "does not burn the retry budget on a non-retryable error (1 attempt, not 5)",
        attempts["n"] == 1,
        failures,
    )


async def main() -> int:
    failures: list[str] = []
    await test_queue_drop_oldest(failures)
    await test_queue_get_batch_respects_max_batch(failures)
    await test_queue_get_batch_waits_for_data(failures)
    await test_retry_succeeds_after_transient_failures(failures)
    await test_retry_gives_up_after_max_attempts(failures)
    await test_retry_short_circuits_on_http_error(failures)

    if failures:
        print(f"\nFAIL -- {len(failures)} check(s) failed.")
        return 1
    print("\nPASS -- all queue and retry checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
