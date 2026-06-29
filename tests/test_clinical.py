"""Tests for clinical.py — hand-checked glucose metrics on known fixtures.

Expected values were computed by hand and independently confirmed with numpy; see the
docstrings. Time is frozen so clinical's routing through integrity.cache_set / freshness
(which read the wall clock) is deterministic.
"""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from freezegun import freeze_time

import clinical
import integrity

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
DUBAI = ZoneInfo("Asia/Dubai")

# Core distribution fixture (mg/dL). Partitions cleanly across every glycemic category.
CORE = [50, 60, 70, 100, 140, 150, 180, 200, 250, 300]


@pytest.fixture(autouse=True)
def _frozen_and_clean():
    with freeze_time(NOW):
        integrity.cache_clear()
        yield
        integrity.cache_clear()


def _store(values, *, subject="p1", start=NOW, step=timedelta(minutes=1)):
    ts = [start - i * step for i in range(len(values))]
    return pd.DataFrame({"subject": subject, "measured_at": ts, "glucose_mgdl": values})


def _val(metrics, name):
    return metrics[name]["value"]


# ---- distribution metrics (order-independent) --------------------------------------
def test_distribution_metrics_match_hand_computed():
    m = clinical.compute_metrics(_store(CORE), now=NOW, walk_adherence=0.8)
    assert _val(m, "n_readings") == 10
    assert _val(m, "mean_mgdl") == pytest.approx(150.0)
    assert _val(m, "gmi_pct") == pytest.approx(6.898)
    assert _val(m, "sd_mgdl") == pytest.approx(83.666003, abs=1e-5)   # sample SD (ddof=1)
    assert _val(m, "cv_pct") == pytest.approx(55.777335, abs=1e-5)
    assert _val(m, "cv_flag") == "high"
    assert _val(m, "tir_pct") == pytest.approx(50.0)
    assert _val(m, "titr_pct") == pytest.approx(30.0)          # the tight-range metric
    assert _val(m, "tbr_pct") == pytest.approx(20.0)
    assert _val(m, "tar_pct") == pytest.approx(30.0)
    assert _val(m, "vlow_pct") == pytest.approx(10.0)
    assert _val(m, "low_pct") == pytest.approx(10.0)
    assert _val(m, "high_pct") == pytest.approx(20.0)
    assert _val(m, "vhigh_pct") == pytest.approx(10.0)


def test_gri_constants_match_klonoff_2022():
    m = clinical.compute_metrics(_store(CORE), now=NOW)
    assert _val(m, "gri_hypo_component") == pytest.approx(18.0)   # VLow + 0.8*Low
    assert _val(m, "gri_hyper_component") == pytest.approx(20.0)  # VHigh + 0.5*High
    assert _val(m, "gri") == pytest.approx(86.0)                  # min(100, 3*18 + 1.6*20)


def test_category_partition_sums_to_100():
    cats = clinical.category_percentages(np.array(CORE, dtype=float))
    assert cats["vlow"] + cats["low"] + cats["tir"] + cats["high"] + cats["vhigh"] == pytest.approx(100.0)
    assert cats["tbr"] == pytest.approx(cats["vlow"] + cats["low"])
    assert cats["tar"] == pytest.approx(cats["high"] + cats["vhigh"])


def test_gmi_formula():
    assert clinical.gmi(150.0) == pytest.approx(6.898)
    assert clinical.gmi(100.0) == pytest.approx(3.31 + 0.02392 * 100)


# ---- MAGE (Service 1970 / Baghurst 2011) -------------------------------------------
def test_mage_alternating_series():
    # every peak-nadir swing is 100 mg/dL, all > 1 SD (54.77) -> MAGE = 100
    assert clinical.mage(np.array([100, 200, 100, 200, 100.0])) == pytest.approx(100.0)


def test_mage_monotonic_single_excursion():
    # SD = 12.91; the single 30 mg/dL rise exceeds 1 SD -> MAGE = 30
    assert clinical.mage(np.array([100, 110, 120, 130.0])) == pytest.approx(30.0)


def test_mage_flat_is_zero():
    assert clinical.mage(np.array([120, 120, 120, 120.0])) == 0.0


def test_mage_plateau_excursion_not_missed():
    # flat-topped excursions (common in CGM) must still be detected
    # [100,150,150,150,100]: SD=35.36; excursions 100->150 and 150->100 (amp 50) -> MAGE 50
    assert clinical.mage(np.array([100, 150, 150, 150, 100.0])) == pytest.approx(50.0)
    # [80,200,200,80]: SD=69.28; single excursion amp 120 -> MAGE 120
    assert clinical.mage(np.array([80, 200, 200, 80.0])) == pytest.approx(120.0)


def test_mage_too_short_is_nan():
    assert np.isnan(clinical.mage(np.array([120, 130.0])))


# ---- spike count -------------------------------------------------------------------
def test_spike_count_counts_upward_crossings():
    # 100->190 (cross), 190->100, 100->200 (cross), 200->100  => 2 spikes
    assert clinical.spike_count(np.array([100, 190, 100, 200, 100.0])) == 2


def test_spike_count_none():
    assert clinical.spike_count(np.array([100, 120, 150, 170.0])) == 0


# ---- metabolic score (transparent, reproducible) -----------------------------------
def test_metabolic_score_hand_checked():
    # 0.40*70 + 0.25*50 + 0.20*80 + 0.15*80 = 28 + 12.5 + 16 + 12 = 68.5
    assert clinical.metabolic_score(70, 45, 2, 0.8) == pytest.approx(68.5)


def test_metabolic_score_clipping():
    # perfect: titr 100, cv 30 (->100), 0 spikes (->100), full adherence -> 100
    assert clinical.metabolic_score(100, 30, 0, 1.0) == pytest.approx(100.0)
    # worst: titr 0, cv 60 (->0), 10 spikes (->0), no adherence -> 0
    assert clinical.metabolic_score(0, 60, 10, 0.0) == pytest.approx(0.0)


def test_metabolic_score_in_metrics_when_adherence_given():
    m = clinical.compute_metrics(_store(CORE), now=NOW, walk_adherence=0.8)
    assert m["daily_metabolic_score"]["value"] is not None
    m2 = clinical.compute_metrics(_store(CORE), now=NOW)  # no adherence
    assert m2["daily_metabolic_score"]["value"] is None
    assert m2["daily_metabolic_score"]["valid"] is False


# ---- dawn phenomenon (Asia/Dubai) --------------------------------------------------
def test_dawn_delta():
    # night [00:00,04:00) mean 100 ; dawn [04:00,08:00) mean 130 -> +30, Asia/Dubai
    rows = []
    for hour, val in [(1, 90), (2, 100), (3, 110), (5, 120), (6, 130), (7, 140)]:
        rows.append({
            "subject": "p1",
            "measured_at": datetime(2026, 6, 28, hour, 0, tzinfo=DUBAI),
            "glucose_mgdl": val,
        })
    m = clinical.compute_metrics(pd.DataFrame(rows), now=NOW)
    assert _val(m, "dawn_delta_mgdl") == pytest.approx(30.0)


# ---- AGP percentile bands ----------------------------------------------------------
def test_agp_slot_percentiles():
    # 5 readings all at 12:00 Asia/Dubai (slot 48) across 5 days: [100,110,120,130,140]
    rows = []
    for day, val in zip(range(24, 29), [100, 110, 120, 130, 140]):
        rows.append({
            "subject": "p1",
            "measured_at": datetime(2026, 6, day, 12, 0, tzinfo=DUBAI),
            "glucose_mgdl": val,
        })
    m = clinical.compute_metrics(pd.DataFrame(rows), now=NOW)
    bands = _val(m, "agp_percentiles")
    assert len(bands) == 96
    assert bands[48] == {"p10": 104.0, "p25": 110.0, "p50": 120.0, "p75": 130.0, "p90": 136.0}
    # exactly one slot populated
    assert sum(b is not None for b in bands) == 1


# ---- routing through integrity: validation + freshness + cache ---------------------
def test_out_of_range_reading_makes_metrics_invalid():
    # 2000 mg/dL is beyond integrity's physiological range (20-800) -> dropped, run untrusted
    store = _store([100, 110, 2000, 120, 130])
    m = clinical.compute_metrics(store, now=NOW)
    assert _val(m, "n_readings") == 4                       # bad row dropped by integrity
    assert m["mean_mgdl"]["valid"] is False                 # but run flagged untrusted
    assert _val(m, "mean_mgdl") is not None                 # value still computed from good rows


def test_window_selects_subset():
    # 10 readings at NOW, NOW-1m, ... NOW-9m; a 5-minute window keeps the 5 most recent
    m = clinical.compute_metrics(_store(CORE), now=NOW,
                                 window=(NOW - timedelta(minutes=4), NOW))
    assert _val(m, "n_readings") == 5


def test_expired_data_makes_metrics_invalid():
    old = NOW - timedelta(days=10)
    m = clinical.compute_metrics(_store(CORE, start=old), now=NOW)
    assert m["mean_mgdl"]["valid"] is False                 # freshness gate via integrity
    assert _val(m, "mean_mgdl") == pytest.approx(150.0)


def test_empty_store_falls_back_to_last_known_good_cache():
    # first run caches the most-recent good glucose as LKG (newest timestamp -> 140)...
    ts = [NOW - timedelta(minutes=2), NOW - timedelta(minutes=1), NOW]
    ordered = pd.DataFrame({"subject": "p1", "measured_at": ts, "glucose_mgdl": [120, 130, 140]})
    clinical.compute_metrics(ordered, now=NOW)
    assert integrity.cache_get_value("glucose_mgdl", "p1", now=NOW) == 140.0
    # ...an empty store yields no metrics but does not crash and surfaces the LKG ts
    empty = pd.DataFrame({"subject": [], "measured_at": pd.Series([], dtype="datetime64[ns, UTC]"),
                          "glucose_mgdl": []})
    m = clinical.compute_metrics(empty, subject="p1", now=NOW)
    assert _val(m, "n_readings") == 0
    assert m["mean_mgdl"]["value"] is None
    assert m["mean_mgdl"]["ts"] is not None                 # last-known-good timestamp


# ---- output schema + serialization -------------------------------------------------
def test_every_metric_has_value_source_ts_valid():
    m = clinical.compute_metrics(_store(CORE), now=NOW, walk_adherence=0.8)
    for name, entry in m.items():
        assert set(entry.keys()) == {"value", "source", "ts", "valid"}, name
        assert isinstance(entry["source"], str)
        assert isinstance(entry["valid"], bool)


def test_write_metrics_json_roundtrip(tmp_path):
    m = clinical.compute_metrics(_store(CORE), now=NOW, walk_adherence=0.8)
    path = tmp_path / "metrics.json"
    clinical.write_metrics(m, str(path))
    loaded = json.loads(path.read_text())
    assert loaded["gri"]["value"] == pytest.approx(86.0)
    assert loaded["titr_pct"]["value"] == pytest.approx(30.0)
    assert set(loaded["mean_mgdl"].keys()) == {"value", "source", "ts", "valid"}
