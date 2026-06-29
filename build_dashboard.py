"""build_dashboard.py — render the tabbed dashboard HTML, routed through integrity.

Tabs: Today · Analytics · Food · Experiments · Protocol · Export. A persistent header shows
the last glucose value with its **age and freshness** (FRESH/STALE/EXPIRED) — it never
pretends to be live (§A rule 6). Every tile carries provenance + freshness. The Analytics tab
renders an AGP chart per window with a window selector (7/14/30/90/180/365/500) wired to the
analytics.py outputs. Self-contained static HTML (works offline / in the PWA).
"""

from __future__ import annotations

import html
import json

import integrity

TABS = ("Today", "Analytics", "Food", "Experiments", "Custom", "Protocol", "Export")
_RANGE_LOW, _RANGE_HIGH = 70.0, 180.0
_Y_MIN, _Y_MAX = 40.0, 300.0


def _esc(x) -> str:
    return html.escape("" if x is None else str(x))


def _fmt(v, nd=1):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


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


def _tile(title, value, *, sub="", source="", freshness="", target="") -> str:
    foot = " · ".join(p for p in (f"src: {_esc(source)}" if source else "",
                                  _esc(freshness)) if p)
    return (f'<div class="tile"><div class="t-title">{_esc(title)}</div>'
            f'<div class="t-value">{_esc(value)}</div>'
            f'<div class="t-sub">{_esc(sub)}{(" · target " + _esc(target)) if target else ""}</div>'
            f'<div class="t-foot">{foot}</div></div>')


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
              f'height="{y(_RANGE_LOW) - y(_RANGE_HIGH):.1f}" fill="#1f8a4c22"/>')
    axis = (f'<line x1="{pad}" y1="{y(_RANGE_LOW):.1f}" x2="{width-pad}" y2="{y(_RANGE_LOW):.1f}" '
            f'stroke="#1f8a4c" stroke-dasharray="3 3"/>'
            f'<line x1="{pad}" y1="{y(_RANGE_HIGH):.1f}" x2="{width-pad}" y2="{y(_RANGE_HIGH):.1f}" '
            f'stroke="#1f8a4c" stroke-dasharray="3 3"/>')
    return (f'<svg class="agp" viewBox="0 0 {width} {height}">{target}'
            f'{band("p10", "p90", "#3b82f633")}{band("p25", "p75", "#3b82f666")}'
            f'<path d="{median}" fill="none" stroke="#1d4ed8" stroke-width="2"/>{axis}'
            f'<text x="{pad}" y="{pad-8}" fill="#555" font-size="12">AGP — 10/25/50/75/90 '
            f'percentile, 70-180 target shaded</text></svg>')


def build_cockpit(*, metrics, trend_by_window, latest_glucose=None, wearables=None,
                  food_ranking=None, experiments=None, assessment=None,
                  insights_text="", quarantine=None, custom_cards=None, now=None) -> dict:
    """Assemble the data the dashboard renders, with header freshness from integrity."""
    now = integrity.now_utc() if now is None else now
    header = {"generated_at": now.isoformat(), "glucose": None}
    if latest_glucose and latest_glucose.get("ts") is not None:
        fr = integrity.freshness(latest_glucose["ts"], now=now)
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
        "custom_cards": custom_cards or [],
    }


def _today_tab(c) -> str:
    m = c["metrics"]

    def mv(k):
        return (m.get(k) or {}).get("value")

    def fresh(k):
        return _freshness_badge("fresh" if (m.get(k) or {}).get("valid") else "stale")

    tiles = [
        _tile("Time in Range", f"{_fmt(mv('tir_pct'),0)}%", target="≥70%",
              source="clinical", freshness=fresh("tir_pct")),
        _tile("Tight Range (70-140)", f"{_fmt(mv('titr_pct'),0)}%", target="≥50%",
              source="clinical", freshness=fresh("titr_pct")),
        _tile("GMI", f"{_fmt(mv('gmi_pct'))}%", source="clinical", freshness=fresh("gmi_pct")),
        _tile("Mean glucose", f"{_fmt(mv('mean_mgdl'),0)} mg/dL", source="clinical",
              freshness=fresh("mean_mgdl")),
        _tile("Variability (CV)", f"{_fmt(mv('cv_pct'))}%", sub=str(mv("cv_flag") or ""),
              source="clinical", freshness=fresh("cv_pct")),
        _tile("GRI", f"{_fmt(mv('gri'),0)}", source="clinical", freshness=fresh("gri")),
        _tile("Below range", f"{_fmt(mv('tbr_pct'),0)}%", target="≤4%", source="clinical",
              freshness=fresh("tbr_pct")),
        _tile("Above range", f"{_fmt(mv('tar_pct'),0)}%", target="≤25%", source="clinical",
              freshness=fresh("tar_pct")),
    ]
    return '<div class="grid">' + "".join(tiles) + "</div>"


def _analytics_tab(c) -> str:
    windows = sorted((int(w) for w in c["trend_by_window"].keys()))
    if not windows:
        return "<p>No trend data.</p>"
    buttons = "".join(
        f'<button class="winbtn" onclick="showWindow({w})" id="winbtn-{w}">{w}d</button>'
        for w in windows)
    panels = []
    for i, w in enumerate(windows):
        tr = c["trend_by_window"][w] if w in c["trend_by_window"] else c["trend_by_window"][str(w)]
        s = tr.get("summary", {})
        summary = "".join(_tile(k, _fmt(s.get(k)), source="analytics")
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


def _protocol_tab(c) -> str:
    a = c["assessment"]
    lines = "".join(f"<li>{_esc(l['text'])}</li>" for l in a.get("lines", []))
    insight = f'<p class="insight">{_esc(c["insights_text"])}</p>' if c["insights_text"] else ""
    head = f'<h3>{_esc(a.get("headline", "Assessment"))}</h3>' if a else ""
    return f'{head}<ul>{lines}</ul>{insight}'


def _export_tab(c) -> str:
    q = c["quarantine"]
    qnote = (f'<p class="warn">{len(q)} row(s) quarantined and excluded from all charts.</p>'
             if q else "<p>No quarantined rows.</p>")
    return (qnote + '<p>Download artifacts: <code>metrics.json</code>, <code>trend.json</code>, '
            '<code>wearables.json</code>.</p>'
            f'<details><summary>cockpit JSON</summary><pre>{_esc(json.dumps(c["header"], indent=2))}'
            '</pre></details>')


_CSS = """
*{box-sizing:border-box}body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;
background:#0f172a;color:#e2e8f0}header{position:sticky;top:0;background:#111827;padding:12px 16px;
border-bottom:1px solid #1f2937;z-index:5}.hdr-glucose{font-size:22px;font-weight:700}
.hdr-age{color:#94a3b8;font-size:13px}nav{display:flex;gap:4px;padding:8px 12px;background:#111827;
overflow-x:auto}nav button{background:#1f2937;color:#cbd5e1;border:0;padding:8px 14px;border-radius:8px;
cursor:pointer}nav button.active{background:#2563eb;color:#fff}main{padding:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.tile{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:12px}
.t-title{font-size:12px;color:#94a3b8}.t-value{font-size:24px;font-weight:700;margin:4px 0}
.t-sub{font-size:12px;color:#cbd5e1}.t-foot{font-size:11px;color:#64748b;margin-top:6px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #334155}
.winbar{margin-bottom:10px}.winbtn{background:#1f2937;color:#cbd5e1;border:0;margin-right:4px;
padding:6px 10px;border-radius:8px;cursor:pointer}.winbtn.active{background:#2563eb;color:#fff}
.agp{width:100%;height:auto;background:#0b1220;border-radius:10px;margin:8px 0}
.muted{color:#94a3b8;font-size:13px}.warn{color:#f59e0b}.insight{color:#a5b4fc;margin-top:10px}
.tab{display:none}.tab.active{display:block}pre{white-space:pre-wrap;background:#0b1220;padding:8px;border-radius:8px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:12px}
.card-title{font-weight:700}.card-filter{font-size:11px;color:#64748b;margin:4px 0 8px}
.bar-row{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}
.bar-lbl{flex:0 0 120px;color:#cbd5e1}.bar{flex:1;background:#0b1220;border-radius:6px;height:12px;overflow:hidden}
.bar i{display:block;height:100%;background:#3b82f6}.bar-val{flex:0 0 48px;text-align:right;color:#e2e8f0}
.card-assess{font-size:12px;color:#a5b4fc;margin-top:8px}
"""

_JS = """
function showTab(name){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
document.getElementById('tab-'+name).classList.add('active');
document.querySelectorAll('nav button').forEach(b=>b.classList.remove('active'));
document.getElementById('navbtn-'+name).classList.add('active');}
function showWindow(w){document.querySelectorAll('.winpanel').forEach(p=>p.style.display='none');
var el=document.getElementById('winpanel-'+w);if(el)el.style.display='block';
document.querySelectorAll('.winbtn').forEach(b=>b.classList.remove('active'));
var bb=document.getElementById('winbtn-'+w);if(bb)bb.classList.add('active');}
"""

# Recompute the glucose age + freshness on the DEVICE clock, so an offline PWA never shows a
# stale "as of" age or pretends a frozen value is live (§A rule 6). Mirrors integrity's
# fresh<=6h / stale<=72h / else expired thresholds.
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
        cfg = json.dumps({"asOf": g.get("as_of"), "freshH": 6, "staleH": 72})
    else:
        hdr = '<div class="hdr-glucose">No glucose data</div>'
        cfg = "null"

    bodies = {
        "Today": _today_tab(cockpit), "Analytics": _analytics_tab(cockpit),
        "Food": _food_tab(cockpit), "Experiments": _experiments_tab(cockpit),
        "Custom": _custom_tab(cockpit),
        "Protocol": _protocol_tab(cockpit), "Export": _export_tab(cockpit),
    }
    nav = "".join(f'<button id="navbtn-{t}" class="{"active" if i==0 else ""}" '
                  f'onclick="showTab(\'{t}\')">{t}</button>' for i, t in enumerate(TABS))
    tabs = "".join(f'<section id="tab-{t}" class="tab {"active" if i==0 else ""}">'
                   f'<h2>{t}</h2>{bodies[t]}</section>' for i, t in enumerate(TABS))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<link rel="manifest" href="manifest.json">'
            f'<title>HealthHub</title><style>{_CSS}</style></head><body>'
            f'<header>{hdr}</header><nav>{nav}</nav><main>{tabs}</main>'
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
    for tab in ("Today", "Analytics"):
        seg = html_out.split(f'id="tab-{tab}"', 1)[-1].split("</section>", 1)[0]
        for v in bad:
            if v and v in seg:
                violations.append(f"quarantined value {v} appears plotted in {tab}")
    return violations
