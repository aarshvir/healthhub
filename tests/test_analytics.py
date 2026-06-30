"""Tests for analytics.py — the trend engine + window recompute."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from freezegun import freeze_time

import analytics
import glucose
import integrity
import store as store_mod

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def st():
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        # 40 days of 12 readings/day, deterministic sawtooth around range
        recs = []
        for d in range(40):
            day = NOW - timedelta(days=d)
            for h in range(0, 24, 2):
                val = 90 + ((h * 7 + d * 13) % 120)  # 90..209
                recs.append({"measured_at": day.replace(hour=h, minute=0), "glucose_mgdl": val})
        glucose.sync(s, glucose.FixtureSource(recs), now=NOW)
        yield s
        s.close()
        integrity.cache_clear()


def test_window_recompute_differs(st):
    r7 = analytics.compute(st, window_days=7, now=NOW)
    r30 = analytics.compute(st, window_days=30, now=NOW)
    assert r7["n_readings"] < r30["n_readings"]
    assert r7["window_days"] == 7 and r30["window_days"] == 30
    # both produce a per-day series and an AGP of 96 slots
    assert len(r7["series"]) <= 8
    assert len(r7["agp"]) == 96


def test_custom_window(st):
    r = analytics.compute(st, window_days=3, now=NOW)
    assert r["window_days"] == 3
    assert r["n_readings"] > 0


def test_group_by_week(st):
    r = analytics.compute(st, window_days=30, now=NOW, group_by="week")
    assert all("W" in row["group"] for row in r["series"])


def test_summary_and_selfcheck_clean(st):
    r = analytics.compute(st, window_days=14, now=NOW)
    assert set(("mean_mgdl", "tir_pct", "gri")).issubset(r["summary"].keys())
    assert analytics.self_check(r) == []


def test_metric_set_filters_output(st):
    r = analytics.compute(st, window_days=7, now=NOW, metric_set=("tir_pct", "gmi_pct"))
    keys = set(r["series"][0].keys())
    assert "tir_pct" in keys and "gmi_pct" in keys
    assert "gri" not in keys  # not requested


def test_empty_store_safe():
    with freeze_time(NOW):
        s = store_mod.Store(":memory:")
        r = analytics.compute(s, window_days=7, now=NOW)
        assert r["n_readings"] == 0 and r["series"] == []
        assert len(r["agp"]) == 96 and all(b is None for b in r["agp"])
        s.close()


def test_standard_windows(st):
    allw = analytics.compute_standard_windows(st, now=NOW)
    assert set(allw.keys()) == set(analytics.STANDARD_WINDOWS)
    assert allw[7]["n_readings"] <= allw[500]["n_readings"]
