"""Transport drivers. A transport is a module (BLE, LSL, ANT+, serial, fake)
that speaks one physical/logical protocol to zero or more devices; a device
profile (see ../devices/registry.py) is data that says which transport to
use and how to interpret it. See plan.md section 3.

Only "fake" (fake.py) is implemented so far -- BIO-4. "ble" and "lsl" are
BIO-7/BIO-8.
"""
