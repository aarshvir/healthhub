"""healthhub-metrics — command-line entry point.

Reads a glucose store from CSV, computes the clinical metrics over an optional window
(routing everything through :mod:`integrity`), and writes ``metrics.json``.

Like every other module, this CLI never reads the clock or coerces values on its own — it
goes through ``integrity`` / ``clinical`` (the architecture test enforces this).

Usage:
    healthhub-metrics examples/glucose_sample.csv -o metrics.json --walk-adherence 0.8
"""

from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

import clinical
import integrity


def _read_store(path: str) -> pd.DataFrame:
    """Load a glucose CSV. ``measured_at`` must carry a timezone (integrity rejects naive)."""
    df = pd.read_csv(path)
    if "measured_at" in df.columns:
        df["measured_at"] = pd.to_datetime(df["measured_at"], utc=False)
    elif "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=False)
    return df


def _parse_window(start: str | None, end: str | None):
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise SystemExit("error: --window-start and --window-end must be given together")
    # validate both timestamps through the integrity normalizer (fails closed on naive/garbage)
    s = integrity.freshness(start, now=integrity.now_utc()).measured_at
    e = integrity.freshness(end, now=integrity.now_utc()).measured_at
    return (s, e)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="healthhub-metrics", description=__doc__)
    parser.add_argument("csv", help="path to the glucose store CSV (subject, measured_at, glucose_mgdl)")
    parser.add_argument("-o", "--output", default="metrics.json", help="output JSON path (default: metrics.json)")
    parser.add_argument("--subject", default=None, help="subject id to compute for / attach")
    parser.add_argument("--walk-adherence", type=float, default=None,
                        help="post-meal-walk adherence 0..1 (enables the daily metabolic score)")
    parser.add_argument("--window-start", default=None, help="ISO start of the analysis window")
    parser.add_argument("--window-end", default=None, help="ISO end of the analysis window")
    parser.add_argument("--source-label", default="glucose_store", help="provenance label for metrics")
    parser.add_argument("--quiet", action="store_true", help="do not print the summary table")
    args = parser.parse_args(argv)

    store = _read_store(args.csv)
    window = _parse_window(args.window_start, args.window_end)

    try:
        metrics = clinical.compute_metrics(
            store,
            window=window,
            subject=args.subject,
            walk_adherence=args.walk_adherence,
            source_label=args.source_label,
        )
    except integrity.IntegrityError as exc:
        print(f"integrity error: {exc}", file=sys.stderr)
        return 2

    clinical.write_metrics(metrics, args.output)

    if not args.quiet:
        headline = ["n_readings", "mean_mgdl", "gmi_pct", "tir_pct", "titr_pct",
                    "tbr_pct", "tar_pct", "cv_pct", "gri", "mage_mgdl", "daily_metabolic_score"]
        print(f"wrote {args.output}")
        for name in headline:
            entry = metrics.get(name)
            if entry is None:
                continue
            value = entry["value"]
            shown = "—" if value is None else (f"{value:.2f}" if isinstance(value, float) else value)
            flag = "" if entry["valid"] else "  (UNTRUSTED)"
            print(f"  {name:24} {shown}{flag}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
