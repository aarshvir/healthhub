"""engine.py — run one full analysis cycle, all routed through integrity.

A cycle: pull glucose → prune to 500 days → compute §B clinical metrics for the window →
compute the analytics trend + AGP → run in-cycle self-checks (store invariants, trend
invariants, metric sanity) → write metrics.json + trend.json. Corrupted rows are quarantined
by the store and never reach the metrics or the plots.
"""

from __future__ import annotations

import dataclasses
import json
import os

import numpy as np
import pandas as pd

import analytics
import assess as assess_mod
import build_dashboard
import clinical
import coach as coach_mod
import correlate as correlate_mod
import custom as custom_mod
import daily as daily_mod
import experiments as experiments_mod
import export_excel
import food_impact
import glucose as glucose_mod
import heartbeat as heartbeat_mod
import insights as insights_mod
import integrity
import journal
import labs as labs_mod
import mood_energy
import patterns as patterns_mod
import reversal as reversal_mod
import streaks as streaks_mod
import symptoms
import wearables as wearables_mod


@dataclasses.dataclass(frozen=True, slots=True)
class CycleResult:
    generated_at: str
    n_stored: int
    n_quarantined: int
    metrics: dict
    trend: dict
    quarantined: list
    self_check_violations: list
    assessment: dict = dataclasses.field(default_factory=dict)
    insights_text: str = ""
    food_ranking: list = dataclasses.field(default_factory=list)
    experiments: list = dataclasses.field(default_factory=list)
    custom_cards: list = dataclasses.field(default_factory=list)
    heartbeat: dict = dataclasses.field(default_factory=dict)
    correlation: dict = dataclasses.field(default_factory=dict)
    daily_frame: list = dataclasses.field(default_factory=list)
    review_text: str = ""
    labs: dict = dataclasses.field(default_factory=dict)
    reversal: dict = dataclasses.field(default_factory=dict)
    streaks: list = dataclasses.field(default_factory=list)
    patterns: dict = dataclasses.field(default_factory=dict)
    coach: list = dataclasses.field(default_factory=list)
    artifacts: dict = dataclasses.field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.self_check_violations


def _metrics_self_check(metrics: dict) -> list[str]:
    """Invariants on the clinical metrics: % partition sane, ts not in the future."""
    violations: list[str] = []
    tir = metrics.get("tir_pct", {}).get("value")
    tbr = metrics.get("tbr_pct", {}).get("value")
    tar = metrics.get("tar_pct", {}).get("value")
    if None not in (tir, tbr, tar):
        total = tir + tbr + tar
        if not (99.0 <= total <= 101.0):
            violations.append(f"metrics: TIR+TBR+TAR={total:.1f} (should be ~100)")
    for name, entry in metrics.items():
        v = entry.get("value")
        if isinstance(v, float) and (np.isinf(v) or np.isnan(v)):
            violations.append(f"metrics: {name} is non-finite")
    return violations


def _write_json(obj, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, default=str)


def run_cycle(*, store, glucose_source=None, log_source=None, wearables_source=None,
              labs_source=None,
              now=None, window_days: int = 14, subject: str = "patient", subject_age=None,
              walk_adherence: float | None = None, out_dir: str = ".",
              prune_retention: int = 500, dashboard: bool = False, excel: bool = False,
              dashboard_windows=analytics.STANDARD_WINDOWS,
              analyses_path: str | None = "analyses.json", alerter=None,
              sleep=None) -> CycleResult:
    """Run a full cycle against *store* and write the artifacts to *out_dir*.

    Always: pull glucose -> prune -> §B metrics -> trend -> self-checks -> metrics.json + trend.json.
    Optionally: ingest the journal log (-> food ranking, experiments, insights), build
    wearables.json, and render dashboard.html (last-known-good with age, quarantine excluded).
    """
    now = integrity.now_utc() if now is None else now
    artifacts: dict = {}

    n_stored = 0
    if glucose_source is not None:
        n_stored = glucose_mod.sync(store, glucose_source, now=now, subject=subject,
                                    sleep=sleep).n_stored
    if log_source is not None:
        journal.ingest(store, log_source, now=now)
        # journal-glucose bridge: CGM checks logged in the journal (glucose_mgdl column) are
        # real readings — merge them into the glucose store (idempotent; store dedups by ts).
        # Sparse but real: keeps the dashboard on YOUR data even before a 5-min feed is wired.
        g_rows = [{"measured_at": r["measured_at"], "glucose_mgdl": r["glucose_mgdl"]}
                  for r in journal.records(store) if r.get("glucose_mgdl") is not None]
        if g_rows:
            n_stored += store.append_glucose(pd.DataFrame(g_rows), source="journal-cgm-check",
                                             subject=subject, now=now).n_stored
    if labs_source is not None:
        labs_mod.ingest(store, labs_source, now=now)
    store.prune(now=now, retention_days=prune_retention)

    # Wearables + the unified daily frame are built FIRST so downstream stages can use them:
    # the daily frame supplies the post-meal-walk adherence that powers the metabolic score,
    # and the wearables feed the cross-stream correlation.
    violations: list[str] = []
    wears = None
    if wearables_source is not None:
        wears = wearables_mod.build(wearables_source, now=now)
        violations += wearables_mod.self_check(wears, now=now)

    corr_window = max(90, window_days)  # widen so associations have enough overlapping days
    daily_frame = daily_mod.build(store, window_days=corr_window, now=now, wearables=wears)
    correlation = correlate_mod.analyze(daily_frame)
    review_text = correlate_mod.narrate(correlation["findings"], redact=True)
    violations += daily_mod.self_check(daily_frame) + correlate_mod.self_check(correlation)

    # derive post-meal-walk adherence from the recent daily frame (unless caller supplied it),
    # so the Daily Metabolic Score is populated in the live cycle instead of silently dropping.
    if walk_adherence is None:
        recent_walk = [r["walk"] for r in daily_frame if r.get("walk") is not None][-window_days:]
        walk_adherence = (sum(recent_walk) / len(recent_walk)) if recent_walk else None

    window_df = store.glucose_last_days(window_days, now=now)
    metrics = clinical.compute_metrics(window_df, now=now, subject=subject,
                                       walk_adherence=walk_adherence)
    trend = analytics.compute(store, window_days=window_days, now=now)
    violations += (store.self_check(now=now) + analytics.self_check(trend)
                   + _metrics_self_check(metrics))

    # labs (liver/inflammation/hormones/…) + the diabetes-reversal view (GMI ladder, 90-day
    # projection, doctor-ready list) — the non-glucose half of the mission.
    labs_panel = labs_mod.panel(store, now=now, age=subject_age)
    reversal_view = reversal_mod.build(daily_frame, metrics=metrics, labs_panel=labs_panel)
    violations += labs_mod.self_check(labs_panel) + reversal_mod.self_check(reversal_view)

    # habit streaks (the daily loop) + time-of-day / dawn / weekday / trend-change patterns,
    # both over the wide daily frame (patterns also uses the wide-window AGP).
    streaks_view = streaks_mod.compute(daily_frame)
    wide_trend = analytics.compute(store, window_days=corr_window, now=now)
    patterns_view = patterns_mod.compute(wide_trend.get("agp", []), daily_frame)
    violations += streaks_mod.self_check(streaks_view) + patterns_mod.self_check(patterns_view)

    # log-driven analytics (only meaningful once a journal exists)
    food_ranking = food_impact.rank_foods(store, now=now)
    experiment_results = experiments_mod.compare_all_tags(store, now=now)
    assessment = assess_mod.assess(metrics, window_days=window_days)
    violations += assess_mod.self_check(assessment) + experiments_mod.self_check(experiment_results)

    findings = insights_mod.rank(
        food_ranking=food_ranking, experiment_results=experiment_results,
        symptom_result=symptoms.analyze(store, window_days=max(90, window_days), now=now),
        mood_result=mood_energy.analyze(store, window_days=max(90, window_days), now=now))
    insights_text = insights_mod.narrate(findings, metrics=metrics)

    # the prescriptive layer — "Today's moves" ranked from the user's own measured levers
    coach_moves = coach_mod.moves(metrics=metrics, experiments=experiment_results,
                                  food_ranking=food_ranking, patterns=patterns_view,
                                  reversal=reversal_view)
    violations += coach_mod.self_check(coach_moves)

    # saved custom analyses: executed deterministically every cycle (no LLM math at run time)
    custom_cards = (custom_mod.run_registry(store, path=analyses_path, now=now)
                    if analyses_path else [])
    for card in custom_cards:
        violations += custom_mod.result_self_check(card)

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        clinical.write_metrics(metrics, os.path.join(out_dir, "metrics.json"))
        _write_json(trend, os.path.join(out_dir, "trend.json"))
        _write_json(correlation, os.path.join(out_dir, "correlation.json"))
        _write_json(labs_panel, os.path.join(out_dir, "labs.json"))
        _write_json(reversal_view, os.path.join(out_dir, "reversal.json"))
        artifacts["metrics"] = os.path.join(out_dir, "metrics.json")
        artifacts["trend"] = os.path.join(out_dir, "trend.json")
        artifacts["correlation"] = os.path.join(out_dir, "correlation.json")
        artifacts["labs"] = os.path.join(out_dir, "labs.json")
        artifacts["reversal"] = os.path.join(out_dir, "reversal.json")
        if wears is not None:
            artifacts["wearables"] = wearables_mod.write_wearables(
                wears, os.path.join(out_dir, "wearables.json"))

    # pipeline heartbeat: last successful datum per source -> liveness + alerts (§A rule 6)
    last_success = {}
    latest_g = store.latest_glucose()
    if latest_g:
        last_success["glucose"] = latest_g["ts"]
    for stream in ("log", "labs", "supplement"):
        evs = store.events(stream)
        if evs:
            last_success[stream] = max(e["ts"] for e in evs)
    if wears and wears.get("days"):
        last_success["wearables"] = max(wears["days"].keys()) + "T12:00:00+04:00"
    health = heartbeat_mod.check(
        last_success, now=now,
        alerter=alerter if alerter is not None else heartbeat_mod.default_alerter(),
        health_path=os.path.join(out_dir, "health.json") if out_dir else None)
    violations += heartbeat_mod.self_check(health)
    if out_dir:
        artifacts["health"] = os.path.join(out_dir, "health.json")

    quarantined = store.quarantined()
    if dashboard:
        trend_by_window = {int(w): analytics.compute(store, window_days=int(w), now=now)
                           for w in dashboard_windows}
        cockpit = build_dashboard.build_cockpit(
            metrics=metrics, trend_by_window=trend_by_window,
            latest_glucose=store.latest_glucose(), wearables=wears, food_ranking=food_ranking,
            experiments=experiment_results, assessment=assessment,
            insights_text=insights_text, quarantine=quarantined,
            custom_cards=custom_cards, heartbeat=health,
            daily_frame=daily_frame, correlation=correlation, review_text=review_text,
            labs=labs_panel, reversal=reversal_view, streaks=streaks_view,
            patterns=patterns_view, coach=coach_moves, now=now)
        violations += build_dashboard.self_check(cockpit)
        if out_dir:
            artifacts["dashboard"] = build_dashboard.write_dashboard(
                build_dashboard.render(cockpit), os.path.join(out_dir, "dashboard.html"))

    # the 500-day Excel workbook, from the SAME store (§D rule 7) — co-published so the
    # dashboard's Export button resolves to a matching file
    if excel and out_dir:
        wb = export_excel.build_workbook(store, now=now, wearables=wears, health=health,
                                         window_days=500)
        violations += export_excel.self_check(store, wb)
        excel_path = os.path.join(out_dir, "HealthOS_500d.xlsx")
        wb.save(excel_path)
        artifacts["excel"] = excel_path

    return CycleResult(
        generated_at=now.isoformat(), n_stored=n_stored, n_quarantined=len(quarantined),
        metrics=metrics, trend=trend, quarantined=quarantined,
        self_check_violations=violations, assessment=assessment, insights_text=insights_text,
        food_ranking=food_ranking, experiments=experiment_results,
        custom_cards=custom_cards, heartbeat=health, correlation=correlation,
        daily_frame=daily_frame, review_text=review_text, labs=labs_panel,
        reversal=reversal_view, streaks=streaks_view, patterns=patterns_view,
        coach=coach_moves, artifacts=artifacts,
    )
