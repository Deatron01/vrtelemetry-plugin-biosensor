# Biosensor Plugin

> Protocol/wire format for the Hub-facing session-lifecycle socket is
> specified once, for every VRTelemetry plugin, in the host repo's
> `plugins/PROTOCOL.md` -- read that first if you're modifying
> `hub_client.py` or writing a new plugin from scratch. This plugin's own
> ingest-facing contract (what it sends *to* the Hub, not what it receives)
> is normatively `plugins/BIOSENSOR_INGEST.md` in the host repo -- **now
> implemented and wired into the running application there** (host ticket
> VRT-33 is done, per that file's own "Status as of this writing" note).
> This repo still ships and defaults to `tools/mock_ingest_server.py` for
> fast offline development and `tests/test_smoke.py`; point
> `BIOSENSOR_INGEST_URL` at the real host to use it for real, and re-check
> that file if behaviour here and there ever seem to disagree -- it wins.

> **"Will my device work with this?"** See `DEVICE_REQUIREMENTS.md` for a
> checklist and per-device notes (smartwatches especially -- most don't
> broadcast heart rate at all without an extra step).

> **Frontend?** The host's own Electron app already covers install/
> start/stop and a live "connected or not" indicator -- see "Frontend"
> below. This plugin *also* ships its own local dashboard now
> (`ui_server.py`, on by default) for the detail the host's generic view
> was never going to have: every configured device with live status, the
> actual sample values it's sending, and an on-demand scan for nearby BLE
> devices that aren't set up yet. See "Local dashboard" below.

An out-of-process producer that connects to external physiological sensors
(chest straps, optical armbands, lab hardware) and pushes measurements into
an active VRTelemetry recording session over the Hub's biosensor ingest API.

It owns **radios and devices**. It owns nothing about storage, sessions,
consent, or the UI. It reads no participant data and writes no files that
outlive a session. **Does not enforce GDPR consent** -- that stays entirely
in the Core Hub, same as every other VRTelemetry plugin; this plugin only
*reads* the `consent_id` the Hub already validated (carried on
`session_start`), so nothing here ever gates recording.

**The one invariant:** this plugin must never fail, slow, or stop the
telemetry recording it runs alongside. It is out-of-process specifically so
that a stalled Bluetooth stack, a vanished adapter, or a misbehaving vendor
SDK costs the study one data column, never the recording itself. See
`plan.md` section 1.

## The two rules that are not negotiable

**No vendor cloud, no participant account.** Fitbit Web API, Garmin Connect,
Withings, Oura, Polar AccessLink, Apple Health -- all prohibited. Direct
device connection only. See `plan.md` section 2 for why (each is a new data
processor, an international transfer, an account tied to an individual, and
batched minutes-to-hours behind besides).

**No inference -- measurements only.** This plugin ships heart rate, RR
intervals, and whatever else a device/channel maps directly to a
measurement. It does **not** ship a stress score, arousal index, engagement
metric, or emotion estimate, ever -- matching the Core Hub's own
`av_recording/stress_score.py` and `medical-plugin-01/stress_inference.py`,
both of which raise unconditionally pending the host's EU AI Act 5(1)(f)
legal opinion. If this repo ever grows a `stress.py`, it raises too.

## Status of this repository

BIO-1 through BIO-9 from `plan.md` section 10 are built; this README is
BIO-10. **BIO-11 through BIO-15 (ANT+, serial/Bluetooth Classic, extended
channels beyond heart rate, Polar PMD raw signal, RR-derived HRV) are
explicitly deferred in the plan itself and are not built** -- nothing here
should be read as having started them.

| Ticket | What | Verification status |
|---|---|---|
| BIO-1 | Repo skeleton, `plugin.json`, packaging | Runs standalone from a clean `venv` (see Running, below). Not installed through a live `PluginManager`/catalog -- no running VRTelemetry host was available while building this. |
| BIO-2 | `tools/mock_ingest_server.py` | Used by every test below. |
| BIO-3 | `devices/registry.py` | `tests/test_smoke.py` (fake), exercised implicitly by every transport test. |
| BIO-4 | `transports/fake.py` | `tests/test_smoke.py` -- streams, stops on `session_stop`, dropout behaviour spot-checked manually (see the module docstring). |
| BIO-5 | `hub_client.py` | `tests/test_smoke.py`, against `tools/mock_hub_ws.py` (a dev-only stand-in -- the real `/ws/hub/telemetry` endpoint already exists on the host but wasn't available to test against here). |
| BIO-6 | `core/sample_queue.py`, `ingest_client.py` retry | `tests/test_queue_and_retry.py` -- drop-oldest eviction, batch draining, retry-with-backoff, and the non-retryable-error short-circuit are all directly tested (not just exercised by the happy path). |
| BIO-7 | `transports/ble.py`, `transports/ble_gatt.py` | **Partially verified.** The GATT byte parser is fully unit-tested (`tests/test_ble_gatt_parser.py`, synthetic round-trips). The transport's failure handling with no Bluetooth adapter at all is verified for real (`tests/test_ble_transport_no_adapter.py` -- this environment genuinely has no adapter). **The connect / notify / reconnect path has never run against a real strap.** Don't take this line to mean more than it says. |
| BIO-8 | `transports/lsl.py` | Verified end to end against a real `pylsl.StreamOutlet` in the same process (`tests/test_lsl_transport.py`) -- discovery, streaming, and stream-loss detection all genuinely exercised. LSL needs no special hardware, unlike BLE, which is why this one *is* a real test. |
| BIO-9 | Status heartbeat (`core/device_worker.py`, `app.py`'s `_producer_heartbeat`) | `tests/test_smoke.py` asserts a heartbeat arrives with the real host-shaped `{producer_id, devices[]}` payload -- **before** `session_start`, continuing **after** `session_stop`, matching `plugins/BIOSENSOR_INGEST.md` #2 exactly (revised after finding the host side already implemented and wired up; see "Frontend" below). |
| BIO-10 | This file | -- |
| -- (not a plan.md ticket) | Local dashboard (`ui_server.py`) | `tests/test_ui_server.py` -- real HTTP requests against a running plugin: serves HTML, `/api/state` tracks a device going `connected` with real growing sample history, `active_session_id` follows the real session lifecycle, and an on-demand BLE scan always finishes cleanly. Not part of the host protocol; see "Local dashboard" below. |

## Supported devices

**BLE (Heart Rate service, 0x180D):** any device that implements the
standard GATT Heart Rate Measurement characteristic (0x2A37). `devices/polar-h10.json`
is the reference profile. Multiple RR intervals per notification are
forwarded as separate `rr_interval_ms` samples; energy-expended is parsed
but not currently forwarded (no channel name reserved for it yet).

**LSL:** any application publishing a stream discoverable by `name` or
`type` (`pylsl.resolve_byprop`). `devices/lsl-generic-hr.json` is a
reference profile matching on `type == "HeartRate"`. This is the transport
with the most free reach per plan.md section 6.1 -- existing LSL bridges
exist for OpenBCI, g.tec, BrainVision, Shimmer, Empatica, Muse, Tobii,
BIOPAC, Polar straps, and a long community tail; a lab that already runs any
of those needs zero code here, only a device profile.

**Scope honesty** (plan.md section 6.2, reproduced verbatim because it's
the thing most likely to cause a support request if it isn't): most
smartwatches do **not** expose live HR over GATT. Apple Watch never does.
Fitbit does not. Galaxy Watch needs a Wear OS companion app. Garmin watches
broadcast only in an explicit "Broadcast Heart Rate" mode the participant
enables. Whoop only in workout mode. Supported scope is "devices
implementing the standard GATT profiles, plus a driver per vendor protocol
we choose to support" -- never "your watch works."

**Not supported, deferred by the plan itself** (plan.md section 6.3 /
section 10's BIO-11/12/14/15): ANT+ (needs a USB dongle), USB
serial/Bluetooth Classic vendor devices, Polar PMD raw ECG/PPG/ACC, and a
phone-bridge path to consumer smartwatches (its own separate project, not a
ticket here). **Blood pressure is not a channel** (plan.md section 8) --
there is no validated continuous consumer BP device; the host models BP as
a discrete pre/post-session measurement, not something this plugin streams.

## Frontend

Per `plan.md` section 1 this plugin "owns nothing about ... the UI" in the
sense that matters -- it has no say over the recording, consent, or session
UI, and doesn't need a bespoke window merged into the host app just to
install and run. The host's Electron app (`src/frontend/VRTelemetry`)
already has two surfaces that cover exactly that, both reading straight
from `plugins/BIOSENSOR_INGEST.md`'s API, no plugin-specific frontend code
required:

- **`pages/Plugins.tsx`** -- the generic install/start/stop catalog every
  plugin gets. Once this plugin is on the operator-approved list (the same
  mechanism `medical-plugin-01` and `ros-unity-bridge` use), it shows up
  here with no changes on this side.
- **`pages/NewSession.tsx`**'s "Hardware handshake" panel -- a live
  "Cardiovascular sensor" row (`no device` / `no skin contact` / `idle` /
  `streaming`) that polls `GET /api/biosensors/status` directly, and is
  explicitly the reason this repo's status heartbeat now runs for the whole
  process lifetime rather than only during a recording (see BIO-9 above,
  and `core/device_worker.py`'s module docstring) -- that panel is read
  *before* a researcher presses Start, when no session exists yet.

Both were already built and wired to the ingest API's real shape by the
time this was checked (Sept 2026) -- discovered by reading the host repo
directly rather than assumed, since `plan.md` predates VRT-33 landing. If a
future host frontend change wants something this plugin doesn't yet report,
that's a `plugins/BIOSENSOR_INGEST.md` change first (it's the contract),
then a change here to match -- never the other way around.

That said, the host's generic status row was never going to show *which
bpm value* a strap just sent, or what's advertising nearby that isn't set
up yet -- that level of detail is legitimately this plugin's own job, not
the host's. See "Local dashboard" immediately below.

## Local dashboard

This plugin ships its own small local web UI, `ui_server.py` -- on by
default, no separate install. Run `python app.py` (standalone or under
`PluginManager`) and open the URL it logs, `http://127.0.0.1:8787/` by
default (`BIOSENSOR_UI_HOST`/`BIOSENSOR_UI_PORT`, off entirely with
`BIOSENSOR_UI_ENABLED=0`). It shows, refreshed every second:

- **Every configured device**, live: connected / present-but-no-contact /
  no device, link quality, time since its last sample, its declared
  channels, and running counters (`samples_sent`, `samples_dropped_by_queue`
  from BIO-6's bounded queue, `samples_discarded_no_session` for anything
  produced while no recording was open, `batches_failed`).
- **The actual data it's sending** -- the last ~50 samples per device
  (channel, value, quality), so "is this thing even working" has a real
  answer instead of a status dot.
- **Whether a recording session is currently open**, and which one --
  the same `session_id` this process is forwarding samples under.
- **Configured-but-inactive device profiles** -- e.g. a profile naming a
  transport that's not in `BIOSENSOR_TRANSPORTS`, so it's obvious *why*
  something in `devices/` isn't showing up above, not just that it isn't.
- **An on-demand "scan for nearby BLE devices" button** -- a structured
  version of `tools/ble_scan.py`'s scan mode, run in its own thread/event
  loop so a slow or hanging scan can never affect the plugin's own device
  connections. Lists address, name, RSSI, and whether it advertises the
  standard Heart Rate service -- exactly the "what's out there that I
  haven't set up yet" view `DEVICE_REQUIREMENTS.md` tells you to get from
  the CLI tool; this is the same check, reachable from a browser tab
  instead of a terminal. A device showing up here is not yet a confirmed
  match -- `tools/ble_scan.py --address <addr>` (or a device profile you
  then actually test) is still the definitive check, same caveat as always.

This is a **local diagnostic only** -- unauthenticated, binds to loopback
by default, and neither `plugins/PROTOCOL.md` nor
`plugins/BIOSENSOR_INGEST.md` (the two things the host actually reads) know
it exists. `plugin.json`'s `ui_port` field is there for a possible future
host feature -- an "Open" button on `Plugins.tsx` that opens a second
Electron `BrowserWindow` pointed at this URL, the same pattern
`electron/main.js` already uses for `monitorWindow` -- but nothing on the
host side reads that field yet; this dashboard is fully usable standalone
either way. Verified end to end in `tests/test_ui_server.py`: the page
serves real HTML, `/api/state` tracks a live device going `connected` and
its sample history actually growing, `active_session_id` follows the real
session lifecycle, and a BLE scan always finishes cleanly whether or not a
real adapter is present.

## Prerequisites

- **BLE:** a Bluetooth adapter, powered on, reachable by `bleak`'s backend
  for this platform (WinRT on Windows, the deployment target; BlueZ/D-Bus on
  Linux). With no adapter at all, `transports/ble.py` logs
  `BLE discovery failed (...) -- is a Bluetooth adapter present and powered on?`
  and retries with backoff forever -- it does not crash the plugin, but it
  also never finds a device. This is exactly what happened while developing
  it in this environment (see the BIO-7 row above).
- **LSL:** none beyond `requirements.txt` -- `pylsl`'s wheel bundles
  `liblsl`, no separate native install needed on the platforms tested here.
  Discovery uses multicast, which some institutional networks block
  (plan.md section 6.1) -- if a stream that's definitely running isn't
  found, that's the first thing to check.

## Running standalone (development)

Both of the plugin's Hub-facing surfaces have a mock counterpart, which is
still the fastest loop for day-to-day development even now that a real
VRTelemetry host with both endpoints implemented is available to test
against (see "Frontend" above) -- point `HUB_WS_URL` / `BIOSENSOR_INGEST_URL`
at the real host instead whenever you want an end-to-end check against it:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt         # Windows
# .venv/bin/pip install -r requirements.txt             # macOS/Linux

# terminal 2: BIO-2's mock ingest server (swap for the real host's
# BIOSENSOR_INGEST_URL -- see plugins/BIOSENSOR_INGEST.md -- any time)
.venv\Scripts\python tools\mock_ingest_server.py

# terminal 3: dev-only stand-in for the Hub's /ws/hub/telemetry socket.
# NOTE: this endpoint already exists on a real VRTelemetry checkout
# (plugins/PROTOCOL.md) -- this script only exists so this repo is
# developable standalone. Point HUB_WS_URL at a real Hub instead any time.
.venv\Scripts\python tools\mock_hub_ws.py
# then, in that same terminal: start <session_id> [consent_id]  /  stop <session_id>

# terminal 1: the plugin itself
set HUB_WS_URL=ws://127.0.0.1:8765/ws/hub/telemetry
set BIOSENSOR_INGEST_URL=http://127.0.0.1:8100
.venv\Scripts\python app.py
```

Or run the automated checks that exercise all of the above without manual
`start`/`stop` typing or separate terminals:

```
.venv\Scripts\python tests\test_smoke.py                    # BIO-1 -> BIO-9, end to end, fake transport
.venv\Scripts\python tests\test_queue_and_retry.py           # BIO-6: drop-oldest queue, retry/backoff
.venv\Scripts\python tests\test_ble_gatt_parser.py           # BIO-7: GATT byte parsing (no hardware needed)
.venv\Scripts\python tests\test_ble_transport_no_adapter.py  # BIO-7: graceful failure with no adapter
.venv\Scripts\python tests\test_lsl_transport.py             # BIO-8: real pylsl outlet, no hardware needed
.venv\Scripts\python tests\test_ui_server.py                 # local dashboard: real HTTP requests, real device state
```

`test_smoke.py` asserts: a `{producer_id, devices[]}` status heartbeat
arrives *before* `session_start` (device connectivity, unlike sample
forwarding, doesn't wait for a session) and keeps arriving *after*
`session_stop`; samples reach the mock ingest server only while a session is
open, with `consent_id` from `session_start` observed; no samples arrive
before `session_start` or after `session_stop`, even past the fake source's
own sample period; no sample ever carries `heart_rate_bpm == 0.0` (the
"never send a stale value" rule).

## Environment variables

| Variable | Set by | Meaning |
|---|---|---|
| `HUB_WS_URL` | `PluginManager` | The Hub Event Router's WebSocket URL (session lifecycle only -- this plugin ignores `telemetry_frame`). |
| `HUB_PLUGIN_TOKEN` | `PluginManager`, if configured | Shared token for the Hub socket. |
| `PLUGIN_ID` | `PluginManager` | This plugin's own id, sent as `?plugin_id=` on the Hub socket for its logs. Defaults to `biosensor-plugin-01`. |
| `BIOSENSOR_INGEST_URL` | operator, via `registry.json`'s `env` (no PluginManager-standard variable yet) | Base URL for `POST /api/biosensors/sample` and `/status`. Defaults to `http://127.0.0.1:8100` (BIO-2's mock server) -- point at the real host's base URL to use `plugins/BIOSENSOR_INGEST.md`'s real, now-implemented API. |
| `BIOSENSOR_INGEST_TOKEN` | operator (optional) | Bearer token for the ingest API. Required by the real host for `/sample` (it refuses unauthenticated writes with `503` if no token is configured server-side); `GET /api/biosensors/status` itself needs none. |
| `BIOSENSOR_TRANSPORTS` | operator (optional) | Comma-separated list of enabled transports: `fake`, `ble`, `lsl`. Defaults to `fake`. |
| `BIOSENSOR_DEVICE_PROFILE_DIR` | operator (optional) | Where to load `*.json` device profiles from. Defaults to `devices/`. |
| `BIOSENSOR_QUEUE_MAXSIZE` | operator (optional) | Per-device bounded queue size (BIO-6). Oldest sample is dropped once full. Defaults to 500. |
| `BIOSENSOR_STATUS_INTERVAL_S` | operator (optional) | Status heartbeat interval in seconds (BIO-9). Runs for the whole process lifetime, not just during a session -- see "Frontend" above. Defaults to 5.0. |
| `BIOSENSOR_UI_ENABLED` | operator (optional) | Set to `0`/`false` to disable the local dashboard (`ui_server.py`) entirely. Defaults to enabled. |
| `BIOSENSOR_UI_HOST` | operator (optional) | Dashboard bind address. Defaults to `127.0.0.1` (loopback only). |
| `BIOSENSOR_UI_PORT` | operator (optional) | Dashboard port. Defaults to `8787`, matching `plugin.json`'s `ui_port`. |

## Event contract

Consumes, from `/ws/hub/telemetry` (read-only -- never sends anything back,
per the host protocol's compatibility rule 6):

- `{"type": "session_start", "session_id": ..., "consent_id": ...}` -- marks
  `session_id` as the one active session; every device worker's already-
  running stream starts having its samples *forwarded* (device connections
  themselves were opened at process startup, not here -- see
  `core/device_worker.py`).
- `{"type": "session_stop", "session_id": ...}` -- stops forwarding. Devices
  stay connected and keep heartbeating status; only sample forwarding stops.
- Everything else (`telemetry_frame`, `questionnaire_submitted`, any future
  type) is ignored.

Produces, to the ingest API (`BIOSENSOR_INGEST_URL`, contract:
`plugins/BIOSENSOR_INGEST.md` in the host repo):

- `POST /api/biosensors/sample` -- one call per device per drained batch (up
  to `core.device_worker.MAX_BATCH_SIZE` samples per request, currently 50),
  with retry-with-backoff on transient failure (`ingest_client.py`). Only
  sent while a session is active -- `{"session_id", "device_id", "samples":
  [{"t_wall_ns", "channel", "value", "quality"}, ...]}`. A batch that still
  fails after its retry budget is dropped, not requeued -- the bounded queue
  (BIO-6) has likely already moved on to newer samples by then anyway. With
  no active session, drained batches are discarded instead of sent (there is
  no `session_id` to attach them to) -- see `core/device_worker.py`.
- `POST /api/biosensors/status` -- **one call per `BIOSENSOR_STATUS_INTERVAL_S`,
  for the whole plugin process, independent of any session.** Matches
  `plugins/BIOSENSOR_INGEST.md` #2 exactly:
  `{"producer_id": "biosensor-plugin-01", "devices": [{"device_id",
  "display_name", "transport", "connected", "contact", "link_quality",
  "last_sample_age_ms", "channels"}, ...]}`, one entry per device worker.
  This is what makes `GET /api/biosensors/status` (and `NewSession.tsx`'s
  "Cardiovascular sensor" row, which polls it) show real connectivity
  *before* a researcher presses Start -- see "Frontend" above.

Nothing is written to disk -- this plugin has no `output/` directory, unlike
`medical-plugin-01`; per `plan.md` section 1 it reads no participant data
and writes no files that outlive a session.

## Adding a device

If it speaks a transport this plugin already implements (`fake`, `ble`, or
`lsl`), add a `devices/<name>.json` file -- see `devices/polar-h10.json`
(BLE) or `devices/lsl-generic-hr.json` (LSL) for the shape. No code change
needed. See `plan.md` section 7.

A new *transport* (ANT+, serial -- BIO-11/BIO-12, both explicitly deferred)
is a real code change: implement `transports.base.Transport`, register it
in `devices/registry.py`'s `TRANSPORT_REGISTRY`, and it's immediately usable
by any device profile naming it.
