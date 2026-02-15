#!/usr/bin/env python3
"""Simple MQTT consumer — subscribes to concept2 topics and logs to stdout + JSON file."""

import json
import signal
import sys
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

MQTT_BROKER = "localhost"
MQTT_PORT = 1883
TOPIC = "concept2/#"
LOG_DIR = Path("./data")


def on_connect(client, userdata, flags, rc, properties):
    print(f"Connected to MQTT broker (rc={rc})")
    client.subscribe(TOPIC)
    print(f"Subscribed to {TOPIC}")


def on_message(client, userdata, msg):
    topic = msg.topic
    try:
        payload = json.loads(msg.payload)
    except json.JSONDecodeError:
        payload = msg.payload.decode()

    # Pretty print to console
    if topic.endswith("/stroke"):
        p = payload
        pace_min = int(p.get("pace_secs", 0)) // 60
        pace_sec = int(p.get("pace_secs", 0)) % 60
        print(
            f"  {p.get('distance_m', 0):>6.0f}m | "
            f"{pace_min}:{pace_sec:02d}/500m | "
            f"{p.get('stroke_rate', 0):>2.0f}spm | "
            f"{p.get('watts', 0):>3.0f}W | "
            f"HR:{p.get('heart_rate', 0)}"
        )
    elif topic.endswith("/event"):
        print(f"\n*** {payload.get('event', 'unknown')} ***\n")
    elif topic.endswith("/status"):
        print(f"[erg status: {payload}]")
    else:
        print(f"[{topic}] {payload}")

    # Append to daily JSON-lines log
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
    with open(log_file, "a") as f:
        f.write(json.dumps({"topic": topic, "payload": payload}) + "\n")


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_BROKER, MQTT_PORT)

    print(f"Listening on {MQTT_BROKER}:{MQTT_PORT}...")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nDone.")
        client.disconnect()


if __name__ == "__main__":
    main()
