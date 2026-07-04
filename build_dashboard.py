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
from datetime import date, timedelta

import assets
import correlate
import daily
import integrity

TABS = ("Today", "Reversal", "Trends", "Patterns", "Correlations", "Analytics", "Food",
        "Experiments", "Custom", "Review", "Export")
# reserved status palette (dataviz): state colours, never reused for a data series
STATUS_COLORS = {"good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b",
                 "unknown": "#8091a7"}
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
          "tbr_pct": "%", "tar_pct": "%", "cv_pct": "%", "gmi_pct": "%",
          "mage_mgdl": "mg/dL", "dawn_delta_mgdl": "mg/dL"}
_TARGETS = {"tir_pct": "≥70%", "titr_pct": "≥50%", "tbr_pct": "≤4%", "tar_pct": "≤25%",
            "cv_pct": "≤36%", "gmi_pct": "<6.5%"}


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
    if field in ("symptom_count", "gri", "hr_avg", "mean_mgdl", "mage_mgdl", "dawn_delta_mgdl",
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


def _date_dot(d, gen_at, *, fresh_days=1, stale_days=4) -> str:
    """Honest freshness chip for a slower stream's calendar date vs the cycle date — a
    2-day-old reading is amber, not green (§A rule 6: never pretend fresh)."""
    try:
        age = (date.fromisoformat((gen_at or "")[:10]) - date.fromisoformat(str(d))).days
    except (ValueError, TypeError):
        return _esc(d)
    dot = "🟢" if age <= fresh_days else "🟡" if age <= stale_days else "🔴"
    return f"{dot} {d}"


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
def _agp_svg(agp: list, *, width=720, height=260) -> str:
    """Inline SVG AGP with real axes: y ticks (54/70/180/250), x hour ticks, 70-180 band."""
    padl, padr, padt, padb = 44, 14, 14, 26
    iw, ih = width - padl - padr, height - padt - padb

    def x(slot):
        return padl + iw * (slot / 95.0)

    def y(val):
        val = max(_Y_MIN, min(_Y_MAX, val))
        return padt + ih * (1 - (val - _Y_MIN) / (_Y_MAX - _Y_MIN))

    slots = [(i, b) for i, b in enumerate(agp) if b is not None]
    if not slots:
        return ('<svg class="agp" viewBox="0 0 %d %d"><text x="%d" y="%d" '
                'fill="#8091a7">No AGP data in window</text></svg>'
                % (width, height, padl, height // 2))

    def band(key_lo, key_hi, fill):
        top = " ".join(f"{x(i):.1f},{y(b[key_hi]):.1f}" for i, b in slots)
        bot = " ".join(f"{x(i):.1f},{y(b[key_lo]):.1f}" for i, b in reversed(slots))
        return f'<polygon points="{top} {bot}" fill="{fill}" stroke="none"/>'

    median = "M" + " L".join(f"{x(i):.1f},{y(b['p50']):.1f}" for i, b in slots)
    target = (f'<rect x="{padl}" y="{y(_RANGE_HIGH):.1f}" width="{iw}" '
              f'height="{y(_RANGE_LOW) - y(_RANGE_HIGH):.1f}" fill="#19875420"/>')
    # y-axis gridlines + labels at clinically meaningful glucose levels
    ylines = []
    for v in (54, 70, 180, 250):
        is_thr = v in (70, 180)
        stroke = "#199e70" if is_thr else "#232a3a"
        dash = ' stroke-dasharray="3 3"' if is_thr else ""
        ylines.append(
            f'<line x1="{padl}" y1="{y(v):.1f}" x2="{width-padr}" y2="{y(v):.1f}" '
            f'stroke="{stroke}"{dash}/>'
            f'<text x="{padl-6}" y="{y(v)+3:.1f}" fill="#8091a7" font-size="11" '
            f'text-anchor="end">{v}</text>')
    yticks = "".join(ylines)
    # x-axis hour ticks (Asia/Dubai, 4 slots/hour -> slot = hour*4)
    xticks = "".join(
        f'<text x="{x(h*4):.1f}" y="{height-8}" fill="#8091a7" font-size="11" '
        f'text-anchor="middle">{h:02d}h</text>' for h in (0, 6, 12, 18))
    return (f'<svg class="agp" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Ambulatory glucose profile">{target}{yticks}'
            f'{band("p10", "p90", "#3987e533")}{band("p25", "p75", "#3987e566")}'
            f'<path d="{median}" fill="none" stroke="#3987e5" stroke-width="2.5" '
            f'vector-effect="non-scaling-stroke"/>{xticks}</svg>')


def _sparkline(points, color, *, width=200, height=48, dot_color=None) -> str:
    """A thin sparkline for (date, value) points; missing values skipped, end-dot marked.

    ``dot_color`` (defaults to *color*) lets the end marker carry a different hue than the line
    — used for lab rows, where the line stays neutral and only the latest point shows status.
    """
    pad = 6
    dot_color = dot_color or color
    vals = [(i, v) for i, (_, v) in enumerate(points) if v is not None]
    if len(vals) < 2:
        return (f'<svg class="spark" viewBox="0 0 {width} {height}">'
                f'<text x="{pad}" y="{height//2+4}" fill="#8091a7" font-size="12">'
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
    dot = f'<circle cx="{X(lx):.1f}" cy="{Y(lv):.1f}" r="3.2" fill="{dot_color}"/>'
    # non-scaling-stroke keeps 2px lines even though the viewBox is stretched to the card width
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2" '
            f'vector-effect="non-scaling-stroke" stroke-linejoin="round" '
            f'stroke-linecap="round"/>{dot}</svg>')


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


# compact, unambiguous column/row labels for the heatmap (no 3-char truncation)
HEATMAP_SHORT = {
    "mean_mgdl": "Mean glu", "tir_pct": "TIR", "cv_pct": "CV", "gri": "GRI",
    "steps": "Steps", "sleep_total_min": "Sleep", "sleep_deep_min": "Deep",
    "hr_avg": "HR", "weight_kg": "Weight", "mood": "Mood", "energy": "Energy",
    "symptom_count": "Sympt", "carbs_g": "Carbs", "titr_pct": "Tight",
}


def _short(field):
    return HEATMAP_SHORT.get(field, daily.LABELS.get(field, field))


def _heatmap(matrix: dict, fields) -> str:
    """A diverging correlation heatmap. Values are shown IN the cells (not hover-gated, which
    fails on touch); a tap surfaces the full pair + n via the shared data-tip layer."""
    all_fields = matrix.get("fields", [])
    idx = {f: all_fields.index(f) for f in fields if f in all_fields}
    fields = [f for f in fields if f in idx]
    if len(fields) < 2:
        return '<p class="muted">Not enough overlapping data for a correlation heatmap yet.</p>'
    grid = matrix["grid"]
    head = '<div class="hm-cell hm-corner"></div>' + "".join(
        f'<div class="hm-cell hm-col">{_esc(_short(f))}</div>' for f in fields)
    rows = [f'<div class="hm-row">{head}</div>']
    for a in fields:
        cells = [f'<div class="hm-cell hm-rowlbl">{_esc(_short(a))}</div>']
        for b in fields:
            cell = grid[idx[a]][idx[b]]
            rho, nn = cell.get("rho"), cell.get("n")
            bg = _rho_color(rho)
            # show the value in-cell for anything non-trivial; keep tiny cells clean
            txt = "" if (rho is None or abs(rho) < 0.2) else f"{rho:+.1f}".replace("0.", ".")
            tip = f"{_short(a)} vs {_short(b)}: " + (
                f"rho={rho:+.2f} (n={nn})" if rho is not None else f"n={nn} (below floor)")
            cells.append(f'<div class="hm-cell" style="background:{bg}" '
                         f'data-tip="{_esc(tip)}">{_esc(txt)}</div>')
        rows.append(f'<div class="hm-row">{"".join(cells)}</div>')
    legend = ('<div class="hm-legend"><span>−1</span>'
              f'<i style="background:linear-gradient(90deg,{_NEG_HUE},{_NEUTRAL},{_POS_HUE})"></i>'
              '<span>+1</span> · rank correlation (Spearman), gray ≈ 0 · tap a cell for detail</div>')
    return (f'<div class="heatwrap"><div class="heatmap" style="--n:{len(fields)}">'
            + "".join(rows) + "</div></div>" + legend)


# ------------------------------------------------------------------------------------------
# cockpit assembly
# ------------------------------------------------------------------------------------------
def build_cockpit(*, metrics, trend_by_window, latest_glucose=None, wearables=None,
                  food_ranking=None, experiments=None, assessment=None,
                  insights_text="", quarantine=None, custom_cards=None,
                  heartbeat=None, daily_frame=None, correlation=None,
                  review_text="", labs=None, reversal=None, streaks=None,
                  patterns=None, coach=None, forecast=None, digest=None, now=None) -> dict:
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
        "review_text": review_text or "", "labs": labs or {}, "reversal": reversal or {},
        "streaks": streaks or [], "patterns": patterns or {}, "coach": coach or [],
        "forecast": forecast or {}, "digest": digest or {},
    }


def _latest(frame, field):
    for r in reversed(frame):
        if r.get(field) is not None:
            return r["date"], r[field]
    return None, None


# ------------------------------------------------------------------------------------------
# tabs
# ------------------------------------------------------------------------------------------
def _top_streak(c):
    """The most motivating live streak for the hero chip (prefer remission)."""
    order = {"remission": 0, "titr": 1, "tir": 2, "safe": 3, "steady": 4, "walk": 5, "logged": 6}
    active = [s for s in (c.get("streaks") or []) if s.get("current", 0) > 0]
    if not active:
        return None
    active.sort(key=lambda s: (order.get(s["key"], 9), -s["current"]))
    return active[0]


def _hero(c) -> str:
    m = c["metrics"]
    a = c["assessment"] or {}
    lad = (c.get("reversal") or {}).get("ladder") or {}

    def mv(k):
        return (m.get(k) or {}).get("value")

    grade = a.get("grade")
    headline = a.get("headline") or "Your metabolic snapshot"
    review = (c.get("review_text") or "").strip()
    lead = review.split(". ")[0].replace("Cross-stream associations (observational, not causal):", "").strip()
    lead = (lead[:2].upper() + lead[2:] + ".") if lead and not lead.endswith(".") else lead
    gr = f'<div class="hero-grade grade-{_esc(grade)}">{_esc(grade or "—")}</div>' if a else ""

    # The signature gauge: current GMI and how far it is from leaving the diabetic range.
    if lad.get("ok"):
        gmi = lad["current_gmi"]
        prog = max(0.0, min(100.0, lad.get("overall_progress", 0) * 100.0))
        nxt = lad.get("next")
        cap = (f'{nxt["mean_gap"]:.0f} mg/dL lower average to reach {_esc(nxt["label"])} '
               f'(GMI &lt;{nxt["gmi"]:g})' if nxt else 'You are in the non-diabetic range — hold it.')
        gauge = (
            f'<div class="hero-eyebrow">Estimated HbA1c · GMI</div>'
            f'<div class="hero-gmi"><span class="num" data-count="{gmi:.1f}">{gmi:.1f}</span>'
            f'<span class="hero-gmi-u">%</span></div>'
            f'<div class="gauge" role="img" aria-label="{prog:.0f} percent from diagnosis to non-diabetic">'
            f'<i style="--w:{prog:.0f}%"></i>'
            f'<span class="gauge-tick" style="left:{prog:.0f}%"></span></div>'
            f'<div class="hero-cap">{cap}</div>')
    else:
        score = a.get("score_pct") or 0
        gauge = (f'<div class="hero-eyebrow">Metabolic score</div>'
                 f'<div class="hero-gmi"><span class="num" data-count="{score:.0f}">{_fmt(score,0)}</span>'
                 f'<span class="hero-gmi-u">/100</span></div>'
                 f'<div class="gauge"><i style="--w:{max(0,min(100,score)):.0f}%"></i></div>')

    chips = []
    tir = mv("tir_pct")
    if tir is not None:
        chips.append(f'<div class="hero-metric"><span class="num">{_fmt(tir,0)}%</span>'
                     f'<label>Time in range</label></div>')
    mtb = mv("daily_metabolic_score")
    if mtb is not None:
        chips.append(f'<div class="hero-metric"><span class="num">{_fmt(mtb,0)}</span>'
                     f'<label>Metabolic score</label></div>')
    st = _top_streak(c)
    if st:
        chips.append(f'<div class="hero-metric hero-streak"><span class="num">🔥 {st["current"]}</span>'
                     f'<label>{_esc(st["label"])}</label></div>')

    return (f'<div class="hero">{gr}'
            f'<div class="hero-body">'
            f'<div class="hero-headline">{_esc(headline)}</div>'
            f'{gauge}'
            f'<div class="hero-metrics">{"".join(chips)}</div>'
            f'{f"<p class=hero-lead>{_esc(lead)}</p>" if lead else ""}</div></div>')


def _moves_card(c) -> str:
    """'Today's moves' — the prescriptive layer: 2-3 grounded actions, highest leverage first."""
    ms = c.get("coach") or []
    if not ms:
        return ""
    rows = "".join(
        f'<div class="move move-p{m.get("priority", 2)}">'
        f'<div class="move-impact">{_esc(m.get("impact"))}</div>'
        f'<div class="move-body"><div class="move-title">{_esc(m.get("title"))}</div>'
        f'<div class="move-detail">{_esc(m.get("detail"))}</div></div></div>' for m in ms)
    return ('<div class="moves"><div class="moves-h">Today\'s moves '
            '<span class="muted">· from your own data</span></div>'
            f'{rows}</div>')


def _today_tab(c) -> str:
    m = c["metrics"]
    frame = c["daily_frame"]

    def mv(k):
        return (m.get(k) or {}).get("value")

    def fresh(k):
        return _freshness_badge("fresh" if (m.get(k) or {}).get("valid") else "stale")

    tiles = []
    # glucose KPIs come from the clinical metrics (with real validity/freshness). GMI leads —
    # it is the "am I still in the diabetic range?" number for a reversal user.
    glucose_kpis = [("gmi_pct", "GMI (est. HbA1c)"), ("tir_pct", "Time in Range"),
                    ("titr_pct", "Tight Range (70-140)"), ("tbr_pct", "Below range (<70)"),
                    ("tar_pct", "Above range (>180)"), ("gri", "Glycemia Risk Index"),
                    ("cv_pct", "Variability (CV)"), ("mean_mgdl", "Mean glucose"),
                    ("mage_mgdl", "MAGE (swings)"), ("dawn_delta_mgdl", "Dawn rise")]
    for field, title in glucose_kpis:
        val = mv(field)
        unit = _UNITS.get(field, "")
        shown = "—" if val is None else f"{_disp(field, val)}{unit}"
        tiles.append(_tile(title, shown, source="clinical", freshness=fresh(field),
                           target=_TARGETS.get(field, ""), accent=_color_of(field)))
    # every other stream's most-recent day, straight from the unified daily frame, each with
    # its own true "as of" date (§A rule 6: slower streams show their real recency).
    other_kpis = ["steps", "sleep_total_min", "hr_avg", "weight_kg",
                  "mood", "energy", "symptom_count", "carbs_g"]
    for field in other_kpis:
        d, val = _latest(frame, field)
        if val is None:
            continue
        unit = _UNITS.get(field, "")
        gen_at = (c.get("header") or {}).get("generated_at")
        stale_days = 10 if field == "weight_kg" else 4  # weight moves slowly; don't cry stale
        tiles.append(_tile(daily.LABELS.get(field, field), f"{_disp(field, val)}{unit}",
                           source="daily",
                           freshness=_date_dot(d, gen_at, stale_days=stale_days),
                           accent=_color_of(field)))
    return _hero(c) + _moves_card(c) + '<div class="grid">' + "".join(tiles) + "</div>"


def _status_dot(status) -> str:
    c = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])
    return f'<span class="sdot" style="background:{c}" title="{_esc(status)}"></span>'


def _ladder_html(lad) -> str:
    if not lad.get("ok"):
        return '<p class="muted">Not enough glucose data yet to place you on the remission ladder.</p>'
    prog = lad["overall_progress"] * 100.0
    rungs = "".join(
        f'<div class="rung {"done" if r["achieved"] else ""}">'
        f'<span class="rk">{"✓" if r["achieved"] else "○"}</span>'
        f'<span class="rl">GMI &lt;{r["gmi"]:g} — {_esc(r["label"])}</span>'
        f'<span class="rm">mean ≤{r["mean"]:g}</span></div>' for r in lad["rungs"])
    nxt = lad.get("next")
    if nxt:
        goal = (f'<p class="rev-next">Next: bring your average down <b>{nxt["mean_gap"]:.0f} mg/dL</b> '
                f'(to ≤{nxt["mean_needed"]:g}) to reach <b>GMI &lt;{nxt["gmi"]:g}</b> — '
                f'{_esc(nxt["label"])}.</p>')
    else:
        goal = '<p class="rev-next good">🎉 You are in the non-diabetic GMI range — hold it.</p>'
    return (f'<div class="rev-hero"><div class="rev-gmi"><span>{lad["current_gmi"]:.1f}</span>'
            f'<label>current GMI (est. HbA1c)</label>'
            f'<div class="muted">mean {lad["current_mean"]:.0f} mg/dL</div></div>'
            f'<div class="rev-prog"><div class="score-bar big"><i style="width:{prog:.0f}%"></i></div>'
            f'<div class="muted">{prog:.0f}% of the way from diagnosis (7.5) to non-diabetic (5.7)</div>'
            f'{goal}</div></div><div class="rungs">{rungs}</div>')


def _projection_html(proj) -> str:
    if not proj.get("ok"):
        return (f'<p class="muted">A 90-day GMI projection needs ~2 weeks of daily data '
                f'(have {proj.get("n", 0)}).</p>')
    arrow = {"improving": "↓", "worsening": "↑", "flat": "→"}.get(proj["direction"], "→")
    cls = {"improving": "good", "worsening": "bad", "flat": ""}.get(proj["direction"], "")
    return (f'<div class="proj {cls}"><div class="proj-main">{arrow} at your current '
            f'{proj["horizon_days"]}-day trend, GMI projects to <b>{proj["projected_gmi"]:.1f}%</b> '
            f'(mean ≈ {proj["projected_mean"]:.0f} mg/dL)</div>'
            f'<div class="muted">from {proj["current_gmi"]:.1f}% now · '
            f'{proj["slope_per_month"]:+.1f} mg/dL per month · fit r²={proj["r2"]:.2f} · '
            f'n={proj["n"]} days · a trajectory, not a promise</div></div>')


def _doctor_html(items) -> str:
    if not items:
        return ""
    rows = "".join(
        f'<div class="doc-item p{it.get("priority", 2)}">'
        f'<span class="doc-area">{_esc(it.get("area"))}</span>'
        f'<div><div class="doc-text">{_esc(it.get("text"))}</div>'
        f'<div class="muted">{_esc(it.get("based_on"))}</div></div></div>' for it in items)
    return ('<h3 class="sec">For your doctor</h3>'
            '<p class="muted">This is a behaviour/analytics tool, not medical advice — bring '
            'these to a physician.</p>'
            f'<div class="doc-list">{rows}</div>')


def _lab_status_word(m) -> str:
    """A text cue so status is never colour-alone: high / low / off / ok."""
    st = m["status"]
    if st == "good":
        return "ok"
    if st == "unknown":
        return "—"
    return {"low_good": "high", "high_good": "low"}.get(m["direction"], "off")


def _lab_row(m) -> str:
    delta = m.get("delta")
    darrow = ""
    if delta is not None and abs(delta) > 1e-9:
        opt = m.get("optimal")
        if opt is not None:   # "better" = moved closer to the optimal target (works for any direction)
            good = abs(m["value"] - opt) < abs((m["value"] - delta) - opt)
        else:
            good = ((m["direction"] == "low_good" and delta < 0)
                    or (m["direction"] == "high_good" and delta > 0))
        darrow = (f'<span class="ldelta {"good" if good else "bad"}">'
                  f'{"▼" if delta < 0 else "▲"} {abs(delta):g}</span>')
    scolor = STATUS_COLORS.get(m["status"], STATUS_COLORS["unknown"])
    # neutral trend line, status only on the latest point (never paint the whole history "red")
    spark = _sparkline(list(m.get("trend", [])), "#475569", dot_color=scolor)
    word = _lab_status_word(m)
    tip = f'{m["name"]}: {m["value"]:g} {m.get("unit") or ""} (ref {m["ref"]}) — {m["status"]}'
    age = f'· {_esc(m["age"])} ago' if m.get("age") else ""
    return (f'<div class="lab" data-tip="{_esc(tip)}">{_status_dot(m["status"])}'
            f'<span class="lab-name">{_esc(m["name"])}</span>'
            f'<span class="lab-val">{m["value"]:g}<small> {_esc(m["unit"])}</small></span>'
            f'<span class="lstat" style="color:{scolor}">{_esc(word)}</span>'
            f'<span class="lab-ref">ref {_esc(m["ref"])}</span>{darrow}'
            f'<span class="lab-spark">{spark}</span>'
            f'<span class="muted lab-age">{_esc(m["ts"][:10])} {age}</span></div>')


def _labs_html(labs) -> str:
    groups = labs.get("groups", {})
    if not groups:
        return ('<h3 class="sec">Labs</h3><p class="muted">No labs entered yet. Add your panel '
                '(HbA1c, ALT/AST/GGT, hs-CRP, testosterone, prolactin, vitamin D…) to track liver, '
                'inflammation and hormones alongside glucose.</p>')
    out = ['<h3 class="sec">Labs — liver · inflammation · hormones · micronutrients</h3>']
    ncrit = len(labs.get("critical", []))
    if ncrit:
        out.append(f'<p class="muted">{ncrit} marker(s) out of range (red) — see "For your doctor".</p>')
    for group, markers in groups.items():
        rows = "".join(_lab_row(m) for m in markers)
        out.append(f'<div class="lab-group"><div class="lg-title">{_esc(group)}</div>{rows}</div>')
    derived = labs.get("derived") or []
    if derived:
        chips = "".join(
            f'<div class="ins-card"><div class="ins-title">{_esc(d["name"])} '
            f'<span style="color:{STATUS_COLORS.get(d["status"], "#8a99b0")}">●</span></div>'
            f'<div class="ins-big num">{d["value"]:g}{_esc(d.get("unit") or "")}</div>'
            f'<div class="ins-detail">{_esc(d.get("note"))}</div></div>' for d in derived)
        out.append('<h3 class="sec">Derived liver scores</h3>'
                   f'<div class="insight-grid">{chips}</div>')
    return "".join(out)


def _weight_html(w) -> str:
    if not w or not w.get("ok"):
        return ""
    bmi = f' · BMI {w["bmi"]:g}' if w.get("bmi") is not None else ""
    return (f'<h3 class="sec">Weight — your strongest lever</h3>'
            f'<div class="rev-hero"><div class="rev-gmi"><span class="num">{w["kg_lost"]:+.1f}</span>'
            f'<label>kg from baseline ({w["baseline_kg"]:g} → {w["current_kg"]:g} kg{bmi})</label></div>'
            f'<div class="rev-prog"><div class="score-bar big">'
            f'<i style="width:{min(100, w["remission_band_pct"]):.0f}%;background:#199e70"></i></div>'
            f'<div class="muted">DiRECT remission likelihood at this loss ≈ '
            f'<b>{w["remission_band_pct"]}%</b> · {_esc(w["note"])}</div></div></div>')


def _reconcile_html(rc) -> str:
    if not rc or not rc.get("ok"):
        return ""
    cls = "bad" if rc.get("discordant") else "good"
    return (f'<div class="proj {cls}"><div class="proj-main">CGM estimate (GMI '
            f'<b>{rc["gmi_pct"]:.1f}%</b>) vs lab HbA1c (<b>{rc["lab_hba1c_pct"]:.1f}%</b>) — '
            f'gap {rc["gap_pct"]:+.1f}%</div><div class="muted">{_esc(rc["note"])}</div></div>')


def _reversal_tab(c) -> str:
    rev = c.get("reversal") or {}
    labs = c.get("labs") or {}
    if not rev and not labs:
        return ('<p class="muted">The reversal view lights up once glucose (and, ideally, your '
                'lab panel) are flowing — it shows your GMI remission ladder, a 90-day projection, '
                'your liver/inflammation/hormone labs, and the short list for your doctor.</p>')
    parts = ['<h3 class="sec">Remission ladder</h3>', _ladder_html(rev.get("ladder", {})),
             _reconcile_html(rev.get("reconcile", {})),
             _weight_html(rev.get("weight", {})),
             '<h3 class="sec">90-day GMI projection</h3>', _projection_html(rev.get("projection", {})),
             _doctor_html(rev.get("doctor_list", [])), _labs_html(labs)]
    return "".join(parts)


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
            d_last, last = _latest(frame, field)
            unit = _UNITS.get(field, "")
            cards.append(
                f'<div class="spark-card">'
                f'<div class="spark-head"><span class="dot" style="background:{color}"></span>'
                f'<span class="spark-lbl">{_esc(daily.LABELS.get(field, field))}</span>'
                f'<span class="spark-val">{_disp(field, last)}{_esc(unit)}</span></div>'
                f'{_sparkline(pts, color)}'
                f'<div class="spark-foot">{n} days · as of {_esc(d_last)}</div></div>')
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
    ci = f.get("ci")
    ci_html = (f'<span class="corr-ci">95% CI {ci[0]:+.2f}…{ci[1]:+.2f}</span>'
               if ci and ci[0] is not None else "")
    sig = ('<span class="pill pill-sig">significant</span>' if f.get("significant")
           else '<span class="pill pill-exp">exploratory</span>')
    return (f'<div class="corr-card">'
            f'<div class="corr-r" style="background:{chip}">{arrow} {r:+.2f}</div>'
            f'<div class="corr-body"><div class="corr-lbl">{_esc(f.get("label"))}</div>'
            f'<div class="corr-meta"><span class="pill">{_esc(kind)}</span>{sig}'
            f'<span class="corr-n">{_esc(f.get("detail"))}</span>{ci_html}</div></div></div>')


def _level_color(v):
    """Glucose level -> in-range green / high amber / very-high red (for daypart bars)."""
    if v is None:
        return "#334155"
    if v <= 140:
        return "#2ea043"
    if v <= 180:
        return "#d9a021"
    return "#e5484d"


def _patterns_tab(c) -> str:
    p = c.get("patterns") or {}
    tod, dawn, wk, tr = (p.get("time_of_day") or {}), (p.get("dawn") or {}), \
        (p.get("weekday") or {}), (p.get("trend") or {})
    if not tod.get("parts") and not tr.get("ok"):
        return ('<p class="muted">Patterns emerge once a couple of weeks of glucose are in. '
                'They show when your day runs high, whether dawn is driving the morning, and the '
                'week your control turned.</p>')
    out = []
    # trend banner — the single most motivating pattern
    if tr.get("ok"):
        dirn = tr["direction"]
        cls = {"improving": "good", "worsening": "bad", "flat": ""}.get(dirn, "")
        arrow = {"improving": "↓", "worsening": "↑", "flat": "→"}.get(dirn, "→")
        drop = (f' The biggest single-week improvement was the week of {_esc(tr["biggest_drop_week"])} '
                f'({tr["biggest_weekly_drop_mgdl"]:+.0f} mg/dL).' if tr.get("biggest_drop_week") else "")
        out.append(
            f'<div class="proj {cls}"><div class="proj-main">{arrow} Average glucose is '
            f'<b>{dirn}</b> — {tr["overall_change_mgdl"]:+.0f} mg/dL over {tr["n_weeks"]} weeks.</div>'
            f'<div class="muted">{drop.strip() or "Keep the streak going."}</div></div>')
    # time-of-day bars (typical day)
    parts = tod.get("parts") or []
    if parts:
        mx = max((p2["median_mgdl"] for p2 in parts), default=1) or 1
        worst = (tod.get("worst") or {}).get("window")
        rows = "".join(
            f'<div class="tod-row"><span class="tod-lbl">{_esc(p2["window"])}'
            f'<small>{_esc(p2["hours"])}</small></span>'
            f'<span class="tod-bar"><i style="width:{100*p2["median_mgdl"]/mx:.0f}%;'
            f'background:{_level_color(p2["median_mgdl"])}"></i></span>'
            f'<span class="tod-val">{p2["median_mgdl"]:.0f}'
            f'{" ◂ worst" if p2["window"] == worst else ""}</span></div>' for p2 in parts)
        out.append('<h3 class="sec">Your typical day</h3>'
                   '<p class="muted">Median glucose by part of day (Asia/Dubai).</p>'
                   f'<div class="tod">{rows}</div>')
    # dawn + weekday insight cards
    cards = []
    if dawn.get("delta_mgdl") is not None:
        d = dawn["delta_mgdl"]
        verdict = ("a clear dawn phenomenon" if dawn.get("present")
                   else "little dawn effect")
        cards.append(_insight_card("Dawn effect", f"{d:+.0f} mg/dL",
                     f"04:00–08:00 runs {d:+.0f} vs the small hours — {verdict}."))
    if wk.get("ok"):
        wm = wk.get("weekend_minus_weekday")
        hi, lo = wk.get("highest", {}), wk.get("lowest", {})
        detail = (f'Weekends run {wm:+.0f} mg/dL vs weekdays. ' if wm is not None else "")
        detail += f'Highest {hi.get("day")} ({hi.get("mean_mgdl"):.0f}), lowest {lo.get("day")} ({lo.get("mean_mgdl"):.0f}).'
        cards.append(_insight_card("Day of week", (f"{wm:+.0f} mg/dL" if wm is not None else hi.get("day", "—")), detail))
    if cards:
        out.append('<h3 class="sec">Signals</h3><div class="insight-grid">' + "".join(cards) + "</div>")
    return "".join(out)


def _insight_card(title, big, detail) -> str:
    return (f'<div class="ins-card"><div class="ins-title">{_esc(title)}</div>'
            f'<div class="ins-big num">{_esc(big)}</div>'
            f'<div class="ins-detail">{_esc(detail)}</div></div>')


def _correlations_tab(c) -> str:
    corr = c["correlation"]
    findings = corr.get("findings", [])
    if not findings and not corr.get("matrix"):
        return ('<p class="muted">Correlations need several days across multiple streams. '
                'Once glucose, sleep, activity and your journal overlap, associations appear here.</p>')
    nsig = corr.get("n_significant", 0)
    note = ('<p class="muted">Associations across everything you log — <b>observational, not '
            'causal</b>. Each carries a 95% confidence interval; '
            f'<b>{nsig}</b> survive multiple-comparison control (Benjamini-Hochberg, FDR 10%) '
            'and are marked <b>significant</b> — the rest are <b>exploratory</b> leads.</p>')
    cards = ('<div class="corr-cards">' + "".join(_corr_card(f) for f in findings) + "</div>"
             if findings else '<p class="muted">No association cleared the evidence floor yet.</p>')
    heat = _heatmap(corr["matrix"], HEATMAP_FIELDS) if corr.get("matrix") else ""
    return note + cards + ('<h3 class="sec">Correlation heatmap</h3>' + heat if heat else "")


def _analytics_tab(c) -> str:
    windows = sorted((int(w) for w in c["trend_by_window"].keys()))
    if not windows:
        return "<p>No trend data.</p>"
    buttons = "".join(
        f'<button class="winbtn {"active" if i == 0 else ""}" onclick="showWindow({w})" '
        f'id="winbtn-{w}">{w}d</button>' for i, w in enumerate(windows))
    agp_legend = ('<div class="agp-head"><span class="agp-title">AGP — Ambulatory Glucose '
                  'Profile <span class="muted">(a typical day, Asia/Dubai)</span></span>'
                  '<span class="agp-legend"><i class="sw sw-band1"></i>10–90% '
                  '<i class="sw sw-band2"></i>25–75% <i class="sw sw-med"></i>median '
                  '<i class="sw sw-tgt"></i>70–180 target</span></div>')
    panels = []
    for i, w in enumerate(windows):
        tr = c["trend_by_window"][w] if w in c["trend_by_window"] else c["trend_by_window"][str(w)]
        s = tr.get("summary", {})
        asof = _esc((tr.get("generated_at") or "")[:10])
        summary = "".join(
            _tile(daily.LABELS.get(k, k), f"{_disp(k, s.get(k))}{_UNITS.get(k, '')}",
                  source="analytics", freshness=f"as of {asof}" if asof else "",
                  target=_TARGETS.get(k, ""), sub=f"{w}d window", accent=_color_of(k))
            for k in ("mean_mgdl", "tir_pct", "titr_pct", "cv_pct", "gri") if k in s)
        style = "" if i == 0 else ' style="display:none"'
        panels.append(
            f'<div class="winpanel" id="winpanel-{w}"{style}>'
            f'<div class="muted">{_esc(tr.get("n_readings"))} readings · {w}-day window</div>'
            f'{agp_legend}{_agp_svg(tr.get("agp", []))}<div class="grid">{summary}</div></div>')
    return f'<div class="winbar">{buttons}</div>' + "".join(panels)


def _empty(title, body) -> str:
    """A designed empty state (ghost card), not a stranded line of gray text."""
    return (f'<div class="empty"><div class="empty-glyph">◒</div>'
            f'<div class="empty-title">{_esc(title)}</div>'
            f'<div class="empty-body">{_esc(body)}</div></div>')


def _forecast_calc(c) -> str:
    """An interactive 'will this spike?' calculator — arithmetic on Python-computed coefficients
    (each food's per-gram response, the user's walk effect, their baseline), so it stays grounded."""
    model = c.get("forecast") or {}
    foods = model.get("foods") or []
    if not foods:
        return ""
    opts = "".join(f'<option value="{i}">{_esc(f["item"])}</option>' for i, f in enumerate(foods))
    walk_txt = (f" · a post-meal walk trims ~{abs(model['walk_effect']):.0f} mg/dL"
                if model.get("walk_effect") is not None else "")
    walk_row = ('<label class="fc-walk"><input type="checkbox" id="fcWalk" '
                'onchange="fcCompute()"> Walk after</label>'
                if model.get("walk_effect") is not None else "")
    return (
        '<h3 class="sec">Will it spike?</h3>'
        f'<p class="muted">A projection from your own measured responses{walk_txt}. '
        'Not a promise — your real curve varies.</p>'
        '<div class="fc"><div class="fc-controls">'
        f'<select id="fcFood" onchange="fcCompute()">{opts}</select>'
        '<label class="fc-carbs">net carbs '
        '<input type="number" id="fcCarbs" value="40" min="0" max="300" step="5" '
        'inputmode="numeric" oninput="fcCompute()"></label>'
        f'{walk_row}</div>'
        '<div class="fc-out"><div><span class="num" id="fcPeak">—</span>'
        '<label>projected peak</label></div>'
        '<div class="fc-verdict" id="fcVerdict"></div></div></div>')


def _food_tab(c) -> str:
    foods = c["food_ranking"]
    if not foods:
        return _empty("No foods ranked yet",
                      "Log a few meals with net carbs and a tag. Once a food has 3+ logged "
                      "meals, its glucose impact (peak rise, iAUC, time-to-peak) appears here, "
                      "worst-to-best.")
    mx = max((abs(f.get("mean_delta_peak_mgdl") or 0) for f in foods), default=1) or 1
    cards = []
    for f in foods:
        dp = f.get("mean_delta_peak_mgdl")
        col = _level_color((dp or 0) + 90)   # map Δpeak onto the green/amber/red scale
        cards.append(
            f'<div class="frow"><div class="frow-top"><span class="frow-name">{_esc(f["item"])}</span>'
            f'<span class="frow-dp num">+{_fmt(dp,0)}<small> mg/dL</small></span></div>'
            f'<div class="frow-bar"><i style="width:{100*abs(dp or 0)/mx:.0f}%;background:{col}"></i></div>'
            f'<div class="frow-foot">iAUC {_fmt(f.get("mean_iauc_120"),0)} · '
            f'{_esc(f.get("n"))} meals</div></div>')
    return (_forecast_calc(c)
            + '<h3 class="sec">Your foods, ranked</h3>'
            '<p class="muted">By average glucose peak — worst first.</p>'
            '<div class="frows">' + "".join(cards) + "</div>")


def _experiments_tab(c) -> str:
    exps = c["experiments"]
    if not exps:
        return _empty("No interventions compared yet",
                      "Tag meals with what you tried (+walk, +acv, +methi, veg-first…). The "
                      "engine matches tagged vs untagged meals and reports the effect with its "
                      "sample size — never labelling anything causal below the evidence floor.")
    cards = []
    for e in exps:
        eff = e.get("effect_abs")
        good = eff is not None and eff < 0
        sign = "good" if good else ("bad" if (eff or 0) > 0 else "")
        cards.append(
            f'<div class="frow"><div class="frow-top">'
            f'<span class="frow-name">{_esc(e["tag"])}</span>'
            f'<span class="frow-dp num {sign}">{_fmt(eff,0)}<small> mg/dL '
            f'({_fmt(e.get("effect_pct"),0)}%)</small></span></div>'
            f'<div class="frow-foot"><span class="pill">{_esc(e.get("signal_strength"))}</span> '
            f'<span class="pill">{_esc(e.get("causal_label"))}</span> '
            f'n {_esc(e.get("n_treated"))} vs {_esc(e.get("n_control"))}</div></div>')
    return ('<p class="muted">Matched-meal interventions (tagged vs untagged) — negative = '
            'blunts the peak. Never called causal below the evidence floor.</p>'
            '<div class="frows">' + "".join(cards) + "</div>")


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


def _digest_html(c) -> str:
    dig = c.get("digest") or {}
    if not dig.get("ok"):
        return ""
    import digest as _dig
    rows = "".join(
        f'<div class="dg-row"><span class="dg-lbl">{_esc(r["label"])}</span>'
        f'<span class="dg-vals"><b class="num">{r["this"]:g}</b>'
        f'<span class="muted"> vs {r["prior"]:g}</span></span>'
        f'<span class="dg-delta {"good" if r["better"] else ("bad" if r["better"] is False else "")}">'
        f'{r["delta"]:+g}{_esc(r["unit"])}</span></div>' for r in dig["rows"])
    return (f'<h3 class="sec">This week vs last</h3>'
            f'<p class="insight" style="margin-bottom:10px">{_esc(_dig.headline(dig))}</p>'
            f'<div class="dg">{rows}</div>')


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
    return head + _digest_html(c) + lines_html + corr_html + ins_html + _coverage_view(c)


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


_TOKENS = """
:root{
  --font-display:'Fraunces',Georgia,'Times New Roman',serif;
  --font-body:'Geist',-apple-system,'Segoe UI',Roboto,system-ui,sans-serif;
  --font-mono:'Geist Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace;
  --blue:#3987e5;--good:#2ea043;--warn:#d9a021;--crit:#e5484d;
}
"""

_CSS = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{font-family:var(--font-body);font-feature-settings:'ss01','cv01';margin:0;
background:#0a0e16;color:#eef2f9;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
padding-bottom:env(safe-area-inset-bottom)}
.num,.t-value,.mono,.hdr-glucose,.spark-val,.lab-val,.hero-metric span,.hero-score span,
.rev-gmi span,tspan{font-variant-numeric:tabular-nums lining-nums}
h1,h2,.hero-headline,.hero-grade,.rev-gmi span,.hero-metric span{font-family:var(--font-display)}
.t-value,.hero-score span,.corr-r,.lab-val,.spark-val,.hdr-glucose,.bar-val{font-family:var(--font-mono)}
.t-title,.lg-title,.sub,.doc-area,.spark-foot,.card-filter,.t-foot{font-family:var(--font-mono);
letter-spacing:.02em}
/* one sticky block so the nav can never slide underneath the header */
.topbar{position:sticky;top:0;z-index:6;background:#0a0e16e6;backdrop-filter:blur(10px);
border-bottom:1px solid #1e2636;padding-top:env(safe-area-inset-top)}
main{padding-left:max(16px,env(safe-area-inset-left));padding-right:max(16px,env(safe-area-inset-right))}
header{position:relative;padding:10px 16px}.hdr-glucose{font-size:22px;font-weight:700}
.hdr-age{color:#9aa8bd;font-size:13px}
.refresh{position:absolute;top:10px;right:12px;background:#1f2937;color:#c3d0e2;border:0;
width:36px;height:36px;border-radius:10px;font-size:18px;cursor:pointer}
.navwrap{position:relative}
.navwrap::after{content:"";position:absolute;top:0;right:0;width:26px;height:100%;
background:linear-gradient(90deg,transparent,#0a0e16);pointer-events:none}
nav{display:flex;gap:4px;padding:6px 12px;overflow-x:auto;scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
nav button{background:#1f2937;color:#c3d0e2;border:0;padding:0 14px;min-height:44px;border-radius:8px;
cursor:pointer;white-space:nowrap;font-size:14px}nav button.active{background:#1f5fbf;color:#fff}
nav button:focus-visible{outline:2px solid #3987e5;outline-offset:2px}
#hhtip{position:fixed;display:none;z-index:20;max-width:180px;background:#1f2937;color:#eef2f9;
border:1px solid #334155;border-radius:8px;padding:6px 9px;font-size:12px;pointer-events:none}
main{padding:16px;max-width:1100px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.tile{background:#111725;border:1px solid #1f2937;border-radius:14px;padding:12px}
/* elevation/light model: top-lit gradient surfaces + ambient depth so cards float */
.tile,.spark-card,.ins-card,.corr-card,.card,.lab-group,.moves,.proj,.doc-item,.rung,.ins-card{
background-image:linear-gradient(180deg,#161d2e 0%,#111725 60%);
box-shadow:inset 0 1px 0 rgba(255,255,255,.045),0 1px 2px rgba(0,0,0,.35),0 10px 26px rgba(0,0,0,.20)}
.tile,.corr-card,nav button,.winbtn,.refresh,.xbtn{transition:transform .14s ease,
background-color .14s ease,border-color .14s ease,box-shadow .14s ease}
.tile:active,.corr-card:active{transform:scale(.992)}
nav button:hover,.winbtn:hover{background-color:#26324a}
.tab.active{animation:fadein .22s ease both}
@keyframes fadein{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
.t-title{font-size:12px;color:#9aa8bd}.t-value{font-size:24px;font-weight:700;margin:4px 0}
.t-sub{font-size:12px;color:#c3d0e2}.t-foot{font-size:12px;color:#8091a7;margin-top:6px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #1f2937}
h2{margin:4px 0 14px;font-size:20px}h3.sec{margin:22px 0 10px;font-size:15px;color:#c3d0e2;
border-left:3px solid #3987e5;padding-left:8px}
.hero{display:flex;gap:18px;background:
radial-gradient(120% 140% at 100% 0%,#17223a 0%,#0e1420 55%),linear-gradient(#0e1420,#0e1420);
border:1px solid #24304a;border-radius:20px;padding:20px;margin-bottom:16px;align-items:flex-start;
position:relative;overflow:hidden;animation:rise .5s ease both}
.hero::after{content:"";position:absolute;inset:0;pointer-events:none;
box-shadow:inset 0 1px 0 #ffffff12,inset 0 0 60px #3987e508}
.hero-grade{font-size:46px;font-weight:600;width:76px;height:76px;border-radius:18px;display:flex;
align-items:center;justify-content:center;background:#0a0e16;border:1px solid #24304a;flex:0 0 auto;
line-height:1}
.hero-body{flex:1;min-width:0}
.hero-eyebrow{font-family:var(--font-mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase;
color:#8a99b0;margin-top:2px}
.hero-headline{font-size:19px;font-weight:600;line-height:1.15;letter-spacing:-.01em;
text-wrap:balance;margin:0 0 10px}
.hero-gmi{display:flex;align-items:baseline;gap:6px;line-height:.9}
.hero-gmi .num{font-family:var(--font-display);font-size:52px;font-weight:600;letter-spacing:-.02em;color:#eaf1fb}
.hero-gmi-u{font-family:var(--font-mono);font-size:18px;color:#9aa8bd}
.gauge{position:relative;height:9px;background:#0a0e16;border:1px solid #223049;border-radius:6px;
overflow:hidden;margin:12px 0 6px}
.gauge i{display:block;height:100%;width:var(--w);border-radius:6px;
background:linear-gradient(90deg,#e5843f,#d9a021 45%,#2ea043);animation:fill .9s cubic-bezier(.2,.8,.2,1) both}
.gauge-tick{position:absolute;top:-3px;width:2px;height:15px;background:#eaf1fb;border-radius:2px;transform:translateX(-1px)}
.hero-cap{font-size:12.5px;color:#9aa8bd}
.hero-metrics{display:flex;gap:22px;margin-top:14px;flex-wrap:wrap}
.hero-metric .num{font-size:22px;font-weight:600}
.hero-metric label{display:block;font-family:var(--font-mono);font-size:10.5px;letter-spacing:.04em;
text-transform:uppercase;color:#8a99b0;margin-top:2px}
.hero-streak .num{color:#e5843f}
.hero-lead{color:#c3d0e2;font-size:13.5px;line-height:1.5;margin:12px 0 0;
border-top:1px solid #1e2636;padding-top:10px}
.grade-A{color:#2ea043}.grade-B{color:#199e70}.grade-C{color:#d9a021}.grade-D{color:#e5843f}.grade-F{color:#e5484d}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes fill{from{width:0}}
@media(prefers-reduced-motion:reduce){.hero,.gauge i{animation:none}}
.winbar{margin-bottom:10px}.winbtn{background:#1f2937;color:#c3d0e2;border:0;margin-right:4px;
padding:6px 10px;border-radius:8px;cursor:pointer}.winbtn.active{background:#1f5fbf;color:#fff}
.agp{width:100%;height:auto;background:#0b111d;border-radius:10px;margin:6px 0}
.agp-head{display:flex;flex-wrap:wrap;justify-content:space-between;gap:6px;align-items:baseline;margin-top:8px}
.agp-title{font-weight:600;font-size:13px}
.agp-legend{font-size:11px;color:#8091a7;display:flex;align-items:center;gap:4px;flex-wrap:wrap}
.sw{display:inline-block;width:12px;height:10px;border-radius:2px;vertical-align:middle;margin-left:6px}
.sw-band1{background:#3987e533}.sw-band2{background:#3987e566}
.sw-med{background:#3987e5;height:3px}.sw-tgt{background:#19875440;border:1px dashed #199e70}
.muted{color:#9aa8bd;font-size:13px}.warn{color:#fab219}.insight{color:#c3d0e2;line-height:1.6;
background:#111725;border:1px solid #1f2937;border-radius:12px;padding:12px}
.tab{display:none}.tab.active{display:block}
pre{white-space:pre-wrap;background:#0b111d;padding:8px;border-radius:8px}
.sparks{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.spark-card{background:#111725;border:1px solid #1f2937;border-radius:14px;padding:10px 12px}
.spark-head{display:flex;align-items:center;gap:6px;font-size:13px;margin-bottom:2px}
.spark-head .dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
.spark-lbl{font-weight:600;color:#eef2f9}.spark-val{margin-left:auto;font-weight:700;color:#eef2f9}
.spark{width:100%;height:48px;display:block}.spark-foot{font-size:12px;color:#8091a7;margin-top:2px}
.corr-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
.corr-card{display:flex;gap:12px;background:#111725;border:1px solid #1f2937;border-radius:14px;padding:10px 12px}
.corr-r{flex:0 0 auto;align-self:flex-start;font-weight:700;color:#fff;border-radius:10px;padding:6px 10px;font-size:13px}
.corr-lbl{font-weight:600;line-height:1.35}.corr-meta{display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap}
.pill{background:#1f2937;color:#c3d0e2;border-radius:20px;padding:2px 9px;font-size:11px}
.corr-n{color:#8091a7;font-size:12px}
.corr-ci{color:#8a99b0;font-size:11px;font-family:var(--font-mono)}
.pill-sig{background:#12351f;color:#5bd47e;border:1px solid #2ea04355}
.pill-exp{background:#241a10;color:#e0b365;border:1px solid #d9a02144}
.heatwrap{position:relative;overflow-x:auto;padding-bottom:4px}
.heatwrap::after{content:"";position:absolute;top:0;right:0;width:22px;height:100%;
background:linear-gradient(90deg,transparent,#0a0e16);pointer-events:none}
.heatmap{display:grid;grid-template-columns:92px repeat(var(--n),minmax(30px,1fr));gap:3px;min-width:min-content}
.hm-row{display:contents}
.hm-cell{aspect-ratio:1;display:flex;align-items:center;justify-content:center;font-size:11px;
color:#eef2f9;border-radius:4px;min-width:30px;cursor:pointer;font-variant-numeric:tabular-nums;
box-shadow:inset 0 0 0 1px rgba(255,255,255,.03)}
.hm-corner{background:transparent;box-shadow:none}
.hm-col{background:transparent;color:#9aa8bd;font-size:11px;aspect-ratio:auto;align-items:flex-end;
white-space:nowrap;line-height:1.05;box-shadow:none}
.hm-rowlbl{background:transparent;color:#c3d0e2;justify-content:flex-end;padding-right:6px;font-size:11px;
aspect-ratio:auto;white-space:nowrap}
.hm-legend{display:flex;align-items:center;gap:8px;margin-top:10px;color:#9aa8bd;font-size:12px}
.hm-legend i{display:inline-block;width:120px;height:10px;border-radius:5px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.card{background:#111725;border:1px solid #1f2937;border-radius:14px;padding:12px}
.card-title{font-weight:700}.card-filter{font-size:12px;color:#8091a7;margin:4px 0 8px}
.bar-row{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}
.bar-lbl{flex:0 0 130px;color:#c3d0e2}.bar{flex:1;background:#0b111d;border-radius:6px;height:12px;overflow:hidden}
.bar i{display:block;height:100%;background:#3987e5}.bar-val{flex:0 0 56px;text-align:right;color:#eef2f9}
.cov .bar-lbl{flex:0 0 150px}
.card-assess{font-size:12px;color:#a5b4fc;margin-top:8px}
.review-head{display:flex;gap:14px;align-items:center;margin-bottom:8px}
.rgrade{font-size:34px;font-weight:800;width:56px;height:56px;border-radius:14px;display:flex;
align-items:center;justify-content:center;background:#111725}
.review-lines{line-height:1.7}
.heartbeat{margin-top:6px;font-size:12px;display:flex;flex-wrap:nowrap;overflow-x:auto;gap:6px;
align-items:center;scrollbar-width:none}.heartbeat::-webkit-scrollbar{display:none}
.heartbeat.hb-bad{color:#fab219}.heartbeat.hb-ok{color:#199e70}
.hb{white-space:nowrap}
.hb{background:#1f2937;border-radius:10px;padding:2px 8px;color:#c3d0e2}
.hb-fresh{border:1px solid #199e70}.hb-stale{border:1px solid #fab219;color:#fbbf24}
.hb-down,.hb-no_data,.hb-future{border:1px solid #d03b3b;color:#fca5a5}
.xbtn{display:inline-block;background:#3987e5;color:#fff;padding:10px 16px;border-radius:10px;text-decoration:none;font-weight:700}
.moves{margin:0 0 16px;background:#0e1524;border:1px solid #223049;border-radius:16px;padding:14px 16px}
.moves-h{font-family:var(--font-display);font-size:16px;margin-bottom:10px}
.move{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-top:1px solid #182238}
.move:first-of-type{border-top:0}
.move-impact{flex:0 0 auto;min-width:74px;font-family:var(--font-mono);font-size:12px;font-weight:600;
color:#eaf1fb;background:#16233b;border:1px solid #263a5c;border-radius:8px;padding:6px 8px;text-align:center}
.move-p1 .move-impact{border-color:#2ea04366;color:#7ee0a0}
.move-title{font-weight:600;font-size:14.5px;margin-bottom:2px}
.move-detail{font-size:13px;color:#c3d0e2;line-height:1.5}
.tod{display:flex;flex-direction:column;gap:8px;margin:6px 0}
.tod-row{display:flex;align-items:center;gap:10px;font-size:13px}
.tod-lbl{flex:0 0 120px;display:flex;flex-direction:column}.tod-lbl small{color:#8a99b0;font-family:var(--font-mono);font-size:10px}
.tod-bar{flex:1;height:14px;background:#0a0e16;border:1px solid #1e2636;border-radius:7px;overflow:hidden}
.tod-bar i{display:block;height:100%;border-radius:7px}
.tod-val{flex:0 0 84px;text-align:right;font-family:var(--font-mono);font-weight:600}
.frows{display:flex;flex-direction:column;gap:10px}
.frow{background-image:linear-gradient(180deg,#161d2e,#111725);border:1px solid #1f2937;
border-radius:12px;padding:11px 13px}
.frow-top{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.frow-name{font-weight:600;font-size:14.5px}
.frow-dp{font-family:var(--font-mono);font-weight:600}.frow-dp small{color:#8a99b0;font-weight:400}
.frow-dp.good{color:#5bd47e}.frow-dp.bad{color:#f0a072}
.frow-bar{height:8px;background:#0a0e16;border:1px solid #1e2636;border-radius:5px;overflow:hidden;margin:8px 0 6px}
.frow-bar i{display:block;height:100%;border-radius:5px}
.frow-foot{font-family:var(--font-mono);font-size:11.5px;color:#8a99b0;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.empty{text-align:center;border:1.5px dashed #2a3654;border-radius:16px;padding:34px 22px;background:#0d1320}
.empty-glyph{font-size:34px;color:#3b4a68;line-height:1}
.empty-title{font-family:var(--font-display);font-size:18px;margin:10px 0 6px}
.empty-body{color:#9aa8bd;font-size:13.5px;line-height:1.55;max-width:44ch;margin:0 auto}
.insight-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.ins-card{background:#111725;border:1px solid #1f2937;border-radius:14px;padding:14px}
.ins-title{font-family:var(--font-mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#8a99b0}
.ins-big{font-family:var(--font-display);font-size:30px;letter-spacing:-.01em;margin:4px 0 6px}
.ins-detail{font-size:13px;color:#c3d0e2;line-height:1.5}
.rev-hero{display:flex;gap:20px;align-items:center;background:linear-gradient(135deg,#111725,#161d2e);
border:1px solid #1f2937;border-radius:16px;padding:16px;margin-bottom:12px;flex-wrap:wrap}
.rev-gmi span{font-size:40px;font-weight:800;color:#3987e5}.rev-gmi label{display:block;font-size:11px;color:#9aa8bd}
.rev-prog{flex:1;min-width:220px}.score-bar.big{height:12px}
.rev-next{margin:8px 0 0;font-size:14px;color:#c3d0e2}.rev-next.good{color:#0ca30c}
.rungs{display:flex;flex-direction:column;gap:6px;margin-bottom:6px}
.rung{display:flex;align-items:center;gap:10px;background:#111725;border:1px solid #1f2937;border-radius:10px;padding:8px 12px;font-size:13px}
.rung.done{border-color:#0ca30c55;background:#0ca30c11}.rung .rk{font-weight:700;color:#8091a7;flex:0 0 auto}
.rung.done .rk{color:#0ca30c}.rung .rl{flex:1}.rung .rm{color:#9aa8bd;font-size:12px}
.proj{background:#111725;border:1px solid #1f2937;border-radius:12px;padding:12px}
.proj.good{border-left:3px solid #0ca30c}.proj.bad{border-left:3px solid #d03b3b}
.proj-main{font-size:15px;margin-bottom:4px}
.doc-list{display:flex;flex-direction:column;gap:8px}
.doc-item{display:flex;gap:10px;background:#111725;border:1px solid #1f2937;border-radius:12px;padding:10px 12px}
.doc-item.p1{border-left:3px solid #d03b3b}.doc-item.p2{border-left:3px solid #fab219}
.doc-area{flex:0 0 84px;font-size:11px;color:#9aa8bd;text-transform:uppercase;padding-top:2px}
.doc-text{font-weight:600}
.lab-group{margin-bottom:14px}.lg-title{font-size:12px;color:#9aa8bd;text-transform:uppercase;margin:8px 0 4px}
.lab{display:flex;align-items:center;gap:10px;padding:7px 4px;border-bottom:1px solid #1f2937;font-size:13px}
.sdot{width:10px;height:10px;border-radius:50%;flex:0 0 auto}
.lab-name{flex:0 0 130px;font-weight:600}.lab-val{flex:0 0 92px;font-weight:700}.lab-val small{color:#9aa8bd;font-weight:400}
.lab-ref{flex:0 0 84px;color:#9aa8bd;font-size:12px}
.ldelta{font-size:12px;flex:0 0 auto}.ldelta.good{color:#0ca30c}.ldelta.bad{color:#d03b3b}
.lab-spark{flex:1;min-width:70px;max-width:150px}.lab-spark .spark{height:30px}
.lstat{flex:0 0 auto;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.3px}
.lab-age{flex:0 0 auto;font-size:11px}
@media(max-width:640px){.lab-spark,.lab-age{display:none}.lab-name{flex:0 0 84px}}
.fc{background-image:linear-gradient(180deg,#161d2e,#111725);border:1px solid #223049;
border-radius:14px;padding:14px}
.fc-controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.fc select,.fc input[type=number]{background:#0a0e16;color:#eef2f9;border:1px solid #26324a;
border-radius:9px;padding:9px 10px;font-family:var(--font-body);font-size:14px;min-height:40px}
.fc select{flex:1;min-width:150px}.fc-carbs{font-size:13px;color:#9aa8bd;display:flex;align-items:center;gap:6px}
.fc-carbs input{width:74px}.fc-walk{font-size:13px;color:#c3d0e2;display:flex;align-items:center;gap:6px}
.fc-out{display:flex;align-items:center;gap:18px;margin-top:14px;padding-top:12px;border-top:1px solid #1e2636}
.fc-out .num{font-family:var(--font-display);font-size:34px;color:#eaf1fb}
.fc-out label{display:block;font-family:var(--font-mono);font-size:10.5px;letter-spacing:.05em;
text-transform:uppercase;color:#8a99b0}
.fc-verdict{font-family:var(--font-mono);font-size:14px;font-weight:600}
.dg{display:flex;flex-direction:column}
.dg-row{display:flex;align-items:baseline;gap:10px;padding:9px 2px;border-bottom:1px solid #1a2336}
.dg-lbl{flex:1;font-size:13.5px}.dg-vals{flex:0 0 auto;font-family:var(--font-mono);font-size:13px}
.dg-delta{flex:0 0 84px;text-align:right;font-family:var(--font-mono);font-weight:600;color:#9aa8bd}
.dg-delta.good{color:#5bd47e}.dg-delta.bad{color:#f0a072}
"""

_JS = """
var HH_SCROLL={};
function showTab(name,fromNav){
var cur=document.querySelector('.tab.active');if(cur)HH_SCROLL[cur.id]=window.scrollY;
document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
var sec=document.getElementById('tab-'+name);if(!sec)return;sec.classList.add('active');
document.querySelectorAll('nav button').forEach(function(b){b.classList.remove('active');
b.setAttribute('aria-selected','false');b.tabIndex=-1;});
var nb=document.getElementById('navbtn-'+name);if(nb){nb.classList.add('active');
nb.setAttribute('aria-selected','true');nb.tabIndex=0;nb.scrollIntoView({block:'nearest',inline:'center'});}
// user taps push history (Back returns to the previous tab); programmatic opens replace
if(fromNav&&history.pushState)history.pushState({tab:name},'','#'+name);
else if(history.replaceState)history.replaceState({tab:name},'','#'+name);
window.scrollTo(0,HH_SCROLL['tab-'+name]||0);}
window.addEventListener('popstate',function(e){var n=(e.state&&e.state.tab)||(location.hash||'').slice(1);
if(n&&document.getElementById('tab-'+n))showTab(n);});
// keyboard: arrow-key roving focus across the tablist (WAI-ARIA tabs pattern)
document.addEventListener('keydown',function(e){
if(!e.target.matches('nav button'))return;var btns=[].slice.call(document.querySelectorAll('nav button'));
var i=btns.indexOf(e.target),j=i;
if(e.key==='ArrowRight')j=(i+1)%btns.length;else if(e.key==='ArrowLeft')j=(i-1+btns.length)%btns.length;
else if(e.key==='Home')j=0;else if(e.key==='End')j=btns.length-1;else return;
e.preventDefault();btns[j].focus();btns[j].click();});
function showWindow(w){document.querySelectorAll('.winpanel').forEach(p=>p.style.display='none');
var el=document.getElementById('winpanel-'+w);if(el)el.style.display='block';
document.querySelectorAll('.winbtn').forEach(b=>b.classList.remove('active'));
var bb=document.getElementById('winbtn-'+w);if(bb)bb.classList.add('active');}
function hhReload(){location.reload();}
// tap-to-read: touch devices can't hover, so surface any data-tip on tap/click
function hhTip(e){var el=e.target.closest('[data-tip]');if(!el)return;
var t=document.getElementById('hhtip');if(!t){t=document.createElement('div');t.id='hhtip';
document.body.appendChild(t);}t.textContent=el.getAttribute('data-tip');
t.style.left=Math.min(e.clientX,window.innerWidth-180)+'px';t.style.top=(e.clientY+14)+'px';
t.style.display='block';clearTimeout(window._tt);window._tt=setTimeout(function(){t.style.display='none';},2600);}
document.addEventListener('click',hhTip);
// "will it spike?" — arithmetic on the Python-computed per-gram / walk / baseline coefficients
function fcCompute(){var M=window.HH_FC||{},foods=M.foods||[];if(!foods.length)return;
var sel=document.getElementById('fcFood');if(!sel)return;var f=foods[+sel.value];if(!f)return;
var carbs=parseFloat((document.getElementById('fcCarbs')||{}).value)||0;var rise=f.per_gram*carbs;
var w=document.getElementById('fcWalk');if(w&&w.checked&&M.walk_effect!=null)rise+=M.walk_effect;
if(rise<0)rise=0;var peak=Math.round((M.baseline||110)+rise);
var band=peak<=140?'tight range':peak<=180?'in range':peak<=250?'above range':'high';
var col=peak<=140?'#2ea043':peak<=180?'#d9a021':'#e5484d';
var pk=document.getElementById('fcPeak');pk.textContent=peak+' mg/dL';pk.style.color=col;
var v=document.getElementById('fcVerdict');v.textContent='+'+Math.round(rise)+' mg/dL · '+band;v.style.color=col;}
// open the tab named in the URL hash (deep-link / PWA reopen)
(function(){var h=(location.hash||'').slice(1);if(h&&document.getElementById('tab-'+h))showTab(h);
if(document.getElementById('fcFood'))fcCompute();})();
// restrained count-up on hero figures (skipped for reduced-motion)
(function(){
if(window.matchMedia&&matchMedia('(prefers-reduced-motion:reduce)').matches)return;
document.querySelectorAll('[data-count]').forEach(function(el){
  var end=parseFloat(el.getAttribute('data-count'));if(isNaN(end))return;
  var dec=(el.getAttribute('data-count').split('.')[1]||'').length,t0=null,dur=850;
  function step(ts){if(!t0)t0=ts;var k=Math.min(1,(ts-t0)/dur);var e=1-Math.pow(1-k,3);
    el.textContent=(end*e).toFixed(dec);if(k<1)requestAnimationFrame(step);else el.textContent=end.toFixed(dec);}
  el.textContent=(0).toFixed(dec);requestAnimationFrame(step);
});})();
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
var b=document.getElementById('hdrBadge');if(b)b.textContent=badge;
// show the reading time in Dubai local time (the user's timezone), not raw UTC ISO
var t=document.getElementById('hdrAsOf');if(t){try{t.textContent=new Date(HH.asOf)
.toLocaleString('en-GB',{timeZone:'Asia/Dubai',day:'2-digit',month:'short',
hour:'2-digit',minute:'2-digit'});}catch(e){}}}
hhRefresh();setInterval(hhRefresh,60000);
// live cockpit: when you return to the tab after a while (online), pull the freshest cycle
var HH_LOADED=Date.now();
document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible'
&&navigator.onLine&&(Date.now()-HH_LOADED)>120000){location.reload();}});
if('serviceWorker' in navigator){navigator.serviceWorker.register('service-worker.js').catch(function(){});}
"""


def render(cockpit: dict) -> str:
    g = cockpit["header"].get("glucose")
    if g:
        hdr = (f'<button class="refresh" onclick="hhReload()" title="Refresh now" '
               f'aria-label="Refresh now">↻</button>'
               f'<div class="hdr-glucose"><span id="hdrVal">{_fmt(g.get("value"),0)}</span> '
               f'mg/dL · <span id="hdrBadge">{_freshness_badge(g.get("state"))}</span></div>'
               f'<div class="hdr-age">as of <span id="hdrAsOf" title="{_esc(g.get("as_of"))}">'
               f'{_esc(g.get("as_of"))}</span> '
               f'(<span id="hdrAge">{_esc(g.get("age"))}</span> ago) — '
               f'last-known-good, not live</div>')
        cfg = json.dumps({"asOf": g.get("as_of"), "freshH": 0.5, "staleH": 1.5})
    else:
        hdr = ('<button class="refresh" onclick="hhReload()" title="Refresh now" '
               'aria-label="Refresh now">↻</button>'
               '<div class="hdr-glucose">No glucose data</div>')
        cfg = "null"

    hb = cockpit.get("heartbeat") or {}
    pills = "".join(
        f'<span class="hb hb-{_esc(s["state"])}">{_esc(s["source"])}: '
        f'{_esc(s["age"] or "no data")} · {_esc(s["state"])}</span>'
        for s in hb.get("sources", []))
    banner = (f'<div class="heartbeat {"hb-ok" if hb.get("overall_ok") else "hb-bad"}">'
              f'{"✓ all feeds current" if hb.get("overall_ok") else "⚠ feed(s) stale"} {pills}</div>'
              if hb.get("sources") else "")

    bodies = {
        "Today": _today_tab(cockpit), "Reversal": _reversal_tab(cockpit),
        "Trends": _trends_tab(cockpit), "Patterns": _patterns_tab(cockpit),
        "Correlations": _correlations_tab(cockpit),
        "Analytics": _analytics_tab(cockpit), "Food": _food_tab(cockpit),
        "Experiments": _experiments_tab(cockpit), "Custom": _custom_tab(cockpit),
        "Review": _review_tab(cockpit), "Export": _export_tab(cockpit),
    }
    nav = "".join(f'<button id="navbtn-{t}" role="tab" '
                  f'aria-selected="{"true" if i==0 else "false"}" '
                  f'aria-controls="tab-{t}" tabindex="{"0" if i==0 else "-1"}" '
                  f'class="{"active" if i==0 else ""}" '
                  f'onclick="showTab(\'{t}\',1)">{t}</button>' for i, t in enumerate(TABS))
    tabs = "".join(f'<section id="tab-{t}" role="tabpanel" aria-labelledby="navbtn-{t}" '
                   f'tabindex="0" class="tab {"active" if i==0 else ""}">'
                   f'<h2>{t}</h2>{bodies[t]}</section>' for i, t in enumerate(TABS))
    # CSP is a load-bearing control, not a claim: everything is inlined/same-origin, so lock the
    # page to its own origin + data-URI fonts and forbid any external fetch. (§A: no external hosts.)
    csp = ("default-src 'none'; base-uri 'none'; img-src 'self' data:; font-src data:; "
           "style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; "
           "manifest-src 'self'; worker-src 'self'; form-action 'none'")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<meta name="theme-color" content="#0a0e16">'
            f'<meta name="apple-mobile-web-app-capable" content="yes">'
            f'<meta name="mobile-web-app-capable" content="yes">'
            f'<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
            f'<meta name="apple-mobile-web-app-title" content="Health OS">'
            f'<meta name="color-scheme" content="dark">'
            f'<link rel="manifest" href="manifest.json">'
            f'<link rel="apple-touch-icon" href="icon-192.png">'
            f'<title>Health OS</title><style>{assets.FONT_CSS}{_TOKENS}{_CSS}</style></head><body>'
            f'<div class="topbar"><header>{hdr}{banner}</header>'
            f'<div class="navwrap"><nav role="tablist" aria-label="Sections">{nav}</nav></div></div>'
            f'<main>{tabs}</main>'
            f'<script>window.HH={cfg};window.HH_FC={json.dumps(cockpit.get("forecast") or {})};'
            f'{_JS}{_FRESH_JS}</script></body></html>')


def write_dashboard(html_str: str, path: str = "dashboard.html") -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_str)
    return path


def self_check(cockpit: dict) -> list[str]:
    """Invariant: a quarantined reading must never be rendered as trusted glucose.

    Checked STRUCTURALLY (not by scanning rendered HTML): every plotted glucose surface —
    the header last-known-good, the daily-frame means, and each window's trend/AGP — is
    computed only from the clean store, so the only place a raw quarantined reading could
    surface is the header value. We assert that directly. (A substring scan of the HTML is
    unsound: small quarantined values collide with SVG coordinates, and int-formatted tiles
    dodge a ``str(float)`` match — so it both false-alarms and misses.)
    """
    violations: list[str] = []
    g = cockpit["header"].get("glucose")
    if g and g.get("state") == "future":
        violations.append("header shows a future-dated glucose value")
    bad = {q.get("payload", {}).get("glucose_mgdl") for q in cockpit.get("quarantine", [])}
    bad.discard(None)
    if g and g.get("value") in bad:
        violations.append(f"header shows quarantined glucose value {g.get('value')}")
    return violations
