"""§E #2 determinism + #11 cycle-level idempotency."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from freezegun import freeze_time

import engine
import glucose
import integrity
import store as store_mod

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
CORE = [50, 60, 70, 100, 140, 150, 180, 200, 250, 300]


def _readings():
    return [{"measured_at": NOW - timedelta(minutes=i), "glucose_mgdl": v}
            for i, v in enumerate(CORE)]


def _run(out_dir):
    integrity.cache_clear()
    s = store_mod.Store(":memory:")
    res = engine.run_cycle(store=s, glucose_source=glucose.FixtureSource(_readings()),
                           now=NOW, window_days=7, walk_adherence=0.8, out_dir=str(out_dir),
                           analyses_path=None)
    s.close()
    integrity.cache_clear()
    return res


def test_metrics_json_byte_identical_on_repeat(tmp_path):
    with freeze_time(NOW):
        _run(tmp_path / "a")
        _run(tmp_path / "b")
    a = (tmp_path / "a" / "metrics.json").read_text()
    b = (tmp_path / "b" / "metrics.json").read_text()
    assert a == b, "metrics.json must be byte-identical for identical input"


def test_trend_json_identical_on_repeat(tmp_path):
    with freeze_time(NOW):
        _run(tmp_path / "a")
        _run(tmp_path / "b")
    assert (tmp_path / "a" / "trend.json").read_text() == (tmp_path / "b" / "trend.json").read_text()


def test_cycle_run_twice_does_not_duplicate_store_rows(tmp_path):
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        src = glucose.FixtureSource(_readings())
        r1 = engine.run_cycle(store=s, glucose_source=src, now=NOW, window_days=7,
                              out_dir=str(tmp_path), analyses_path=None)
        n1 = len(s.glucose_all())
        r2 = engine.run_cycle(store=s, glucose_source=src, now=NOW, window_days=7,
                              out_dir=str(tmp_path), analyses_path=None)
        n2 = len(s.glucose_all())
        assert n1 == n2 == 10                       # idempotent: re-running never duplicates
        assert r2.n_stored == 10                    # upserts, not inserts
        assert r1.metrics["mean_mgdl"]["value"] == pytest.approx(
            r2.metrics["mean_mgdl"]["value"])
        s.close()
        integrity.cache_clear()
