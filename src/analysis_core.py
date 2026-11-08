"""Additive, site-fixtures-only Over-goals analysis.

This module deliberately reads the existing fixture / history / quote tables but
never changes the main prediction or pick records.  It produces independent
Over 2.5 and Over 5.5 estimates from one Dixon-Coles score matrix, records
pre-kickoff snapshots in separate append-only tables, and is deliberately
honest about information the current free data stack does not provide.

Worldwide discovery is intentionally NOT implemented here: it requires a
current-season worldwide provider.  The page therefore analyses the leagues
already published by the site, rather than pretending that it scanned 100+
matches.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import dixon_coles
from . import historical
from . import model as model_mod
from .settings import load_config
from .teamnames import canonical
from .util import lusaka_today, rolling_window_dates

VERSION = "site-totals-1.0"
MARKETS = {"OU25": {"line": 2.5, "min_probability": 0.55},
           "OU55": {"line": 5.5, "min_probability": 0.08}}
CALIBRATION_MIN = 300
CALIBRATION_MAX_ECE = 0.05

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_runs(
  run_id TEXT PRIMARY KEY, run_date TEXT NOT NULL, generated_utc TEXT NOT NULL,
  version TEXT NOT NULL, counts TEXT NOT NULL, notes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, fixture_key TEXT NOT NULL, fixture_id TEXT,
  date TEXT NOT NULL, kickoff_utc TEXT, league TEXT NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL,
  market TEXT NOT NULL, line REAL NOT NULL, p_over REAL NOT NULL, fair_over REAL,
  lambda_home REAL, lambda_away REAL, verification REAL, label TEXT, confidence TEXT,
  generated_at TEXT NOT NULL, payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', revision_of INTEGER, revision_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_active
  ON analysis_snapshots(fixture_key, market, version_key) WHERE status='active';
CREATE TABLE IF NOT EXISTS analysis_results(
  snapshot_id INTEGER PRIMARY KEY, hg INTEGER, ag INTEGER, total INTEGER,
  over_happened INTEGER, outcome TEXT NOT NULL, settled_at TEXT NOT NULL, source TEXT NOT NULL
);
"""

# Older SQLite installations have no expression/default migration helper.  The
# model version belongs in the unique key so a model change makes a new audited
# forecast; it is harmless to add it after table creation.


def ensure_schema(c) -> None:
    """Create the independent persistence tables and immutable forecast fields."""
    # Keep the version in a real column: SQLite partial unique indexes cannot
    # reference a value embedded in JSON safely.
    c.executescript(SCHEMA.replace(
        "market TEXT NOT NULL, line REAL", "market TEXT NOT NULL, version_key TEXT NOT NULL DEFAULT 'site-totals-1.0', line REAL"))
    c.executescript("""
    CREATE TRIGGER IF NOT EXISTS trg_analysis_snapshot_immutable
    BEFORE UPDATE OF fixture_key, fixture_id, date, kickoff_utc, league, home, away,
      market, version_key, line, p_over, fair_over, lambda_home, lambda_away,
      verification, label, confidence, generated_at, payload
    ON analysis_snapshots
    BEGIN SELECT RAISE(ABORT, 'analysis forecast snapshots are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS trg_analysis_snapshot_nodelete
    BEFORE DELETE ON analysis_snapshots
    BEGIN SELECT RAISE(ABORT, 'analysis forecast snapshots cannot be deleted'); END;
    """)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        v = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)
    except ValueError:
        return None


def _fixture_kickoff(row: dict) -> datetime | None:
    kick = _parse_utc(row.get("kickoff_utc"))
    if kick:
        return kick
    if row.get("date"):
        try:
            time_s = str(row.get("time_utc") or "00:00")[:5]
            return datetime.fromisoformat(f"{row['date']}T{time_s}:00+00:00")
        except ValueError:
            pass
    return None


def _team_rows(hist: pd.DataFrame, team: str, venue: str | None, before: date) -> pd.DataFrame:
    if hist is None or hist.empty:
        return pd.DataFrame()
    if venue == "home":
        out = hist[hist.home == team]
    elif venue == "away":
        out = hist[hist.away == team]
    else:
        out = hist[(hist.home == team) | (hist.away == team)]
    return out[out.date < pd.Timestamp(before)].sort_values("date", ascending=False)


def _summary(rows: pd.DataFrame, team: str) -> dict:
    """Only completed, pre-kickoff results.  Missing sample = None, never zero."""
    if rows is None or rows.empty:
        return {"n": 0, "over25": None, "over55": None, "avg_total": None,
                "avg_gf": None, "avg_ga": None, "last_date": None}
    goals_for, goals_against, totals = [], [], []
    for r in rows.itertuples():
        gf, ga = (int(r.hg), int(r.ag)) if r.home == team else (int(r.ag), int(r.hg))
        goals_for.append(gf); goals_against.append(ga); totals.append(gf + ga)
    n = len(totals)
    return {"n": n, "over25": round(sum(x >= 3 for x in totals) / n, 4),
            "over55": round(sum(x >= 6 for x in totals) / n, 4),
            "avg_total": round(sum(totals) / n, 3), "avg_gf": round(sum(goals_for) / n, 3),
            "avg_ga": round(sum(goals_against) / n, 3),
            "last_date": rows.iloc[0].date.date().isoformat()}


def _h2h(hist: pd.DataFrame, home: str, away: str, before: date) -> dict:
    if hist is None or hist.empty:
        return {"n": 0, "over25": None, "over55": None, "avg_total": None}
    h = hist[((hist.home == home) & (hist.away == away)) |
             ((hist.home == away) & (hist.away == home))]
    h = h[h.date < pd.Timestamp(before)].sort_values("date", ascending=False).head(5)
    # Team name is irrelevant for total-goals H2H statistics.
    if h.empty:
        return {"n": 0, "over25": None, "over55": None, "avg_total": None}
    totals = [int(r.hg) + int(r.ag) for r in h.itertuples()]
    return {"n": len(totals), "over25": round(sum(x >= 3 for x in totals) / len(totals), 4),
            "over55": round(sum(x >= 6 for x in totals) / len(totals), 4),
            "avg_total": round(sum(totals) / len(totals), 3)}


def evidence_bundle(hist: pd.DataFrame, home: str, away: str, before: date) -> dict:
    return {
        "form10_home": _summary(_team_rows(hist, home, None, before).head(10), home),
        "form10_away": _summary(_team_rows(hist, away, None, before).head(10), away),
        "venue10_home": _summary(_team_rows(hist, home, "home", before).head(10), home),
        "venue10_away": _summary(_team_rows(hist, away, "away", before).head(10), away),
        "h2h": _h2h(hist, home, away, before),
    }


def verification_score(row: dict, ev: dict, now: datetime) -> tuple[float, str, list[str]]:
    """Data-quality score, explicitly not a probability or accuracy measure."""
    notes: list[str] = []
    verified = bool(row.get("verified"))
    completeness = float(row.get("completeness") or 0.0)
    fixture = 25.0 if verified else min(15.0, completeness * 0.25)
    form = min(10, ev["form10_home"]["n"]) + min(10, ev["form10_away"]["n"])
    form = 20.0 * form / 20
    venue_n = min(5, ev["venue10_home"]["n"]) + min(5, ev["venue10_away"]["n"])
    venue = 15.0 * venue_n / 10
    h2h = 10.0 * min(3, ev["h2h"]["n"]) / 3
    last = [x.get("last_date") for x in (ev["form10_home"], ev["form10_away"]) if x.get("last_date")]
    fresh = 0.0
    if last:
        ages = [(now.date() - date.fromisoformat(x)).days for x in last]
        fresh = 10.0 if max(ages) <= 60 else 5.0
    # There is no verified team-news or independent worldwide source on this
    # scope.  A cap prevents this page calling anything "fully verified".
    source = 10.0 if verified else 5.0
    total = min(85.0, round(fixture + form + venue + h2h + fresh + source, 1))
    if not verified:
        notes.append("site fixture did not pass the main-pipeline verification gate")
    if min(ev["form10_home"]["n"], ev["form10_away"]["n"]) < 5:
        notes.append("one or both teams have fewer than five recorded recent matches")
    if min(ev["venue10_home"]["n"], ev["venue10_away"]["n"]) < 3:
        notes.append("home/away venue sample is below three matches")
    notes.append("team news and a second worldwide fixture source are unavailable in tracked-leagues-only mode")
    category = "Verified with limitations" if total >= 70 else "Partially verified" if total >= 50 else "Insufficient evidence"
    return total, category, notes


def goal_probabilities(lam: float, mu: float, rho: float, max_goals: int) -> dict[str, float]:
    """O2.5 and O5.5 from the same normalized score matrix."""
    matrix = dixon_coles.score_matrix(lam, mu, rho, max_goals)
    g = np.arange(matrix.shape[0])
    totals = g[:, None] + g[None, :]
    out = {}
    for market, spec in MARKETS.items():
        p = float(matrix[totals > spec["line"]].sum())
        out[market] = max(0.0, min(1.0, p))
    return out


def _quote_block(c, mkey: str, now: datetime, stale_hours: float) -> dict:
    """Use only the main pipeline's most recent two-sided O2.5 prices."""
    run = c.execute("SELECT run_id FROM quotes WHERE mkey=? AND market='OU25' ORDER BY rowid DESC LIMIT 1", (mkey,)).fetchone()
    if not run:
        return {"available": False, "reason": "no current O2.5 bookmaker prices recorded"}
    rows = c.execute("SELECT book,selection,odds,captured_at FROM quotes WHERE mkey=? AND market='OU25' AND run_id=?",
                     (mkey, run["run_id"])).fetchall()
    books: dict[str, dict] = {}
    stamps: list[datetime] = []
    for r in rows:
        if r["selection"] not in ("over", "under") or not isinstance(r["odds"], (int, float)) or r["odds"] <= 1.01:
            continue
        books.setdefault(r["book"], {})[r["selection"]] = float(r["odds"])
        stamp = _parse_utc(r["captured_at"])
        if stamp:
            stamps.append(stamp)
    twosided = {b: x for b, x in books.items() if "over" in x and "under" in x}
    if not twosided:
        return {"available": False, "reason": "no usable two-sided O2.5 bookmaker price"}
    captured = min(stamps) if stamps else None
    if captured and (now - captured).total_seconds() / 3600 > stale_hours:
        return {"available": False, "reason": "O2.5 prices are stale and were not used"}
    fair = []
    for x in twosided.values():
        io, iu = 1 / x["over"], 1 / x["under"]
        fair.append(io / (io + iu))
    best_book = max(twosided, key=lambda b: twosided[b]["over"])
    return {"available": True, "best_book": best_book, "best_over_odds": twosided[best_book]["over"],
            "fair_over": sum(fair) / len(fair), "books": len(twosided),
            "captured_at": _iso_now(captured) if captured else None}


def _market_row(market: str, p: float, ev: dict, verification: float, quote: dict | None) -> dict:
    spec = MARKETS[market]
    key = "over25" if market == "OU25" else "over55"
    form_values = [ev["form10_home"].get(key), ev["form10_away"].get(key)]
    vals = [x for x in form_values if x is not None]
    form_rate = round(sum(vals) / len(vals), 4) if vals else None
    # O5.5 is a tail market: require an actual six-goal historical instance or
    # a very high combined recent-goals average; never inherit O2.5 enthusiasm.
    structural = True
    if market == "OU55":
        structural = any((x or 0) > 0 for x in form_values) or (
            (ev["form10_home"].get("avg_total") or 0) + (ev["form10_away"].get("avg_total") or 0)) / 2 >= 3.2
    if p < spec["min_probability"]:
        label, why = "No recommendation", f"model probability below {spec['min_probability'] * 100:.0f}% threshold"
    elif not structural:
        label, why = "No recommendation", "no independent high-scoring support for the O5.5 tail"
    elif verification >= 70:
        # No Good/Strong labels until 300 resolved forecasts are calibrated.
        label, why = "Moderate statistical candidate", "ranked by probability and evidence; not by EV"
    else:
        label, why = "Risky candidate", "probability passes but evidence quality is below the qualifying threshold"
    value = {"available": False, "reason": "no real current bookmaker price for this market"}
    if quote and quote.get("available"):
        value = {**quote, "edge": round(p - quote["fair_over"], 4),
                 "ev": round(p * quote["best_over_odds"] - 1, 4)}
    return {"market": market, "line": spec["line"], "p_over": round(p, 4),
            "p_under": round(1 - p, 4), "fair_over": round(1 / p, 3) if p else None,
            "form_rate": form_rate, "label": label, "label_reason": why, "value": value}


def _snapshot(c, run_id: str, a: dict, market: dict, now: datetime) -> str:
    """Append forecast or keep the active one.  A mature snapshot is superseded,
    never edited, preserving pre-match information for calibration."""
    kick = _parse_utc(a["kickoff_utc"])
    if kick is None or now >= kick:
        return "blocked_after_kickoff"
    ex = c.execute("SELECT id,generated_at FROM analysis_snapshots WHERE fixture_key=? AND market=? AND version_key=? AND status='active'",
                   (a["mkey"], market["market"], VERSION)).fetchone()
    parent = None
    reason = None
    if ex:
        old = _parse_utc(ex["generated_at"])
        if old and (now - old).total_seconds() < 24 * 3600:
            return "reused"
        parent, reason = ex["id"], "scheduled refresh after 24 hours"
        c.execute("UPDATE analysis_snapshots SET status='superseded', revision_reason=? WHERE id=?", (reason, parent))
    v = market["value"]
    payload = json.dumps({"evidence": a["evidence"], "market": market, "notes": a["notes"]}, sort_keys=True)
    c.execute("""INSERT INTO analysis_snapshots
      (run_id,fixture_key,fixture_id,date,kickoff_utc,league,home,away,market,version_key,line,p_over,fair_over,
       lambda_home,lambda_away,verification,label,confidence,generated_at,payload,status,revision_of,revision_reason)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (run_id, a["mkey"], a["fid"], a["date"], a["kickoff_utc"], a["league"], a["home"], a["away"],
       market["market"], VERSION, market["line"], market["p_over"], market["fair_over"],
       a["lambda_home"], a["lambda_away"], a["verification"], market["label"], a["confidence"],
       _iso_now(now), payload, "active", parent, reason))
    return "revised" if parent else "inserted"


def _metrics(c, market: str) -> dict:
    rows = c.execute("""SELECT s.p_over,r.over_happened FROM analysis_snapshots s
                        JOIN analysis_results r ON r.snapshot_id=s.id
                        WHERE s.status='active' AND r.outcome='settled' AND s.market=?
                        ORDER BY s.kickoff_utc,s.id""", (market,)).fetchall()
    ps = [float(r["p_over"]) for r in rows]
    ys = [int(r["over_happened"]) for r in rows]
    if not ps:
        return {"n": 0, "meaningful": False, "note": "no settled analysis snapshots yet"}
    brier = sum((p-y)**2 for p, y in zip(ps, ys)) / len(ps)
    # 5 broad probability bins; ECE is a diagnostic, never a claim of accuracy.
    bins = [(0,.4),(.4,.55),(.55,.7),(.7,.85),(.85,1.00001)] if market == "OU25" else [(0,.05),(.05,.1),(.1,.2),(.2,.35),(.35,1.00001)]
    ece = 0.0
    for lo, hi in bins:
        idx = [i for i,p in enumerate(ps) if lo <= p < hi]
        if idx:
            ece += len(idx)/len(ps)*abs(sum(ps[i] for i in idx)/len(idx)-sum(ys[i] for i in idx)/len(idx))
    return {"n": len(ps), "brier": round(brier,4), "ece": round(ece,4),
            "meaningful": len(ps) >= 30,
            "calibrated": len(ps) >= CALIBRATION_MIN and ece <= CALIBRATION_MAX_ECE,
            "note": "metrics are not statistically meaningful until at least 30 settled snapshots" if len(ps) < 30 else ""}


def settle_from_history(c, cfg: dict, now: datetime) -> dict:
    """Settle only active snapshots against real football-data.co.uk results.

    Missing or not-yet-published results remain unresolved; no result is guessed.
    """
    pending = c.execute("""SELECT s.* FROM analysis_snapshots s LEFT JOIN analysis_results r ON r.snapshot_id=s.id
                           WHERE s.status='active' AND r.snapshot_id IS NULL AND s.date < ?""",
                        (lusaka_today().isoformat(),)).fetchall()
    by_league = {x["name"]: x for x in cfg.get("leagues", [])}
    cache: dict[str, pd.DataFrame] = {}
    count = {"settled": 0, "unresolved": 0}
    for s in pending:
        league = by_league.get(s["league"])
        if not league:
            count["unresolved"] += 1; continue
        try:
            hist = cache.setdefault(s["league"], historical.load_results(league["fd"], historical.seasons_back(
                __import__("src.settings", fromlist=["current_season_code"]).current_season_code(now.date()), 2)))
            d = pd.Timestamp(s["date"])
            found = hist[(hist.date >= d - pd.Timedelta(days=1)) & (hist.date <= d + pd.Timedelta(days=1)) &
                         (hist.home.map(canonical) == canonical(s["home"])) &
                         (hist.away.map(canonical) == canonical(s["away"]))]
            if len(found) != 1:
                count["unresolved"] += 1; continue
            r = found.iloc[0]; total = int(r.hg) + int(r.ag)
            c.execute("INSERT INTO analysis_results VALUES (?,?,?,?,?,?,?,?)",
                      (s["id"], int(r.hg), int(r.ag), total, int(total > float(s["line"])),
                       "settled", _iso_now(now), "football-data.co.uk"))
            count["settled"] += 1
        except Exception:
            count["unresolved"] += 1
    return count


def run(c, cfg: dict | None = None, now: datetime | None = None) -> dict:
    """Build the tracked-leagues-only independent analysis result."""
    cfg = cfg or load_config()
    now = now or _utc_now()
    ensure_schema(c)
    run_date = lusaka_today()
    dates = rolling_window_dates(run_date, int(cfg.get("report_window_days", 4)))
    tz = ZoneInfo(cfg.get("timezone", "Africa/Lusaka"))
    settle = settle_from_history(c, cfg, now)
    qmarks = ",".join("?" * len(dates))
    fixtures = [dict(x) for x in c.execute(
        f"""SELECT * FROM fixtures WHERE date IN ({qmarks}) AND status='scheduled'
             ORDER BY date, COALESCE(kickoff_utc,time_utc), league, home""", dates).fetchall()]
    leagues = {x["name"]: x for x in cfg.get("leagues", [])}
    contexts: dict[str, dict] = {}
    analyses, excluded, notes = [], [], [
        "Tracked-leagues-only mode: the page does not claim a worldwide 100+ match scan.",
        "No paid worldwide provider is configured; team news is shown as unavailable rather than invented.",
    ]
    for f in fixtures:
        kick = _fixture_kickoff(f)
        if kick is None or kick <= now:
            excluded.append({"fixture": f, "reason": "kickoff missing or already passed"}); continue
        league = leagues.get(f["league"])
        if not league:
            excluded.append({"fixture": f, "reason": "league is not configured for historical analysis"}); continue
        if f["league"] not in contexts:
            try:
                hist = historical.load_results(league["fd"], historical.seasons_back(
                    __import__("src.settings", fromlist=["current_season_code"]).current_season_code(run_date),
                    int(cfg.get("model", {}).get("history_seasons", 3))))
                if hist.empty:
                    raise ValueError("no historical results")
                contexts[f["league"]] = {"hist": hist, "model": model_mod.fit(hist, cfg.get("model", {}),
                    pd.Timestamp(run_date), league["fd"])}
            except Exception as exc:
                contexts[f["league"]] = {"error": f"historical/model load failed: {type(exc).__name__}"}
        ctx = contexts[f["league"]]
        if "error" in ctx:
            excluded.append({"fixture": f, "reason": ctx["error"]}); continue
        hist, fitted = ctx["hist"], ctx["model"]
        pred = fitted.predict(f["home"], f["away"])
        if not pred:
            excluded.append({"fixture": f, "reason": "model could not map both teams"}); continue
        before = kick.astimezone(tz).date()
        ev = evidence_bundle(hist, f["home"], f["away"], before)
        ver, category, vnotes = verification_score(f, ev, now)
        probs = goal_probabilities(pred["lambda_home"], pred["lambda_away"], fitted.dc["rho"], fitted.max_goals)
        quote = _quote_block(c, f["mkey"], now, float(cfg.get("odds_stale_hours", 36)))
        a = {"fid": f["fid"], "mkey": f["mkey"], "date": f["date"], "kickoff_utc": kick.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "kickoff_cat": kick.astimezone(tz).strftime("%a %d %b %H:%M CAT"), "league": f["league"],
             "country": f.get("country") or "", "home": f["home"], "away": f["away"],
             "lambda_home": round(pred["lambda_home"], 3), "lambda_away": round(pred["lambda_away"], 3),
             "expected_total": round(pred["lambda_home"] + pred["lambda_away"], 3),
             "verification": ver, "verification_category": category, "confidence": "moderate" if ver >= 70 else "low",
             "evidence": ev, "notes": vnotes, "markets": []}
        for market, p in probs.items():
            m = _market_row(market, p, ev, ver, quote if market == "OU25" else None)
            a["markets"].append(m)
        analyses.append(a)
    actions = Counter()
    run_id = f"analysis_{run_date.isoformat()}_{now.strftime('%H%M%S')}"
    for a in analyses:
        for m in a["markets"]:
            actions[_snapshot(c, run_id, a, m, now)] += 1
    rank25 = sorted([a for a in analyses if a["markets"][0]["label"] != "No recommendation"],
                    key=lambda a: (-a["markets"][0]["p_over"], -a["verification"], a["kickoff_utc"]))
    rank55 = sorted([a for a in analyses if a["markets"][1]["label"] != "No recommendation"],
                    key=lambda a: (-a["markets"][1]["p_over"], -a["verification"], a["kickoff_utc"]))
    metrics = {market: _metrics(c, market) for market in MARKETS}
    counts = {"fixtures_discovered": len(fixtures), "analysed": len(analyses), "excluded": len(excluded),
              "ou25_candidates": len(rank25), "ou55_candidates": len(rank55), "snapshots": dict(actions), **settle}
    c.execute("INSERT OR REPLACE INTO analysis_runs VALUES (?,?,?,?,?,?)",
              (run_id, run_date.isoformat(), _iso_now(now), VERSION, json.dumps(counts), json.dumps(notes)))
    return {"run_id": run_id, "run_date": run_date.isoformat(), "generated_utc": _iso_now(now),
            "version": VERSION, "window": dates, "counts": counts, "notes": notes,
            "analyses": analyses, "excluded": excluded, "rank_ou25": rank25, "rank_ou55": rank55,
            "metrics": metrics}
