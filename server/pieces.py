#!/usr/bin/env python3
"""Detect finished PM5 pieces from the MQTT stream and POST them to the fitness server.

Subscribes to concept2/#, folds strokes into pieces, writes each finished piece
to a JSONL outbox and posts it (retrying the outbox on start and every 60 s).
The bridge's own JSONL logging (consumer.py) is untouched.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STALL_SECS = 30
MIN_SECS = 60
MIN_METERS = 100
SAMPLE_SPACING = 5
RETRY_SECS = 60
POST_TIMEOUT = 15


def _parse(ts) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def summarize(strokes: list) -> dict:
    first, last = strokes[0], strokes[-1]
    start = _parse(first["timestamp"])
    meters = last.get("distance_m", 0)
    elapsed = last.get("elapsed_secs", 0)
    watts = [s["watts"] for s in strokes if s.get("watts")]
    hrs = [s["heart_rate"] for s in strokes if s.get("heart_rate")]
    rows, kept = [], None
    for s in strokes:
        off = (_parse(s["timestamp"]) - start).total_seconds()
        if kept is None or off - kept >= SAMPLE_SPACING:
            kept = off
            rows.append([round(off, 1), s.get("distance_m", 0), s.get("pace_secs", 0),
                         s.get("stroke_rate", 0), s.get("watts", 0), s.get("heart_rate", 0)])
    return {
        "piece_key": "pm5-" + _iso(start),
        "started_at": _iso(start),
        "ended_at": _iso(_parse(last["timestamp"])),
        "meters": meters,
        "elapsed_seconds": elapsed,
        "avg_pace_seconds": elapsed / (meters / 500) if meters else None,
        "avg_watts": sum(watts) / len(watts) if watts else None,
        "avg_heart_rate": sum(hrs) / len(hrs) if hrs else None,
        "max_heart_rate": max(hrs) if hrs else None,
        "strokes": rows,
    }


class PieceDetector:
    def __init__(self):
        self._buf = []
        self._last_inc = -1       # index in _buf of the last distance increase
        self._closed_at = None    # distance of a just-closed piece; flat idle strokes are ignored

    def _finish(self, upto=None):
        buf = self._buf if upto is None else self._buf[:upto + 1]
        self._buf, self._last_inc = [], -1
        if not buf:
            return None
        self._closed_at = buf[-1].get("distance_m", 0)
        last = buf[-1]
        if last.get("elapsed_secs", 0) < MIN_SECS or last.get("distance_m", 0) < MIN_METERS:
            return None
        return summarize(buf)

    def stroke(self, payload: dict):
        d = payload.get("distance_m", 0)
        done = None
        if self._buf:
            prev = self._buf[-1].get("distance_m", 0)
            if d < prev:
                done = self._finish()
                self._closed_at = None
        elif self._closed_at is not None:
            if d == self._closed_at:
                return None  # idle strokes after a close
            self._closed_at = None
        prev_best = self._buf[self._last_inc].get("distance_m", 0) if self._last_inc >= 0 else None
        self._buf.append(payload)
        if prev_best is None or d > prev_best:
            self._last_inc = len(self._buf) - 1
        return done

    def event(self, payload: dict):
        if payload.get("event") != "workout_end":
            return None
        return self._finish()

    def tick(self, now: datetime):
        if self._last_inc < 0:
            return None
        last = _parse(self._buf[self._last_inc]["timestamp"])
        if (now - last).total_seconds() >= STALL_SECS:
            return self._finish(upto=self._last_inc)
        return None


class Outbox:
    def __init__(self, path):
        self.path = Path(path)

    def pending(self) -> list:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    def add(self, summary: dict):
        with self.path.open("a") as f:
            f.write(json.dumps(summary) + "\n")

    def remove(self, piece_key: str):
        keep = [p for p in self.pending() if p.get("piece_key") != piece_key]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(p) + "\n" for p in keep))
        tmp.replace(self.path)


def post_piece(summary, url, token, opener=urllib.request.urlopen) -> bool:
    req = urllib.request.Request(
        url.rstrip("/") + "/api/v1/erg_pieces",
        data=json.dumps(summary).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with opener(req, timeout=POST_TIMEOUT) as resp:
            return resp.getcode() in (200, 201)
    except (urllib.error.URLError, OSError):
        return False


def load_env(path) -> dict:
    env = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def main():
    import paho.mqtt.client as mqtt

    here = Path(__file__).parent
    env = load_env(here / "pieces.env")
    url = env.get("FITNESS_URL", "http://127.0.0.1:8082")
    token = env.get("FITNESS_TOKEN", "")
    broker = env.get("MQTT_BROKER", "localhost")
    outbox = Outbox(here / "pieces-outbox.jsonl")
    detector = PieceDetector()
    lock = threading.Lock()

    def flush():
        for piece in outbox.pending():
            if post_piece(piece, url, token):
                outbox.remove(piece["piece_key"])
                print(f"posted {piece['piece_key']}", flush=True)

    def handle(summary):
        if summary:
            outbox.add(summary)
            print(f"piece {summary['piece_key']} {summary['meters']}m", flush=True)
            flush()

    def on_connect(client, userdata, flags, rc, properties=None):
        client.subscribe("concept2/#")

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        with lock:
            if msg.topic.endswith("/stroke"):
                handle(detector.stroke(payload))
            elif msg.topic.endswith("/event"):
                handle(detector.event(payload))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(broker, 1883)
    client.loop_start()
    flush()
    last_retry = time.monotonic()
    try:
        while True:
            time.sleep(1)
            with lock:
                handle(detector.tick(datetime.now(timezone.utc)))
            if time.monotonic() - last_retry >= RETRY_SECS:
                last_retry = time.monotonic()
                with lock:
                    flush()
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    sys.exit(main())
