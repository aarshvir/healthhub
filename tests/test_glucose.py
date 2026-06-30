"""Tests for glucose.py — pull with backoff, sync into the store, LKG degradation."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from freezegun import freeze_time

import glucose
import integrity
import store as store_mod

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
NOSLEEP = lambda _seconds: None  # noqa: E731 - keep retry tests instant


@pytest.fixture()
def st():
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        yield s
        s.close()
        integrity.cache_clear()


def _readings(values):
    return [{"measured_at": NOW - timedelta(minutes=15 * i), "glucose_mgdl": v}
            for i, v in enumerate(values)]


def test_sync_fixture_source(st):
    src = glucose.FixtureSource(_readings([100, 110, 120]))
    res = glucose.sync(st, src, now=NOW)
    assert res.n_stored == 3
    assert len(st.glucose_last_days(1, now=NOW)) == 3


def test_pull_retries_then_succeeds(st):
    inner = glucose.FixtureSource(_readings([100, 110]))
    flaky = glucose._FlakySource(inner, fail_times=3)  # 3 failures, 4th attempt succeeds
    df = glucose.pull(flaky, now=NOW, attempts=4, sleep=NOSLEEP)
    assert len(df) == 2
    assert flaky._calls == 4


def test_pull_gives_up_after_attempts(st):
    flaky = glucose._FlakySource(glucose.FixtureSource(_readings([100])), fail_times=10)
    with pytest.raises(glucose.GlucosePullError):
        glucose.pull(flaky, now=NOW, attempts=4, sleep=NOSLEEP)


def test_sync_degrades_to_last_known_good_on_failure(st):
    # seed history + LKG
    glucose.sync(st, glucose.FixtureSource(_readings([100, 110, 140])), now=NOW)
    assert glucose.last_known_good("patient", now=NOW).value == 100.0
    # a failing source must not crash the cycle and must not lose history
    dead = glucose._FlakySource(glucose.FixtureSource(_readings([1])), fail_times=99)
    res = glucose.sync(st, dead, now=NOW, attempts=2, sleep=NOSLEEP)
    assert res.n_stored == 0
    assert len(st.glucose_all()) == 3                 # history intact
    assert glucose.last_known_good("patient", now=NOW).value == 100.0


def test_sync_quarantines_bad_rows(st):
    src = glucose.FixtureSource(_readings([100, 9000, 120]))  # 9000 out of range
    res = glucose.sync(st, src, now=NOW)
    assert res.n_stored == 2 and res.n_quarantined == 1
    assert len(st.quarantined("glucose")) == 1
