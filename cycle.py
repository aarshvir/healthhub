"""cycle.py — the always-on cron entrypoint (console script ``healthhub-cycle``).

Builds the live sources from environment config (Nightscout > Dexcom > a committed demo
fixture when no secrets are set), runs one full engine cycle (dashboard + Excel + heartbeat +
health.json), then runs the secret leak-gate over every published artifact (§A: nothing
sensitive in the repo or the rendered output). Designed to be driven by a */15 GitHub Action
that deploys ``publish/`` to GitHub Pages behind an access gate (see docs/CLOUD_SETUP.md).
"""

from __future__ import annotations

import argparse
import json
import os

import config
import engine
import glucose as glucose_mod
import integrity
import journal
import store as store_mod
import wearables

DEMO_GLUCOSE_CSV = os.path.join(os.path.dirname(__file__), "examples", "glucose_sample.csv")


def build_glucose_source(*, now=None):
    """Nightscout > Dexcom > demo fixture. Returns (source, mode)."""
    if config.nightscout_configured():
        return glucose_mod.NightscoutSource(config.get("NS_URL"), token=config.get("NS_TOKEN")), "nightscout"
    if config.dexcom_configured():
        return (glucose_mod.PydexcomSource(config.get("DEXCOM_USERNAME"),
                                           config.get("DEXCOM_PASSWORD"),
                                           region=config.get("DEXCOM_REGION", "ous")), "dexcom")
    if os.path.exists(DEMO_GLUCOSE_CSV):
        import pandas as pd
        df = pd.read_csv(DEMO_GLUCOSE_CSV)
        df["measured_at"] = pd.to_datetime(df["measured_at"], utc=False)
        return glucose_mod.FixtureSource(df.to_dict("records")), "demo-fixture"
    return None, "none"


def build_log_source():
    if config.sheets_configured() and config.is_set("HEALTH_LOG_SHEET_ID"):
        return journal.GSheetLogSource(config.get("HEALTH_LOG_SHEET_ID"),
                                       service_account_path=config.google_sa_path())
    return None


def build_wearables_source():
    if config.sheets_configured() and config.is_set("WEARABLES_SHEET_ID"):
        return wearables.GSheetWearablesSource(config.get("WEARABLES_SHEET_ID"))
    return None


def run(*, out_dir: str = "publish", db_path: str | None = None, window_days: int = 14,
        subject: str = "patient", walk_adherence: float | None = None, now=None):
    """Run one cycle and leak-scan the output. Returns (CycleResult, mode)."""
    now = integrity.now_utc() if now is None else now
    st = store_mod.Store(db_path or ":memory:")
    gsrc, mode = build_glucose_source(now=now)
    result = engine.run_cycle(
        store=st, glucose_source=gsrc, log_source=build_log_source(),
        wearables_source=build_wearables_source(), now=now, window_days=window_days,
        subject=subject, walk_adherence=walk_adherence, out_dir=out_dir,
        dashboard=True, excel=True)
    # leak gate: refuse to publish if any secret value reached an artifact (§A)
    config.scan_paths(list(result.artifacts.values()), where="publish")
    return result, mode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="healthhub-cycle", description=__doc__)
    p.add_argument("--out-dir", default="publish")
    p.add_argument("--db", default=None, help="persistent sqlite store path (default in-memory)")
    p.add_argument("--window-days", type=int, default=14)
    p.add_argument("--subject", default="patient")
    p.add_argument("--walk-adherence", type=float, default=None)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    presence = config.presence_report()
    if not args.quiet:
        print("config:", json.dumps(presence))

    result, mode = run(out_dir=args.out_dir, db_path=args.db, window_days=args.window_days,
                       subject=args.subject, walk_adherence=args.walk_adherence)

    os.makedirs(args.out_dir, exist_ok=True)
    summary = {"generated_at": result.generated_at, "glucose_mode": mode,
               "n_stored": result.n_stored, "n_quarantined": result.n_quarantined,
               "ok": result.ok, "alerts": result.heartbeat.get("alerted", []),
               "self_check_violations": result.self_check_violations, "presence": presence}
    run_json = os.path.join(args.out_dir, "run.json")
    with open(run_json, "w", encoding="utf-8") as fh:
        fh.write(config.assert_no_secrets(json.dumps(summary, indent=2, default=str)))
    open(os.path.join(args.out_dir, ".nojekyll"), "w").close()  # Pages serves _-prefixed files

    if not args.quiet:
        print(f"cycle ok={result.ok} mode={mode} stored={result.n_stored} "
              f"quarantined={result.n_quarantined} -> {args.out_dir}/")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
