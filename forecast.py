"""forecast.py — a grounded "will this spike?" meal model.

The only forward-looking piece the mission needs and the anti-hallucination contract can bless:
it composes numbers the engine ALREADY measured deterministically — each food's per-gram glucose
response (food_impact), the user's own post-meal-walk effect (experiments), and their recent
baseline glucose (clinical) — into a projected peak. No model invents anything; the client-side
calculator only does arithmetic on these Python-computed coefficients, so the projection is as
grounded as every other number on the dashboard. It is a projection, and is labelled as one.
"""

from __future__ import annotations


def build(food_ranking=None, experiments=None, metrics=None) -> dict:
    """Assemble the forecast model: per-food per-gram response + walk effect + baseline."""
    foods = []
    for f in food_ranking or []:
        pg = f.get("mean_per_gram")
        if pg is not None:
            foods.append({"item": f["item"], "per_gram": round(float(pg), 3),
                          "mean_delta": round(float(f.get("mean_delta_peak_mgdl") or 0), 1),
                          "n": f.get("n")})
    walk = None
    for e in experiments or []:
        if e.get("tag") == "+walk" and e.get("effect_abs") is not None:
            walk = round(float(e["effect_abs"]), 1)   # negative = blunts the peak
            break
    baseline = ((metrics or {}).get("mean_mgdl") or {}).get("value")
    baseline = round(float(baseline), 0) if isinstance(baseline, (int, float)) else 110.0
    return {"foods": foods, "walk_effect": walk, "baseline": baseline}


def predict(model: dict, item: str, carbs: float, *, walk: bool = False) -> dict:
    """Projected peak for a meal, from the model. Returns delta (rise) + absolute peak."""
    per_gram = next((f["per_gram"] for f in model.get("foods", []) if f["item"] == item), None)
    if per_gram is None or carbs is None:
        return {"ok": False}
    rise = per_gram * float(carbs)
    if walk and model.get("walk_effect") is not None:
        rise = rise + model["walk_effect"]        # walk_effect is negative
    rise = max(0.0, rise)
    baseline = model.get("baseline", 110.0)
    return {"ok": True, "delta_mgdl": round(rise, 0), "peak_mgdl": round(baseline + rise, 0),
            "in_range": baseline + rise <= 180.0}


def self_check(model: dict) -> list[str]:
    violations = []
    if not isinstance(model.get("baseline"), (int, float)):
        violations.append("forecast baseline not numeric")
    for f in model.get("foods", []):
        if not isinstance(f.get("per_gram"), (int, float)):
            violations.append(f"forecast food {f.get('item')}: per_gram not numeric")
    return violations
