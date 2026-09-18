# Biosensor plugin — project plan

Written 18 Sep 2026. This is the founding document for a **new repository**,
separate from `Deatron01/VRTelemetry`. Working name `biosensor-plugin-01`,
following the `medical-plugin-01` convention.

Host-side counterpart: `claude/vrtelemetry-biosensor-host-changes.md` in the
VRTelemetry repo. Design rationale for both:
`claude/biosensor-plugin-plan.md`.

> **Verification status.** Nothing here has been implemented or run. Claims
> about the host's existing behaviour come from its docs and source read during
> a planning session, not a live checkout — verify before relying on them.

---

## 1. What this plugin is

An out-of-process producer that connects to external physiological sensors —
chest straps, optical armbands, lab hardware — and pushes measurements into an
active VRTelemetry recording session over the Hub's biosensor ingest API.

It owns **radios and devices**. It owns nothing about storage, sessions,
consent, or the UI. It reads no participant data and writes no files that
outlive a session.

### The one invariant

> The plugin must never fail, slow, or stop the telemetry recording it runs
> alongside.

The host's acquisition loop runs at 120 Hz in another process. Windows'
Bluetooth stack stalls, adapters vanish, `bleak` can hang on disconnect, vendor
SDKs are of unknown quality. Out-of-process with a restart budget, all of that
is a gap in one data column. That isolation is the entire reason this is a
plugin and not a module in the host — do not undermine it by making the host
wait on anything here.

## 2. Two rules that are not negotiable

These come from the host project's legal position, not from preference. They
belong in this repo's README and in its catalog description so they survive a
future maintainer who never reads this plan.

**No vendor cloud, no participant account.** Fitbit Web API, Garmin Connect,
Withings, Oura, Polar AccessLink and Apple Health are all prohibited. Each means
a new data processor, an international transfer, and an account tied to an
individual. They are also batched minutes-to-hours behind, so they cannot
populate a live session regardless. Direct device connection only.

**No inference — measurements only.** Ship HR, RR intervals, SpO2, temperature,
acceleration. Do **not** ship a stress score, arousal index, engagement metric
or emotion estimate. HR plus HRV is exactly the input that makes one tempting;
the host's published privacy policy currently asserts no such inference exists,
and two modules in the host (`av_recording/stress_score.py`,
`medical-plugin-01/stress_inference.py`) raise unconditionally to enforce it. If
this repo ever grows a `stress.py`, it raises too — same message, same pointer.

## 3. Architecture

```
  devices                transports              core                host
  ───────                ──────────              ────                ────
  Polar H10  ──BLE──┐
  Garmin HRM ──BLE──┤
  OpenBCI ────LSL───┼──► transport driver ──► sample queue ──► ingest client ──► POST /api/biosensors/sample
  Shimmer ────LSL───┤         ▲                                     │
  (anything) ─push──┘         │                                     └─► POST /api/biosensors/status
                         device profile                    ▲
                          (JSON registry)                  │
                                                   session lifecycle
                                                           │
                                              WS /ws/hub/telemetry (read-only)
```

**Transport × device profile.** A transport is a module (BLE, LSL, ANT+,
serial). A device profile is a JSON file declaring which transport, how to match
the device, and what its characteristics mean. Adding another strap that speaks
standard GATT is a JSON entry a researcher can write; only a genuinely new
protocol needs Python.

**Session lifecycle is read-only.** The plugin subscribes to
`/ws/hub/telemetry` for `session_start` / `session_stop`, which carry the
already-validated `consent_id` — so output is traceable to the consent it was
produced under, exactly as `medical-plugin-01` does. It never speaks on that
socket.

**The sample queue is bounded and drops oldest.** If ingest is slow or the host
is down, the plugin sheds samples rather than growing memory or blocking a BLE
callback. Dropped counts are reported in status.

## 4. The host contract

Normative source is `plugins/BIOSENSOR_INGEST.md` in the VRTelemetry repo,
versioned alongside its `PROTOCOL_VERSION`. Summarised here so this repo is
usable standalone; **if the two disagree, the host wins.**

```
POST /api/biosensors/sample
Authorization: Bearer <shared_token>

{
  "session_id": "<from session_start>",
  "device_id":  "<stable opaque id>",
  "samples": [
    { "t_wall_ns": 1758134400123456789,
      "channel":   "heart_rate_bpm",
      "value":     72.0,
      "quality":   1.0 }
  ]
}
```

Four things to internalise:

1. **`heart_rate_bpm` is the one reserved channel name.** It is folded into the
   host's telemetry frame. Every other channel name lands in a side file. This
   is why adding SpO2 or accelerometer data never requires a host schema change
   — pick a sensible name and send it.
2. **`t_wall_ns` is `time.time_ns()`, not `perf_counter_ns()`.** Monotonic
   clocks have per-process epochs and are meaningless across the boundary. The
   host converts using an offset it captures at session start.
3. **Never send a stale value.** The host treats `heart_rate_bpm > 0` as valid
   and `0.0` as absent. If contact is lost, stop sending — do not repeat the
   last reading. Forward-filling a 30 s dropout injects thousands of fabricated
   samples into the study's summary statistics. This is the single easiest way
   for this plugin to corrupt research data.
4. **The token is required.** Ingest is a write path into a research record.

Status heartbeat (`POST /api/biosensors/status`) reports per-device connected
state, sensor contact, link quality, last-sample age and channel list. Push on a
timer; a producer that stops heartbeating is reported gone rather than stale.

## 5. Develop against a stub

The host ingest route (VRT-33) will not exist on day one. **Do not wait for it.**
BIO-2 builds a ~60-line mock ingest server that implements §4 and prints what it
receives. Everything through BIO-8 is developable and testable against it, and
swapping to the real host is a base-URL change.

This is what makes the two repos genuinely parallel tracks rather than a
sequence.

## 6. Transports

Build order reflects reach-per-effort, not familiarity.

### 6.1 LSL (build first)

Lab Streaming Layer is the de facto standard in psychophysiology and neuro labs:
any application publishes a named typed stream on the local network, any other
discovers and subscribes. Critically it does **clock synchronisation across
processes and machines as a built-in feature**, which is otherwise the hardest
part of this whole problem.

Free reach: existing LSL bridges for OpenBCI, g.tec, BrainVision, Shimmer,
Empatica, Muse, Tobii, BIOPAC, Polar straps and a long community tail. A lab
that already owns any of it integrates with zero work here.

Costs to document: `liblsl` is a native binary in the plugin venv (fine — the
plugin has its own venv by design), and discovery uses multicast, which some
institutional networks block.

### 6.2 BLE GATT

The consumer-strap default, no extra hardware. `bleak` is the only serious
cross-platform Python BLE library; WinRT backend on Windows, which is the target.

Standard services, cheap once the stack exists:

| UUID | Service | Channels |
|---|---|---|
| 0x180D | Heart Rate | `heart_rate_bpm`, `rr_interval_ms`, contact bit, energy expended |
| 0x1822 | Pulse Oximeter | `spo2_pct` |
| 0x1809 | Health Thermometer | `temperature_c` |
| 0x180F | Battery | `device_battery_pct` |
| 0x1810 | Blood Pressure | discrete — see §8 |

Vendor extensions behind the same driver interface where raw signal is needed:
Polar PMD (ECG/PPG/ACC), Movesense. EDA means Shimmer or Empatica SDKs, not GATT.

**Scope honesty, to go in the README verbatim:** most smartwatches do **not**
expose live HR over GATT. Apple Watch never does. Fitbit does not. Galaxy Watch
needs a Wear OS companion app. Garmin watches broadcast only in an explicit
"Broadcast Heart Rate" mode the participant enables. Whoop only in workout mode.
Supported scope is "devices implementing the standard GATT profiles, plus a
driver per vendor protocol we choose to support" — never "your watch works."

### 6.3 Deferred transports

- **ANT+** — needs a USB stick (`openant`). Its advantage is specific: ANT+ is
  broadcast, so one dongle receives many straps reliably, where BLE straps hold
  one or two connections and pairing gets fragile. Build when multi-participant
  sessions arrive, or when Windows BLE pairing pain does.
- **USB serial / vendor SDK** — most reliable option by a distance (no radio, no
  pairing, deterministic latency; `pyserial` over a COM port). The catch is a
  cable on a participant turning around in a headset. Build when a lab arrives
  with hardware they already own.
- **Bluetooth Classic (SPP)** — some older BP monitors and Shimmer units. On
  Windows it is a virtual COM port, so it falls out of the serial transport for
  free.
- **Phone bridge** — the only path to consumer smartwatches: a watchOS app
  running an `HKWorkoutSession` (that session is what unlocks near-live HR), or
  a Wear OS app. Note Health Connect is batched storage, not a live stream.
  Needs its own developer accounts and distribution story — a separate project,
  not a ticket here.

Generic network push needs nothing from this repo: it is the host's ingest API,
which any third party can POST to directly.

## 7. Device profile registry

```jsonc
// devices/polar-h10.json
{
  "id": "polar-h10",
  "display_name": "Polar H10",
  "transport": "ble",
  "match": { "service_uuid": "0000180d-...", "name_prefix": "Polar H10" },
  "channels": {
    "heart_rate_bpm": { "parser": "gatt_hr.bpm" },
    "rr_interval_ms": { "parser": "gatt_hr.rr" }
  }
}
```

Shape is illustrative — settle it in BIO-3. The principle is what matters: a new
device that speaks a protocol already implemented must be **data**, not a code
change and a release.

## 8. Blood pressure

Worth stating so nobody plans it as a channel. There is no validated continuous
consumer BP device. Cuff monitors give a discrete reading every 30–60 s at best
and need the arm still and at heart level — incompatible with an active VR task.
Continuous non-invasive BP means research-grade finger cuffs (Finapres, CNAP):
expensive, tethered, serial or analog.

The host models BP as a pre/post-session measurement attached to session
metadata. If this plugin ever supports a 0x1810 cuff, it sends discrete
timestamped events, and nothing here implies a continuous channel.

## 9. Packaging

Follows `PluginManager` as already built in the host: `plugin.json` with a
matching `protocol_version`, `requirements.txt`, own venv, installed from the
Central Server catalog, paused and resumed from the Plugins panel.

Config the plugin needs: hub WS URL, ingest base URL, shared token, enabled
transports, device profile directory, queue bounds, status heartbeat interval.
Note the host's own convention that `hub_ws_url` is deliberately *not* derived
from other host settings — don't invent a derivation here either.

## 10. Tickets

Own board, own numbering — this is a separate project. `BIO-1` through `BIO-8`
are the buildable core; the rest are explicitly deferred so the board does not
fill with work nobody committed to.

**First slice: BIO-1 → BIO-5.** That is a plugin that installs, connects,
follows session lifecycle, and streams fake data into a stub — provable before
any radio is involved.

| Ticket | Work | Gate |
|---|---|---|
| **BIO-1** | Repo skeleton, `plugin.json`, venv, catalog install, start/stop | Installs from the catalog, starts and stops from the Plugins panel, survives a forced kill within its restart budget |
| **BIO-2** | Mock ingest + status server implementing §4 | Full §4 contract; everything through BIO-8 testable with no host |
| **BIO-3** | Transport abstraction + JSON device-profile registry | A second profile for the same transport is a file, with no code change |
| **BIO-4** | Fake source driver (configurable rate, injectable dropouts) | Produces a realistic HR trace and a realistic contact-loss gap |
| **BIO-5** | Session lifecycle over `/ws/hub/telemetry` | Streams only between `session_start` and `session_stop`; carries `consent_id`; never transmits on the socket; reconnects if the hub restarts mid-session |
| **BIO-6** | Bounded sample queue + ingest client + retry | Ingest failure or host downtime sheds samples without unbounded memory or blocking a device callback; drops are counted and surfaced in status |
| **BIO-7** | BLE GATT transport (0x180D) | Real strap connects and streams; **removed and replaced mid-session without a single stale value being sent** (§4.3) |
| **BIO-8** | LSL inbound transport | Discovers and subscribes to a stream from a second process; alignment demonstrated against the host's stated bound |
| **BIO-9** | Status heartbeat reporting (§4) | Distinguishes no-device, device-present-no-contact, and connected; goes gone on heartbeat loss |
| **BIO-10** | README, supported-device matrix, catalog description, §2 rules | Scope honesty from §6.2 present verbatim; both §2 rules stated where a future maintainer will see them |
| *BIO-11* | *ANT+ transport* | *Deferred — build when multi-participant or BLE pairing pain arrives* |
| *BIO-12* | *Serial / Bluetooth Classic transport* | *Deferred — build when a lab arrives with hardware* |
| *BIO-13* | *Extended channels: SpO2, temperature, battery* | *Deferred — trivial once BIO-7 exists* |
| *BIO-14* | *Polar PMD raw signal (ECG/PPG/ACC)* | *Deferred — only if a study needs raw* |
| *BIO-15* | *RR-derived HRV metrics (RMSSD, SDNN)* | *Deferred — §2's inference rule applies; measurements only* |

## 11. Dependency on the host

| This repo needs | Host ticket | Workaround until then |
|---|---|---|
| Ingest API + contract doc | VRT-33 | BIO-2 stub |
| Status endpoints | VRT-36 | BIO-2 stub |
| `session_start` / `session_stop` on the WS | already exists | — |
| Nothing else | — | — |

Everything else in the host plan (fold-in, sidecar storage, upload, erasure,
legal) is invisible from here. That is the intended shape: this repo can be
handed to someone who never opens the VRTelemetry tree.

## 12. Open questions

1. **Batch size and rate limits** the host will accept — HR is ~1 Hz, but a raw
   ECG device is 130 Hz per channel. Decide with a measurement, jointly with
   VRT-33.
2. **Should device profiles eventually be catalog-managed**, the way
   `plugin_catalog` became data rather than a hand-edited file? Not in the first
   pass, but the JSON shape should not preclude it.
3. **Does `medical-plugin-01` already have a config or lifecycle pattern to copy
   rather than reinvent?** Read it before BIO-1.
4. **Windows BLE adapter contention** — does the headset streaming setup (Vive
   Business Streaming, dongles) share an adapter with anything? Test early; it
   is the kind of problem that only appears on the real rig.
