"""Model orchestration: fit Dixon-Coles + Elo on history, predict matches."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from . import dixon_coles as dcmod
from .elo import Elo
from .teamnames import LeagueMatcher

# bump whenever the model math/features change (written to every prediction)
MODEL_VERSION = "dc-elo-1.1"


@dataclass
class FittedModel:
    league_code: str
    dc: dict
    elo: Elo
    base_draw: float
    w_dc: float
    max_goals: int
    fitted_at: pd.Timestamp
    form_n: int = 5
    history: pd.DataFrame | None = None

    @property
    def teams(self) -> list[str]:
        return self.dc["teams"]

    def matcher(self) -> LeagueMatcher:
        return LeagueMatcher(self.teams)

    def predict(self, home: str, away: str) -> dict | None:
        """Full market probabilities for canonical team names."""
        lam, mu, full_hist = dcmod.expected_goals(self.dc, home, away)
        P = dcmod.score_matrix(lam, mu, self.dc["rho"], self.max_goals)
        mkts = dcmod.market_probs(P)
        # blend 1X2 with Elo (Elo says nothing credible about goals markets)
        e = self.elo.prob_1x2(home, away, self.base_draw)
        for sel, pe in e.items():
            mkts["1X2"][sel] = self.w_dc * mkts["1X2"][sel] + (1 - self.w_dc) * pe
        return {
            "markets": mkts,
            "lambda_home": lam,
            "lambda_away": mu,
            "full_history": full_hist,
            "elo": {"home": round(self.elo.r(home)), "away": round(self.elo.r(away))},
        }

    def explain(self, home: str, away: str, pred: dict) -> str:
        """Short human-readable statistical support (real numbers only)."""
        bits = [f"model xG {pred['lambda_home']:.2f}-{pred['lambda_away']:.2f}",
                f"Elo {pred['elo']['home']} vs {pred['elo']['away']}"]
        fh = self.form(home, date.today()) if self.history is not None else {}
        fa = self.form(away, date.today()) if self.history is not None else {}
        if fh.get("played"):
            bits.append(f"last-{fh['played']} pts {fh['pts']} v {fa.get('pts', 0)}, "
                        f"goals {fh['gf']}:{fh['ga']} v {fa.get('gf', 0)}:{fa.get('ga', 0)}")
        return "; ".join(bits)

    def form(self, team: str, before: date, n: int | None = None) -> dict:
        """Last-N form (points, goals) strictly before `before`. Display only."""
        n = n or self.form_n
        h = self.history
        if h is None:
            return {}
        rows = h[((h.home == team) | (h.away == team)) & (h.date < pd.Timestamp(before))].tail(n)
        pts = gf = ga = 0
        for r in rows.itertuples():
            if r.home == team:
                gf += r.hg; ga += r.ag
                pts += 3 if r.hg > r.ag else 1 if r.hg == r.ag else 0
            else:
                gf += r.ag; ga += r.hg
                pts += 3 if r.ag > r.hg else 1 if r.ag == r.hg else 0
        return {"played": len(rows), "pts": pts, "gf": gf, "ga": ga}


def fit(history: pd.DataFrame, model_cfg: dict, fitted_at: pd.Timestamp,
        league_code: str, x0=None) -> FittedModel:
    hist = history[history.date < fitted_at]
    dc = dcmod.fit(hist, model_cfg.get("decay_per_day", 0.004), fitted_at, x0=x0)
    elo = Elo()
    elo.process(hist)
    # Heuristic prior for teams with no history (promoted/new clubs):
    # mean strength of the 6 weakest fitted teams, ditto for Elo.
    if len(dc["teams"]) >= 8:
        atts = sorted(dc["att"].values())
        dfs_ = sorted(dc["def"].values(), reverse=True)
        dc["fallback_att"] = float(np.mean(atts[:6]))
        dc["fallback_def"] = float(np.mean(dfs_[:6]))
        rts = sorted(elo.ratings.values())
        elo.default = float(np.mean(rts[:6]))
    base_draw = float((hist.ftr == "D").mean()) if len(hist) else 0.27
    return FittedModel(
        league_code=league_code, dc=dc, elo=elo, base_draw=base_draw,
        w_dc=float(model_cfg.get("blend_w_dc", 0.75)),
        max_goals=int(model_cfg.get("max_goals", 10)),
        fitted_at=fitted_at,
        form_n=int(model_cfg.get("form_last_n", 5)),
        history=hist,
    )
