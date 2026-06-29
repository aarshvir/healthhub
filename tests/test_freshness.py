"""Tests for integrity.freshness — age + state of a timestamp."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import integrity
from integrity import FreshnessState, freshness

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


# --- the REQUIRED case: a stale timestamp -------------------------------------------
def test_required_stale_timestamp():
    f = freshness(NOW - timedelta(hours=24), now=NOW)
    assert f.state is FreshnessState.STALE
    assert f.age == timedelta(hours=24)
    assert f.measured_at == NOW - timedelta(hours=24)
    assert f.evaluated_at == NOW


# --- fresh + its boundary ------------------------------------------------------------
def test_fresh_within_window():
    assert freshness(NOW - timedelta(hours=2), now=NOW).state is FreshnessState.FRESH


def test_fresh_exact_boundary_is_fresh():
    assert freshness(NOW - timedelta(hours=6), now=NOW).state is FreshnessState.FRESH


def test_just_past_fresh_is_stale():
    assert freshness(NOW - timedelta(hours=6, seconds=1), now=NOW).state is FreshnessState.STALE


# --- expired + its boundary ----------------------------------------------------------
def test_expired():
    assert freshness(NOW - timedelta(days=4), now=NOW).state is FreshnessState.EXPIRED


def test_stale_exact_boundary_is_stale():
    assert freshness(NOW - timedelta(hours=72), now=NOW).state is FreshnessState.STALE


def test_just_past_stale_is_expired():
    assert freshness(NOW - timedelta(hours=72, seconds=1), now=NOW).state is FreshnessState.EXPIRED


# --- future + skew -------------------------------------------------------------------
def test_future_is_distinct_fail_state():
    f = freshness(NOW + timedelta(hours=1), now=NOW)
    assert f.state is FreshnessState.FUTURE
    assert f.age < timedelta(0)


def test_small_future_within_skew_is_fresh():
    assert freshness(NOW + timedelta(minutes=2), now=NOW).state is FreshnessState.FRESH


def test_future_skew_exact_boundary_is_fresh():
    assert freshness(NOW + timedelta(minutes=5), now=NOW).state is FreshnessState.FRESH


def test_just_beyond_skew_is_future():
    assert freshness(NOW + timedelta(minutes=5, seconds=1), now=NOW).state is FreshnessState.FUTURE


# --- timezone handling ---------------------------------------------------------------
def test_naive_ts_rejected_without_assume_tz():
    with pytest.raises(integrity.IntegrityError):
        freshness(datetime(2026, 6, 28, 10, 0, 0), now=NOW)


def test_naive_ts_with_assume_tz_works():
    f = freshness(datetime(2026, 6, 28, 6, 0, 0), now=NOW, assume_tz=timezone.utc)
    assert f.state is FreshnessState.FRESH
    assert f.age == timedelta(hours=6)


def test_naive_now_rejected():
    with pytest.raises(integrity.IntegrityError):
        freshness(NOW, now=datetime(2026, 6, 28, 12, 0, 0))


def test_non_utc_aware_ts_normalized():
    ist = timezone(timedelta(hours=5, minutes=30))
    # 17:30 IST == 12:00 UTC == NOW -> age 0
    ts = datetime(2026, 6, 28, 17, 30, 0, tzinfo=ist)
    f = freshness(ts, now=NOW)
    assert f.age == timedelta(0)
    assert f.state is FreshnessState.FRESH


def test_iso_string_parsed():
    assert freshness("2026-06-28T10:00:00+00:00", now=NOW).state is FreshnessState.FRESH


def test_garbage_string_rejected():
    with pytest.raises(integrity.IntegrityError):
        freshness("not-a-timestamp", now=NOW)


def test_nat_rejected():
    with pytest.raises(integrity.IntegrityError):
        freshness(pd.NaT, now=NOW)


def test_pandas_timestamp_supported():
    ts = pd.Timestamp("2026-06-28T08:00:00", tz="UTC")
    assert freshness(ts, now=NOW).state is FreshnessState.FRESH


def test_default_now_uses_wall_clock():
    # measured "now-ish" via the sanctioned clock is fresh without injecting now
    assert freshness(integrity.now_utc()).state is FreshnessState.FRESH


# --- config guards -------------------------------------------------------------------
def test_config_guard_fresh_not_less_than_stale():
    with pytest.raises(integrity.IntegrityError):
        freshness(NOW, now=NOW, fresh_within=timedelta(hours=72), stale_within=timedelta(hours=6))


def test_config_guard_negative_skew():
    with pytest.raises(integrity.IntegrityError):
        freshness(NOW, now=NOW, future_skew=timedelta(minutes=-1))


def test_custom_thresholds_honored():
    f = freshness(NOW - timedelta(minutes=30), now=NOW,
                  fresh_within=timedelta(minutes=10), stale_within=timedelta(hours=1))
    assert f.state is FreshnessState.STALE
