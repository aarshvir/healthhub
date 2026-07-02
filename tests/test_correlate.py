"""Tests for daily.py (unified per-day frame) and correlate.py (cross-stream intelligence).

Synthetic scenario with a KNOWN structure so correlations are hand-checkable:
  * each day's mean glucose and that day's carbs both increase monotonically with the day index
    -> their rank (Spearman) correlation must be ~1.0 and cross-stream,
  * a post-meal walk tag on even days and intimacy every third day exercise the binary levers,
  * a small wearables dict exercises the wearables join.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from freezegun import freeze_time

import correlate
import daily
import integrity
import journal
import store as store_mod

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
DUBAI = ZoneInfo("Asia/Dubai")
DAYS = 14


def _dubai_date(day_offset: int) -> str:
    return (NOW - timedelta(days=day_offset)).astimezone(DUBAI).strftime("%Y-%m-%d")


@pytest.fixture()
def wear():
    days = {}
    for d in range(1, DAYS + 1):
        days[_dubai_date(d)] = {"steps": float(2000 + 300 * d),
                                "sleep_total_min": float(360 + 3 * d),
                                "sleep_deep_min": float(60 + d),
                                "hr_avg": float(60 + d % 5), "spo2_avg": 96.0}
    return {"days": days}


@pytest.fixture()
def st():
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        raws = []
        for d in range(1, DAYS + 1):
            day = NOW - timedelta(days=d)
            level = 90 + 6 * d           # daily mean glucose, monotonic in d
            carbs = 20 + 5 * d           # carbs, monotonic in d -> tracks glucose
            for h in (4, 8, 12, 16):     # 4 readings, mean == level exactly
                s.append_glucose(pd.DataFrame([{"measured_at": day.replace(hour=h, minute=0),
                                                "glucose_mgdl": level + (h - 10)}]), now=NOW)
            dstr = _dubai_date(d)
            raws.append({"entry_id": f"meal{d}", "date": dstr, "time": "13:00", "type": "meal",
                         "item": "Rice", "net_carbs_g": str(carbs),
                         "tags": "meal; +walk" if d % 2 == 0 else "meal"})
            raws.append({"entry_id": f"mood{d}", "date": dstr, "time": "21:00", "type": "mood",
                         "mood_1to5": str(1 + d % 5), "energy_1to5": str(1 + d % 4), "tags": ""})
            if d % 3 == 0:
                raws.append({"entry_id": f"sex{d}", "date": dstr, "time": "22:00",
                             "type": "sex", "tags": ""})
        s.append_events("log", journal.parse_rows(raws), key_field="key", now=NOW)
        yield s
        s.close()
        integrity.cache_clear()


# ---- daily.py ----------------------------------------------------------------------------
def test_daily_frame_joins_every_stream(st, wear):
    with freeze_time(NOW):
        frame = daily.build(st, window_days=30, now=NOW, wearables=wear)
    assert len(frame) == DAYS
    assert daily.self_check(frame) == []
    cov = daily.coverage(frame)
    assert cov["mean_mgdl"] == DAYS          # glucose every day
    assert cov["carbs_g"] == DAYS            # a meal every day
    assert cov["steps"] == DAYS              # wearables joined every day
    assert cov["mood"] == DAYS               # a mood log every day
    # sex logged every 3rd day -> 4 days flagged 1.0 (rest 0.0, still "present")
    assert sum(v for _, v in daily.series(frame, "sex")) == 4.0


def test_daily_frame_is_date_sorted_and_unique(st, wear):
    with freeze_time(NOW):
        frame = daily.build(st, window_days=30, now=NOW, wearables=wear)
    dates = [r["date"] for r in frame]
    assert dates == sorted(dates)
    assert len(dates) == len(set(dates))


def test_daily_missing_stream_is_none_not_guessed(st):
    # no wearables passed -> those fields must be None (never fabricated)
    with freeze_time(NOW):
        frame = daily.build(st, window_days=30, now=NOW)
    assert all(r["steps"] is None for r in frame)
    assert daily.coverage(frame)["steps"] == 0


# ---- correlate.py ------------------------------------------------------------------------
def test_food_glucose_correlation_is_found(st, wear):
    with freeze_time(NOW):
        frame = daily.build(st, window_days=30, now=NOW, wearables=wear)
    res = correlate.analyze(frame, min_n=5)
    assert correlate.self_check(res) == []
    hits = [f for f in res["all_findings"]
            if {f["a"], f["b"]} == {"mean_mgdl", "carbs_g"} and f["kind"] == "same-day"]
    assert hits, "expected a mean-glucose vs carbs same-day association"
    assert hits[0]["r"] >= 0.9                # both monotonic in day index -> rho ~ 1
    assert hits[0]["cross_stream"] is True
    assert hits[0]["strength"] == "strong"


def test_matrix_is_square_and_diagonal_is_one(st, wear):
    with freeze_time(NOW):
        frame = daily.build(st, window_days=30, now=NOW, wearables=wear)
    mtx = correlate.matrix(frame)
    n = len(daily.NUMERIC_FIELDS)
    assert len(mtx["grid"]) == n and all(len(row) == n for row in mtx["grid"])
    # a well-populated field correlates perfectly with itself
    i = mtx["fields"].index("mean_mgdl")
    assert mtx["grid"][i][i]["rho"] == pytest.approx(1.0)


def test_findings_ranked_and_never_causal(st, wear):
    with freeze_time(NOW):
        frame = daily.build(st, window_days=30, now=NOW, wearables=wear)
    res = correlate.analyze(frame, min_n=5)
    scores = [f["score"] for f in res["findings"]]
    assert scores == sorted(scores, reverse=True)
    # every finding carries its sample size and stays cross-stream (headline mode)
    assert all(f["n"] >= 5 for f in res["findings"])
    assert all(f["cross_stream"] for f in res["findings"])
    text = correlate.narrate(res["findings"])
    assert "observational" in text.lower()
    assert "cause" not in text.lower() and "because" not in text.lower()


def test_lag_pairs_skip_calendar_gaps():
    frame = [{"date": "2026-06-01", "x": 1.0, "y": 10.0},
             {"date": "2026-06-02", "x": 2.0, "y": 20.0},
             {"date": "2026-06-04", "x": 4.0, "y": 40.0}]   # 06-03 missing
    x, y = correlate._lag_pairs(frame, "x", "y")
    # only 06-01 -> 06-02 is calendar-adjacent; the gap and the last day yield no pair
    assert list(x) == [1.0] and list(y) == [20.0]


def test_narrate_blocks_ungrounded_numbers(st, wear):
    # a forged finding whose label smuggles an unsupported number must be scrubbed/flagged
    forged = [{"kind": "same-day", "a": "mean_mgdl", "b": "steps",
               "label": "Mean glucose spikes 999 points after steps", "r": 0.5,
               "direction": "+", "n": 9, "cross_stream": True, "strength": "moderate",
               "detail": "n=9 days", "score": 0.3}]
    text = correlate.narrate(forged, redact=True)
    assert "999" not in text            # 999 is not in grounded_numbers -> scrubbed
    assert "[UNVERIFIED]" in text
    # and fail-closed by default: an ungrounded number raises rather than leaking
    with pytest.raises(integrity.NarrationIntegrityError):
        correlate.narrate(forged)


def test_empty_frame_is_safe():
    res = correlate.analyze([])
    assert res["findings"] == [] and res["n_days"] == 0
    assert correlate.self_check(res) == []
    assert "keep logging" in correlate.narrate(res["findings"]).lower()
