"""BIO-2: a mock ingest + status server implementing plan.md section 4 /
`plugins/BIOSENSOR_INGEST.md`'s contract, so this whole repo is developable
and testable with no VRTelemetry host checkout at all. The host's real
ingest route (VRT-33) is now implemented there; swapping to it is a
`BIOSENSOR_INGEST_URL` change, nothing else -- this mock stays useful for
fast, offline iteration and for tests/test_smoke.py either way.

Deliberately small and dependency-free (stdlib `http.server` only) -- this
is a development aid, not a piece of the shipped plugin, and
`requirements.txt` should not grow because of it.

Run standalone:

    python tools/mock_ingest_server.py                       # port 8100
    python tools/mock_ingest_server.py --port 8100 --token secret

Every request is logged to stdout; `GET /api/biosensors/_debug/summary`
returns a small JSON summary of what's been received so far, which is what
tests/test_smoke.py polls instead of scraping log lines.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger("mock_ingest_server")


class _State:
    """Shared, lock-protected state the debug endpoint reports."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sample_requests: list[dict[str, Any]] = []
        self.status_requests: list[dict[str, Any]] = []
        self.total_samples = 0
        self.zero_heart_rate_count = 0

    def record_sample(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.sample_requests.append(payload)
            samples = payload.get("samples", [])
            self.total_samples += len(samples)
            for s in samples:
                if s.get("channel") == "heart_rate_bpm" and s.get("value") == 0.0:
                    self.zero_heart_rate_count += 1

    def record_status(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.status_requests.append(payload)

    def summary(self) -> dict[str, Any]:
        with self.lock:
            return {
                "sample_requests": len(self.sample_requests),
                "status_requests": len(self.status_requests),
                "total_samples": self.total_samples,
                "zero_heart_rate_count": self.zero_heart_rate_count,
                "last_sample_request": self.sample_requests[-1] if self.sample_requests else None,
                "last_status_request": self.status_requests[-1] if self.status_requests else None,
            }


def make_handler(state: _State, expected_token: str | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _check_token(self) -> bool:
            if expected_token is None:
                return True
            header = self.headers.get("Authorization", "")
            return header == f"Bearer {expected_token}"

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None

        def do_POST(self) -> None:  # noqa: N802
            if not self._check_token():
                self._send_json(401, {"ok": False, "error": "invalid or missing token"})
                return

            payload = self._read_json()
            if payload is None:
                self._send_json(400, {"ok": False, "error": "invalid JSON body"})
                return

            if self.path == "/api/biosensors/sample":
                missing = [f for f in ("session_id", "device_id", "samples") if f not in payload]
                if missing:
                    self._send_json(422, {"ok": False, "error": f"missing field(s) {missing}"})
                    return
                for sample in payload["samples"]:
                    if sample.get("channel") == "heart_rate_bpm" and sample.get("value") == 0.0:
                        logger.warning(
                            "Received heart_rate_bpm=0.0 -- per PROTOCOL, 0.0 means "
                            "'absent': the plugin should have stopped sending rather "
                            "than send this."
                        )
                state.record_sample(payload)
                print(f"[sample] {json.dumps(payload)}")
                self._send_json(200, {"ok": True})
            elif self.path == "/api/biosensors/status":
                state.record_status(payload)
                print(f"[status] {json.dumps(payload)}")
                self._send_json(200, {"ok": True})
            else:
                self._send_json(404, {"ok": False, "error": "unknown path"})

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/biosensors/_debug/summary":
                self._send_json(200, state.summary())
            else:
                self._send_json(404, {"ok": False, "error": "unknown path"})

    return Handler


def run_server(host: str, port: int, token: str | None) -> ThreadingHTTPServer:
    state = _State()
    handler_cls = make_handler(state, token)
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.mock_state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="mock-ingest-server")
    thread.start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--token", default=None, help="If set, require this bearer token.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = run_server(args.host, args.port, args.token)
    logger.info(f"Mock ingest server listening on http://{args.host}:{args.port}")
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
