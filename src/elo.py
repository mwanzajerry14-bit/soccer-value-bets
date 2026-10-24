"""Simple margin-aware Elo (ClubElo-style), used to blend into 1X2."""
from __future__ import annotations

import pandas as pd


class Elo:
    def __init__(self, k: float = 20.0, home_bonus: float = 65.0,
                 default_rating: float = 1500.0):
        self.k = k
        self.home_bonus = home_bonus
        self.default = default_rating  # set post-fit to bottom-quintile mean
        self.ratings: dict[str, float] = {}

    def r(self, team: str) -> float:
        return self.ratings.get(team, self.default)

    def expected(self, home: str, away: str) -> float:
        """P(scale result for home), draw = 0.5 shared."""
        diff = self.r(home) + self.home_bonus - self.r(away)
        return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))

    @staticmethod
    def _mult(gd: int) -> float:
        return {0: 1.0, 1: 1.0, 2: 1.5, 3: 1.75}.get(gd, 1.75 + 0.25 * (gd - 3))

    def update(self, home: str, away: str, hg: int, ag: int) -> None:
        e = self.expected(home, away)
        s = 1.0 if hg > ag else 0.5 if hg == ag else 0.0
        k = self.k * self._mult(abs(hg - ag))
        self.ratings[home] = self.r(home) + k * (s - e)
        self.ratings[away] = self.r(away) + k * ((1 - s) - (1 - e))

    def process(self, matches: pd.DataFrame) -> None:
        for row in matches.itertuples():
            self.update(row.home, row.away, int(row.hg), int(row.ag))

    def prob_1x2(self, home: str, away: str, base_draw: float = 0.27) -> dict:
        """Convert expectancy to W/D/L. Draw mass shrinks as diff grows."""
        e = self.expected(home, away)
        dev = abs(2.0 * e - 1.0)
        pd_draw = max(0.05, base_draw * (1.0 - dev))
        return {"home": (1 - pd_draw) * e, "draw": pd_draw, "away": (1 - pd_draw) * (1 - e)}
