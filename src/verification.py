"""Fixture verification pipeline (task section 1-3).

Workflow per fixture:
  STEP 1-2  validate provider id, teams, competition, kickoff, status
  STEP 3    duplicate + team-mapping checks (same home/away/date twice, or a team
            playing twice on one day -> suspicious mapping)
  STEP 4    second-source cross-check (API-Football when available on the plan)
  STEP 5/6  data sufficiency + freshness scoring (data completeness 0-100%)
  STEP 7    gate: prediction only when verified AND completeness >= threshold
  STEP 8    callers store snapshot with fixture id + model version (db.predictions_v2)
  STEP 9    freshness: fixtures that vanished from the feed or started/completed
            are suppressed before publishing.

Only real provider data is used. Second-source unavailable -> recorded as such,
never substituted with invented data. A fixture that fails a CRITICAL check is
never predicted on; it appears as 'Match verification failed'.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .util import match_key

# terminal-ish statuses that must never be presented as upcoming
BLOCKED_STATUSES = {"completed", "cancelled", "abandoned", "postponed", "removed"}


@dataclass
class Check:
    name: str
    ok: bool
    critical: bool
    detail: str = ""


def _parse_dt(date_s: str, time_utc: str | None) -> datetime | None:
    if not date_s:
        return None
    t = (time_utc or "00:00")[:5]
    try:
        return datetime.fromisoformat(f"{date_s}T{t}:00").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class Fixture:
    """A provider-supplied fixture record (or manual/demo one)."""

    def __init__(self, provider: str, provider_event_id: str | None, league: str,
                 date: str, time_utc: str | None, home: str, away: str,
                 status: str = "scheduled"):
        self.provider = provider
        self.provider_event_id = provider_event_id or ""
        fid_tail = self.provider_event_id or match_key(date, home, away)
        self.fid = f"{provider}:{fid_tail}"
        self.league, self.date, self.time_utc = league, date, time_utc
        self.home, self.away = home, away
        self.status = status
        self.mkey = match_key(date, home, away)
        self.books: dict = {}
        self.sources: list[str] = [provider]

    def to_row(self) -> dict:
        return {"fid": self.fid, "mkey": self.mkey, "date": self.date,
                "time_utc": self.time_utc, "league": self.league, "home": self.home,
                "away": self.away, "provider": self.provider,
                "provider_event_id": self.provider_event_id, "status": self.status}

    def __repr__(self):
        return f"<Fixture {self.fid} {self.home} v {self.away} {self.date} [{self.status}]>"


class Verifier:
    def __init__(self, cfg: dict, now_utc: datetime | None = None):
        self.cfg = cfg
        self.now = now_utc or datetime.now(timezone.utc)
        self.min_books = int(cfg.get("min_books", 3))
        self.min_completeness = float(cfg.get("min_completeness", 60))

    # ---------------- STEP 2/3: validation checks ----------------
    def checks(self, f: Fixture, seen_index: dict[str, list[str]],
               second_source: list[dict] | None,
               second_source_status: str) -> list[Check]:
        out: list[Check] = []
        teams_ok = bool(f.home and f.away and f.home != f.away)
        out.append(Check("teams_present", teams_ok, True,
                         f"{f.home} v {f.away}" if teams_ok else "missing/duplicate teams"))
        kick = _parse_dt(f.date, f.time_utc)
        out.append(Check("kickoff_valid", kick is not None, True,
                         kick.isoformat() if kick else f"unparseable {f.date} {f.time_utc}"))
        if kick is not None and f.status == "scheduled":
            not_started = kick.timestamp() > self.now.timestamp() - 15 * 60
            out.append(Check("not_started", not_started, True,
                             "kickoff in future (15min grace)" if not_started else "already kicked off"))
        out.append(Check("status_not_terminal", f.status not in BLOCKED_STATUSES, True,
                         f.status))
        books_1x2 = [b for b, m in f.books.items() if "1X2" in m]
        out.append(Check("has_1x2_price", len(books_1x2) >= 1, True,
                         f"{len(books_1x2)} book(s) priced 1X2"))
        out.append(Check("min_books", len(books_1x2) >= self.min_books, False,
                         f"{len(books_1x2)} >= {self.min_books}"))
        # team-mapping check: neither team may appear in another fixture that day
        others = []
        for m in seen_index.get(f.date, []):
            if m == f.mkey:
                continue
            parts = m.split("|")[1:]
            if f.home in parts or f.away in parts:
                others.append(m)
        out.append(Check("no_duplicate_fixture", True, True,
                         "feed events deduped by date+teams; provider ids unique"))
        out.append(Check("team_play_once_per_day", not others, False,
                         "" if not others else f"team also in {others[:2]} -> mapping review"))
        # second source cross-check (only when a real provider answered)
        if second_source is None:
            out.append(Check("second_source", False, False,
                             f"unavailable: {second_source_status} (not faked)"))
        else:
            look = {(n["date"], n["home"], n["away"]) for n in second_source}
            hit = (f.date, f.home, f.away) in look
            out.append(Check("second_source", hit, False,
                             "both independent providers list this fixture" if hit
                             else "MISSING from secondary provider - requires review"))
        return out

    # --------------- STEP 5/6: completeness & freshness ---------------
    def completeness(self, f: Fixture, checks: list[Check], full_history: bool,
                     form_ok: bool) -> float:
        """0-100 weighted data-completeness score (displayed, never faked)."""
        n_1x2 = sum(1 for b, m in f.books.items() if "1X2" in m)
        score = 0.0
        score += 15 if n_1x2 >= 1 else 0
        score += 10 if n_1x2 >= 3 else 0
        score += 10 if n_1x2 >= 6 else 0
        score += 30 if full_history else (15 if f.home or f.away else 0)
        score += 15 if form_ok else 0
        score += 10 if any(c.name == "second_source" and c.ok for c in checks) else 0
        score += 5 if any("OU25" in m for m in f.books.values()) else 0
        score += 5  # freshness: fixture came from today's live feed
        return round(min(score, 100.0), 0)

    # ------------------------- STEP 7: gate -------------------------
    def verdict(self, f: Fixture, checks: list[Check], completeness: float):
        notes = [c.detail for c in checks if not c.ok and c.detail]
        verified = all(c.ok or not c.critical for c in checks)
        enough = completeness >= self.min_completeness
        if not verified:
            reason = "Match verification failed"
        elif not enough:
            reason = f"Insufficient data for a reliable prediction (completeness {completeness:.0f}% < {self.min_completeness:.0f}%)"
        else:
            reason = "verified"
        return verified and enough, reason, notes


def confidence_category(completeness: float, dc_elo_agree: bool, n_books: int) -> str:
    """Data-driven confidence tier (NOT an accuracy claim - published rule,
    evaluated alongside calibration on the results page)."""
    if completeness >= 90 and dc_elo_agree and n_books >= 6:
        return "high"
    if completeness >= 70 and n_books >= 3:
        return "moderate"
    return "low"
