"""BIO-3: the device-profile registry. Loads every `*.json` file in a
directory (default `devices/`, see config.py) into a `DeviceProfile`, and
maps each one to a `Transport` instance if -- and only if -- its
`transport` name is both implemented in this repo and enabled for this
process (`BIOSENSOR_TRANSPORTS`).

The point, per plan.md section 7: a device that speaks a protocol this
plugin already implements is a JSON file, not a code change. Adding
`devices/polar-verity-sense.json` next to `devices/polar-h10.json` -- both
BLE, both matched by the same `transports/ble.py` -- needs no change here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from transports.base import DeviceProfile, Transport
from transports.ble import BleTransport
from transports.fake import FakeTransport
from transports.lsl import LslTransport

logger = logging.getLogger("biosensor_plugin.devices.registry")

# One entry per implemented transport. A profile naming a transport that
# isn't (or isn't yet) in this map is loaded -- so it still validates and
# shows up in logs -- but never instantiated; see build_transports below.
TRANSPORT_REGISTRY: dict[str, type[Transport]] = {
    FakeTransport.name: FakeTransport,
    BleTransport.name: BleTransport,
    LslTransport.name: LslTransport,
}

_REQUIRED_FIELDS = ("id", "display_name", "transport")


def load_profiles(directory: Path) -> list[DeviceProfile]:
    """Load every `*.json` file in `directory` as a DeviceProfile. A
    malformed profile is logged and skipped rather than raising -- one bad
    file in the registry directory must not prevent every other device from
    working."""
    profiles: list[DeviceProfile] = []
    if not directory.is_dir():
        logger.warning(f"Device profile directory {directory} does not exist; no devices loaded.")
        return profiles

    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"Skipping device profile {path.name}: {exc}")
            continue

        missing = [f for f in _REQUIRED_FIELDS if f not in raw]
        if missing:
            logger.warning(f"Skipping device profile {path.name}: missing field(s) {missing}")
            continue

        profiles.append(
            DeviceProfile(
                id=raw["id"],
                display_name=raw["display_name"],
                transport=raw["transport"],
                match=raw.get("match", {}),
                channels=raw.get("channels", {}),
                config=raw.get("config", {}),
            )
        )
    return profiles


def build_transports(
    profiles: list[DeviceProfile], enabled_transport_names: list[str]
) -> list[Transport]:
    """Instantiate one Transport per profile whose transport is both
    enabled for this process and implemented in this repo. Everything else
    is logged and skipped, never an error -- an operator listing "ble" in
    `BIOSENSOR_TRANSPORTS` before BIO-7 ships is a config that will start
    working later, not a broken one now."""
    enabled = set(enabled_transport_names)
    transports: list[Transport] = []

    for profile in profiles:
        if profile.transport not in enabled:
            logger.info(
                f"Device profile '{profile.id}' uses transport '{profile.transport}', "
                f"which is not in BIOSENSOR_TRANSPORTS={sorted(enabled)}; skipping."
            )
            continue

        transport_cls = TRANSPORT_REGISTRY.get(profile.transport)
        if transport_cls is None:
            logger.info(
                f"Device profile '{profile.id}' uses transport '{profile.transport}', "
                "which is not implemented yet in this repo (deferred ticket); skipping."
            )
            continue

        try:
            transports.append(transport_cls(profile))
        except (ValueError, TypeError) as exc:
            # A malformed profile (e.g. an lsl profile missing both
            # match.stream_name and match.stream_type) must not take the
            # rest of the registry down with it -- same principle as
            # load_profiles skipping a malformed JSON file above.
            logger.warning(f"Skipping device profile '{profile.id}': {exc}")
            continue
        logger.info(f"Device profile '{profile.id}' -> {transport_cls.__name__}")

    return transports
