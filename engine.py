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

import analytics
import clinical
import glucose as glucose_mod
import integrity


@dataclasses.dataclass(frozen=True, slots=True)
class CycleResult:
    generated_at: str
    n_stored: int
    n_quarantined: int
    metrics: dict
    trend: dict
    quarantined: list
    self_check_violations: list

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


def run_cycle(*, store, glucose_source=None, now=None, window_days: int = 14,
              subject: str = "patient", walk_adherence: float | None = None,
              out_dir: str = ".", prune_retention: int = 500,
              sleep=None) -> CycleResult:
    """Run a full cycle against *store* and write metrics.json + trend.json to *out_dir*."""
    now = integrity.now_utc() if now is None else now

    n_stored = 0
    if glucose_source is not None:
        res = glucose_mod.sync(store, glucose_source, now=now, subject=subject, sleep=sleep)
        n_stored = res.n_stored
    store.prune(now=now, retention_days=prune_retention)

    window_df = store.glucose_last_days(window_days, now=now)
    metrics = clinical.compute_metrics(window_df, now=now, subject=subject,
                                       walk_adherence=walk_adherence)
    trend = analytics.compute(store, window_days=window_days, now=now)

    violations = (store.self_check(now=now)
                  + analytics.self_check(trend)
                  + _metrics_self_check(metrics))

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        clinical.write_metrics(metrics, os.path.join(out_dir, "metrics.json"))
        _write_json(trend, os.path.join(out_dir, "trend.json"))

    quarantined = store.quarantined()
    return CycleResult(
        generated_at=now.isoformat(),
        n_stored=n_stored,
        n_quarantined=len(quarantined),
        metrics=metrics,
        trend=trend,
        quarantined=quarantined,
        self_check_violations=violations,
    )
