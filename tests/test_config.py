"""Tests for config.py — secrets from env only + leak scanning."""

import pytest

import config


@pytest.fixture()
def env(monkeypatch):
    for k in list(config.SECRET_KEYS) + ["DEXCOM_USERNAME", "NS_URL", "TELEGRAM_CHAT_ID",
                                         "SMTP_HOST", "ALERT_EMAIL_TO"]:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_defaults_and_get(env):
    assert config.get("DEXCOM_REGION") == "ous"
    assert config.get("MISSING", "fallback") == "fallback"
    with pytest.raises(RuntimeError):
        config.get("ANTHROPIC_API_KEY", required=True)


def test_source_configured_flags(env):
    assert config.dexcom_configured() is False
    env.setenv("DEXCOM_USERNAME", "u")
    env.setenv("DEXCOM_PASSWORD", "p-secret-123")
    assert config.dexcom_configured() is True
    env.setenv("TELEGRAM_TOKEN", "tok-secret-123")
    env.setenv("TELEGRAM_CHAT_ID", "42")
    assert config.telegram_configured() is True


def test_leak_scan_blocks_secret_in_output(env):
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecretvalue")
    html = "<div>dashboard with sk-ant-supersecretvalue embedded</div>"
    with pytest.raises(RuntimeError):
        config.assert_no_secrets(html, where="dashboard.html")
    assert "[REDACTED]" in config.redact(html)
    # clean output passes
    assert config.assert_no_secrets("<div>no secrets here</div>") is not None


def test_secret_values_excludes_unset(env):
    assert config.secret_values() == []
    env.setenv("TELEGRAM_TOKEN", "abcd1234")
    assert "abcd1234" in config.secret_values()


def test_leak_gate_scans_inside_xlsx(env, tmp_path):
    import zipfile
    env.setenv("NS_TOKEN", "super-secret-token-1234")
    # a zip (like the shipped .xlsx) hiding the secret in one entry must NOT slip past
    xlsx = tmp_path / "book.xlsx"
    with zipfile.ZipFile(xlsx, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", "<c>super-secret-token-1234</c>")
    with pytest.raises(RuntimeError):
        config.scan_paths([str(xlsx)], where="publish")


def test_leak_gate_catches_service_account_subfield(env, tmp_path):
    import json
    env.setenv("GOOGLE_SA_JSON", json.dumps({
        "private_key": "-----BEGIN PRIVATE KEY-----AAAABBBBCCCC-----END-----",
        "client_email": "robot@proj.iam.gserviceaccount.com"}))
    f = tmp_path / "leak.json"
    f.write_text('{"who":"robot@proj.iam.gserviceaccount.com"}')   # sub-field, not whole blob
    with pytest.raises(RuntimeError):
        config.scan_paths([str(f)])


def test_leak_gate_passes_clean_files(env, tmp_path):
    env.setenv("NS_TOKEN", "super-secret-token-1234")
    f = tmp_path / "ok.json"
    f.write_text('{"glucose": 120, "note": "nothing secret here"}')
    assert config.scan_paths([str(f)]) == [str(f)]
