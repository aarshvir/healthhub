"""narration.py — patient-facing glucose summaries, grounded through `integrity`.

This is a consumer module: it turns a clinical metrics mapping into natural-language text
and routes EVERY narration through ``integrity.sanitize_narration`` so that no number can
appear that is not grounded in the computed metrics (within honest display rounding). The
intended threat model is an LLM writing the prose: :func:`verify_llm_narration` will block
any hallucinated figure it tries to smuggle in.
"""

from __future__ import annotations

import integrity


def grounded_numbers(metrics: dict[str, dict]) -> list[float]:
    """The set of numbers that may legitimately appear in a narration of *metrics*."""
    out: list[float] = []
    for entry in metrics.values():
        value = entry.get("value")
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out.append(float(value))
    return out


def _fmt(value) -> str:
    """Format a metric number for display at <=1 decimal (always within the rounding band)."""
    r = round(float(value), 1)
    return str(int(r)) if r == int(r) else f"{r:.1f}"


def render(metrics: dict[str, dict]) -> str:
    """Deterministic, grounded-by-construction summary template."""
    def v(name):
        return metrics.get(name, {}).get("value")

    parts: list[str] = []
    if v("mean_mgdl") is not None:
        parts.append(f"Mean glucose {_fmt(v('mean_mgdl'))} mg/dL")
    if v("gmi_pct") is not None:
        parts.append(f"GMI {_fmt(v('gmi_pct'))}%")
    if v("tir_pct") is not None and v("titr_pct") is not None:
        parts.append(f"time in range {_fmt(v('tir_pct'))}% "
                     f"(tight range {_fmt(v('titr_pct'))}%)")
    if v("tbr_pct") is not None and v("tar_pct") is not None:
        parts.append(f"below range {_fmt(v('tbr_pct'))}%, above range {_fmt(v('tar_pct'))}%")
    if v("cv_pct") is not None:
        flag = v("cv_flag")
        parts.append(f"variability CV {_fmt(v('cv_pct'))}%"
                     + (f" ({flag})" if isinstance(flag, str) else ""))
    if v("gri") is not None:
        parts.append(f"GRI {_fmt(v('gri'))}")
    if v("mage_mgdl") is not None:
        parts.append(f"MAGE {_fmt(v('mage_mgdl'))} mg/dL")
    return ". ".join(parts) + ("." if parts else "")


def narrate(metrics: dict[str, dict], *, redact: bool = False) -> str:
    """Render and then *verify* the template through the integrity guard (defence in depth)."""
    text = render(metrics)
    return integrity.sanitize_narration(text, grounded_numbers(metrics), redact=redact)


def verify_llm_narration(
    metrics: dict[str, dict],
    llm_text: str,
    *,
    redact: bool = False,
    allow_dates: bool = False,
) -> str:
    """Validate externally-authored (e.g. LLM) narration against the grounded metric numbers.

    Raises ``integrity.NarrationIntegrityError`` on the first ungrounded number (or redacts
    it when ``redact=True``). This is the guarantee that a hallucinated vital cannot reach
    a patient.
    """
    return integrity.sanitize_narration(
        llm_text, grounded_numbers(metrics), redact=redact, allow_dates=allow_dates
    )
