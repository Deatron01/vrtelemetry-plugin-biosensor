"""A single mutable slot holding "the session currently being recorded, if
any" -- shared by every `DeviceWorker` (core/device_worker.py) so that
device connectivity (which now runs for the whole plugin process lifetime,
see that module's docstring) and sample *forwarding* (which must only
happen while a real recording is open, per plan.md section 1) can be two
different things without duplicating session bookkeeping in each worker.

Deliberately not a dataclass/frozen value: `BiosensorPlugin.on_session_start`
/ `on_session_stop` mutate `.session_id` in place on the one shared instance
every worker holds a reference to, so a change is visible to every worker's
`_consume` loop on its very next iteration -- no pub/sub, no polling, no
event needed for something this simple.
"""

from __future__ import annotations


class SessionContext:
    def __init__(self) -> None:
        self.session_id: str | None = None
