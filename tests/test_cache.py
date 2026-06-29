"""Tests for the integrity last-known-good cache."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

import integrity
from integrity import CacheMiss, FreshnessState

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _frozen_clock_and_clean_cache():
    # cache_set reads the real wall clock (for its FUTURE check and stored_at stamp), so
    # freeze it to NOW to make the future-rejection and freshness gates deterministic.
    with freeze_time(NOW):
        integrity.cache_clear()
        yield
        integrity.cache_clear()


def test_set_then_get_returns_entry():
    entry = integrity.cache_set("glucose_mgdl", "p1", 120, NOW - timedelta(hours=1))
    assert entry.value == 120.0
    assert entry.metric == "glucose_mgdl"
    assert entry.measured_at == NOW - timedelta(hours=1)
    assert entry.stored_at.tzinfo is not None  # stamped by cache_set, not caller-supplied
    got = integrity.cache_get("glucose_mgdl", "p1", now=NOW)
    assert got.value == 120.0


def test_get_unknown_key_misses():
    with pytest.raises(CacheMiss):
        integrity.cache_get("glucose_mgdl", "nobody", now=NOW)


def test_monotonic_older_write_ignored():
    integrity.cache_set("glucose_mgdl", "p1", 120, NOW - timedelta(hours=1))
    integrity.cache_set("glucose_mgdl", "p1", 80, NOW - timedelta(hours=5))  # older
    assert integrity.cache_get_value("glucose_mgdl", "p1", now=NOW) == 120.0


def test_equal_timestamp_keeps_existing():
    integrity.cache_set("glucose_mgdl", "p1", 120, NOW - timedelta(hours=1))
    entry = integrity.cache_set("glucose_mgdl", "p1", 999, NOW - timedelta(hours=1))
    assert entry.value == 120.0


def test_newer_write_replaces():
    integrity.cache_set("glucose_mgdl", "p1", 120, NOW - timedelta(hours=5))
    integrity.cache_set("glucose_mgdl", "p1", 140, NOW - timedelta(hours=1))
    assert integrity.cache_get_value("glucose_mgdl", "p1", now=NOW) == 140.0


def test_future_measurement_rejected():
    with pytest.raises(integrity.IntegrityError):
        integrity.cache_set("glucose_mgdl", "p1", 120, NOW + timedelta(hours=1),
                            assume_tz=timezone.utc)


def test_unknown_metric_rejected_on_set():
    with pytest.raises(integrity.IntegrityError):
        integrity.cache_set("moon_phase", "p1", 1, NOW - timedelta(hours=1))


def test_non_finite_value_rejected_on_set():
    with pytest.raises(integrity.IntegrityError):
        integrity.cache_set("glucose_mgdl", "p1", float("nan"), NOW - timedelta(hours=1))


def test_read_time_freshness_gate():
    integrity.cache_set("glucose_mgdl", "p1", 120, NOW - timedelta(hours=24))  # stale
    with pytest.raises(CacheMiss):
        integrity.cache_get("glucose_mgdl", "p1", max_state=FreshnessState.FRESH, now=NOW)
    # looser gate still returns it; the too-stale miss did NOT evict
    assert integrity.cache_get_value("glucose_mgdl", "p1",
                                     max_state=FreshnessState.STALE, now=NOW) == 120.0


def test_expired_entry_self_evicts():
    integrity.cache_set("glucose_mgdl", "p1", 120, NOW - timedelta(days=4))
    with pytest.raises(CacheMiss):
        integrity.cache_get("glucose_mgdl", "p1", now=NOW)
    # gone afterwards — even a future "now" relative to stored data cannot resurrect it
    with pytest.raises(CacheMiss):
        integrity.cache_get("glucose_mgdl", "p1", max_state=FreshnessState.EXPIRED, now=NOW)


def test_cache_clear_empties():
    integrity.cache_set("glucose_mgdl", "p1", 120, NOW - timedelta(hours=1))
    integrity.cache_clear()
    with pytest.raises(CacheMiss):
        integrity.cache_get("glucose_mgdl", "p1", now=NOW)


def test_thread_safe_monotonic_under_contention():
    base = NOW - timedelta(hours=50)
    times = [base + timedelta(minutes=i) for i in range(50)]

    def worker(i):
        integrity.cache_set("glucose_mgdl", "p1", float(i), times[i])

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(worker, range(50)))

    entry = integrity.cache_get("glucose_mgdl", "p1", now=NOW)
    assert entry.measured_at == times[-1]  # newest measurement wins, no lost update/crash
