"""Tests for store.py — the 500-day idempotent store + quarantine."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from freezegun import freeze_time

import integrity
import store as store_mod

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def st():
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        yield s
        s.close()
        integrity.cache_clear()


def _glucose_df(values, start=NOW, step=timedelta(minutes=15), subject="p1"):
    ts = [start - i * step for i in range(len(values))]
    return pd.DataFrame({"subject": subject, "measured_at": ts, "glucose_mgdl": values})


def test_append_and_window(st):
    res = st.append_glucose(_glucose_df([100, 110, 120]), now=NOW)
    assert res.n_in == 3 and res.n_stored == 3 and res.n_quarantined == 0
    df = st.glucose_last_days(1, now=NOW)
    assert len(df) == 3
    assert sorted(df["glucose_mgdl"].tolist()) == [100.0, 110.0, 120.0]


def test_idempotent_reingest(st):
    df = _glucose_df([100, 110, 120])
    st.append_glucose(df, now=NOW)
    st.append_glucose(df, now=NOW)          # same timestamps -> no duplicates
    st.append_glucose(df, now=NOW)
    assert len(st.glucose_all()) == 3


def test_out_of_range_row_is_quarantined_not_stored(st):
    # 5000 mg/dL is beyond the physiological range -> quarantined, not plotted
    res = st.append_glucose(_glucose_df([100, 5000, 120]), now=NOW)
    assert res.n_stored == 2
    assert res.n_quarantined == 1
    q = st.quarantined("glucose")
    assert len(q) == 1 and "out_of_range" in q[0]["reason"]
    assert 5000.0 not in st.glucose_all()["glucose_mgdl"].tolist()


def test_future_timestamp_is_quarantined(st):
    df = pd.DataFrame({"subject": "p1",
                       "measured_at": [NOW + timedelta(hours=2)],
                       "glucose_mgdl": [120]})
    res = st.append_glucose(df, now=NOW)
    assert res.n_stored == 0 and res.n_quarantined == 1
    assert "future" in st.quarantined("glucose")[0]["reason"]


def test_naive_timestamp_is_quarantined(st):
    df = pd.DataFrame({"subject": "p1",
                       "measured_at": [datetime(2026, 6, 28, 10, 0, 0)],  # naive
                       "glucose_mgdl": [120]})
    res = st.append_glucose(df, now=NOW)
    assert res.n_stored == 0 and res.n_quarantined == 1


def test_window_filters(st):
    st.append_glucose(_glucose_df([100, 110, 120, 130, 140], step=timedelta(days=2)), now=NOW)
    recent = st.glucose_last_days(3, now=NOW)
    assert len(recent) == 2  # only readings within 3 days (NOW and NOW-2d)


def test_prune_retention(st):
    old = NOW - timedelta(days=600)
    st.append_glucose(_glucose_df([100], start=old), now=NOW)
    st.append_glucose(_glucose_df([120], start=NOW), now=NOW)
    deleted = st.prune(now=NOW, retention_days=500)
    assert deleted == 1
    assert len(st.glucose_all()) == 1


def test_last_known_good_cached(st):
    st.append_glucose(_glucose_df([100, 110, 140]), now=NOW)
    assert integrity.cache_get_value("glucose_mgdl", "p1", now=NOW) == 100.0  # newest ts


def test_events_idempotent(st):
    recs = [{"key": "e1", "measured_at": NOW.isoformat(), "type": "meal", "carbs": 40},
            {"key": "e2", "measured_at": (NOW - timedelta(hours=1)).isoformat(), "type": "mood"}]
    st.append_events("log", recs, now=NOW)
    st.append_events("log", recs, now=NOW)  # idempotent on key
    got = st.events("log")
    assert len(got) == 2


def test_self_check_clean(st):
    st.append_glucose(_glucose_df([100, 110, 120]), now=NOW)
    assert st.self_check(now=NOW) == []
