"""statutil.py — small dependency-free stats helpers (Spearman, lift).

scipy is not a dependency, so Spearman is computed as Pearson correlation on average-tie
ranks. Everything reports ``n`` so callers can refuse to over-interpret tiny samples.
"""

from __future__ import annotations

import math

import numpy as np


def _phi(x: float) -> float:
    """Standard-normal CDF via erf (no scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fisher(rho, n) -> dict:
    """Fisher-z 95% CI and a two-sided p-value for a (Spearman) correlation.

    Uncertainty is a first-class number here: a correlation with no interval is as untrustworthy
    as an ungrounded one. Uses the Fieller SE for Spearman z (≈1.03/√(n−3)).
    """
    if rho is None or n is None or n < 5 or abs(rho) >= 1.0:
        return {"ci": (None, None), "p": None}
    z = math.atanh(rho)
    se = 1.03 / math.sqrt(n - 3)
    lo, hi = math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se)
    p = 2.0 * (1.0 - _phi(abs(z / se)))
    return {"ci": (round(lo, 3), round(hi, 3)), "p": round(min(1.0, p), 4)}


def cohens_d_ci(d, n_treated, n_control) -> dict:
    """Approximate 95% CI for Cohen's d (Hedges large-sample SE)."""
    if d is None or n_treated < 2 or n_control < 2:
        return {"ci": (None, None)}
    nt, nc = n_treated, n_control
    se = math.sqrt((nt + nc) / (nt * nc) + d * d / (2.0 * (nt + nc)))
    return {"ci": (round(d - 1.96 * se, 3), round(d + 1.96 * se, 3))}


def bh_fdr(pvals, q: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg: which p-values are significant controlling FDR at *q*.

    Returns a bool list aligned to *pvals* (None p-values are never significant). With hundreds
    of pairwise tests at n≈75, raw |rho| thresholds are a false-discovery machine; BH keeps the
    expected proportion of false 'findings' below q.
    """
    idx = [i for i, p in enumerate(pvals) if p is not None]
    m = len(idx)
    sig = [False] * len(pvals)
    if m == 0:
        return sig
    order = sorted(idx, key=lambda i: pvals[i])
    kmax = 0
    for rank, i in enumerate(order, start=1):
        if pvals[i] <= q * rank / m:
            kmax = rank
    for rank, i in enumerate(order, start=1):
        if rank <= kmax:
            sig[i] = True
    return sig


def rankdata(a: np.ndarray) -> np.ndarray:
    """Average ranks (1..n), ties share the mean rank — matches scipy.stats.rankdata."""
    a = np.asarray(a, dtype=float)
    order = a.argsort(kind="stable")
    ranks = np.empty(a.size, dtype=float)
    sorted_a = a[order]
    i = 0
    while i < a.size:
        j = i
        while j + 1 < a.size and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # 1-based average rank for the tie block
        ranks[order[i:j + 1]] = avg
        i = j + 1
    return ranks


def spearman(x, y) -> dict:
    """Spearman rho + n over the finite paired values. rho is None if undefined."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = ~(np.isnan(x) | np.isnan(y))
    x, y = x[mask], y[mask]
    n = int(x.size)
    if n < 3 or np.ptp(x) == 0 or np.ptp(y) == 0:
        return {"rho": None, "n": n}
    rho = float(np.corrcoef(rankdata(x), rankdata(y))[0, 1])
    return {"rho": rho, "n": n}


def lift(flag: np.ndarray, condition: np.ndarray) -> dict:
    """P(flag | condition) / P(flag | not condition), with the two sub-sample sizes.

    flag, condition are boolean arrays of equal length. Returns lift=None if a base rate
    is undefined (no condition or no control members, or zero baseline rate).
    """
    flag = np.asarray(flag, dtype=bool)
    cond = np.asarray(condition, dtype=bool)
    n_cond = int(cond.sum())
    n_ctrl = int((~cond).sum())
    if n_cond == 0 or n_ctrl == 0:
        return {"lift": None, "p_treated": None, "p_control": None,
                "n_treated": n_cond, "n_control": n_ctrl}
    p_t = float(flag[cond].mean())
    p_c = float(flag[~cond].mean())
    lift_val = (p_t / p_c) if p_c > 0 else None
    return {"lift": lift_val, "p_treated": p_t, "p_control": p_c,
            "n_treated": n_cond, "n_control": n_ctrl}


def cohens_d(treated, control) -> float | None:
    """Pooled-SD standardized mean difference; None if undefined."""
    t = np.asarray(treated, dtype=float)
    c = np.asarray(control, dtype=float)
    t, c = t[~np.isnan(t)], c[~np.isnan(c)]
    if t.size < 2 or c.size < 2:
        return None
    nt, nc = t.size, c.size
    sp2 = ((nt - 1) * t.var(ddof=1) + (nc - 1) * c.var(ddof=1)) / (nt + nc - 2)
    if sp2 <= 0:
        return None
    return float((t.mean() - c.mean()) / np.sqrt(sp2))


def strength_label(d: float | None) -> str:
    if d is None:
        return "undefined"
    a = abs(d)
    if a >= 0.8:
        return "strong"
    if a >= 0.5:
        return "moderate"
    if a >= 0.2:
        return "weak"
    return "negligible"
