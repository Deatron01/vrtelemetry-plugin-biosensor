"""Configuration for the biosensor plugin, read entirely from the process
environment -- never from a hardcoded host/port. See README.md's
"Environment variables" table for the authoritative description of each of
these; keep the two in sync.

Two variables (`HUB_WS_URL`, `HUB_PLUGIN_TOKEN`) follow the convention every
VRTelemetry plugin uses, set by `PluginManager` when this plugin is launched
normally (see `plugins/medical-plugin-01/app.py` in the host repo). The
rest (`BIOSENSOR_*`) are specific to this plugin, since the host's
`registry.json` `env` block is exactly how an operator would set them --
see plan.md section 9.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    # Hub WebSocket (session lifecycle -- read-only, see hub_client.py).
    hub_ws_url: str = "ws://127.0.0.1:8000/ws/hub/telemetry"
    hub_token: str | None = None
    plugin_id: str = "biosensor-plugin-01"

    # Ingest API (plugins/BIOSENSOR_INGEST.md in the host repo / plan.md
    # section 4). Not yet a PluginManager-provided variable, so this defaults
    # to tools/mock_ingest_server.py (BIO-2) for standalone development;
    # pointing this at the real host (now implemented -- VRT-33 is done) is
    # a base-URL change, nothing else (see plan.md section 5).
    ingest_base_url: str = "http://127.0.0.1:8100"
    ingest_token: str | None = None

    # Which transports this instance should run: "fake", "ble", "lsl".
    # Defaults to "fake" so a fresh checkout never tries to open a real
    # radio by accident. Naming a transport not registered in
    # devices/registry.py's TRANSPORT_REGISTRY (there is none currently --
    # all three plan.md BIO-3/4/7/8 transports are implemented) is a no-op,
    # not an error: the registry just skips any matching device profile.
    enabled_transports: list[str] = field(default_factory=lambda: ["fake"])

    device_profile_dir: Path = field(default_factory=lambda: Path(__file__).parent / "devices")

    # Bounded, drop-oldest sample queue size per device (BIO-6,
    # core/sample_queue.py).
    queue_maxsize: int = 500

    # Status heartbeat interval, seconds (BIO-9, app.py's _producer_heartbeat).
    status_heartbeat_interval_s: float = 5.0

    # The local live dashboard (ui_server.py) -- a diagnostic page showing
    # configured/connected devices, live sample values, and an on-demand
    # nearby-BLE-device scan. Not part of any host protocol; binds to
    # loopback by default since it's meant for whoever is at this machine,
    # not a second network-facing surface. plugin.json's `ui_port` mirrors
    # this default so a future host "Open" button (see README.md's
    # "Frontend" section) has somewhere to point.
    ui_enabled: bool = True
    ui_host: str = "127.0.0.1"
    ui_port: int = 8787

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            hub_ws_url=os.environ.get("HUB_WS_URL", cls.hub_ws_url),
            hub_token=os.environ.get("HUB_PLUGIN_TOKEN") or None,
            plugin_id=os.environ.get("PLUGIN_ID", cls.plugin_id),
            ingest_base_url=os.environ.get("BIOSENSOR_INGEST_URL", cls.ingest_base_url),
            ingest_token=os.environ.get("BIOSENSOR_INGEST_TOKEN") or None,
            enabled_transports=_csv_env("BIOSENSOR_TRANSPORTS", ["fake"]),
            device_profile_dir=Path(
                os.environ.get(
                    "BIOSENSOR_DEVICE_PROFILE_DIR", str(Path(__file__).parent / "devices")
                )
            ),
            queue_maxsize=int(os.environ.get("BIOSENSOR_QUEUE_MAXSIZE", "500")),
            status_heartbeat_interval_s=float(
                os.environ.get("BIOSENSOR_STATUS_INTERVAL_S", "5.0")
            ),
            ui_enabled=_bool_env("BIOSENSOR_UI_ENABLED", True),
            ui_host=os.environ.get("BIOSENSOR_UI_HOST", cls.ui_host),
            ui_port=int(os.environ.get("BIOSENSOR_UI_PORT", "8787")),
        )
