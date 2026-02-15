#!/usr/bin/env python3
"""Concept2 PM5 monitor — reads stroke data via USB HID and publishes to MQTT."""

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

import hid
import paho.mqtt.client as mqtt
from pyrow.csafe import csafe_cmd

# -- Config ------------------------------------------------------------------
MQTT_BROKER = os.environ.get("MQTT_BROKER", "mac-mini-server.local")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC_PREFIX = "concept2"
POLL_INTERVAL = 0.25  # seconds between reads
C2_VENDOR_ID = 0x17A4
C2_PRODUCT_ID = 0x0003
MIN_FRAME_GAP = 0.050  # minimum gap between USB commands (seconds)

# -- HID Erg wrapper ---------------------------------------------------------

class Erg:
    """Communicates with a Concept2 PM5 over USB HID using CSAFE."""

    def __init__(self, dev):
        self._dev = dev
        self._last_send = 0.0

    def send(self, command, retries=3):
        """Encode a CSAFE command list, send via HID, and return decoded response."""
        csafe = csafe_cmd.write(command)
        frame = list(csafe[1:])  # first byte is expected response length
        msg = [0x00] + frame + [0] * (64 - len(frame) - 1)

        for attempt in range(retries):
            # Throttle to avoid overwhelming the PM5
            elapsed = time.monotonic() - self._last_send
            if elapsed < MIN_FRAME_GAP:
                time.sleep(MIN_FRAME_GAP - elapsed)

            self._dev.write(msg[:64])
            self._last_send = time.monotonic()

            time.sleep(0.05)
            resp = self._dev.read(64, timeout_ms=2000)
            if not resp:
                continue

            # csafe_cmd.read() expects [report_id, start_flag, ...data..., stop_flag]
            result = csafe_cmd.read(resp)
            if isinstance(result, dict) and len(result) > 1:
                return result
            # Got garbage — flush and retry
            while self._dev.read(64, timeout_ms=50):
                pass

        raise TimeoutError("No valid response from PM5")

    def get_monitor(self):
        command = ['CSAFE_PM_GET_WORKTIME', 'CSAFE_PM_GET_WORKDISTANCE',
                   'CSAFE_GETCADENCE_CMD', 'CSAFE_GETPOWER_CMD',
                   'CSAFE_GETCALORIES_CMD', 'CSAFE_GETHRCUR_CMD']
        results = self.send(command)

        monitor = {}
        monitor['time'] = (results['CSAFE_PM_GET_WORKTIME'][0] +
                           results['CSAFE_PM_GET_WORKTIME'][1]) / 100.0
        monitor['distance'] = (results['CSAFE_PM_GET_WORKDISTANCE'][0] +
                               results['CSAFE_PM_GET_WORKDISTANCE'][1]) / 10.0
        monitor['spm'] = results['CSAFE_GETCADENCE_CMD'][0]
        monitor['power'] = results['CSAFE_GETPOWER_CMD'][0]
        if monitor['power']:
            monitor['pace'] = ((2.8 / monitor['power']) ** (1.0 / 3)) * 500
            monitor['calhr'] = monitor['power'] * (4.0 * 0.8604) + 300.0
        else:
            monitor['pace'] = 0
            monitor['calhr'] = 0
        monitor['calories'] = results['CSAFE_GETCALORIES_CMD'][0]
        monitor['heartrate'] = results['CSAFE_GETHRCUR_CMD'][0]
        monitor['status'] = results['CSAFE_GETSTATUS_CMD'][0] & 0xF
        return monitor

    def get_workout(self):
        command = ['CSAFE_GETID_CMD', 'CSAFE_PM_GET_WORKOUTTYPE',
                   'CSAFE_PM_GET_WORKOUTSTATE', 'CSAFE_PM_GET_INTERVALTYPE',
                   'CSAFE_PM_GET_WORKOUTINTERVALCOUNT']
        results = self.send(command)

        workout = {}
        workout['userid'] = results['CSAFE_GETID_CMD'][0]
        workout['type'] = results['CSAFE_PM_GET_WORKOUTTYPE'][0]
        workout['state'] = results['CSAFE_PM_GET_WORKOUTSTATE'][0]
        workout['inttype'] = results['CSAFE_PM_GET_INTERVALTYPE'][0]
        workout['intcount'] = results['CSAFE_PM_GET_WORKOUTINTERVALCOUNT'][0]
        workout['status'] = results['CSAFE_GETSTATUS_CMD'][0] & 0xF
        return workout

    def close(self):
        self._dev.close()


# -- Helpers -----------------------------------------------------------------

def find_erg():
    """Block until a PM5 is connected via USB HID."""
    print("Searching for Concept2 PM5...")
    while True:
        try:
            dev = hid.device()
            dev.open(C2_VENDOR_ID, C2_PRODUCT_ID)
            # Flush any stale data
            while dev.read(64, timeout_ms=100):
                pass
            # Send a warmup status command to sync the connection
            warmup = [0x00, 0xF1, 0x80, 0x80, 0xF2] + [0] * 59
            dev.write(warmup)
            time.sleep(0.1)
            dev.read(64, timeout_ms=1000)  # discard warmup response
            print(f"Connected to: {dev.get_product_string()}")
            return Erg(dev)
        except OSError:
            time.sleep(1)


def build_message(monitor: dict, workout: dict) -> dict:
    """Flatten into a clean JSON payload."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stroke_rate": monitor.get("spm", 0),
        "pace_secs": monitor.get("pace", 0),
        "watts": monitor.get("power", 0),
        "calories": monitor.get("calories", 0),
        "cal_per_hr": round(monitor.get("calhr", 0)),
        "heart_rate": monitor.get("heartrate", 0),
        "distance_m": monitor.get("distance", 0),
        "elapsed_secs": monitor.get("time", 0),
        "workout_type": workout.get("type", ""),
        "workout_state": workout.get("state", ""),
        "interval_count": workout.get("intcount", 0),
    }


# -- Main loop ---------------------------------------------------------------

def main():
    running = True

    def shutdown(sig, frame):
        nonlocal running
        print("\nShutting down...")
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # MQTT setup
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.will_set(f"{MQTT_TOPIC_PREFIX}/status", "offline", retain=True)
    client.connect(MQTT_BROKER, MQTT_PORT)
    client.loop_start()
    client.publish(f"{MQTT_TOPIC_PREFIX}/status", "online", retain=True)
    print(f"MQTT connected to {MQTT_BROKER}:{MQTT_PORT}")

    erg = find_erg()

    last_state = None

    while running:
        try:
            monitor = erg.get_monitor()
            workout = erg.get_workout()
        except OSError:
            # USB device disconnected
            print("Lost USB connection to erg")
            erg.close()
            erg = find_erg()
            continue
        except (TimeoutError, KeyError, ValueError, TypeError):
            # Transient read error — retry without reconnecting
            continue

        state = workout.get("state", 0)

        # Detect workout start/end
        if state != last_state:
            if state == 1:  # rowing
                print("Workout started")
                client.publish(
                    f"{MQTT_TOPIC_PREFIX}/event",
                    json.dumps({"event": "workout_start",
                                "timestamp": datetime.now(timezone.utc).isoformat()}),
                )
            elif last_state == 1 and state != 1:
                print("Workout ended")
                client.publish(
                    f"{MQTT_TOPIC_PREFIX}/event",
                    json.dumps({"event": "workout_end",
                                "timestamp": datetime.now(timezone.utc).isoformat()}),
                )
            last_state = state

        # Only publish stroke data while actively rowing
        if state == 1:
            msg = build_message(monitor, workout)
            payload = json.dumps(msg)
            client.publish(f"{MQTT_TOPIC_PREFIX}/stroke", payload)

            pace_min = int(msg["pace_secs"]) // 60
            pace_sec = int(msg["pace_secs"]) % 60
            print(
                f"  {msg['distance_m']:>6.0f}m | "
                f"{pace_min}:{pace_sec:02d}/500m | "
                f"{msg['stroke_rate']:>2.0f}spm | "
                f"{msg['watts']:>3.0f}W | "
                f"HR:{msg['heart_rate']}"
            )

        time.sleep(POLL_INTERVAL)

    # Cleanup
    client.publish(f"{MQTT_TOPIC_PREFIX}/status", "offline", retain=True)
    client.loop_stop()
    client.disconnect()
    erg.close()
    print("Done.")


if __name__ == "__main__":
    main()
