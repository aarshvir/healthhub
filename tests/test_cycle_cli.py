"""Tests for cycle.py — the cron entrypoint (demo mode) + leak gate + artifacts."""

import json
from datetime import datetime, timezone

import pytest
from freezegun import freeze_time

import config
import cycle
import integrity

# after the demo fixture's last reading (2026-06-28 Dubai), so nothing is future-dated
NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def clean_env(monkeypatch):
    for k in ("DEXCOM_USERNAME", "DEXCOM_PASSWORD", "NS_URL", "GOOGLE_SA_JSON",
              "HEALTH_LOG_SHEET_ID", "WEARABLES_SHEET_ID", *config.SECRET_KEYS):
        monkeypatch.delenv(k, raising=False)
    integrity.cache_clear()
    yield
    integrity.cache_clear()


def test_demo_cycle_writes_all_artifacts(clean_env, tmp_path):
    with freeze_time(NOW):
        result, mode = cycle.run(out_dir=str(tmp_path), now=NOW)
    assert mode == "demo-fixture"
    for name in ("dashboard.html", "metrics.json", "trend.json", "health.json",
                 "HealthOS_500d.xlsx"):
        assert (tmp_path / name).exists(), name
    assert result.ok, result.self_check_violations
    # dashboard Export button resolves to the co-published workbook
    assert "HealthOS_500d.xlsx" in (tmp_path / "dashboard.html").read_text()


def test_main_writes_run_json_and_returns_code(clean_env, tmp_path, capsys):
    with freeze_time(NOW):
        rc = cycle.main(["--out-dir", str(tmp_path), "--quiet"])
    assert rc in (0, 1)
    run = json.loads((tmp_path / "run.json").read_text())
    assert run["glucose_mode"] == "demo-fixture"
    assert run["presence"]["demo_mode"] is True
    assert (tmp_path / ".nojekyll").exists()


def test_leak_gate_aborts_publish_when_secret_in_artifact(clean_env, tmp_path, monkeypatch):
    # plant a secret value that the deterministic output contains (metrics.json's source label)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "glucose_store")
    with freeze_time(NOW):
        with pytest.raises(RuntimeError, match="leak"):
            cycle.run(out_dir=str(tmp_path), now=NOW)
