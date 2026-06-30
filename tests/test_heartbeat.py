"""Tests for heartbeat.py — per-source liveness + alerting."""

import json
from datetime import datetime, timedelta, timezone

import heartbeat

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def test_status_states():
    last = {
        "glucose": _iso(NOW - timedelta(minutes=10)),   # fresh (<30m)
        "wearables": _iso(NOW - timedelta(hours=10)),   # stale (>6h, <18h)
        "log": _iso(NOW - timedelta(days=10)),          # fresh (<36h? no, >36h -> down)
    }
    st = heartbeat.status(last, now=NOW)
    by = {s["source"]: s for s in st["sources"]}
    assert by["glucose"]["state"] == "fresh" and not by["glucose"]["stale"]
    assert by["wearables"]["state"] == "stale" and by["wearables"]["stale"]
    assert by["log"]["state"] == "down"
    # labs/supplement absent and not required -> not evaluated (no spam)
    assert "labs" not in by and "supplement" not in by
    assert st["overall_ok"] is False
    assert heartbeat.self_check(st) == []


def test_killed_feed_triggers_alert():
    last = {"glucose": _iso(NOW - timedelta(hours=2)),   # stale: feed "killed"
            "wearables": _iso(NOW - timedelta(minutes=30))}
    alerter = heartbeat.NoopAlerter()
    st = heartbeat.check(last, now=NOW, alerter=alerter)
    assert "glucose" in st["alerted"]
    assert any("glucose" in subj for subj, _ in alerter.sent)
    # wearables within 6h threshold stays fresh, not alerted
    by = {s["source"]: s for s in st["sources"]}
    assert by["wearables"]["state"] == "fresh"


def test_fresh_glucose_not_alerted():
    last = {"glucose": _iso(NOW - timedelta(minutes=5)),
            "wearables": _iso(NOW - timedelta(hours=1)),
            "log": _iso(NOW - timedelta(hours=2)),
            "labs": _iso(NOW - timedelta(days=20)),
            "supplement": _iso(NOW - timedelta(hours=2))}
    alerter = heartbeat.NoopAlerter()
    st = heartbeat.check(last, now=NOW, alerter=alerter)
    assert st["overall_ok"] is True
    assert alerter.sent == []


def test_write_health(tmp_path):
    st = heartbeat.status({"glucose": _iso(NOW)}, now=NOW)
    path = heartbeat.write_health(st, str(tmp_path / "health.json"))
    loaded = json.loads(open(path).read())
    assert "sources" in loaded and "overall_ok" in loaded
