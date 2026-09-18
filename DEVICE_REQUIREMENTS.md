# What a device needs to work with this plugin

A quick-reference checklist for "will my device work with biosensor-plugin-01,"
separate from `README.md`'s own status/verification notes and `plan.md`'s
design rationale. Read this before buying, pairing, or asking "does X work."

## The two hard requirements (plan.md section 2 -- not negotiable)

1. **Direct local connection only.** The device must connect straight to
   this machine over BLE or LSL. No vendor cloud API (Fitbit Web API,
   Garmin Connect, Withings, Oura, Polar AccessLink, Apple Health), no
   participant account, no phone-app-in-the-middle syncing to a server.
2. **Standard, open protocol only -- no vendor SDK.** The device must speak
   a protocol this plugin already implements (see below), using the
   standard characteristic/UUID for it. A device that only works through
   the manufacturer's own proprietary app or SDK is out of scope, however
   good that SDK is -- there is no exception for "but it's well documented."

Everything below is about how to tell, for a specific device, whether it
clears these two bars.

## Path 1: BLE, Heart Rate service (0x180D) -- `transports/ble.py`

**What the device must expose, once connected:** GATT service
`0000180d-0000-1000-8000-00805f9b34fb` (Heart Rate), with characteristic
`00002a37-0000-1000-8000-00805f9b34fb` (Heart Rate Measurement) supporting
**notify**. That's it -- nothing else about the device matters. It does not
need to advertise that service in its BLE advertisement packet (many
devices only expose it after you connect); it does not need any particular
device name.

**How to check, for real, on any specific device:**

```
python -m venv .venv
.venv\Scripts\pip install bleak
.venv\Scripts\python tools\ble_scan.py                          # see what's nearby
.venv\Scripts\python tools\ble_scan.py --address <its address>  # connect, list its GATT tree
```

The inspect mode tells you directly whether `0000180d.../00002a37...` is
present. This is the one question that actually decides compatibility --
everything else in this document is just "what usually determines the
answer," not a substitute for running the scan.

**Classes of device that usually pass this test out of the box:**
chest straps and similar dedicated HR hardware built around the standard
GATT Heart Rate profile (`devices/polar-h10.json` is the reference profile
in this repo), and some clinical/medical-grade HR monitors that were built
to the same open standard for interoperability.

**Classes of device that need an extra step first, if they work at all:**
consumer smartwatches. Most do **not** expose this out of the box -- see
the "smartwatches" section below before assuming yours does.

**Once it passes:** copy `devices/polar-h10.json`, change `id`,
`display_name`, and `match` (`service_uuid` stays the same;
`name_prefix` to whatever the device actually advertises as, from the scan
output). No code change. See `plan.md` section 7.

## Path 2: LSL -- `transports/lsl.py`

**What's needed:** something on the local network publishing a
[Lab Streaming Layer](https://labstreaminglayer.org) stream discoverable by
`name` or `type` (`pylsl.resolve_byprop`). This is a software-side
requirement, not a specific radio or chipset -- the actual sensor can be
anything, as long as *something* (vendor app, LSL bridge project, your own
script) is bridging it into an LSL outlet.

**What tends to already have this:** OpenBCI, g.tec, BrainVision, Shimmer,
Empatica, Muse, Tobii, BIOPAC, and some Polar straps all have existing
community or vendor LSL bridges (plan.md section 6.1) -- if a lab already
uses any of this equipment for other work, it likely needs zero new code or
hardware here, only a `devices/<name>.json` profile (`devices/lsl-generic-hr.json`
is the reference).

**What can silently break it:** LSL stream discovery uses multicast, which
some institutional/corporate networks block. If a stream you know is
running isn't found, that's the first thing to check -- it showed up as a
real warning (`No local network interface addresses found...`) even while
testing this plugin, and streaming still worked once discovery found a
route, so it's usually recoverable, not fatal.

## Smartwatches specifically -- read this before assuming yours works

Reproduced from `plan.md` section 6.2, because it's the single most common
wrong assumption: most smartwatches do **not** expose live HR over GATT.

- **Apple Watch:** never does, full stop -- not even with a third-party app.
  The apps that claim to bridge it (BlueHeart, HeartCast, etc.) run on
  *both* the watch and your iPhone: the watch sends HR to the phone app,
  and the phone re-broadcasts it. What a BLE scan would find is the phone
  impersonating a strap, not the watch. This "phone bridge" path is
  explicitly called out and deferred in plan.md section 6.3 -- not a ticket
  in this repo, unverified, and requires the phone to stay in range running
  the app for the whole session.
- **Fitbit:** does not expose it at all.
- **Galaxy Watch (Wear OS):** no native broadcast. Needs a third-party
  companion app installed *on the watch* (e.g. a Play Store app built for
  exactly this). Whether that app's broadcast uses the standard GATT
  service is something to verify with `tools/ble_scan.py`, not assume.
- **Garmin:** broadcasts only in an explicit "Broadcast Heart Rate" mode the
  wearer has to turn on. When enabled, this is the standard GATT service.
- **Huawei (WATCH/Band):** some models have an explicit *Settings > HR Data
  Broadcasts* toggle; many don't ("if this option isn't available, your
  device doesn't support this feature," per Huawei's own support page).
  Whether the broadcast uses the standard GATT service wasn't confirmed
  while building this -- verify with the scan tool.
- **Whoop:** only broadcasts during an active workout mode.

The pattern across all of these: a smartwatch "supporting" broadcast at all
is the exception, not the default, and even when it exists it's usually an
opt-in mode or a separate app, not something that's just on. Always verify
with `tools/ble_scan.py` rather than trusting a marketing claim, a forum
post, or a Bluetooth "works with X" badge.

## What's standards-based but NOT built yet (so "no" today, not "never")

These devices might well pass the "standard protocol, no vendor SDK" bar,
but this repo doesn't parse their data yet:

- **Pulse oximeter (BLE service `0x1822`), health thermometer (`0x1809`),
  battery level (`0x180F`)** -- real standard GATT services, no parser
  written for them yet. Ticket BIO-13, explicitly deferred by the plan but
  described there as "trivial once BIO-7 exists" -- ask if you have a
  specific device and want this added.
- **Blood pressure (`0x1810`)** -- deliberately never a streamed channel
  here, standards-based or not. Plan.md section 8: there's no validated
  continuous consumer BP device, cuff readings are discrete every 30-60s at
  best, and the host models BP as a pre/post-session measurement, not
  something this plugin streams live.
- **Raw physiological signal (e.g. Polar PMD ECG/PPG/accelerometer)** --
  ticket BIO-14, deferred, "only if a study needs raw."
- **Any RR-derived metric (HRV / RMSSD / SDNN)** -- ticket BIO-15, deferred
  on principle: plan.md section 2's "no inference" rule applies. This
  plugin ships raw RR intervals; it does not, and by design will not, ship
  a derived stress/arousal number.
- **ANT+ broadcast devices** (need a USB dongle) and **USB-serial /
  Bluetooth Classic medical devices** -- BIO-11/BIO-12, deferred until
  multi-participant sessions or a lab with that exact hardware shows up.

## Checklist

- [ ] Does the device connect directly to this machine (no vendor cloud, no
      account)?
- [ ] Does it speak BLE Heart Rate (0x180D/0x2A37) or publish an LSL
      stream? (`tools/ble_scan.py`, or check for an existing LSL bridge.)
- [ ] If it's a smartwatch: does it have an explicit broadcast mode
      enabled, or a companion app installed and running? (See above --
      most don't have this at all.)
- [ ] Is the channel you need actually implemented? (Heart rate: yes.
      SpO2/temperature/battery/raw signal/HRV: not yet -- see above.)

If every box is checked: add a `devices/<name>.json` profile
(`plan.md` section 7) and it should just work, no code change. If not,
say which device and which box failed -- some of the "not yet" items above
are genuinely quick to add.
