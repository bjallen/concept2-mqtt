#!/usr/bin/env python3
"""Detect finished PM5 pieces from the MQTT stream and POST them to the fitness server.

Subscribes to concept2/#, folds strokes into pieces, writes each finished piece
to a JSONL outbox and posts it (retrying the outbox on start and every 60 s).
The bridge's own JSONL logging (consumer.py) is untouched.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# A piece ends on workout_end or a distance reset. A pause (strokes stop, or
# keep coming with the distance flat) never ends it: the PM5's totals run on
# through a pause, so splitting there would give the second half the whole
# row's meters. Only a rower left idle this long is finished on its own.
IDLE_FINISH_SECS = 600
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
        "avg_heart_rate": round(sum(hrs) / len(hrs)) if hrs else None,
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
        """Finish a piece only once the rower has sat idle IDLE_FINISH_SECS since
        the last distance increase, trimming the flat tail. Shorter pauses keep
        the piece open: rowing on continues it."""
        if self._last_inc < 0:
            return None
        last = _parse(self._buf[self._last_inc]["timestamp"])
        if (now - last).total_seconds() >= IDLE_FINISH_SECS:
            return self._finish(upto=self._last_inc)
        return None


class Outbox:
    """JSONL queue. Corrupt lines are kept verbatim (never parsed, never dropped)."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _lines(self) -> list:
        if not self.path.exists():
            return []
        return [ln for ln in self.path.read_text().splitlines() if ln.strip()]

    @staticmethod
    def _parse(line):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    def pending(self) -> list:
        with self._lock:
            return [o for o in (self._parse(ln) for ln in self._lines()) if o is not None]

    def add(self, summary: dict):
        with self._lock, self.path.open("a") as f:
            # A crash mid-append leaves a torn last line with no newline; start
            # a fresh line so this entry isn't glued onto it and lost with it.
            if self.path.stat().st_size > 0:
                with self.path.open("rb") as r:
                    r.seek(-1, os.SEEK_END)
                    if r.read(1) != b"\n":
                        f.write("\n")
            f.write(json.dumps(summary) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def remove(self, piece_key: str):
        with self._lock:
            keep = []
            for ln in self._lines():
                obj = self._parse(ln)
                if obj is None or obj.get("piece_key") != piece_key:
                    keep.append(ln)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w") as f:
                f.write("".join(ln + "\n" for ln in keep))
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.path)


POSTED, RETRY, REJECTED = "posted", "retry", "rejected"

# 4xx answers that can heal without the piece changing: auth (a token being
# rotated), a route the fitness server hasn't deployed yet, timeouts and
# throttling. Any other 4xx is the server refusing this piece for good.
RETRYABLE_4XX = {401, 403, 404, 408, 429}


def post_piece(summary, url, token, opener=urllib.request.urlopen) -> str:
    """POSTED on 200/201; REJECTED on a permanent 4xx (see RETRYABLE_4XX);
    RETRY for everything else, network errors included."""
    req = urllib.request.Request(
        url.rstrip("/") + "/api/v1/erg_pieces",
        data=json.dumps(summary).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with opener(req, timeout=POST_TIMEOUT) as resp:
            status = resp.getcode()
    except urllib.error.HTTPError as e:
        print(f"post failed: HTTP {e.code} for {summary.get('piece_key')}", flush=True)
        if 400 <= e.code < 500 and e.code not in RETRYABLE_4XX:
            return REJECTED
        return RETRY
    except Exception as e:  # network, bad URL, anything: the sender must survive
        print(f"post failed: {type(e).__name__}: {e}", flush=True)
        return RETRY
    if status in (200, 201):
        return POSTED
    print(f"post failed: HTTP {status} for {summary.get('piece_key')}", flush=True)
    return RETRY


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


class Reporter:
    """Detector + outbox + sender. The on_* / tick methods are fast (detector lock,
    local file append only); all network I/O happens in flush(), which never takes
    the detector lock."""

    def __init__(self, outbox, url, token, opener=urllib.request.urlopen, dead=None):
        self.outbox, self.url, self.token, self.opener = outbox, url, token, opener
        # Pieces the server refused for good, kept (never retried, never dropped).
        self.dead = dead or Outbox(outbox.path.with_name("pieces-dead.jsonl"))
        self.detector = PieceDetector()
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self.wake = threading.Event()

    def _handle(self, summary):
        if summary:
            self.outbox.add(summary)
            print(f"piece {summary['piece_key']} {summary['meters']}m", flush=True)
            self.wake.set()

    def on_stroke(self, payload):
        with self._lock:
            self._handle(self.detector.stroke(payload))

    def on_event(self, payload):
        with self._lock:
            self._handle(self.detector.event(payload))

    def tick(self, now=None):
        with self._lock:
            self._handle(self.detector.tick(now or datetime.now(timezone.utc)))

    def flush(self):
        with self._flush_lock:
            for piece in self.outbox.pending():
                try:
                    result = post_piece(piece, self.url, self.token, opener=self.opener)
                    if result == POSTED:
                        self.outbox.remove(piece["piece_key"])
                        print(f"posted {piece['piece_key']}", flush=True)
                    elif result == REJECTED:
                        self.dead.add(piece)
                        self.outbox.remove(piece["piece_key"])
                        print(f"dead-lettered {piece['piece_key']} to {self.dead.path.name}", flush=True)
                except Exception as e:
                    print(f"flush error: {type(e).__name__}: {e}", flush=True)

    def sender_loop(self, stop: threading.Event):
        while not stop.is_set():
            self.flush()
            self.wake.wait(RETRY_SECS)
            self.wake.clear()


def main():
    import paho.mqtt.client as mqtt

    here = Path(__file__).parent
    env = load_env(here / "pieces.env")
    reporter = Reporter(
        Outbox(here / "pieces-outbox.jsonl"),
        env.get("FITNESS_URL", "http://127.0.0.1:8082"),
        env.get("FITNESS_TOKEN", ""),
        dead=Outbox(here / "pieces-dead.jsonl"),
    )
    broker = env.get("MQTT_BROKER", "localhost")

    def on_connect(client, userdata, flags, rc, properties=None):
        client.subscribe("concept2/#")

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        try:
            if msg.topic.endswith("/stroke"):
                reporter.on_stroke(payload)
            elif msg.topic.endswith("/event"):
                reporter.on_event(payload)
        except Exception as e:
            print(f"handler error: {type(e).__name__}: {e}", flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    delay = 1
    while True:
        try:
            client.connect(broker, 1883)
            break
        except OSError as e:
            print(f"mqtt connect failed ({e}); retrying in {delay}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    client.loop_start()
    stop = threading.Event()
    sender = threading.Thread(target=reporter.sender_loop, args=(stop,), daemon=True)
    sender.start()
    try:
        while True:
            time.sleep(1)
            try:
                reporter.tick()
            except Exception as e:
                print(f"tick error: {type(e).__name__}: {e}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        reporter.wake.set()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    sys.exit(main())
