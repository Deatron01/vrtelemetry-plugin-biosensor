"""The transport abstraction every driver (fake, and later ble/lsl) implements.

Kept deliberately small: a transport's only job is to call `on_sample` for
each measurement it produces, and to *stop* calling it -- not call it with a
repeated value -- the instant it loses contact. See plan.md section 4, point
3: forward-filling a dropout is "the single easiest way for this plugin to
corrupt research data." Nothing here buffers, batches, or talks to the host;
that's ingest_client.py's job, one layer up.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class Sample:
    """One measurement, shaped to drop directly into the ingest API's
    `samples[]` array (plan.md section 4). `t_wall_ns` must be
    `time.time_ns()` -- a wall clock reading -- never a monotonic/perf
    counter; the host converts using an offset captured at session start,
    and a monotonic clock's epoch is meaningless across the process
    boundary (plan.md section 4, point 2)."""

    channel: str
    value: float
    t_wall_ns: int = field(default_factory=time.time_ns)
    quality: float = 1.0

    def to_wire(self) -> dict[str, Any]:
        return {
            "t_wall_ns": self.t_wall_ns,
            "channel": self.channel,
            "value": self.value,
            "quality": self.quality,
        }


@dataclass(frozen=True)
class DeviceProfile:
    """One entry from the JSON device-profile registry (devices/*.json,
    plan.md section 7). `config` and `channels` are transport-specific and
    passed through unparsed -- the profile's own transport driver decides
    what they mean; the registry itself only routes by `transport`."""

    id: str
    display_name: str
    transport: str
    match: dict[str, Any]
    channels: dict[str, Any]
    config: dict[str, Any]


class ConnectionState(str, Enum):
    """BIO-9's three heartbeat states (plan.md section 4: "Distinguishes
    no-device, device-present-no-contact, and connected"). `UNKNOWN` is for
    a transport that hasn't implemented status reporting at all (the base
    class default) -- it is deliberately distinct from NO_DEVICE, which
    means "this transport looked and found nothing.\""""

    NO_DEVICE = "no_device"
    PRESENT_NO_CONTACT = "present_no_contact"
    CONNECTED = "connected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TransportStatus:
    """One point-in-time status snapshot, folded into the heartbeat
    `DeviceWorker.snapshot_status()` builds (core/device_worker.py).
    `link_quality` is 0.0-1.0 or None when the transport has no notion of
    it (the fake transport, for instance)."""

    state: ConnectionState
    link_quality: float | None = None


# Called once per measurement. Transports must not block in here for long --
# per plan.md section 3/4 the whole plugin must never slow the host's 120 Hz
# acquisition loop, and a slow on_sample would back up the transport's own
# read loop (a BLE notification callback, an LSL pull) behind it. Ingest is
# always fire-and-forget from the transport's point of view.
OnSample = Callable[[Sample], Awaitable[None]]


class Transport(ABC):
    """One connected instance of a device profile. `stream()` owns the
    device connection for the whole lifetime of the plugin *process* -- not
    just one recording session, see `core/device_worker.py`'s module
    docstring -- and is cancelled by the caller (`DeviceWorker.stop()`) only
    on process shutdown. It must not swallow `asyncio.CancelledError`, and
    any transport-native connection needs to be torn down promptly when
    cancelled so this doesn't leak radios or connections."""

    name: str = "unset"

    def __init__(self, profile: DeviceProfile) -> None:
        self.profile = profile

    @property
    def device_id(self) -> str:
        """Stable opaque id for this connected device instance, sent to the
        host as `device_id` (plan.md section 4). Stable across reconnects
        within one process; does not need to be stable across processes."""
        return self.profile.id

    @abstractmethod
    async def stream(self, on_sample: OnSample) -> None:
        """Run until cancelled. Call `on_sample` for each measurement.
        Must never repeat a stale value across a contact-loss gap -- see
        the module docstring."""
        raise NotImplementedError

    def get_status(self) -> TransportStatus:
        """Point-in-time status for BIO-9's heartbeat. Override to report
        something real (see FakeTransport, BleTransport, LslTransport);
        the base implementation is `UNKNOWN`, not a guess."""
        return TransportStatus(state=ConnectionState.UNKNOWN)
