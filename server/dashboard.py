#!/usr/bin/env python3
"""Concept2 live dashboard — bridges MQTT to WebSocket for browser clients."""

import asyncio
import json
import os
import queue
import sys
from datetime import datetime, timedelta
from pathlib import Path

import paho.mqtt.client as mqtt
from aiohttp import web

MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC = "concept2/#"
HTTP_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
LOG_DIR = Path(os.environ.get("LOG_DIR", "./data"))

HERE = Path(__file__).parent
ws_clients: set = set()
_msg_queue: queue.Queue = queue.Queue()
_latest_battery: dict | None = None


# -- MQTT -> WebSocket bridge ------------------------------------------------

def create_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, rc, properties):
        print(f"MQTT connected (rc={rc})", flush=True)
        client.subscribe(MQTT_TOPIC)

    def on_message(client, userdata, msg):
        global _latest_battery
        payload = msg.payload.decode()
        envelope = json.dumps({"topic": msg.topic, "payload": json.loads(payload)
                                if msg.topic != "concept2/status" else payload})

        # Track latest battery reading
        if msg.topic == "concept2/battery":
            try:
                _latest_battery = json.loads(payload)
            except json.JSONDecodeError:
                pass

        # Log stroke data to JSONL
        if msg.topic == "concept2/stroke":
            LOG_DIR.mkdir(exist_ok=True)
            log_file = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
            with open(log_file, "a") as f:
                f.write(json.dumps({"topic": msg.topic,
                                    "payload": json.loads(payload)}) + "\n")

        # Put on thread-safe queue for the async broadcast worker
        _msg_queue.put(envelope)

    client.on_connect = on_connect
    client.on_message = on_message
    return client


async def _broadcast_worker():
    """Poll the thread-safe queue and broadcast to WebSocket clients."""
    while True:
        try:
            message = _msg_queue.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue
        dead = set()
        for ws in list(ws_clients):
            try:
                await ws.send_str(message)
            except Exception:
                dead.add(ws)
        ws_clients -= dead


# -- Summary computation (cached) -------------------------------------------

_summary_cache = {"mtimes": {}, "data": []}
SESSION_GAP = 30  # seconds between records to split sessions


def _compute_summaries():
    """Parse all JSONL files into daily aggregates. Cached by file mtimes."""
    jsonl_files = sorted(LOG_DIR.glob("*.jsonl"))
    if not jsonl_files:
        return []

    # Check cache validity
    current_mtimes = {str(f): f.stat().st_mtime for f in jsonl_files}
    if current_mtimes == _summary_cache["mtimes"]:
        return _summary_cache["data"]

    daily = {}
    for f in jsonl_files:
        date_str = f.stem  # filename is YYYY-MM-DD.jsonl
        records = []
        for line in f.read_text().splitlines():
            try:
                entry = json.loads(line)
                if entry.get("topic") == "concept2/stroke":
                    records.append(entry["payload"])
            except (json.JSONDecodeError, KeyError):
                continue

        if not records:
            continue

        # Segment into sessions by timestamp gaps
        sessions = []
        current_session = [records[0]]
        for i in range(1, len(records)):
            try:
                t_prev = datetime.fromisoformat(records[i - 1]["timestamp"])
                t_curr = datetime.fromisoformat(records[i]["timestamp"])
                gap = (t_curr - t_prev).total_seconds()
            except (KeyError, ValueError):
                gap = 0
            if gap > SESSION_GAP:
                sessions.append(current_session)
                current_session = []
            current_session.append(records[i])
        if current_session:
            sessions.append(current_session)

        # Aggregate sessions into daily summary
        total_meters = 0
        total_secs = 0
        pace_sum = 0
        watts_sum = 0
        spm_sum = 0
        hr_sum = 0
        hr_count = 0
        cal_total = 0
        n_records = 0

        for sess in sessions:
            if len(sess) < 2:
                continue
            dist = sess[-1].get("distance_m", 0) - sess[0].get("distance_m", 0)
            dur = sess[-1].get("elapsed_secs", 0) - sess[0].get("elapsed_secs", 0)
            cals = sess[-1].get("calories", 0) - sess[0].get("calories", 0)
            total_meters += max(dist, 0)
            total_secs += max(dur, 0)
            cal_total += max(cals, 0)
            for r in sess:
                pace = r.get("pace_secs", 0)
                watts = r.get("watts", 0)
                if pace > 0 and pace < 600 and watts > 0:
                    pace_sum += pace
                    watts_sum += watts
                    spm_sum += r.get("stroke_rate", 0)
                    n_records += 1
                hr = r.get("heart_rate", 0)
                if hr > 0:
                    hr_sum += hr
                    hr_count += 1

        if total_meters > 0:
            daily[date_str] = {
                "date": date_str,
                "total_meters": round(total_meters),
                "total_secs": round(total_secs),
                "avg_pace": round(pace_sum / n_records, 1) if n_records else 0,
                "avg_watts": round(watts_sum / n_records) if n_records else 0,
                "avg_spm": round(spm_sum / n_records) if n_records else 0,
                "avg_hr": round(hr_sum / hr_count) if hr_count else 0,
                "calories": round(cal_total),
                "sessions": len(sessions),
            }

    result = [daily[k] for k in sorted(daily.keys())]
    _summary_cache["mtimes"] = current_mtimes
    _summary_cache["data"] = result
    return result


# -- HTTP handlers -----------------------------------------------------------

async def index(request: web.Request) -> web.Response:
    return web.FileResponse(HERE / "dashboard.html")


async def history_page(request: web.Request) -> web.Response:
    return web.FileResponse(HERE / "history.html")


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.add(ws)
    print(f"WebSocket client connected ({len(ws_clients)} total)")

    try:
        async for msg in ws:
            pass  # We only send, never receive
    finally:
        ws_clients.discard(ws)
        print(f"WebSocket client disconnected ({len(ws_clients)} total)")

    return ws


async def history(request: web.Request) -> web.Response:
    """Return today's stroke data (or a specified date's) as JSON array."""
    date_str = request.query.get("date", f"{datetime.now():%Y-%m-%d}")
    log_file = LOG_DIR / f"{date_str}.jsonl"

    if not log_file.exists():
        return web.json_response([])

    entries = []
    for line in log_file.read_text().splitlines():
        try:
            entry = json.loads(line)
            if entry.get("topic") == "concept2/stroke":
                entries.append(entry["payload"])
        except json.JSONDecodeError:
            continue

    return web.json_response(entries)


async def summary(request: web.Request) -> web.Response:
    """Return daily aggregates for all logged data."""
    data = _compute_summaries()
    return web.json_response(data)


async def battery(request: web.Request) -> web.Response:
    """Return the latest battery reading."""
    return web.json_response(_latest_battery)


# -- App setup ---------------------------------------------------------------

async def start_mqtt(app: web.Application):
    client = create_mqtt_client()
    client.connect(MQTT_BROKER, MQTT_PORT)
    client.loop_start()
    app["mqtt_client"] = client
    app["broadcast_task"] = asyncio.ensure_future(_broadcast_worker())


async def stop_mqtt(app: web.Application):
    task = app.get("broadcast_task")
    if task:
        task.cancel()
    client = app.get("mqtt_client")
    if client:
        client.loop_stop()
        client.disconnect()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/history", history_page)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/api/history", history)
    app.router.add_get("/api/summary", summary)
    app.router.add_get("/api/battery", battery)
    app.on_startup.append(start_mqtt)
    app.on_cleanup.append(stop_mqtt)
    return app


if __name__ == "__main__":
    print(f"Starting dashboard on :{HTTP_PORT} (MQTT: {MQTT_BROKER}:{MQTT_PORT})")
    web.run_app(create_app(), port=HTTP_PORT)
