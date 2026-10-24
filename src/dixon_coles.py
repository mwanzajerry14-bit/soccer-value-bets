"""Dixon-Coles (1997) bivariate-Poisson goals model with time decay.

Parameterisation (log-linear):
    log lambda_home = attack[h] + defence_weakness[a] + home_adv
    log mu_away     = attack[a] + defence_weakness[h]
Identifiability: sum(attack) = 0 (enforced inside the likelihood).

Low-score dependence via tau(x,y; lambda, mu, rho) as in the paper.
Time decay: each match weighs exp(-decay * days_ago).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson


def fit(matches: pd.DataFrame, decay_per_day: float, now: pd.Timestamp,
        x0: np.ndarray | None = None) -> dict:
    """Fit on matches (must be pre-filtered to dates < now)."""
    teams = sorted(set(matches["home"]) | set(matches["away"]))
    tid = {t: i for i, t in enumerate(teams)}
    T = len(teams)
    hi = matches["home"].map(tid).to_numpy()
    ai = matches["away"].map(tid).to_numpy()
    hg = matches["hg"].to_numpy()
    ag = matches["ag"].to_numpy()
    days = (now - matches["date"]).dt.days.clip(lower=0).to_numpy(dtype=float)
    w = np.exp(-decay_per_day * days)

    m00 = (hg == 0) & (ag == 0)
    m01 = (hg == 0) & (ag == 1)
    m10 = (hg == 1) & (ag == 0)
    m11 = (hg == 1) & (ag == 1)

    def negll(p: np.ndarray) -> float:
        att = p[:T] - p[:T].mean()
        dfn = p[T:2 * T]
        h, rho = p[-2], p[-1]
        lam = np.exp(att[hi] + dfn[ai] + h)
        mu = np.exp(att[ai] + dfn[hi])
        tau = np.ones(len(matches))
        tau[m00] = 1.0 - lam[m00] * mu[m00] * rho
        tau[m01] = 1.0 + lam[m01] * rho
        tau[m10] = 1.0 + mu[m10] * rho
        tau[m11] = 1.0 - rho
        np.clip(tau, 1e-10, None, out=tau)
        ll = w * (np.log(tau) + hg * np.log(lam) - lam + ag * np.log(mu) - mu)
        return -float(ll.sum())

    if x0 is None or len(x0) != 2 * T + 2:
        p0 = np.concatenate([np.zeros(2 * T), [0.25, -0.10]])
    else:
        p0 = x0
    bounds = [(-3.0, 3.0)] * (2 * T) + [(0.0, 0.9), (-0.30, 0.25)]
    res = minimize(negll, p0, method="L-BFGS-B", bounds=bounds,
                   options={"maxiter": 600, "ftol": 1e-9})

    att = res.x[:T] - res.x[:T].mean()
    dfn = res.x[T:2 * T]
    return {
        "teams": teams,
        "att": dict(zip(teams, att)),
        "def": dict(zip(teams, dfn)),
        "home_adv": float(res.x[-2]),
        "rho": float(res.x[-1]),
        "converged": bool(res.success),
        "n_matches": int(len(matches)),
        "n_teams": T,
    }


def team_strengths(dc: dict) -> dict[str, dict[str, float]]:
    return {t: {"attack": dc["att"][t], "defence": dc["def"][t]} for t in dc["teams"]}


def expected_goals(dc: dict, home: str, away: str) -> tuple[float, float, bool]:
    """Returns (lambda, mu, full_history).

    Teams with no history (e.g. newly promoted) get `fallback_att` /
    `fallback_def` = average of the worst-ranked teams in the fitted data,
    computed in model.fit(). This is a heuristic, not a measurement - every
    such prediction is flagged "limited history" downstream.
    """
    fb_att = dc.get("fallback_att", 0.0)
    fb_def = dc.get("fallback_def", 0.0)
    known_h, known_a = home in dc["att"], away in dc["att"]
    lam = np.exp(dc["att"].get(home, fb_att) + dc["def"].get(away, fb_def) + dc["home_adv"])
    mu = np.exp(dc["att"].get(away, fb_att) + dc["def"].get(home, fb_def))
    return float(lam), float(mu), bool(known_h and known_a)


def score_matrix(lam: float, mu: float, rho: float, max_goals: int = 10) -> np.ndarray:
    g = np.arange(max_goals + 1)
    P = np.outer(poisson.pmf(g, lam), poisson.pmf(g, mu))
    P[0, 0] *= max(1e-12, 1 - lam * mu * rho)
    P[0, 1] *= 1 + lam * rho
    P[1, 0] *= 1 + mu * rho
    P[1, 1] *= 1 - rho
    return P / P.sum()


def market_probs(P: np.ndarray) -> dict[str, dict[str, float]]:
    """All supported markets from a scoreline matrix.
    1X2, O/U 1.5 / 2.5 / 3.5, BTTS, Double Chance - same underlying matrix,
    mathematically consistent by construction."""
    home = float(np.tril(P, -1).sum())
    draw = float(np.trace(P))
    away = float(np.triu(P, 1).sum())
    gi = np.arange(P.shape[0])
    tot = gi[:, None] + gi[None, :]
    over15 = float(P[tot > 1.5].sum())
    over25 = float(P[tot > 2.5].sum())
    over35 = float(P[tot > 3.5].sum())
    btts_yes = float(P[1:, 1:].sum())
    return {
        "1X2": {"home": home, "draw": draw, "away": away},
        "OU15": {"over": over15, "under": 1.0 - over15},
        "OU25": {"over": over25, "under": 1.0 - over25},
        "OU35": {"over": over35, "under": 1.0 - over35},
        "BTTS": {"yes": btts_yes, "no": 1.0 - btts_yes},
        "DC": {"1X": home + draw, "12": home + away, "X2": draw + away},
    }


def top_scoreline(P: np.ndarray) -> tuple[int, int]:
    """Most probable exact score (the predicted scoreline shown on cards)."""
    h, a = np.unravel_index(int(np.argmax(P)), P.shape)
    return int(h), int(a)
