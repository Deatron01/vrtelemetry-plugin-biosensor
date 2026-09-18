"""Optional local dashboard: a same-origin HTTP page + JSON API for
watching this plugin live -- which devices are configured, whether each is
connected right now, what it's actually sending, and (for BLE) what's
discoverable nearby but not yet set up as a device profile.

Not part of the Hub/host protocol at all -- `plugins/PROTOCOL.md` and
`plugins/BIOSENSOR_INGEST.md` in the host repo don't know this exists, and
nothing here talks to the host. This is purely a local operator/developer
view onto this process's own state, for exactly the situations those two
APIs don't cover: "is my strap even turned on," "why is nothing showing up
in the recording," "what bpm value did it just send." A future host-side
"Open" button (the architecture this was built in response to -- a plugin
that wants its own window follows the same `electron/main.js` pattern as
`monitorWindow`, per that discussion) would just point an Electron
`BrowserWindow` at this server's URL; `plugin.json`'s `ui_port` field is
what that integration would read. Nothing on the host side reads it yet --
this is usable standalone (`python app.py`, then open the URL it logs)
whether or not that host feature ever lands.

Deliberately stdlib `http.server` only, matching `tools/mock_ingest_server.py`
-- this is a diagnostic aid, not a piece of the wire protocol, and
`requirements.txt` shouldn't grow because of it. The one real dependency,
`bleak`, is already required for `transports/ble.py`; the on-demand "scan
for nearby BLE devices" action reuses it directly rather than shelling out
to `tools/ble_scan.py` (a human-facing CLI that prints, not something built
to hand structured results back to a caller).

Every route here is unauthenticated and binds to `BIOSENSOR_UI_HOST`
(default `127.0.0.1`, i.e. loopback-only) -- this is a local status page
for whoever is sitting at this machine, not a second ingest surface, and it
never accepts anything that reaches a transport or the ingest client; the
one POST route (`/api/scan/ble`) only ever reads nearby BLE advertisements.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from app import BiosensorPlugin

logger = logging.getLogger("biosensor_plugin.ui_server")

DEFAULT_BLE_SCAN_TIMEOUT_S = 8.0
MIN_BLE_SCAN_TIMEOUT_S = 1.0
MAX_BLE_SCAN_TIMEOUT_S = 30.0
HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"


class _BleScanState:
    """One in-memory slot for the most recent on-demand BLE scan
    (`POST /api/scan/ble`). This dashboard is inherently single-operator --
    `localhost`, no auth, meant for one person looking at their own machine
    -- so "the last scan that ran" is the only state worth keeping; no scan
    history, no queue of pending requests."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.results: list[dict[str, Any]] | None = None
        self.error: str | None = None
        self.finished_at_wall_ns: int | None = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "results": self.results,
                "error": self.error,
                "finished_at_wall_ns": self.finished_at_wall_ns,
            }


def _run_ble_scan(state: _BleScanState, timeout_s: float) -> None:
    """Runs in its own OS thread with its own event loop -- deliberately
    *not* scheduled onto the plugin's main asyncio loop via
    `run_coroutine_threadsafe`. A scan touches nothing the main loop owns
    (no shared transport, no shared queue), so giving it a fully separate
    loop means a slow or hanging scan can never affect this process's own
    device workers or its Hub connection. Mirrors `tools/ble_scan.py`'s
    `scan()` in what it checks for, but returns structured results instead
    of printing them."""

    async def _scan() -> list[dict[str, Any]]:
        from bleak import BleakScanner

        found = await BleakScanner.discover(timeout=timeout_s, return_adv=True)
        results = []
        for device, adv in found.values():
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            results.append(
                {
                    "address": device.address,
                    "name": device.name or "(no name)",
                    "rssi": adv.rssi,
                    "advertises_heart_rate_service": HEART_RATE_SERVICE_UUID in uuids,
                }
            )
        # Strongest signal first -- purely a display convenience, the
        # address is what actually identifies a device.
        results.sort(key=lambda r: (r["rssi"] is None, -(r["rssi"] or 0)))
        return results

    try:
        results = asyncio.run(_scan())
        with state.lock:
            state.results = results
            state.error = None
    except Exception as exc:  # noqa: BLE001 -- a scan failing must never crash the dashboard.
        # The known, expected case here is "no Bluetooth adapter at all"
        # (FileNotFoundError via BlueZ/D-Bus, same as transports/ble.py's
        # own discovery failure -- see that module's docstring) -- but
        # this is a UI-facing best-effort action, so any failure is
        # reported to the page rather than raised.
        logger.warning(f"BLE scan failed: {exc}")
        with state.lock:
            state.error = str(exc)
            state.results = None
    finally:
        with state.lock:
            state.running = False
            state.finished_at_wall_ns = time.time_ns()


def make_handler(plugin: "BiosensorPlugin", scan_state: _BleScanState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send_html(_PAGE_HTML)
            elif path == "/api/state":
                self._send_json(200, plugin.dashboard_state())
            elif path == "/api/scan/ble":
                self._send_json(200, scan_state.snapshot())
            else:
                self._send_json(404, {"error": "unknown path"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/scan/ble":
                self._send_json(404, {"error": "unknown path"})
                return

            qs = parse_qs(parsed.query)
            try:
                timeout_s = float(qs.get("timeout_s", [DEFAULT_BLE_SCAN_TIMEOUT_S])[0])
            except (ValueError, IndexError):
                timeout_s = DEFAULT_BLE_SCAN_TIMEOUT_S
            timeout_s = max(MIN_BLE_SCAN_TIMEOUT_S, min(MAX_BLE_SCAN_TIMEOUT_S, timeout_s))

            with scan_state.lock:
                already_running = scan_state.running
                if not already_running:
                    scan_state.running = True
            if already_running:
                self._send_json(200, {"status": "already_running"})
                return

            threading.Thread(
                target=_run_ble_scan,
                args=(scan_state, timeout_s),
                daemon=True,
                name="biosensor-ui-ble-scan",
            ).start()
            self._send_json(202, {"status": "started", "timeout_s": timeout_s})

    return Handler


def run_server(plugin: "BiosensorPlugin", host: str, port: int) -> ThreadingHTTPServer:
    scan_state = _BleScanState()
    handler_cls = make_handler(plugin, scan_state)
    server = ThreadingHTTPServer((host, port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="biosensor-ui-server")
    thread.start()
    return server


_PAGE_HTML = b"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>biosensor-plugin-01 -- live dashboard</title>
<style>
  :root {
    --bg: #0f1115; --panel: #161a22; --panel-2: #1d2330; --border: #2a3040;
    --text: #e4e7ee; --text-dim: #8b93a6; --accent: #4fd1c5; --good: #4fd17c;
    --warn: #e0b84f; --bad: #e0654f; --mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    padding: 20px; max-width: 1100px; margin: 0 auto;
  }
  header { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; margin-bottom: 6px; }
  h1 { font-size: 18px; margin: 0; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-dim); margin: 26px 0 10px; }
  .sub { color: var(--text-dim); font-size: 12px; }
  .mono { font-family: var(--mono); }
  #conn-warning {
    display: none; background: #3a1f1f; border: 1px solid var(--bad); color: #ffb3a7;
    padding: 8px 12px; border-radius: 6px; margin: 10px 0; font-size: 12.5px;
  }
  .banner {
    padding: 10px 14px; border-radius: 8px; font-size: 13px; margin: 14px 0;
    border: 1px solid var(--border); display: flex; align-items: center; gap: 8px;
  }
  .banner.on { background: #142a20; border-color: var(--good); }
  .banner.off { background: var(--panel); }
  .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; flex: none; }
  .dot.good { background: var(--good); box-shadow: 0 0 6px var(--good); }
  .dot.warn { background: var(--warn); box-shadow: 0 0 6px var(--warn); }
  .dot.bad { background: var(--bad); }
  .dot.dim { background: #454c5c; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
  .card h3 { margin: 0 0 2px; font-size: 14.5px; }
  .card .id { color: var(--text-dim); font-size: 11.5px; margin-bottom: 10px; }
  .status-row { display: flex; align-items: center; gap: 8px; font-size: 13px; margin-bottom: 10px; }
  .chips { display: flex; flex-wrap: wrap; gap: 5px; margin: 8px 0; }
  .chip {
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 999px;
    padding: 2px 9px; font-size: 11px; color: var(--text-dim); font-family: var(--mono);
  }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th { color: var(--text-dim); font-weight: 500; }
  .counters { display: grid; grid-template-columns: 1fr 1fr; gap: 3px 14px; font-size: 12px; color: var(--text-dim); margin: 10px 0; }
  .counters b { color: var(--text); }
  .samples-wrap { max-height: 170px; overflow-y: auto; border: 1px solid var(--border); border-radius: 6px; margin-top: 4px; }
  .empty { color: var(--text-dim); font-size: 12.5px; font-style: italic; padding: 8px 0; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
  button {
    background: var(--accent); color: #06201c; border: none; border-radius: 6px;
    padding: 7px 14px; font-size: 13px; font-weight: 600; cursor: pointer;
  }
  button:disabled { opacity: .5; cursor: default; }
  input[type=number] {
    width: 60px; background: var(--panel-2); border: 1px solid var(--border); color: var(--text);
    border-radius: 5px; padding: 5px 6px; font-family: var(--mono); font-size: 12.5px;
  }
  .inactive-list { font-size: 12.5px; color: var(--text-dim); }
  .inactive-list li { margin-bottom: 4px; }
  footer { margin-top: 34px; color: var(--text-dim); font-size: 11.5px; border-top: 1px solid var(--border); padding-top: 12px; }
</style>
</head>
<body>

<header>
  <h1 id="title">biosensor-plugin-01 -- live dashboard</h1>
  <div class="sub mono" id="uptime">--</div>
</header>
<div class="sub" id="meta">--</div>
<div id="conn-warning">Can't reach this plugin's own dashboard API -- the process may have stopped. Retrying...</div>

<div id="session-banner" class="banner off">
  <span class="dot dim"></span><span id="session-text">Checking session state...</span>
</div>

<h2>Devices</h2>
<div class="grid" id="devices"></div>
<div id="no-devices" class="empty" style="display:none">No devices configured -- check BIOSENSOR_TRANSPORTS and the device profile directory (see README.md, DEVICE_REQUIREMENTS.md).</div>

<div id="inactive-section" style="display:none">
  <h2>Configured but not active</h2>
  <ul class="inactive-list" id="inactive-list"></ul>
</div>

<h2>Nearby BLE devices (not yet set up)</h2>
<div class="panel">
  <div class="status-row">
    <button id="scan-btn" onclick="startScan()">Scan for nearby BLE devices</button>
    <span>for <input type="number" id="scan-timeout" value="8" min="1" max="30"> s</span>
    <span class="sub" id="scan-status"></span>
  </div>
  <table id="scan-table" style="display:none">
    <thead><tr><th>Address</th><th>Name</th><th>RSSI</th><th>Heart Rate service (0x180D)</th></tr></thead>
    <tbody id="scan-tbody"></tbody>
  </table>
  <div class="empty" id="scan-empty">No scan run yet. A device showing up here only means it's <em>advertising</em> --
    run <span class="mono">tools/ble_scan.py --address &lt;addr&gt;</span> to connect and confirm it actually implements
    the Heart Rate GATT service before writing a device profile for it. See DEVICE_REQUIREMENTS.md.</div>
</div>

<footer>
  Local diagnostic only -- not part of the Hub protocol (plugins/PROTOCOL.md) or the ingest API
  (plugins/BIOSENSOR_INGEST.md). Auto-refreshes every second. Nothing on this page is sent anywhere.
</footer>

<script>
const fmtAge = ms => ms == null ? '--' : (ms < 1000 ? Math.round(ms) + 'ms' : (ms/1000).toFixed(1) + 's');
const fmtSecs = s => { s = Math.floor(s); const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  return (h ? h+'h ' : '') + (h||m ? m+'m ' : '') + sec+'s'; };
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function statusOf(d) {
  if (!d.connected) return {cls: 'dim', label: 'no device'};
  if (d.contact === false) return {cls: 'warn', label: 'present, no skin contact'};
  if (d.contact === true) return {cls: 'good', label: 'connected'};
  return {cls: 'warn', label: 'connected (contact unknown)'};
}

function renderDevices(devices) {
  const grid = document.getElementById('devices');
  document.getElementById('no-devices').style.display = devices.length ? 'none' : 'block';
  grid.innerHTML = devices.map(d => {
    const s = statusOf(d);
    const chips = (d.channels || []).map(c => `<span class="chip">${esc(c)}</span>`).join('') || '<span class="chip">--</span>';
    const samples = (d.recent_samples || []).slice(-8).reverse();
    const rows = samples.length
      ? samples.map(sm => `<tr><td>${esc(sm.channel)}</td><td>${sm.value}</td><td>${(sm.quality*100).toFixed(0)}%</td></tr>`).join('')
      : '';
    const samplesBlock = samples.length
      ? `<div class="samples-wrap"><table><thead><tr><th>channel</th><th>value</th><th>quality</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : '<div class="empty">No samples yet.</div>';
    return `
      <div class="card">
        <h3>${esc(d.display_name || d.device_id)}</h3>
        <div class="id mono">${esc(d.device_id)} &middot; ${esc(d.transport)}</div>
        <div class="status-row"><span class="dot ${s.cls}"></span>${s.label}
          ${d.link_quality != null ? `<span class="sub">&middot; link ${d.link_quality}</span>` : ''}
          <span class="sub">&middot; last sample ${fmtAge(d.last_sample_age_ms)} ago</span>
        </div>
        <div class="chips">${chips}</div>
        <div class="counters">
          <div>sent: <b>${d.samples_sent}</b></div>
          <div>queue depth: <b>${d.queue_depth}</b></div>
          <div>dropped (queue full): <b>${d.samples_dropped_by_queue}</b></div>
          <div>discarded (no session): <b>${d.samples_discarded_no_session}</b></div>
          <div>batches failed: <b>${d.batches_failed}</b></div>
        </div>
        ${samplesBlock}
      </div>`;
  }).join('');
}

function renderInactive(list) {
  const section = document.getElementById('inactive-section');
  section.style.display = list.length ? 'block' : 'none';
  document.getElementById('inactive-list').innerHTML = list.map(p =>
    `<li><span class="mono">${esc(p.id)}</span> (${esc(p.display_name)}, transport: ${esc(p.transport)}) -- ${esc(p.reason)}</li>`
  ).join('');
}

async function refresh() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) throw new Error('bad response');
    const state = await res.json();
    document.getElementById('conn-warning').style.display = 'none';
    document.getElementById('title').textContent = state.plugin_id + ' -- live dashboard';
    document.getElementById('uptime').textContent = 'uptime ' + fmtSecs(state.uptime_s);
    document.getElementById('meta').textContent =
      `ingest: ${state.ingest_base_url}  |  enabled transports: ${state.enabled_transports.join(', ') || '(none)'}`;

    const banner = document.getElementById('session-banner');
    const text = document.getElementById('session-text');
    const dot = banner.querySelector('.dot');
    if (state.active_session_id) {
      banner.className = 'banner on';
      dot.className = 'dot good';
      text.textContent = `Recording session active (${state.active_session_id}) -- forwarding samples to the host.`;
    } else {
      banner.className = 'banner off';
      dot.className = 'dot dim';
      text.textContent = 'No active recording session -- devices stay connected, but nothing is being forwarded.';
    }

    renderDevices(state.devices || []);
    renderInactive(state.inactive_profiles || []);
  } catch (e) {
    document.getElementById('conn-warning').style.display = 'block';
  }
}

let scanPoll = null;
async function startScan() {
  const timeout_s = document.getElementById('scan-timeout').value || 8;
  document.getElementById('scan-btn').disabled = true;
  document.getElementById('scan-status').textContent = `Scanning for ${timeout_s}s...`;
  await fetch(`/api/scan/ble?timeout_s=${encodeURIComponent(timeout_s)}`, { method: 'POST' });
  if (scanPoll) clearInterval(scanPoll);
  scanPoll = setInterval(pollScan, 500);
}

async function pollScan() {
  const res = await fetch('/api/scan/ble');
  const s = await res.json();
  if (s.running) return;
  clearInterval(scanPoll);
  scanPoll = null;
  document.getElementById('scan-btn').disabled = false;
  const statusEl = document.getElementById('scan-status');
  const table = document.getElementById('scan-table');
  const empty = document.getElementById('scan-empty');
  if (s.error) {
    statusEl.textContent = `Scan failed: ${s.error}`;
    table.style.display = 'none';
    empty.style.display = 'block';
    empty.textContent = `Scan failed (${s.error}). Common cause: no Bluetooth adapter reachable on this machine -- see README.md's Prerequisites section.`;
  } else if (s.results && s.results.length) {
    statusEl.textContent = `Found ${s.results.length} device(s).`;
    table.style.display = 'table';
    empty.style.display = 'none';
    document.getElementById('scan-tbody').innerHTML = s.results.map(r => `
      <tr>
        <td class="mono">${esc(r.address)}</td>
        <td>${esc(r.name)}</td>
        <td>${r.rssi == null ? '--' : r.rssi}</td>
        <td>${r.advertises_heart_rate_service ? '<span class="dot good"></span> yes' : '--'}</td>
      </tr>`).join('');
  } else if (s.results) {
    statusEl.textContent = 'No devices found nearby.';
    table.style.display = 'none';
    empty.style.display = 'block';
  }
}

refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""
