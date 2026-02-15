#!/usr/bin/env python3
"""Concept2 PM5 monitor — reads stroke data via USB and publishes to MQTT."""

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from pyrow import pyrow

# -- Config ------------------------------------------------------------------
MQTT_BROKER = os.environ.get("MQTT_BROKER", "mac-mini-server.local")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC_PREFIX = "concept2"
POLL_INTERVAL = 0.25  # seconds between reads (4 Hz is plenty for stroke data)

# -- Helpers -----------------------------------------------------------------

def find_erg():
    """Block until a PM5 is connected via USB."""
    print("Searching for Concept2 PM5...")
    while True:
        for dev in pyrow.find():
            erg = pyrow.PyErg(dev)
            print(f"Connected to erg: {dev}")
            return erg
        time.sleep(1)


def build_message(monitor: dict, workout: dict) -> dict:
    """Flatten pyrow dicts into a clean JSON payload."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # Stroke data
        "stroke_rate": monitor.get("spm", 0),
        "pace_secs": monitor.get("pace", 0),       # seconds per 500m
        "watts": monitor.get("power", 0),
        "calories": monitor.get("calories", 0),
        "heart_rate": monitor.get("heartrate", 0),
        # Cumulative
        "distance_m": monitor.get("distance", 0),
        "elapsed_secs": monitor.get("time", 0),
        # Workout context
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

    stroke_count = 0
    last_state = None

    while running:
        try:
            monitor = erg.get_monitor()
            workout = erg.get_workout()
        except Exception as e:
            print(f"Lost connection to erg: {e}")
            erg = find_erg()
            continue

        state = workout.get("state", 0)

        # Detect workout start/end
        if state != last_state:
            if state == 1:  # rowing
                print("Workout started")
                stroke_count = 0
                client.publish(
                    f"{MQTT_TOPIC_PREFIX}/event",
                    json.dumps({"event": "workout_start", "timestamp": datetime.now(timezone.utc).isoformat()}),
                )
            elif last_state == 1 and state != 1:
                print("Workout ended")
                client.publish(
                    f"{MQTT_TOPIC_PREFIX}/event",
                    json.dumps({"event": "workout_end", "timestamp": datetime.now(timezone.utc).isoformat()}),
                )
            last_state = state

        # Only publish stroke data while actively rowing
        if state == 1:
            msg = build_message(monitor, workout)
            stroke_count += 1
            msg["stroke_count"] = stroke_count

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
    print("Done.")


if __name__ == "__main__":
    main()
