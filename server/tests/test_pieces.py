import io
import json
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import threading
import time

from pieces import Outbox, PieceDetector, Reporter, load_env, post_piece  # noqa: E402

T0 = datetime(2026, 11, 14, 15, 0, 0, tzinfo=timezone.utc)


def strokes(start=T0, secs=600, meters=2312, hr=118, base_distance=0.0, step=1):
    out = []
    for s in range(0, secs + 1, step):
        out.append({
            "timestamp": (start + timedelta(seconds=s)).isoformat(),
            "distance_m": base_distance + meters * s / secs,
            "pace_secs": 130.0, "stroke_rate": 20, "watts": 150,
            "heart_rate": hr, "elapsed_secs": s,
        })
    return out


def feed(d, items):
    last = None
    for p in items:
        r = d.stroke(p)
        if r:
            last = r
    return last


def test_workout_end_closes_a_piece_with_summary():
    d = PieceDetector()
    feed(d, strokes(secs=600, meters=2312, hr=118))
    s = d.event({"event": "workout_end"})
    assert s["meters"] == 2312 and s["piece_key"] == "pm5-2026-11-14T15:00:00Z"
    assert s["avg_heart_rate"] == 118 and s["max_heart_rate"] == 118
    assert abs(s["avg_pace_seconds"] - 600 / (2312 / 500)) < 0.01
    assert s["elapsed_seconds"] == 600 and s["avg_watts"] == 150
    assert s["started_at"] == "2026-11-14T15:00:00Z"
    assert s["ended_at"] == "2026-11-14T15:10:00Z"
    assert d.event({"event": "workout_end"}) is None


def test_non_end_event_is_ignored():
    d = PieceDetector()
    feed(d, strokes())
    assert d.event({"event": "workout_start"}) is None


def test_distance_reset_starts_a_new_piece():
    d = PieceDetector()
    first = strokes(secs=300, meters=1000)
    second = strokes(start=T0 + timedelta(seconds=400), secs=300, meters=900)
    done = feed(d, first + second)
    assert done["meters"] == 1000
    s2 = d.event({"event": "workout_end"})
    assert s2["meters"] == 900
    assert s2["piece_key"] == "pm5-2026-11-14T15:06:40Z"


def test_stall_closes_at_the_last_distance_increase():
    d = PieceDetector()
    items = strokes(secs=300, meters=1000)
    feed(d, items)
    last = datetime.fromisoformat(items[-1]["timestamp"])
    assert d.tick(last + timedelta(seconds=10)) is None
    s = d.tick(last + timedelta(seconds=31))
    assert s["ended_at"] == "2026-11-14T15:05:00Z" and s["meters"] == 1000
    assert d.tick(last + timedelta(seconds=60)) is None


def test_stall_trims_flat_tail_strokes():
    d = PieceDetector()
    items = strokes(secs=300, meters=1000)
    flat = []
    for i in range(1, 41):
        p = dict(items[-1])
        p["timestamp"] = (T0 + timedelta(seconds=300 + i)).isoformat()
        p["elapsed_secs"] = 300 + i
        flat.append(p)
    feed(d, items + flat)
    s = d.tick(T0 + timedelta(seconds=341))
    assert s["ended_at"] == "2026-11-14T15:05:00Z" and s["elapsed_seconds"] == 300
    assert d.stroke(flat[-1]) is None  # idle strokes after close do not open a piece
    assert d.tick(T0 + timedelta(seconds=500)) is None


def test_short_pieces_are_dropped():
    d = PieceDetector()
    feed(d, strokes(secs=40, meters=500))
    assert d.event({"event": "workout_end"}) is None
    d = PieceDetector()
    feed(d, strokes(secs=600, meters=80))
    assert d.event({"event": "workout_end"}) is None


def test_zero_heart_rate_means_no_hr():
    d = PieceDetector()
    feed(d, strokes(hr=0))
    s = d.event({"event": "workout_end"})
    assert s["avg_heart_rate"] is None and s["max_heart_rate"] is None
    assert [row[5] for row in s["strokes"]] == [0] * len(s["strokes"])


def test_strokes_are_downsampled_to_five_seconds():
    d = PieceDetector()
    feed(d, strokes(secs=600, meters=2312))
    s = d.event({"event": "workout_end"})
    rows = s["strokes"]
    assert rows[0][0] == 0 and len(rows) == 121
    assert all(b[0] - a[0] >= 5 for a, b in zip(rows, rows[1:]))
    assert len(rows[0]) == 6


def test_outbox_survives_restart_and_remove(tmp_path):
    p = tmp_path / "outbox.jsonl"
    ob = Outbox(p)
    ob.add({"piece_key": "a", "meters": 1})
    ob.add({"piece_key": "b", "meters": 2})
    ob2 = Outbox(p)
    assert [x["piece_key"] for x in ob2.pending()] == ["a", "b"]
    ob2.remove("a")
    assert [x["piece_key"] for x in Outbox(p).pending()] == ["b"]
    assert Outbox(tmp_path / "none.jsonl").pending() == []


class FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self.status


def test_post_piece_sends_bearer_json_and_reports_status():
    seen = {}

    def opener(req, timeout=None):
        seen["req"] = req
        return FakeResp(201)

    assert post_piece({"piece_key": "k"}, "http://x:1", "tok", opener=opener) is True
    req = seen["req"]
    assert req.full_url == "http://x:1/api/v1/erg_pieces"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == {"piece_key": "k"}

    assert post_piece({}, "http://x", "t", opener=lambda r, timeout=None: FakeResp(200)) is True

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "err", {}, io.BytesIO(b""))

    assert post_piece({}, "http://x", "t", opener=boom) is False

    def down(req, timeout=None):
        raise urllib.error.URLError("down")

    assert post_piece({}, "http://x", "t", opener=down) is False


def test_load_env(tmp_path):
    f = tmp_path / "pieces.env"
    f.write_text("# c\nFITNESS_TOKEN=abc\n\nMQTT_BROKER = localhost\nBAD\n")
    assert load_env(f) == {"FITNESS_TOKEN": "abc", "MQTT_BROKER": "localhost"}
    assert load_env(tmp_path / "missing") == {}


def test_corrupt_outbox_line_survives_remove(tmp_path):
    p = tmp_path / "o.jsonl"
    ob = Outbox(p)
    ob.add({"piece_key": "a"})
    with p.open("a") as f:
        f.write("{not json\n")
    ob.add({"piece_key": "b"})
    ob.remove("a")
    assert "{not json" in p.read_text()
    assert [x["piece_key"] for x in ob.pending()] == ["b"]


def test_failed_post_leaves_outbox_intact(tmp_path):
    ob = Outbox(tmp_path / "o.jsonl")
    ob.add({"piece_key": "a", "meters": 1})

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b""))

    r = Reporter(ob, "http://x", "t", opener=boom)
    r.flush()
    assert [x["piece_key"] for x in ob.pending()] == ["a"]
    r.opener = lambda req, timeout=None: FakeResp(201)
    r.flush()
    assert ob.pending() == []


def test_bad_url_does_not_kill_flush(tmp_path):
    ob = Outbox(tmp_path / "o.jsonl")
    ob.add({"piece_key": "a"})

    def weird(req, timeout=None):
        raise ValueError("bad url")

    Reporter(ob, "nope", "t", opener=weird).flush()
    assert len(ob.pending()) == 1


def test_detector_never_blocks_on_posting(tmp_path):
    ob = Outbox(tmp_path / "o.jsonl")
    release = threading.Event()

    def slow(req, timeout=None):
        release.wait(5)
        return FakeResp(201)

    r = Reporter(ob, "http://x", "t", opener=slow)
    feed_r = strokes(secs=300, meters=1000)
    for p in feed_r:
        r.on_stroke(p)
    r.on_event({"event": "workout_end"})  # piece lands in the outbox
    sender = threading.Thread(target=r.flush)
    sender.start()
    time.sleep(0.1)  # sender is now stuck inside the opener
    t0 = time.monotonic()
    for p in strokes(start=T0 + timedelta(seconds=1000), secs=60, meters=200):
        r.on_stroke(p)
    r.tick(T0 + timedelta(seconds=1060))
    assert time.monotonic() - t0 < 1.0
    assert sender.is_alive()
    release.set()
    sender.join(5)
    assert ob.pending() == []  # released: the first piece posted and was removed


def test_offset_timestamp_is_normalized_to_utc():
    d = PieceDetector()
    items = strokes(start=datetime(2026, 11, 14, 20, 0, 0, tzinfo=timezone(timedelta(hours=5))), secs=120, meters=400)
    feed(d, items)
    s = d.event({"event": "workout_end"})
    assert s["piece_key"] == "pm5-2026-11-14T15:00:00Z" and s["started_at"] == "2026-11-14T15:00:00Z"
    assert s["ended_at"] == "2026-11-14T15:02:00Z"
