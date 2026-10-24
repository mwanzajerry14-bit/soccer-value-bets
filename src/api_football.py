"""Client for API-Football v3 (api-sports.io). Free tier: 100 requests/day.

Used for:
  - results (settlement): /fixtures?league&season&from&to
  - additional odds markets NOT on The Odds API: BTTS, Double Chance
    plus extra 1X2 / Over-Under prices: /odds?league&season&date
Free plan note: odds availability lags on some leagues; responses are
disk-cached for the day so re-running the pipeline doesn't burn the quota.
"""
from __future__ import annotations

from datetime import date

from .settings import CACHE
from .util import http_get, read_json, write_json

BASE = "https://v3.football.api-sports.io"

# API-Football bet-name -> (our market, value-label -> our selection)
BET_MAP = {
    "Match Winner": ("1X2", {"Home": "home", "Draw": "draw", "Away": "away"}),
    "Goals Over/Under": ("OU25", None),       # "Over 2.5" parsed dynamically
    "Both Teams Score": ("BTTS", {"Yes": "yes", "No": "no"}),
    "Double Chance": ("DC", {"Home/Draw": "1X", "Home/Away": "12", "Draw/Away": "X2"}),
}


def classify_errors(errs) -> tuple[str, str]:
    """Classify an API-Football `errors` payload into (kind, short message).

    The v3 response carries errors as a dict (or occasionally a list).
    The short message is safe to show to users; the raw payload belongs in
    the sync log only - never in a public report's notes column.
    """
    if isinstance(errs, list):
        text = "; ".join(str(e) for e in errs)
    else:
        text = "; ".join(f"{k}: {v}" for k, v in errs.items())
    low = text.lower()
    if "free plan" in low or "do not have access to this season" in low:
        return "plan_scope", "plan limit: this subscription cannot serve the requested season"
    if "exceeded" in low or "limit" in low or "quota" in low:
        return "rate_limit", "rate limit / quota reached"
    if "parameter" in low or "invalid" in low or "wrong" in low:
        return "bad_parameter", f"invalid parameters: {text[:120]}"
    return "other", text[:160]


class APIFootballError(RuntimeError):
    """Structured provider error (kind: plan_scope / rate_limit /
    bad_parameter / http / other)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class ApiFootball:
    def __init__(self, key: str):
        self.headers = {"x-apisports-key": key}
        self.remaining: str | None = None

    def _get(self, path: str, params: dict) -> dict:
        r = http_get(BASE + path, params=params, headers=self.headers)
        self.remaining = r.headers.get("x-ratelimit-requests-remaining")
        if r.status_code != 200:
            raise APIFootballError("http", f"HTTP {r.status_code} from {path}")
        js = r.json()
        errs = js.get("errors")
        if errs:
            kind, msg = classify_errors(errs)
            raise APIFootballError(kind, msg)
        return js

    # -------------------- coverage discovery (task B) --------------------
    def league_coverage(self, league_id: int) -> dict:
        """GET /leagues?id=... -> what this subscription ACTUALLY covers:
        available seasons + flags for fixtures/odds/statistics.
        We ask the provider instead of assuming (the free plan walls the
        current season; coverage is cached 24h in the DB by the caller)."""
        js = self._get("/leagues", {"id": league_id})
        resp = js.get("response", [])
        if not resp:
            raise APIFootballError("bad_parameter",
                                   f"league id {league_id} unknown to provider")
        entry = resp[0]
        seasons = []
        for s in entry.get("seasons", []):
            seasons.append({"year": s.get("year"), "start": s.get("start"),
                            "end": s.get("end"), "current": bool(s.get("current"))})
        cov = entry.get("coverage") or {}
        return {
            "league_id": league_id,
            "name": (entry.get("league") or {}).get("name", ""),
            "country": (entry.get("country") or {}).get("name", ""),
            "seasons": seasons,
            "coverage": {"fixtures": bool((cov.get("fixtures") or {}).get("events") or
                                          (cov.get("fixtures") or {}).get("lineups")),
                         "odds": bool(cov.get("odds")),
                         "injuries": bool(cov.get("injuries")),
                         "standings": bool(cov.get("standings"))},
        }


def season_covered(coverage: dict, season_year: int) -> bool:
    """True iff the provider-confirmed coverage includes `season_year`."""
    return any(s.get("year") == season_year for s in coverage.get("seasons", []))


def covered_seasons_label(coverage: dict) -> str:
    ys = sorted(s["year"] for s in coverage.get("seasons", []) if s.get("year"))
    if not ys:
        return "no seasons"
    if ys == list(range(ys[0], ys[-1] + 1)):
        return f"{ys[0]}-{ys[-1]}"
    return ",".join(map(str, ys))

    def results_range(self, league_id: int, season: int, dfrom: str, dto: str) -> list[dict]:
        js = self._get("/fixtures", {"league": league_id, "season": season,
                                     "from": dfrom, "to": dto, "status": "FT"})
        out = []
        for f in js.get("response", []):
            out.append({
                "date": f["fixture"]["date"][:10],
                "home": f["teams"]["home"]["name"],
                "away": f["teams"]["away"]["name"],
                "hg": f["goals"]["home"], "ag": f["goals"]["away"],
            })
        return out

    def odds_for_date(self, league_id: int, season: int, d: str,
                      wanted_bets: list[str]) -> list[dict]:
        """All odds for one league+date (paginated). Disk-cached per day."""
        today = date.today().isoformat()
        cp = CACHE / f"af_odds_{league_id}_{d}.json"
        cached = read_json(cp)
        if cached and cached.get("_cached_on") == today and d != today:
            return cached["data"]
        if cached and cached.get("_cached_on") == today == d:
            # today's file: refetch at most every 2h handled outside; keep simple: reuse
            return cached["data"]

        events: dict[int, dict] = {}
        page = 1
        while True:
            js = self._get("/odds", {"league": league_id, "season": season,
                                     "date": d, "page": page})
            paging = js.get("paging", {})
            for item in js.get("response", []):
                fx = item.get("fixture", {})
                ev = events.setdefault(fx["id"], {
                    "date": (fx.get("date") or d)[:10],
                    "home": None, "away": None,
                    "books": {}, "source": "api-football",
                })
                for bm in item.get("bookmakers", []):
                    bname = f"{bm.get('name', 'book')} (AF)"
                    for bet in bm.get("bets", []):
                        if bet["name"] not in wanted_bets or bet["name"] not in BET_MAP:
                            continue
                        market, label_map = BET_MAP[bet["name"]]
                        d2: dict[str, float] = {}
                        for v in bet.get("values", []):
                            val, odd = v.get("value", ""), v.get("odd")
                            try:
                                odd = float(odd)
                            except (TypeError, ValueError):
                                continue
                            if market == "OU25":
                                low = val.lower()
                                if "over" in low and "2.5" in val:
                                    d2["over"] = odd
                                elif "under" in low and "2.5" in val:
                                    d2["under"] = odd
                            else:
                                sel = label_map.get(val)
                                if sel:
                                    d2[sel] = odd
                        n_expected = 3 if market == "1X2" else 2 if market in ("OU25", "BTTS") else 3
                        if len(d2) == n_expected:
                            ev["books"].setdefault(bname, {})[market] = d2
            if not js.get("response") or page >= paging.get("total", 1):
                break
            page += 1

        # team names need a second (cheap-ish) fixture call; merge by fixture id
        if events:
            names = self._fixture_names(league_id, season, d)
            for fid, nm in names.items():
                if fid in events:
                    events[fid]["home"], events[fid]["away"] = nm
        data = [e for e in events.values() if e["home"]]
        write_json(cp, {"_cached_on": today, "data": data})
        return data

    def fixtures_on(self, league_id: int, season: int, d: str) -> list[dict]:
        """Second-source fixture list for cross-verification (task STEP 4).
        NOTE: the free plan cannot serve the CURRENT season - callers must
        catch the plan error and record 'second source unavailable'."""
        js = self._get("/fixtures", {"league": league_id, "season": season, "date": d})
        out = []
        for f in js.get("response", []):
            out.append({
                "date": f["fixture"]["date"][:10],
                "home": f["teams"]["home"]["name"],
                "away": f["teams"]["away"]["name"],
                "status": (f.get("fixture", {}).get("status") or {}).get("long", ""),
            })
        return out

    def _fixture_names(self, league_id: int, season: int, d: str) -> dict[int, tuple[str, str]]:
        js = self._get("/fixtures", {"league": league_id, "season": season, "date": d})
        return {f["fixture"]["id"]: (f["teams"]["home"]["name"], f["teams"]["away"]["name"])
                for f in js.get("response", [])}
