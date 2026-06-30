"""config.py — all secrets/settings come from the environment, never the repo (§A golden rule).

Nothing sensitive is hard-coded or committed. Modules read credentials through here; a
leak-scan helper asserts no secret value ever appears in rendered HTML/JSON before publish.

Recognized environment variables (see .env.example):
  DEXCOM_USERNAME, DEXCOM_PASSWORD, DEXCOM_REGION(=ous)
  NS_URL, NS_TOKEN, NS_API_SECRET
  GOOGLE_SA_JSON (service-account JSON or a path to it)
  HEALTH_LOG_SHEET_ID, WEARABLES_SHEET_ID
  ANTHROPIC_API_KEY, MODEL_FAST, MODEL_DEEP
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
  ALERT_EMAIL_TO, SMTP_HOST, SMTP_USER, SMTP_PASSWORD
"""

from __future__ import annotations

import os

# Names whose VALUES are secret (used by the leak scan / redaction). Non-secret settings
# (regions, model names, sheet ids, hostnames) are intentionally excluded.
SECRET_KEYS = (
    "DEXCOM_PASSWORD", "NS_TOKEN", "NS_API_SECRET", "GOOGLE_SA_JSON",
    "ANTHROPIC_API_KEY", "TELEGRAM_TOKEN", "SMTP_PASSWORD", "OURA_TOKEN",
)
DEFAULTS = {"DEXCOM_REGION": "ous", "MODEL_FAST": "claude-haiku-4-5",
            "MODEL_DEEP": "claude-opus-4-8"}


def get(name: str, default=None, *, required: bool = False) -> str | None:
    val = os.environ.get(name, DEFAULTS.get(name, default))
    if required and not val:
        raise RuntimeError(f"missing required configuration: {name}")
    return val


def is_set(name: str) -> bool:
    return bool(os.environ.get(name))


# ---- source-configured checks (which live feeds are wired) -------------------------
def dexcom_configured() -> bool:
    return is_set("DEXCOM_USERNAME") and is_set("DEXCOM_PASSWORD")


def nightscout_configured() -> bool:
    return is_set("NS_URL")


def sheets_configured() -> bool:
    return is_set("GOOGLE_SA_JSON")


def telegram_configured() -> bool:
    return is_set("TELEGRAM_TOKEN") and is_set("TELEGRAM_CHAT_ID")


def email_configured() -> bool:
    return is_set("SMTP_HOST") and is_set("ALERT_EMAIL_TO")


def oura_configured() -> bool:
    return is_set("OURA_TOKEN")


def google_sa_path() -> str | None:
    """Path to the Google service-account JSON. Inline JSON is materialized to a 0600 temp file."""
    val = os.environ.get("GOOGLE_SA_JSON")
    if not val:
        return None
    if val.lstrip().startswith("{"):
        import json
        import tempfile
        json.loads(val)  # validate
        path = os.path.join(tempfile.gettempdir(), "healthhub-sa.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(val)
        os.chmod(path, 0o600)
        return path
    return val


def presence_report() -> dict:
    """Which integrations are wired (booleans only — no values). demo_mode = no live glucose."""
    dex, ns = dexcom_configured(), nightscout_configured()
    return {"dexcom": dex, "nightscout": ns, "sheets": sheets_configured(),
            "telegram": telegram_configured(), "email": email_configured(),
            "anthropic": is_set("ANTHROPIC_API_KEY"), "oura": oura_configured(),
            "demo_mode": not (dex or ns)}


# ---- secret hygiene ----------------------------------------------------------------
def secret_values() -> list[str]:
    """Every currently-set secret value (longer than a trivial length) for leak scanning."""
    out = []
    for k in SECRET_KEYS:
        v = os.environ.get(k)
        if v and len(v) >= 4:
            out.append(v)
    return out


def redact(text: str) -> str:
    """Replace any set secret value in *text* with [REDACTED]."""
    for v in secret_values():
        text = text.replace(v, "[REDACTED]")
    return text


def assert_no_secrets(text: str, *, where: str = "output") -> str:
    """Raise if any secret value appears in *text* (call before publishing HTML/JSON)."""
    leaked = [k for k in SECRET_KEYS
              if (os.environ.get(k) or "") and len(os.environ[k]) >= 4
              and os.environ[k] in text]
    if leaked:
        raise RuntimeError(f"secret(s) {leaked} would leak into {where}; aborting publish")
    return text


def scan_paths(paths, *, where: str = "publish") -> list:
    """Leak gate: read each existing file and abort if any secret value appears (§A)."""
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
            continue
        assert_no_secrets(text, where=f"{where}:{path}")
    return list(paths)
