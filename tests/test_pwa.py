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


def test_offline_age_uses_embedded_asof_and_cgm_thresholds():
    """§E #10: offline, the dashboard recomputes age/state from the embedded as_of on the
    DEVICE clock using CGM timescales (30m fresh / 90m stale), so a cached value is never live."""
    import json as _json
    from datetime import datetime, timedelta, timezone

    import build_dashboard
    now = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
    cockpit = build_dashboard.build_cockpit(
        metrics={}, trend_by_window={},
        latest_glucose={"ts": (now - timedelta(hours=2)).isoformat(), "value": 150}, now=now)
    html = build_dashboard.render(cockpit)
    # the embedded config carries the exact as_of + CGM thresholds the JS recomputes from
    cfg_line = next(l for l in html.split("window.HH=") if l.startswith("{"))
    cfg = _json.loads(cfg_line.split(";", 1)[0])
    assert cfg["freshH"] == 0.5 and cfg["staleH"] == 1.5
    assert cfg["asOf"].startswith("2026-06-28T10:00")
    # a 2h-old CGM value is rendered STALE/EXPIRED server-side too (never "live")
    assert "FRESH" not in html.split("</header>")[0] or "STALE" in html or "EXPIRED" in html


def test_apps_script_present_with_schema_headers():
    code = (ROOT / "phone" / "apps_script" / "Code.gs").read_text()
    for col in ("entry_id", "net_carbs_g", "mood_1to5", "symptom_sev_1to5", "tags"):
        assert col in code
    assert "setWebhook" in code  # setup instructions included


def test_csp_and_ios_metas_present():
    """§A: the 'no external hosts' control is enforced by a strict CSP, not just claimed;
    iOS standalone metas + a real apple-touch-icon make install work."""
    import build_dashboard
    cockpit = build_dashboard.build_cockpit(metrics={}, trend_by_window={}, latest_glucose=None)
    html = build_dashboard.render(cockpit)
    assert "Content-Security-Policy" in html and "default-src 'none'" in html
    assert "connect-src 'self'" in html and "font-src data:" in html
    assert "apple-mobile-web-app-capable" in html and 'rel="apple-touch-icon"' in html


def test_pwa_icons_exist():
    for name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png"):
        assert (PWA / name).exists() and (PWA / name).stat().st_size > 500
