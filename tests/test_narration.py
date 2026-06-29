"""Tests for narration.py — patient summaries grounded through integrity."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from freezegun import freeze_time

import clinical
import integrity
import narration

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
CORE = [50, 60, 70, 100, 140, 150, 180, 200, 250, 300]


@pytest.fixture()
def metrics():
    with freeze_time(NOW):
        integrity.cache_clear()
        ts = [NOW - timedelta(minutes=i) for i in range(len(CORE))]
        store = pd.DataFrame({"subject": "p1", "measured_at": ts, "glucose_mgdl": CORE})
        yield clinical.compute_metrics(store, now=NOW, walk_adherence=0.8)
        integrity.cache_clear()


def test_template_narration_is_grounded(metrics):
    # render() is grounded by construction; narrate() re-verifies through the guard
    text = narration.narrate(metrics)
    assert "150" in text and "86" in text and "30%" in text


def test_grounded_numbers_include_metric_values(metrics):
    nums = narration.grounded_numbers(metrics)
    assert 150.0 in nums and 86.0 in nums and 6.898 in nums


def test_llm_invented_number_is_blocked(metrics):
    with pytest.raises(integrity.NarrationIntegrityError) as exc:
        narration.verify_llm_narration(
            metrics, "Your average glucose was 150 mg/dL but it spiked to 420 mg/dL today."
        )
    assert exc.value.token == "420"


def test_llm_grounded_narration_passes(metrics):
    text = "Mean glucose 150 mg/dL with GRI 86 and time in range 50%."
    assert narration.verify_llm_narration(metrics, text) == text


def test_llm_honest_rounding_passes(metrics):
    # GMI exact is 6.898; the LLM writing 6.9% is honest display rounding -> allowed
    assert narration.verify_llm_narration(metrics, "GMI was about 6.9%.") == "GMI was about 6.9%."


def test_llm_redaction_mode(metrics):
    out = narration.verify_llm_narration(
        metrics, "Mean 150 mg/dL, but a reading hit 999.", redact=True
    )
    assert "[UNVERIFIED]" in out and "999" not in out
