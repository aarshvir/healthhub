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
