"""BIO-6: the ingest API client (plan.md section 4; normative source
`plugins/BIOSENSOR_INGEST.md` in the host repo -- now implemented there;
see tools/mock_ingest_server.py / BIO-2 for the standalone-development
stand-in this ships against by default). `send_sample` retries a batch a
bounded number of times with backoff before giving up on it; giving up is
deliberate, not a bug -- it's what keeps a persistently-down host from
stalling this client indefinitely while `core.sample_queue.BoundedSampleQueue`
keeps accepting (and drop-oldest-evicting) newer samples in the meantime.
Retrying forever would just mean retrying an ever-more-stale batch while the
real backlog grows unbounded in front of it -- the opposite of the point.

Uses `urllib.request` in a worker thread (`asyncio.to_thread`) rather than
pulling in an async HTTP dependency, matching this repo's low-dependency
convention (see requirements.txt and `plugins/medical-plugin-01`'s own
choice to depend on nothing but `websockets`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from transports.base import Sample

logger = logging.getLogger("biosensor_plugin.ingest_client")


class IngestClient:
    def __init__(self, base_url: str, token: str | None, timeout_s: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_s = timeout_s

    def _post(self, path: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
        url = f"{self._base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                ok = 200 <= response.status < 300
                return ok, None if ok else f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            # A 4xx (e.g. bad token, malformed payload) will never succeed
            # on retry -- don't waste the retry budget on it.
            return False, f"HTTP {exc.code} (not retryable)"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, str(exc)

    async def _post_with_retry(
        self, path: str, payload: dict[str, Any], max_attempts: int, base_backoff_s: float
    ) -> bool:
        backoff = base_backoff_s
        last_error: str | None = None
        for attempt in range(1, max_attempts + 1):
            ok, error = await asyncio.to_thread(self._post, path, payload)
            if ok:
                return True
            last_error = error
            if error is not None and "not retryable" in error:
                break
            if attempt < max_attempts:
                await asyncio.sleep(backoff)
                backoff = min(5.0, backoff * 2)

        logger.warning(
            f"Ingest POST {path} failed after {max_attempts} attempt(s): {last_error}. Giving up."
        )
        return False

    async def send_sample(
        self,
        session_id: str,
        device_id: str,
        samples: list[Sample],
        max_attempts: int = 3,
        base_backoff_s: float = 0.5,
    ) -> bool:
        payload = {
            "session_id": session_id,
            "device_id": device_id,
            "samples": [s.to_wire() for s in samples],
        }
        return await self._post_with_retry(
            "/api/biosensors/sample", payload, max_attempts, base_backoff_s
        )

    async def send_status(
        self, payload: dict[str, Any], max_attempts: int = 1, base_backoff_s: float = 0.5
    ) -> bool:
        # Default is a single attempt, no retry: a status heartbeat that's a
        # few seconds late is superseded by the next tick anyway (see
        # app.py's _producer_heartbeat) -- retrying it is not worth delaying
        # the next real heartbeat behind it.
        return await self._post_with_retry(
            "/api/biosensors/status", payload, max_attempts, base_backoff_s
        )
