"""Client for The Odds API v4 (https://the-odds-api.com/liveapi/guides/v4/).

Soccer specifics:
  - market 'h2h' is 3-way for soccer (home/draw/away) -> our "1X2"
  - market 'totals' with points == 2.5 -> our "OU25"
  - BTTS and Double Chance are NOT offered by this API (as of 2024 docs);
    those come from API-Football or data/manual_odds.csv.

Quota: usage cost per request = regions x markets (per their docs).
/sports and /scores are free. Remaining quota is read from headers and shown.
"""
from __future__ import annotations

from .util import http_get

BASE = "https://api.the-odds-api.com/v4"


class OddsApi:
    def __init__(self, key: str):
        self.key = key
        self.last_remaining: str | None = None

    def _get(self, path: str, params: dict):
        r = http_get(f"{BASE}{path}", params={"apiKey": self.key, **params})
        self.last_remaining = r.headers.get("x-requests-remaining")
        self.last_used = r.headers.get("x-requests-used")
        if r.status_code == 401:
            raise SystemExit("The Odds API: 401 unauthorised - check ODDS_API_KEY in .env")
        if r.status_code != 200:
            raise RuntimeError(f"Odds API {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def list_soccer_leagues(self) -> list[dict]:
        return [s for s in self._get("/sports", {}) if s.get("group") == "Soccer"]

    def league_scores(self, sport_key: str, days_from: int = 3) -> list[dict]:
        """FREE endpoint: recent results + status (completed flag, live info).

        Used to suppress live/completed fixtures from 'upcoming' predictions
        (task step 9 / section 6). Postponed/cancelled fixtures simply
        disappear from feeds and are caught by the removed-from-feed check.
        """
        return self._get(f"/sports/{sport_key}/scores",
                         {"daysFrom": days_from, "dateFormat": "iso"})

    def league_odds(self, sport_key: str, regions: str, markets: list[str]) -> list[dict]:
        """Returns events: {date, time_utc, home, away, books{book{market{sel:odds}}}}
        plus book_update{book: newest ISO last_update} for staleness checks."""
        js = self._get(f"/sports/{sport_key}/odds", {
            "regions": regions,
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        })
        events = []
        for ev in js:
            home, away = ev["home_team"], ev["away_team"]
            books: dict[str, dict] = {}
            book_update: dict[str, str] = {}
            for bk in ev.get("bookmakers", []):
                bname = bk.get("title") or bk["key"]
                lu = bk.get("last_update") or ""
                if lu:
                    mlu = book_update.get(bname)
                    if mlu is None or lu > mlu:
                        book_update[bname] = lu
                for mk in bk.get("markets", []):
                    if mk.get("last_update"):
                        mlu = book_update.get(bname)
                        if mlu is None or mk["last_update"] > mlu:
                            book_update[bname] = mk["last_update"]
                    if mk["key"] == "h2h":
                        d: dict[str, float] = {}
                        for oc in mk["outcomes"]:
                            if oc["name"] == home:
                                d["home"] = oc["price"]
                            elif oc["name"] == away:
                                d["away"] = oc["price"]
                            else:
                                d["draw"] = oc["price"]
                        if len(d) == 3:
                            books.setdefault(bname, {})["1X2"] = d
                    elif mk["key"] == "totals":
                        d = {}
                        for oc in mk["outcomes"]:
                            if abs(oc.get("point", 2.5) - 2.5) > 1e-9:
                                continue  # keep only the 2.5 line
                            nm = oc["name"].lower()
                            if nm in ("over", "under"):
                                d[nm] = oc["price"]
                        if len(d) == 2:
                            books.setdefault(bname, {})["OU25"] = d
            events.append({
                "date": ev["commence_time"][:10],
                "time_utc": ev["commence_time"][11:16],
                "home": home, "away": away,
                "id": ev.get("id"),
                "books": books, "source": "the-odds-api",
                "book_update": book_update,
            })
        return events
