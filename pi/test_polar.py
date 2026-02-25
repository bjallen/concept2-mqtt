#!/usr/bin/env python3
"""Quick test: connect to Polar H10 and print HR notifications."""
import asyncio
from bleak import BleakClient, BleakScanner

HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

hr_count = 0


def hr_callback(sender, data):
    global hr_count
    hr_count += 1
    flags = data[0]
    hr = int.from_bytes(data[1:3], "little") if flags & 0x01 else data[1]
    print(f"  HR: {hr} bpm (raw: {data.hex()})", flush=True)


async def main():
    print("Scanning for Polar H10...", flush=True)
    device = await BleakScanner.find_device_by_filter(
        lambda dev, adv: dev.name is not None and dev.name.startswith("Polar H10"),
        timeout=20,
    )
    if not device:
        print("No Polar H10 found")
        return

    print(f"Found: {device.name} ({device.address})", flush=True)
    print("Connecting (no service filter)...", flush=True)

    async with BleakClient(device, timeout=30) as client:
        print(f"Connected: {client.is_connected}", flush=True)
        print(f"Services: {[s.uuid for s in client.services]}", flush=True)

        print("Subscribing to HR notifications...", flush=True)
        await client.start_notify(HR_MEASUREMENT_UUID, hr_callback)
        print("Subscribed! Waiting 30s for data...\n", flush=True)

        for i in range(30):
            await asyncio.sleep(1)
            if not client.is_connected:
                print(f"\nDisconnected after {i}s!", flush=True)
                break
            if i > 0 and i % 10 == 0:
                print(f"  ... {i}s elapsed, {hr_count} HR readings", flush=True)

        print(f"\nTotal HR readings: {hr_count}", flush=True)


asyncio.run(main())
