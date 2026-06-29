"""Tests for wearables.py — parse the Health Connect export (synthetic, real schema)."""

import json
from datetime import datetime, timezone

import pytest
from freezegun import freeze_time

import wearables

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)

ACT_HDR = ["Date", "Source(s)", "Timezone", "Steps", "Distance (m)", "Elevation (m)",
           "Floors climbed", "Total Calories (kcal)", "Active Calories (kcal)",
           "Power min (W)", "Power max (W)", "Power avg (W)", "Speed min (m/s)",
           "Speed max (m/s)", "Speed avg (m/s)", "VO2 max min", "VO2 max max",
           "VO2 max avg", "Wheelchair pushes", "Start Date/Time", "Exercise Name",
           "Duration (min)"]


def _act(date, source, steps, *, total_cal="", start="", ex="", dur="", dist=""):
    row = [date, source, "Asia/Dubai", steps, dist, "", "", total_cal, "", "", "", "",
           "", "", "", "", "", "", "", start, ex, dur]
    return row


def _rows():
    rows = []
    rows.append(ACT_HDR)
    rows.append(_act("2026-06-27", "android", "3000", total_cal="1564"))
    rows.append(_act("2026-06-27", "com.sec.android.app.shealth", "5000", total_cal="1719"))
    rows.append(_act("2026-06-27", "com.sec.android.app.shealth", "", start="2026-06-27 20:56:36",
                     ex="79 - Walking", dur="17", dist="1150.7"))
    rows.append(_act("2027-01-01", "android", "9999", total_cal="2000"))  # future -> quarantine
    rows.append([])  # blank separator
    rows.append(["Date/Time", "Source(s)", "Timezone", "Weight (kg)", "Body Fat (%)",
                 "Bone mass (kg)", "Height (m)", "Lean body mass (kg)"])
    rows.append(["2026-06-27 21:52:04", "life.simple", "Asia/Dubai", "127.10", "", "", "", ""])
    rows.append([])
    rows.append(["Date", "Source(s)", "Timezone", "Hydration (ml)"])
    rows.append(["2026-06-27", "life.simple", "Asia/Dubai", "2000"])
    rows.append([])
    rows.append(["Date", "Source(s)", "Timezone", "Start Time", "End Time",
                 "Light Sleep (min)", "Deep Sleep (min)", "REM Sleep (min)", "Awake (min)"])
    rows.append(["2026-06-27", "com.sec.android.app.shealth", "Asia/Dubai",
                 "2026-06-27 02:44:00", "2026-06-27 05:22:00", "86", "56", "3", "5"])
    rows.append([])
    rows.append(["Date", "Source(s)", "Timezone", "Heart rate min (bpm)",
                 "Heart rate max (bpm)", "Heart rate avg (bpm)", "Oxygen saturation avg (%)"])
    rows.append(["2026-06-27", "com.sec.android.app.shealth", "Asia/Dubai",
                 "59", "115", "76.43", "95.5"])
    return rows


def test_parse_dedups_and_merges():
    with freeze_time(NOW):
        data = wearables.parse(_rows(), now=NOW)
    day = data["days"]["2026-06-27"]
    assert day["steps"] == 5000.0                    # shealth preferred over android(3000)
    assert day["activity_source"] == "com.sec.android.app.shealth"
    assert day["weight_kg"] == pytest.approx(127.10)
    assert day["hydration_ml"] == 2000.0
    assert day["sleep_total_min"] == 145.0           # 86+56+3
    assert day["sleep_deep_min"] == 56.0
    assert day["hr_avg"] == pytest.approx(76.43)
    assert day["spo2_avg"] == pytest.approx(95.5)


def test_walk_events_extracted():
    with freeze_time(NOW):
        data = wearables.parse(_rows(), now=NOW)
    assert len(data["walk_events"]) == 1
    assert data["walk_events"][0]["duration_min"] == 17.0


def test_future_date_quarantined():
    with freeze_time(NOW):
        data = wearables.parse(_rows(), now=NOW)
    assert "2027-01-01" not in data["days"]
    assert any(q["reason"] == "future_date" for q in data["quarantine"])


def test_self_check_clean_and_write(tmp_path):
    with freeze_time(NOW):
        data = wearables.parse(_rows(), now=NOW)
        assert wearables.self_check(data, now=NOW) == []
    path = wearables.write_wearables(data, str(tmp_path / "wearables.json"))
    loaded = json.loads(open(path).read())
    assert loaded["n_days"] == 1


def test_build_from_fixture_source():
    with freeze_time(NOW):
        data = wearables.build(wearables.FixtureRows(_rows()), now=NOW)
    assert data["days"]["2026-06-27"]["steps"] == 5000.0
