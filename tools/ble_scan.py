"""Standalone diagnostic: scan for nearby BLE devices and, optionally,
inspect one device's GATT services/characteristics -- not part of the
shipped plugin (`plugin.json`'s entrypoint never points here), and not tied
to any specific device.

This is the tool to run **first** when adding a new BLE device profile
(plan.md section 7): it answers "what does this device actually advertise,
and does it implement the standard Heart Rate service (0x180D) `ble.py`
already speaks?" -- the two facts `devices/polar-h10.json`-style profiles
need (`match.name_prefix`, `match.service_uuid`), and the one fact that
determines whether a device needs a JSON profile only or a whole new
transport.

Must be run where a real Bluetooth adapter is reachable -- a sandboxed
container/VM (as opposed to the machine's own OS) typically has no adapter
at all, which is a `discover` returning nothing, not a crash; see
`transports/ble.py`'s module docstring for how the plugin itself handles
that same case.

Usage:

    python tools/ble_scan.py                       # list nearby devices
    python tools/ble_scan.py --address AA:BB:CC:DD:EE:FF   # inspect one device's GATT tree
    python tools/ble_scan.py --name-prefix "HUAWEI"          # scan, filtered by name
    python tools/ble_scan.py --timeout 15
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner

HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


async def scan(timeout: float, name_prefix: str | None) -> None:
    print(f"Scanning for {timeout:.0f}s (Ctrl+C to stop early)...\n")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    if not devices:
        print(
            "No BLE devices found. Either nothing nearby is advertising, or this "
            "machine has no usable Bluetooth adapter (a sandboxed container/VM "
            "commonly has none -- run this on the machine's own OS instead)."
        )
        return

    found_hr_service = False
    for device, adv in devices.values():
        name = device.name or "(no name)"
        if name_prefix and not name.startswith(name_prefix):
            continue
        uuids = adv.service_uuids or []
        has_hr = any(u.lower() == HEART_RATE_SERVICE_UUID for u in uuids)
        found_hr_service = found_hr_service or has_hr
        marker = "  <-- advertises standard Heart Rate service (0x180D)" if has_hr else ""
        print(f"{device.address}  {name!r}  rssi={adv.rssi}{marker}")
        if uuids:
            print(f"    service UUIDs: {uuids}")

    print()
    if not found_hr_service:
        print(
            "None of the above advertised the standard Heart Rate service (0x180D) in "
            "their advertisement data. That's common even for devices that DO support it "
            "-- many only expose it after connecting, not in the advertisement itself. "
            "Run this script again with --address <the device's address> to connect and "
            "check its actual GATT service tree, which is the definitive answer."
        )


async def inspect(address: str) -> None:
    print(f"Connecting to {address}...\n")
    async with BleakClient(address) as client:
        print(f"Connected: {client.is_connected}\n")
        found_hr_service = False
        found_hr_char = False
        for service in client.services:
            is_hr_service = service.uuid.lower() == HEART_RATE_SERVICE_UUID
            found_hr_service = found_hr_service or is_hr_service
            marker = "  <-- standard Heart Rate service (0x180D)" if is_hr_service else ""
            print(f"service {service.uuid} ({service.description}){marker}")
            for char in service.characteristics:
                is_hr_char = char.uuid.lower() == HEART_RATE_MEASUREMENT_UUID
                found_hr_char = found_hr_char or is_hr_char
                char_marker = "  <-- Heart Rate Measurement characteristic" if is_hr_char else ""
                print(f"    characteristic {char.uuid} ({char.description}) {char.properties}{char_marker}")

        print()
        if found_hr_service and found_hr_char:
            print(
                "This device implements the standard Heart Rate service and "
                "Measurement characteristic -- transports/ble.py (biosensor-plugin-01) "
                "should work with it out of the box via a devices/<name>.json profile, "
                "no code change needed. See devices/polar-h10.json for the shape."
            )
        else:
            print(
                "This device does NOT expose the standard Heart Rate service/characteristic "
                "while connected this way. That means either: it needs a different mode "
                "enabled first (check the device/app for a 'broadcast heart rate' setting "
                "and re-run this scan while that mode is active), or it uses a proprietary "
                "protocol -- which plan.md section 2 rules out supporting via a vendor SDK "
                "or cloud API, but a genuinely standards-based alternative (this device's "
                "own local GATT, once found) would still be in scope."
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", default=None, help="Connect to this device and list its GATT tree.")
    parser.add_argument("--name-prefix", default=None, help="When scanning, only show devices whose name starts with this.")
    parser.add_argument("--timeout", type=float, default=10.0, help="Scan duration in seconds (default 10).")
    args = parser.parse_args()

    if args.address:
        asyncio.run(inspect(args.address))
    else:
        asyncio.run(scan(args.timeout, args.name_prefix))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
