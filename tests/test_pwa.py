"""Tests for the offline PWA wiring + Apps Script presence (Part 1 artifacts)."""

import json
import pathlib

import build_dashboard

ROOT = pathlib.Path(__file__).resolve().parent.parent
PWA = ROOT / "phone" / "pwa"


def test_manifest_valid_and_points_at_dashboard():
    manifest = json.loads((PWA / "manifest.json").read_text())
    assert manifest["start_url"] == "dashboard.html"
    assert manifest["display"] == "standalone"
    assert manifest["icons"]


def test_service_worker_caches_artifacts():
    sw = (PWA / "service-worker.js").read_text()
    for asset in ("dashboard.html", "metrics.json", "trend.json", "wearables.json"):
        assert asset in sw
    assert "addEventListener('fetch'" in sw
    assert "addEventListener('install'" in sw


def test_dashboard_registers_service_worker_and_links_manifest():
    cockpit = build_dashboard.build_cockpit(metrics={}, trend_by_window={},
                                            latest_glucose=None)
    html = build_dashboard.render(cockpit)
    assert "serviceWorker" in html and "service-worker.js" in html
    assert 'rel="manifest"' in html
    # client-side freshness recompute is present (offline correct "as of" ages)
    assert "hhRefresh" in html and "EXPIRED" in html


def test_apps_script_present_with_schema_headers():
    code = (ROOT / "phone" / "apps_script" / "Code.gs").read_text()
    for col in ("entry_id", "net_carbs_g", "mood_1to5", "symptom_sev_1to5", "tags"):
        assert col in code
    assert "setWebhook" in code  # setup instructions included
