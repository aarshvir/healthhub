"""heartbeat.py — pipeline liveness per source + alerting (Part 3.4, §A rule 6).

Each feed has a freshness threshold. The heartbeat computes, per source, the age of its most
recent successfully-stored datum and a state (FRESH / STALE / DOWN / NO_DATA) via
``integrity.freshness``. A stale-or-worse feed:
  * is flagged on the dashboard with its true age (never shown as live),
  * is written to ``health.json`` (a machine-readable health endpoint),
  * triggers an alert through an injectable alerter (Telegram / email in production,
    a recording no-op in tests).

The clock is read only via ``integrity.now_utc``.
"""

from __future__ import annotations

import json
from datetime import timedelta

import config
import integrity

# Per-source freshness thresholds (a feed older than this is "stale").
SOURCE_THRESHOLDS = {
    "glucose": timedelta(minutes=30),
    "wearables": timedelta(hours=6),
    "log": timedelta(hours=36),
    "labs": timedelta(days=180),
    "supplement": timedelta(hours=36),
}
DEFAULT_THRESHOLD = timedelta(minutes=30)
# Always evaluated even with no data (a missing glucose feed is itself an incident).
REQUIRED_SOURCES = ("glucose",)


def _age_str(age: timedelta) -> str:
    secs = abs(age.total_seconds())
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def status(last_success: dict, *, now=None, thresholds: dict | None = None) -> dict:
    """Compute per-source liveness from a {source: last_success_ts} map."""
    now = integrity.now_utc() if now is None else now
    thresholds = thresholds or SOURCE_THRESHOLDS
    sources = []
    overall_ok = True
    # evaluate sources that have data (or are required); never spam unconfigured streams
    to_check = sorted(set(last_success) | set(REQUIRED_SOURCES))
    for source in to_check:
        thr = thresholds.get(source, DEFAULT_THRESHOLD)
        ts = last_success.get(source)
        if not ts:
            sources.append({"source": source, "state": "no_data", "stale": True,
                            "last_success": None, "age": None,
                            "threshold_min": round(thr.total_seconds() / 60)})
            overall_ok = False
            continue
        fr = integrity.freshness(ts, now=now)
        age = fr.age
        if fr.state is integrity.FreshnessState.FUTURE:
            state, stale = "future", True
        elif age <= thr:
            state, stale = "fresh", False
        elif age <= 3 * thr:
            state, stale = "stale", True
        else:
            state, stale = "down", True
        overall_ok = overall_ok and not stale
        sources.append({"source": source, "state": state, "stale": stale,
                        "last_success": fr.measured_at.isoformat(), "age": _age_str(age),
                        "age_seconds": round(age.total_seconds()),
                        "threshold_min": round(thr.total_seconds() / 60)})
    return {"generated_at": now.isoformat(), "overall_ok": overall_ok, "sources": sources}


def write_health(status_obj: dict, path: str = "health.json") -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(status_obj, fh, indent=2, sort_keys=True)
    return path


# ======================================================================================
# Alerters (injectable; production wires Telegram + email from config)
# ======================================================================================
class NoopAlerter:
    """Records alerts instead of sending (default / tests)."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, subject: str, body: str) -> None:
        self.sent.append((subject, body))


class TelegramAlerter:
    def send(self, subject: str, body: str) -> None:
        if not config.telegram_configured():
            return
        import requests  # lazy
        requests.post(
            f"https://api.telegram.org/bot{config.get('TELEGRAM_TOKEN')}/sendMessage",
            json={"chat_id": config.get("TELEGRAM_CHAT_ID"), "text": f"{subject}\n{body}"},
            timeout=15)


class EmailAlerter:
    def send(self, subject: str, body: str) -> None:
        if not config.email_configured():
            return
        import smtplib  # lazy
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"], msg["To"] = subject, config.get("ALERT_EMAIL_TO")
        msg["From"] = config.get("SMTP_USER") or config.get("ALERT_EMAIL_TO")
        msg.set_content(body)
        with smtplib.SMTP(config.get("SMTP_HOST"), int(config.get("SMTP_PORT", "587"))) as s:
            s.starttls()
            if config.is_set("SMTP_USER"):
                s.login(config.get("SMTP_USER"), config.get("SMTP_PASSWORD"))
            s.send_message(msg)


class MultiAlerter:
    def __init__(self, alerters):
        self.alerters = list(alerters)

    def send(self, subject: str, body: str) -> None:
        for a in self.alerters:
            try:
                a.send(subject, body)
            except Exception:  # noqa: BLE001 - an alert channel failing must not crash the cycle
                pass


def default_alerter():
    """Compose alerters from whatever is configured; Noop if nothing is."""
    channels = []
    if config.telegram_configured():
        channels.append(TelegramAlerter())
    if config.email_configured():
        channels.append(EmailAlerter())
    return MultiAlerter(channels) if channels else NoopAlerter()


def alert_for_stale(status_obj: dict, alerter) -> list[str]:
    """Send one alert per stale/down/no_data feed; returns the source names alerted."""
    alerted = []
    for s in status_obj.get("sources", []):
        if s["stale"]:
            age = s["age"] or "never"
            alerter.send(f"⚠️ HealthHub feed stale: {s['source']}",
                         f"{s['source']} is {s['state']} (age {age}; "
                         f"threshold {s['threshold_min']}m). Dashboard is showing "
                         f"last-known-good, not live.")
            alerted.append(s["source"])
    return alerted


def check(last_success: dict, *, now=None, alerter=None, health_path: str | None = None) -> dict:
    """Compute status, optionally write health.json, and fire alerts. Returns status."""
    now = integrity.now_utc() if now is None else now
    st = status(last_success, now=now)
    if health_path:
        write_health(st, health_path)
    if alerter is not None:
        st["alerted"] = alert_for_stale(st, alerter)
    return st


def self_check(status_obj: dict) -> list[str]:
    violations = []
    for s in status_obj.get("sources", []):
        if s["state"] == "fresh" and s["stale"]:
            violations.append(f"{s['source']}: fresh but marked stale")
        if s.get("age_seconds") is not None and s["age_seconds"] < -300:
            violations.append(f"{s['source']}: negative age (future) without future state")
    return violations
