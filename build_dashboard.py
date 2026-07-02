"""build_dashboard.py — render the tabbed dashboard HTML, routed through integrity.

A phone-first cockpit for *everything logged end to end*: glucose, sleep, heart rate, SpO₂,
steps, weight, mood, energy, symptoms, food and lifestyle levers (post-meal walk, intimacy).

Tabs: Today · Trends · Correlations · Analytics · Food · Experiments · Custom · Review · Export.
A persistent header shows the last glucose value with its **age and freshness**
(FRESH/STALE/EXPIRED) — it never pretends to be live (§A rule 6). Every tile carries provenance
+ freshness. Trends draws multi-stream sparklines from the unified daily frame; Correlations
ranks cross-stream associations (never causal) and draws a diverging rho heatmap; Review is a
grounded natural-language summary of the whole picture plus a coverage map.

Colors come from the validated categorical/diverging palette (dataviz skill): each stream owns a
fixed hue, every series is direct-labeled (never colour-alone), and the heatmap is a proper
two-hue diverging scale with a neutral-gray midpoint. Self-contained static HTML (works offline
/ in the PWA).
"""

from __future__ import annotations

import html
import json
from datetime import timedelta

import correlate
import daily
import integrity

TABS = ("Today", "Trends", "Correlations", "Analytics", "Food", "Experiments",
        "Custom", "Review", "Export")
# CGM is true-5-min: judge the header glucose value on CGM timescales, not the generic 6h
# validity window — a 2h-old CGM reading must not render as "live" (§A rule 6).
CGM_FRESH = timedelta(minutes=30)
CGM_STALE = timedelta(minutes=90)
_RANGE_LOW, _RANGE_HIGH = 70.0, 180.0
_Y_MIN, _Y_MAX = 40.0, 300.0

# --- validated palette (dataviz) ----------------------------------------------------------
# one fixed hue per stream (categorical, never cycled); every series is also direct-labeled.
STREAM_COLORS = {
    "glucose": "#3987e5",    # blue
    "activity": "#199e70",   # aqua
    "sleep": "#9085e9",      # violet
    "cardio": "#d55181",     # magenta
    "affect": "#c98500",     # yellow
    "food": "#d95926",       # orange
    "symptom": "#e66767",    # red
    "body": "#008300",       # green
    "lifestyle": "#94a3b8",  # muted (events, not a hue)
    "other": "#94a3b8",
}
_NEUTRAL = "#334155"          # diverging midpoint (neutral gray)
_POS_HUE = "#d95926"          # positive correlation -> warm
_NEG_HUE = "#3987e5"          # negative correlation -> cool

# stream sections for the Trends small-multiples (ordered)
TREND_SECTIONS = (
    ("Glucose", ("mean_mgdl", "tir_pct", "cv_pct", "gri")),
    ("Sleep", ("sleep_total_min", "sleep_deep_min", "sleep_rem_min")),
    ("Activity", ("steps", "active_calories")),
    ("Cardio", ("hr_avg", "spo2_avg")),
    ("Body", ("weight_kg",)),
    ("Mood & energy", ("mood", "energy")),
    ("Symptoms", ("symptom_count", "symptom_severity")),
    ("Food", ("carbs_g", "meal_count")),
)
# the KPI strip on Today: (field, target-or-None) grouped implicitly by stream color
KPI_FIELDS = (
    "mean_mgdl", "tir_pct", "titr_pct", "gri", "cv_pct",
    "steps", "sleep_total_min", "hr_avg", "weight_kg",
    "mood", "energy", "symptom_count", "carbs_g",
)
# a readable subset for the heatmap (all NUMERIC_FIELDS would be 24×24)
HEATMAP_FIELDS = ("mean_mgdl", "tir_pct", "cv_pct", "gri", "steps", "sleep_total_min",
                  "sleep_deep_min", "hr_avg", "weight_kg", "mood", "energy",
                  "symptom_count", "carbs_g")
_UNITS = {"mean_mgdl": "mg/dL", "hr_avg": "bpm", "weight_kg": "kg", "carbs_g": "g",
          "active_calories": "kcal", "spo2_avg": "%", "tir_pct": "%", "titr_pct": "%",
          "tbr_pct": "%", "tar_pct": "%", "cv_pct": "%"}
_TARGETS = {"tir_pct": "≥70%", "titr_pct": "≥50%", "tbr_pct": "≤4%", "tar_pct": "≤25%",
            "cv_pct": "≤36%"}


def _esc(x) -> str:
    return html.escape("" if x is None else str(x))


def _fmt(v, nd=1):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _stream_of(field: str) -> str:
    return correlate.STREAMS.get(field, "other")


def _color_of(field: str) -> str:
    return STREAM_COLORS.get(_stream_of(field), STREAM_COLORS["other"])


def _disp(field: str, v) -> str:
    """Human display for a field value (units handled separately)."""
    if v is None:
        return "—"
    if field == "sleep_total_min":
        return f"{v / 60:.1f}h"
    if field in ("sleep_deep_min", "sleep_rem_min"):
        return f"{v:.0f}m"
    if field in ("steps", "active_calories", "meal_count", "supplement_count"):
        return f"{v:,.0f}"
    if field in ("symptom_count", "gri", "hr_avg", "mean_mgdl", "mage_mgdl",
                 "tir_pct", "titr_pct", "tbr_pct", "tar_pct"):
        return f"{v:.0f}"
    return f"{v:.1f}"


def _freshness_badge(state) -> str:
    return {"fresh": "🟢 FRESH", "stale": "🟡 STALE",
            "expired": "🔴 EXPIRED", "future": "⚠️ FUTURE"}.get(getattr(state, "value", state), "—")


def _age_str(age) -> str:
    secs = abs(age.total_seconds())
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def _tile(title, value, *, sub="", source="", freshness="", target="", accent="") -> str:
    foot = " · ".join(p for p in (f"src: {_esc(source)}" if source else "",
                                  _esc(freshness)) if p)
    style = f' style="border-left:3px solid {accent}"' if accent else ""
    return (f'<div class="tile"{style}><div class="t-title">{_esc(title)}</div>'
            f'<div class="t-value">{_esc(value)}</div>'
            f'<div class="t-sub">{_esc(sub)}{(" · target " + _esc(target)) if target else ""}</div>'
            f'<div class="t-foot">{foot}</div></div>')


# ------------------------------------------------------------------------------------------
# charts
# ------------------------------------------------------------------------------------------
def _agp_svg(agp: list, *, width=720, height=240) -> str:
    """Inline SVG AGP: 70-180 band shaded, p10-90 + p25-75 ribbons, median line."""
    pad = 30
    iw, ih = width - 2 * pad, height - 2 * pad

    def x(slot):
        return pad + iw * (slot / 95.0)

    def y(val):
        val = max(_Y_MIN, min(_Y_MAX, val))
        return pad + ih * (1 - (val - _Y_MIN) / (_Y_MAX - _Y_MIN))

    slots = [(i, b) for i, b in enumerate(agp) if b is not None]
    if not slots:
        return ('<svg class="agp" viewBox="0 0 %d %d"><text x="%d" y="%d" '
                'fill="#888">No AGP data in window</text></svg>' % (width, height, pad, height // 2))

    def band(key_lo, key_hi, fill):
        top = " ".join(f"{x(i):.1f},{y(b[key_hi]):.1f}" for i, b in slots)
        bot = " ".join(f"{x(i):.1f},{y(b[key_lo]):.1f}" for i, b in reversed(slots))
        return f'<polygon points="{top} {bot}" fill="{fill}" stroke="none"/>'

    median = "M" + " L".join(f"{x(i):.1f},{y(b['p50']):.1f}" for i, b in slots)
    target = (f'<rect x="{pad}" y="{y(_RANGE_HIGH):.1f}" width="{iw}" '
              f'height="{y(_RANGE_LOW) - y(_RANGE_HIGH):.1f}" fill="#19875422"/>')
    axis = (f'<line x1="{pad}" y1="{y(_RANGE_LOW):.1f}" x2="{width-pad}" y2="{y(_RANGE_LOW):.1f}" '
            f'stroke="#199e70" stroke-dasharray="3 3"/>'
            f'<line x1="{pad}" y1="{y(_RANGE_HIGH):.1f}" x2="{width-pad}" y2="{y(_RANGE_HIGH):.1f}" '
            f'stroke="#199e70" stroke-dasharray="3 3"/>')
    return (f'<svg class="agp" viewBox="0 0 {width} {height}">{target}'
            f'{band("p10", "p90", "#3987e533")}{band("p25", "p75", "#3987e566")}'
            f'<path d="{median}" fill="none" stroke="#3987e5" stroke-width="2"/>{axis}'
            f'<text x="{pad}" y="{pad-8}" fill="#94a3b8" font-size="12">AGP — 10/25/50/75/90 '
            f'percentile, 70-180 target shaded</text></svg>')


def _sparkline(points, color, *, width=200, height=48) -> str:
    """A thin sparkline for (date, value) points; missing values are skipped, end-dot marked."""
    pad = 6
    vals = [(i, v) for i, (_, v) in enumerate(points) if v is not None]
    if len(vals) < 2:
        return (f'<svg class="spark" viewBox="0 0 {width} {height}">'
                f'<text x="{pad}" y="{height//2+4}" fill="#64748b" font-size="12">'
                f'not enough data</text></svg>')
    ys = [v for _, v in vals]
    mn, mx = min(ys), max(ys)
    rng = (mx - mn) or 1.0
    n = (len(points) - 1) or 1

    def X(i):
        return pad + (width - 2 * pad) * (i / n)

    def Y(v):
        return pad + (height - 2 * pad) * (1 - (v - mn) / rng)

    path = "M" + " L".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in vals)
    lx, lv = vals[-1]
    dot = f'<circle cx="{X(lx):.1f}" cy="{Y(lv):.1f}" r="3" fill="{color}"/>'
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>{dot}</svg>')


def _hex(c):
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _mix(a, b, f):
    ra, ga, ba = _hex(a)
    rb, gb, bb = _hex(b)
    f = max(0.0, min(1.0, f))
    return f"#{int(ra+(rb-ra)*f):02x}{int(ga+(gb-ga)*f):02x}{int(ba+(bb-ba)*f):02x}"


def _rho_color(rho) -> str:
    """Diverging scale: neutral gray at 0, warm toward +1, cool toward -1."""
    if rho is None:
        return "#171d2b"
    if rho >= 0:
        return _mix(_NEUTRAL, _POS_HUE, rho)
    return _mix(_NEUTRAL, _NEG_HUE, -rho)


def _heatmap(matrix: dict, fields) -> str:
    """A diverging correlation heatmap over *fields* (values shown selectively; all on hover)."""
    all_fields = matrix.get("fields", [])
    idx = {f: all_fields.index(f) for f in fields if f in all_fields}
    fields = [f for f in fields if f in idx]
    if len(fields) < 2:
        return '<p class="muted">Not enough overlapping data for a correlation heatmap yet.</p>'
    grid = matrix["grid"]
    head = '<div class="hm-cell hm-corner"></div>' + "".join(
        f'<div class="hm-cell hm-col" title="{_esc(daily.LABELS.get(f, f))}">'
        f'{_esc((daily.LABELS.get(f, f))[:3])}</div>' for f in fields)
    rows = [f'<div class="hm-row">{head}</div>']
    for a in fields:
        cells = [f'<div class="hm-cell hm-rowlbl">{_esc(daily.LABELS.get(a, a))}</div>']
        for b in fields:
            cell = grid[idx[a]][idx[b]]
            rho, nn = cell.get("rho"), cell.get("n")
            bg = _rho_color(rho)
            txt = "" if (rho is None or abs(rho) < 0.3) else f"{rho:+.1f}"[:4].replace("0.", ".")
            tip = f"{daily.LABELS.get(a, a)} vs {daily.LABELS.get(b, b)}: " + (
                f"rho={rho:+.2f} (n={nn})" if rho is not None else f"n={nn} (below floor)")
            cells.append(f'<div class="hm-cell" style="background:{bg}" title="{_esc(tip)}">'
                         f'{_esc(txt)}</div>')
        rows.append(f'<div class="hm-row">{"".join(cells)}</div>')
    legend = ('<div class="hm-legend"><span>−1</span>'
              f'<i style="background:linear-gradient(90deg,{_NEG_HUE},{_NEUTRAL},{_POS_HUE})"></i>'
              '<span>+1</span> · rank correlation (Spearman), gray = ~0</div>')
    return f'<div class="heatmap" style="--n:{len(fields)}">' + "".join(rows) + "</div>" + legend


# ------------------------------------------------------------------------------------------
# cockpit assembly
# ------------------------------------------------------------------------------------------
def build_cockpit(*, metrics, trend_by_window, latest_glucose=None, wearables=None,
                  food_ranking=None, experiments=None, assessment=None,
                  insights_text="", quarantine=None, custom_cards=None,
                  heartbeat=None, daily_frame=None, correlation=None,
                  review_text="", now=None) -> dict:
    """Assemble the data the dashboard renders, with header freshness from integrity."""
    now = integrity.now_utc() if now is None else now
    header = {"generated_at": now.isoformat(), "glucose": None}
    if latest_glucose and latest_glucose.get("ts") is not None:
        fr = integrity.freshness(latest_glucose["ts"], now=now,
                                 fresh_within=CGM_FRESH, stale_within=CGM_STALE)
        header["glucose"] = {
            "value": latest_glucose.get("value"),
            "as_of": fr.measured_at.isoformat(),
            "age": _age_str(fr.age),
            "state": fr.state.value,
        }
    return {
        "header": header, "metrics": metrics or {}, "trend_by_window": trend_by_window or {},
        "wearables": wearables or {}, "food_ranking": food_ranking or [],
        "experiments": experiments or [], "assessment": assessment or {},
        "insights_text": insights_text or "", "quarantine": quarantine or [],
        "custom_cards": custom_cards or [], "heartbeat": heartbeat or {},
        "daily_frame": daily_frame or [], "correlation": correlation or {},
        "review_text": review_text or "",
    }


def _latest(frame, field):
    for r in reversed(frame):
        if r.get(field) is not None:
            return r["date"], r[field]
    return None, None


# ------------------------------------------------------------------------------------------
# tabs
# ------------------------------------------------------------------------------------------
def _hero(c) -> str:
    m = c["metrics"]
    a = c["assessment"]

    def mv(k):
        return (m.get(k) or {}).get("value")

    grade = a.get("grade")
    score = a.get("score_pct")
    headline = a.get("headline") or "Your metabolic snapshot"
    review = (c.get("review_text") or "").strip()
    lead = review.split(". ")[0] + "." if review else ""
    gr = f'<div class="hero-grade grade-{_esc(grade)}">{_esc(grade or "—")}</div>' if a else ""
    sc = (f'<div class="hero-score"><span>{_fmt(score, 0)}<small>/100</small></span>'
          f'<div class="score-bar"><i style="width:{max(0, min(100, score or 0)):.0f}%"></i></div>'
          f'</div>') if a else ""
    mtb = mv("daily_metabolic_score")
    mtb_html = (f'<div class="hero-metric"><span>{_fmt(mtb, 0)}</span>'
                f'<label>Daily metabolic score</label></div>') if mtb is not None else ""
    tir = mv("tir_pct")
    tir_html = (f'<div class="hero-metric"><span>{_fmt(tir, 0)}%</span>'
                f'<label>Time in range</label></div>') if tir is not None else ""
    return (f'<div class="hero">{gr}'
            f'<div class="hero-body"><div class="hero-headline">{_esc(headline)}</div>'
            f'{sc}<div class="hero-metrics">{tir_html}{mtb_html}</div>'
            f'{f"<p class=hero-lead>{_esc(lead)}</p>" if lead else ""}</div></div>')


def _today_tab(c) -> str:
    m = c["metrics"]
    frame = c["daily_frame"]

    def mv(k):
        return (m.get(k) or {}).get("value")

    def fresh(k):
        return _freshness_badge("fresh" if (m.get(k) or {}).get("valid") else "stale")

    tiles = []
    # glucose KPIs come from the clinical metrics (with real validity/freshness)
    glucose_kpis = [("tir_pct", "Time in Range"), ("titr_pct", "Tight Range (70-140)"),
                    ("gri", "Glycemia Risk Index"), ("cv_pct", "Variability (CV)"),
                    ("mean_mgdl", "Mean glucose")]
    for field, title in glucose_kpis:
        val = mv(field)
        unit = _UNITS.get(field, "")
        shown = "—" if val is None else f"{_disp(field, val)}{unit}"
        tiles.append(_tile(title, shown, source="clinical", freshness=fresh(field),
                           target=_TARGETS.get(field, ""), accent=_color_of(field)))
    # every other stream's most-recent day, straight from the unified daily frame
    other_kpis = ["steps", "sleep_total_min", "hr_avg", "weight_kg",
                  "mood", "energy", "symptom_count", "carbs_g"]
    for field in other_kpis:
        d, val = _latest(frame, field)
        if val is None:
            continue
        unit = _UNITS.get(field, "")
        tiles.append(_tile(daily.LABELS.get(field, field), f"{_disp(field, val)}{unit}",
                           sub=f"as of {d}", source="daily", accent=_color_of(field)))
    return _hero(c) + '<div class="grid">' + "".join(tiles) + "</div>"


def _trends_tab(c) -> str:
    frame = c["daily_frame"]
    if len(frame) < 2:
        return ('<p class="muted">Trends appear once at least two days are logged. Keep '
                'syncing your CGM, wearable and journal.</p>')
    span = f"{frame[0]['date']} → {frame[-1]['date']}"
    sections = []
    for title, fields in TREND_SECTIONS:
        cards = []
        for field in fields:
            pts = daily.series(frame, field)
            n = sum(1 for _, v in pts if v is not None)
            if n == 0:
                continue
            color = _color_of(field)
            _, last = _latest(frame, field)
            unit = _UNITS.get(field, "")
            cards.append(
                f'<div class="spark-card">'
                f'<div class="spark-head"><span class="dot" style="background:{color}"></span>'
                f'<span class="spark-lbl" style="color:{color}">{_esc(daily.LABELS.get(field, field))}</span>'
                f'<span class="spark-val">{_disp(field, last)}{_esc(unit)}</span></div>'
                f'{_sparkline(pts, color)}'
                f'<div class="spark-foot">{n} days</div></div>')
        if cards:
            sections.append(f'<h3 class="sec">{_esc(title)}</h3>'
                            f'<div class="sparks">{"".join(cards)}</div>')
    if not sections:
        return '<p class="muted">No multi-day streams yet.</p>'
    return f'<div class="muted">{_esc(span)} · one line per signal, latest value marked</div>' \
           + "".join(sections)


def _corr_card(f) -> str:
    r = f.get("r", 0.0)
    chip = _rho_color(r)
    arrow = "↑" if f.get("direction") == "+" else "↓"
    kind = {"same-day": "same-day", "lag-1": "next-day", "lever": "on/off"}.get(f.get("kind"), "")
    return (f'<div class="corr-card">'
            f'<div class="corr-r" style="background:{chip}">{arrow} {r:+.2f}</div>'
            f'<div class="corr-body"><div class="corr-lbl">{_esc(f.get("label"))}</div>'
            f'<div class="corr-meta"><span class="pill">{_esc(kind)}</span>'
            f'<span class="pill">{_esc(f.get("strength"))}</span>'
            f'<span class="corr-n">{_esc(f.get("detail"))}</span></div></div></div>')


def _correlations_tab(c) -> str:
    corr = c["correlation"]
    findings = corr.get("findings", [])
    if not findings and not corr.get("matrix"):
        return ('<p class="muted">Correlations need several days across multiple streams. '
                'Once glucose, sleep, activity and your journal overlap, associations appear here.</p>')
    note = ('<p class="muted">Associations across everything you log — <b>observational, not '
            'causal</b>. Each carries its sample size; weak/tiny-sample links are hidden.</p>')
    cards = ('<div class="corr-cards">' + "".join(_corr_card(f) for f in findings) + "</div>"
             if findings else '<p class="muted">No association cleared the evidence floor yet.</p>')
    heat = _heatmap(corr["matrix"], HEATMAP_FIELDS) if corr.get("matrix") else ""
    return note + cards + ('<h3 class="sec">Correlation heatmap</h3>' + heat if heat else "")


def _analytics_tab(c) -> str:
    windows = sorted((int(w) for w in c["trend_by_window"].keys()))
    if not windows:
        return "<p>No trend data.</p>"
    buttons = "".join(
        f'<button class="winbtn" onclick="showWindow({w})" id="winbtn-{w}">{w}d</button>'
        for w in windows)
    gstate = ((c.get("header") or {}).get("glucose") or {}).get("state")
    fresh_badge = _freshness_badge(gstate) if gstate else ""
    panels = []
    for i, w in enumerate(windows):
        tr = c["trend_by_window"][w] if w in c["trend_by_window"] else c["trend_by_window"][str(w)]
        s = tr.get("summary", {})
        summary = "".join(_tile(k, _fmt(s.get(k)), source="analytics", freshness=fresh_badge,
                                sub=f"{w}d window", accent=_color_of(k))
                          for k in ("mean_mgdl", "tir_pct", "titr_pct", "cv_pct", "gri")
                          if k in s)
        style = "" if i == 0 else ' style="display:none"'
        panels.append(
            f'<div class="winpanel" id="winpanel-{w}"{style}>'
            f'<div class="muted">{_esc(tr.get("n_readings"))} readings · {w}-day window</div>'
            f'{_agp_svg(tr.get("agp", []))}<div class="grid">{summary}</div></div>')
    return f'<div class="winbar">{buttons}</div>' + "".join(panels)


def _food_tab(c) -> str:
    if not c["food_ranking"]:
        return "<p>No foods with n≥3 yet.</p>"
    rows = "".join(
        f"<tr><td>{_esc(f['item'])}</td><td>{_fmt(f.get('mean_delta_peak_mgdl'),0)}</td>"
        f"<td>{_fmt(f.get('mean_iauc_120'),0)}</td><td>{_esc(f.get('n'))}</td></tr>"
        for f in c["food_ranking"])
    return ('<table><thead><tr><th>Food</th><th>Δpeak (mg/dL)</th><th>iAUC</th>'
            f'<th>n</th></tr></thead><tbody>{rows}</tbody></table>')


def _experiments_tab(c) -> str:
    if not c["experiments"]:
        return "<p>No interventions logged.</p>"
    rows = "".join(
        f"<tr><td>{_esc(e['tag'])}</td><td>{_fmt(e.get('effect_abs'),0)}</td>"
        f"<td>{_fmt(e.get('effect_pct'),0)}%</td><td>{_esc(e.get('n_treated'))}/"
        f"{_esc(e.get('n_control'))}</td><td>{_esc(e.get('signal_strength'))}</td>"
        f"<td>{_esc(e.get('causal_label'))}</td></tr>" for e in c["experiments"])
    return ('<table><thead><tr><th>Tag</th><th>Effect</th><th>%</th><th>n T/C</th>'
            f'<th>signal</th><th>causal?</th></tr></thead><tbody>{rows}</tbody></table>')


def _custom_card(card) -> str:
    if card.get("error"):
        return (f'<div class="tile"><div class="t-title">{_esc(card.get("title"))}</div>'
                f'<div class="warn">{_esc(card["error"])}</div></div>')
    groups = [g for g in card.get("groups", []) if g.get("value") is not None]
    mx = max((abs(g["value"]) for g in groups), default=1) or 1
    bars = "".join(
        f'<div class="bar-row"><span class="bar-lbl">{_esc(g["group"])} '
        f'(n={_esc(g["n"])})</span>'
        f'<span class="bar"><i style="width:{max(2, 100*abs(g["value"])/mx):.0f}%"></i></span>'
        f'<span class="bar-val">{_fmt(g["value"])}</span></div>' for g in groups)
    if not groups:
        bars = '<div class="muted">No data matched.</div>'
    return (f'<div class="card"><div class="card-title">{_esc(card.get("title"))}</div>'
            f'<div class="card-filter">filter: {_esc(card.get("filter"))}</div>'
            f'<div class="bars">{bars}</div>'
            f'<div class="card-assess">{_esc(card.get("assessment"))}</div>'
            f'<div class="t-foot">overall n={_esc((card.get("overall") or {}).get("n"))} · '
            f'src: custom analysis</div></div>')


def _custom_tab(c) -> str:
    cards = c.get("custom_cards", [])
    if not cards:
        return ('<p class="muted">No saved analyses yet. Describe one in chat — Claude writes '
                'the spec once into analyses.json; the engine then recomputes it every cycle.</p>')
    return '<div class="cards">' + "".join(_custom_card(card) for card in cards) + "</div>"


def _coverage_view(c) -> str:
    corr = c["correlation"]
    cov = corr.get("coverage") or (c["daily_frame"] and daily.coverage(c["daily_frame"])) or {}
    ndays = corr.get("n_days") or len(c["daily_frame"])
    if not cov or not ndays:
        return ""
    items = sorted(((f, n) for f, n in cov.items() if n > 0), key=lambda kv: -kv[1])
    if not items:
        return ""
    rows = "".join(
        f'<div class="bar-row"><span class="bar-lbl">{_esc(daily.LABELS.get(f, f))}</span>'
        f'<span class="bar"><i style="width:{100*n/ndays:.0f}%;background:{_color_of(f)}"></i></span>'
        f'<span class="bar-val">{n}/{ndays}</span></div>' for f, n in items)
    return (f'<h3 class="sec">Everything you\'re tracking ({ndays} days)</h3>'
            f'<div class="bars cov">{rows}</div>')


def _review_tab(c) -> str:
    a = c["assessment"]
    head = ""
    if a:
        head = (f'<div class="review-head"><span class="grade-{_esc(a.get("grade"))} rgrade">'
                f'{_esc(a.get("grade"))}</span><div><h3>{_esc(a.get("headline", "Assessment"))}'
                f'</h3><div class="muted">score {_fmt(a.get("score_pct"), 0)}/100</div></div></div>')
    lines = "".join(f"<li>{_esc(l['text'])}</li>" for l in a.get("lines", []))
    lines_html = f"<ul class=review-lines>{lines}</ul>" if lines else ""
    corr_txt = (c.get("review_text") or "").strip()
    corr_html = (f'<h3 class="sec">Cross-stream signals</h3><p class="insight">{_esc(corr_txt)}</p>'
                 if corr_txt else "")
    ins = c["insights_text"]
    ins_html = (f'<h3 class="sec">Top findings</h3><p class="insight">{_esc(ins)}</p>'
                if ins else "")
    return head + lines_html + corr_html + ins_html + _coverage_view(c)


def _export_tab(c) -> str:
    q = c["quarantine"]
    qnote = (f'<p class="warn">{len(q)} row(s) quarantined and excluded from all charts.</p>'
             if q else "<p>No quarantined rows.</p>")
    return (qnote
            + '<p><a class="xbtn" href="HealthOS_500d.xlsx" download>⬇ Export 500-day Excel</a></p>'
            + '<p>Artifacts (same store as the dashboard): <code>metrics.json</code>, '
            '<code>trend.json</code>, <code>wearables.json</code>, <code>health.json</code>.</p>'
            f'<details><summary>cockpit JSON</summary><pre>{_esc(json.dumps(c["header"], indent=2))}'
            '</pre></details>')


_CSS = """
*{box-sizing:border-box}body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;
background:#0b0f1a;color:#e2e8f0;-webkit-font-smoothing:antialiased}
header{position:sticky;top:0;background:#111827ee;backdrop-filter:blur(8px);padding:12px 16px;
border-bottom:1px solid #1f2937;z-index:5}.hdr-glucose{font-size:22px;font-weight:700}
.hdr-age{color:#94a3b8;font-size:13px}
nav{display:flex;gap:4px;padding:8px 12px;background:#111827;overflow-x:auto;position:sticky;top:58px;z-index:4}
nav button{background:#1f2937;color:#cbd5e1;border:0;padding:8px 14px;border-radius:8px;cursor:pointer;
white-space:nowrap;font-size:14px}nav button.active{background:#3987e5;color:#fff}
main{padding:16px;max-width:1100px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.tile{background:#12161f;border:1px solid #1f2937;border-radius:14px;padding:12px}
.t-title{font-size:12px;color:#94a3b8}.t-value{font-size:24px;font-weight:700;margin:4px 0}
.t-sub{font-size:12px;color:#cbd5e1}.t-foot{font-size:11px;color:#64748b;margin-top:6px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #1f2937}
h2{margin:4px 0 14px;font-size:20px}h3.sec{margin:22px 0 10px;font-size:15px;color:#cbd5e1;
border-left:3px solid #3987e5;padding-left:8px}
.hero{display:flex;gap:16px;background:linear-gradient(135deg,#12161f,#161d2e);border:1px solid #1f2937;
border-radius:18px;padding:18px;margin-bottom:16px;align-items:center}
.hero-grade{font-size:44px;font-weight:800;width:72px;height:72px;border-radius:16px;display:flex;
align-items:center;justify-content:center;background:#0b0f1a;flex:0 0 auto}
.hero-body{flex:1}.hero-headline{font-size:17px;font-weight:600;margin-bottom:8px}
.hero-score{display:flex;align-items:center;gap:10px;margin:6px 0}
.hero-score span{font-size:22px;font-weight:700}.hero-score small{font-size:12px;color:#94a3b8}
.score-bar{flex:1;height:8px;background:#0b0f1a;border-radius:6px;overflow:hidden}
.score-bar i{display:block;height:100%;background:linear-gradient(90deg,#d95926,#c98500,#199e70)}
.hero-metrics{display:flex;gap:20px;margin-top:8px}
.hero-metric span{font-size:20px;font-weight:700}.hero-metric label{display:block;font-size:11px;color:#94a3b8}
.hero-lead{color:#cbd5e1;font-size:13px;margin:10px 0 0}
.grade-A{color:#0ca30c}.grade-B{color:#199e70}.grade-C{color:#fab219}.grade-D{color:#ec835a}.grade-F{color:#d03b3b}
.winbar{margin-bottom:10px}.winbtn{background:#1f2937;color:#cbd5e1;border:0;margin-right:4px;
padding:6px 10px;border-radius:8px;cursor:pointer}.winbtn.active{background:#3987e5;color:#fff}
.agp{width:100%;height:auto;background:#0b1220;border-radius:10px;margin:8px 0}
.muted{color:#94a3b8;font-size:13px}.warn{color:#fab219}.insight{color:#cbd5e1;line-height:1.6;
background:#12161f;border:1px solid #1f2937;border-radius:12px;padding:12px}
.tab{display:none}.tab.active{display:block}
pre{white-space:pre-wrap;background:#0b1220;padding:8px;border-radius:8px}
.sparks{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.spark-card{background:#12161f;border:1px solid #1f2937;border-radius:14px;padding:10px 12px}
.spark-head{display:flex;align-items:center;gap:6px;font-size:13px;margin-bottom:2px}
.spark-head .dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
.spark-lbl{font-weight:600}.spark-val{margin-left:auto;font-weight:700;color:#e2e8f0}
.spark{width:100%;height:48px;display:block}.spark-foot{font-size:11px;color:#64748b;margin-top:2px}
.corr-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
.corr-card{display:flex;gap:12px;background:#12161f;border:1px solid #1f2937;border-radius:14px;padding:10px 12px}
.corr-r{flex:0 0 auto;align-self:flex-start;font-weight:700;color:#fff;border-radius:10px;padding:6px 10px;font-size:13px}
.corr-lbl{font-weight:600;line-height:1.35}.corr-meta{display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap}
.pill{background:#1f2937;color:#cbd5e1;border-radius:20px;padding:2px 9px;font-size:11px}
.corr-n{color:#64748b;font-size:11px}
.heatmap{display:grid;grid-template-columns:110px repeat(var(--n),1fr);gap:2px;overflow-x:auto}
.hm-row{display:contents}.hm-cell{aspect-ratio:1;display:flex;align-items:center;justify-content:center;
font-size:10px;color:#e2e8f0;border-radius:3px;min-width:22px}
.hm-corner{background:transparent}.hm-col{background:transparent;color:#94a3b8;font-size:10px;aspect-ratio:auto}
.hm-rowlbl{background:transparent;color:#94a3b8;justify-content:flex-end;padding-right:6px;font-size:11px;aspect-ratio:auto}
.hm-legend{display:flex;align-items:center;gap:8px;margin-top:10px;color:#94a3b8;font-size:12px}
.hm-legend i{display:inline-block;width:120px;height:10px;border-radius:5px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.card{background:#12161f;border:1px solid #1f2937;border-radius:14px;padding:12px}
.card-title{font-weight:700}.card-filter{font-size:11px;color:#64748b;margin:4px 0 8px}
.bar-row{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}
.bar-lbl{flex:0 0 130px;color:#cbd5e1}.bar{flex:1;background:#0b1220;border-radius:6px;height:12px;overflow:hidden}
.bar i{display:block;height:100%;background:#3987e5}.bar-val{flex:0 0 56px;text-align:right;color:#e2e8f0}
.cov .bar-lbl{flex:0 0 150px}
.card-assess{font-size:12px;color:#a5b4fc;margin-top:8px}
.review-head{display:flex;gap:14px;align-items:center;margin-bottom:8px}
.rgrade{font-size:34px;font-weight:800;width:56px;height:56px;border-radius:14px;display:flex;
align-items:center;justify-content:center;background:#12161f}
.review-lines{line-height:1.7}
.heartbeat{margin-top:8px;font-size:12px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.heartbeat.hb-bad{color:#fab219}.heartbeat.hb-ok{color:#199e70}
.hb{background:#1f2937;border-radius:10px;padding:2px 8px;color:#cbd5e1}
.hb-fresh{border:1px solid #199e70}.hb-stale{border:1px solid #fab219;color:#fbbf24}
.hb-down,.hb-no_data,.hb-future{border:1px solid #d03b3b;color:#fca5a5}
.xbtn{display:inline-block;background:#3987e5;color:#fff;padding:10px 16px;border-radius:10px;text-decoration:none;font-weight:700}
"""

_JS = """
function showTab(name){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
document.getElementById('tab-'+name).classList.add('active');
document.querySelectorAll('nav button').forEach(b=>b.classList.remove('active'));
document.getElementById('navbtn-'+name).classList.add('active');window.scrollTo(0,0);}
function showWindow(w){document.querySelectorAll('.winpanel').forEach(p=>p.style.display='none');
var el=document.getElementById('winpanel-'+w);if(el)el.style.display='block';
document.querySelectorAll('.winbtn').forEach(b=>b.classList.remove('active'));
var bb=document.getElementById('winbtn-'+w);if(bb)bb.classList.add('active');}
"""

# Recompute the glucose age + freshness on the DEVICE clock, so an offline PWA never shows a
# stale "as of" age or pretends a frozen value is live (§A rule 6). Mirrors the CGM
# fresh<=30m / stale<=90m thresholds embedded in the config.
_FRESH_JS = """
function hhAge(ms){var s=Math.abs(ms)/1000;if(s<3600)return Math.round(s/60)+'m';
if(s<86400)return (s/3600).toFixed(1)+'h';return (s/86400).toFixed(1)+'d';}
function hhRefresh(){if(!window.HH||!HH.asOf)return;var ageMs=Date.now()-Date.parse(HH.asOf);
var h=ageMs/3600000,badge;if(ageMs< -5*60000)badge='⚠️ FUTURE';else if(h<=HH.freshH)badge='🟢 FRESH';
else if(h<=HH.staleH)badge='🟡 STALE';else badge='🔴 EXPIRED';
var a=document.getElementById('hdrAge');if(a)a.textContent=hhAge(ageMs);
var b=document.getElementById('hdrBadge');if(b)b.textContent=badge;}
hhRefresh();setInterval(hhRefresh,60000);
if('serviceWorker' in navigator){navigator.serviceWorker.register('service-worker.js').catch(function(){});}
"""


def render(cockpit: dict) -> str:
    g = cockpit["header"].get("glucose")
    if g:
        hdr = (f'<div class="hdr-glucose"><span id="hdrVal">{_fmt(g.get("value"),0)}</span> '
               f'mg/dL · <span id="hdrBadge">{_freshness_badge(g.get("state"))}</span></div>'
               f'<div class="hdr-age">as of <span id="hdrAsOf">{_esc(g.get("as_of"))}</span> '
               f'(<span id="hdrAge">{_esc(g.get("age"))}</span> ago) — '
               f'last-known-good, not live</div>')
        cfg = json.dumps({"asOf": g.get("as_of"), "freshH": 0.5, "staleH": 1.5})
    else:
        hdr = '<div class="hdr-glucose">No glucose data</div>'
        cfg = "null"

    hb = cockpit.get("heartbeat") or {}
    pills = "".join(
        f'<span class="hb hb-{_esc(s["state"])}">{_esc(s["source"])}: '
        f'{_esc(s["age"] or "no data")} · {_esc(s["state"])}</span>'
        for s in hb.get("sources", []))
    banner = (f'<div class="heartbeat {"hb-ok" if hb.get("overall_ok") else "hb-bad"}">'
              f'{"✓ all feeds live" if hb.get("overall_ok") else "⚠ feed(s) stale"} {pills}</div>'
              if hb.get("sources") else "")

    bodies = {
        "Today": _today_tab(cockpit), "Trends": _trends_tab(cockpit),
        "Correlations": _correlations_tab(cockpit), "Analytics": _analytics_tab(cockpit),
        "Food": _food_tab(cockpit), "Experiments": _experiments_tab(cockpit),
        "Custom": _custom_tab(cockpit), "Review": _review_tab(cockpit),
        "Export": _export_tab(cockpit),
    }
    nav = "".join(f'<button id="navbtn-{t}" class="{"active" if i==0 else ""}" '
                  f'onclick="showTab(\'{t}\')">{t}</button>' for i, t in enumerate(TABS))
    tabs = "".join(f'<section id="tab-{t}" class="tab {"active" if i==0 else ""}">'
                   f'<h2>{t}</h2>{bodies[t]}</section>' for i, t in enumerate(TABS))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<link rel="manifest" href="manifest.json">'
            f'<title>HealthHub</title><style>{_CSS}</style></head><body>'
            f'<header>{hdr}{banner}</header><nav>{nav}</nav><main>{tabs}</main>'
            f'<script>window.HH={cfg};{_JS}{_FRESH_JS}</script></body></html>')


def write_dashboard(html_str: str, path: str = "dashboard.html") -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_str)
    return path


def self_check(cockpit: dict) -> list[str]:
    """Invariant: a quarantined value must never be rendered as a plotted glucose tile."""
    violations: list[str] = []
    g = cockpit["header"].get("glucose")
    if g and g.get("state") == "future":
        violations.append("header shows a future-dated glucose value")
    bad = {str(q.get("payload", {}).get("glucose_mgdl")) for q in cockpit.get("quarantine", [])}
    bad.discard("None")
    html_out = render(cockpit)
    for tab in ("Today", "Analytics", "Trends"):
        seg = html_out.split(f'id="tab-{tab}"', 1)[-1].split("</section>", 1)[0]
        for v in bad:
            if v and v in seg:
                violations.append(f"quarantined value {v} appears plotted in {tab}")
    return violations
