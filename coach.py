"""coach.py — "Today's moves": the prescriptive layer that closes the daily loop.

Everything else in the engine is diagnostic — it explains what already happened. A reversal
product also has to say *what to do next*, but honestly: every move here is ranked from the
user's OWN measured data (their post-meal-walk effect, their worst food, their highest time of
day, their distance to the next GMI rung), never from generic advice. The numbers are computed
upstream (experiments/food_impact/patterns/clinical/reversal), so the move text is grounded by
construction — no invented figures, and nothing is framed as medical instruction.
"""

from __future__ import annotations


def _mv(metrics, key):
    v = ((metrics or {}).get(key) or {}).get("value")
    return v if isinstance(v, (int, float)) else None


def _tag(experiments, tag):
    for e in experiments or []:
        if e.get("tag") == tag and e.get("effect_abs") is not None:
            return e
    return None


def moves(*, metrics=None, experiments=None, food_ranking=None, patterns=None,
          reversal=None, top: int = 3) -> list[dict]:
    """Rank concrete, data-grounded daily actions (highest leverage first)."""
    out: list[dict] = []

    # 1) the next GMI milestone — the single most motivating target
    nxt = ((reversal or {}).get("ladder") or {}).get("next")
    if nxt and nxt.get("mean_gap", 0) > 0:
        out.append({"key": "gmi", "priority": 1,
                    "title": f"{nxt['mean_gap']:.0f} mg/dL from {nxt['label'].lower()}",
                    "detail": (f"Bring your average down {nxt['mean_gap']:.0f} mg/dL to reach "
                               f"GMI below {nxt['gmi']:g}. Every in-range day compounds."),
                    "impact": f"GMI <{nxt['gmi']:g}"})

    # 2) post-meal walk — usually the strongest measured lever
    walk = _tag(experiments, "+walk")
    if walk and walk["effect_abs"] < -3:
        n = min(walk.get("n_treated", 0), walk.get("n_control", 0))
        out.append({"key": "walk", "priority": 1,
                    "title": "Walk after your biggest meal",
                    "detail": (f"Your own data: a post-meal walk blunted the peak by "
                               f"{abs(walk['effect_abs']):.0f} mg/dL (n={n})."),
                    "impact": f"−{abs(walk['effect_abs']):.0f} mg/dL peak"})

    # 3) worst food — shrink or swap it
    if food_ranking:
        worst = max(food_ranking, key=lambda f: f.get("mean_delta_peak_mgdl") or 0)
        dp = worst.get("mean_delta_peak_mgdl")
        if dp and dp > 40:
            out.append({"key": "food", "priority": 2,
                        "title": f"Rethink {worst['item']}",
                        "detail": (f"It raises your glucose about {dp:.0f} mg/dL on average "
                                   f"(n={worst.get('n')} meals) — shrink the portion, eat protein "
                                   f"and fibre first, or swap it."),
                        "impact": f"~{dp:.0f} mg/dL spike"})

    # 4) highest time of day — target the worst window
    worst_win = ((patterns or {}).get("time_of_day") or {}).get("worst")
    if worst_win and worst_win.get("median_mgdl", 0) > 145:
        out.append({"key": "tod", "priority": 3,
                    "title": f"Target the {worst_win['window'].lower()} ({worst_win['hours']})",
                    "detail": (f"It's your highest window at a median {worst_win['median_mgdl']:.0f} "
                               f"mg/dL — a walk, lighter carbs, or an earlier dinner here moves the "
                               f"needle most."),
                    "impact": f"{worst_win['median_mgdl']:.0f} mg/dL median"})

    # 5) tight-time-in-range — the remission needle
    titr = _mv(metrics, "titr_pct")
    if titr is not None and titr < 50:
        out.append({"key": "titr", "priority": 2,
                    "title": "Lift tight-time-in-range toward 50%",
                    "detail": (f"You're at {titr:.0f}% of the day between 70–140 mg/dL — the "
                               f"remission-grade band. Front-loading protein and walking after "
                               f"meals is the fastest lever."),
                    "impact": f"{titr:.0f}% → 50%"})

    # de-dup by key, keep the highest-priority version, return the top few
    seen, picked = set(), []
    for m in sorted(out, key=lambda m: m["priority"]):
        if m["key"] in seen:
            continue
        seen.add(m["key"])
        picked.append(m)
    return picked[:top]


def self_check(ms: list[dict]) -> list[str]:
    violations = []
    for m in ms:
        if m.get("priority") not in (1, 2, 3):
            violations.append(f"move {m.get('key')}: bad priority")
        if not m.get("title") or not m.get("detail"):
            violations.append(f"move {m.get('key')}: missing text")
    return violations
