#!/usr/bin/env python3
"""Daily pipeline v3: fetch -> VERIFY -> gate -> predict -> publish 4-day window.

Window (task A): every fixture whose actual kickoff TIMESTAMP falls within the
next `report_window_days` calendar days in the configured timezone
(Africa/Lusaka). Dates are never hardcoded. Fixtures outside the window are
still tracked in the DB (freshness/status) but not presented as upcoming.

Publication gate (tasks C/D/F): a fixture only gets published numbers when
  verified + completeness >= threshold + model trained on REAL history for
  both teams (never the promoted-team heuristic fallback) + probabilities pass
  mathematical validation (sums/bounds) + kickoff in window + not started.
EV/edge only when a real, current (non-stale), correctly mapped bookmaker
price exists. Anything else: "Prediction withheld - insufficient verified
data" with the concrete missing-data reason; the fixture stays visible.

Prediction identity (task E): (fixture_id, market, selection, model_version).
Repeats REUSE the stored snapshot; deliberate recalculation (RECALC_PREDICTIONS)
supersedes with a revision record; originals are never overwritten.

Usage:
    python run_daily.py                     # live run (4-day window)
    python run_daily.py --date 2026-10-25   # base the window on another date
    python run_daily.py --demo E0           # OFFLINE demo (no API keys)
Env:  SUSPEND_PREDICTIONS=1  -> admin kill-switch: verify & report only.
      RECALC_PREDICTIONS=1   -> deliberate recalculation (revision-logged).
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src import db
from src import goals_analysis as gan
from src.historical import load_results, seasons_back, latest_results_with_odds
from src.model import MODEL_VERSION, fit as fit_model
from src.settings import (REPORTS, api_key, af_season_year, current_season_code,
                          load_config, load_env)
from src.teamnames import canonical
from src.util import match_key
from src.value import evaluate_market, bet_won, probs_valid, odds_freshness
from src.verification import (BLOCKED_STATUSES, Fixture, Verifier,
                              confidence_category, publication_gate)
from src.report import render, write_picks_csv

AGG_BOOK_NAMES = {"Max", "Avg", "BbMx", "BbAv", "BFE"}
MODEL_MKTS = ["1X2", "OU15", "OU25", "OU35", "BTTS", "DC"]   # model computes all
SUSPEND = os.environ.get("SUSPEND_PREDICTIONS", "").lower() in ("1", "true", "yes")
RECALC = os.environ.get("RECALC_PREDICTIONS", "").lower() in ("1", "true", "yes")


def log(msg: str) -> None:
    print(msg, flush=True)


def git_short_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def model_version() -> str:
    return f"{MODEL_VERSION}@{git_short_sha()}"


def kickoff_dt(date_s: str, time_utc: str | None) -> datetime | None:
    """Actual kickoff timestamp (UTC) - the ONLY basis for window/status logic."""
    if not date_s:
        return None
    t = (time_utc or "00:00")[:5]
    try:
        return datetime.fromisoformat(f"{date_s}T{t}:00").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_manual_odds(path: Path, wanted: set[str]) -> list[dict]:
    """Manual local-book prices for ANY date inside the window (not just today)."""
    if not path.exists():
        return []
    events: dict[tuple, dict] = {}
    with open(path) as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#") and ln.strip()]
    for row in csv.DictReader(lines):
        rdate = (row.get("date") or "").strip()
        if rdate not in wanted:
            continue
        try:
            odds = float(row["odds"])
        except (TypeError, ValueError):
            log(f"  [manual] bad odds in row: {row}")
            continue
        if odds <= 1.0:
            continue
        market = row["market"].strip().upper().replace(" ", "")
        sel = (row["selection"].strip().lower()
               if market in ("1X2", "OU15", "OU25", "OU35", "BTTS")
               else row["selection"].strip().upper())
        if market not in ("1X2", "OU15", "OU25", "OU35", "BTTS", "DC"):
            log(f"  [manual] unknown market '{market}' -> row skipped")
            continue
        if market in ("OU15", "OU25", "OU35") and sel not in ("over", "under"):
            log(f"  [manual] {market} selection must be over/under -> row skipped")
            continue
        key = (rdate, canonical(row["home"]), canonical(row["away"]))
        ev = events.setdefault(key, {
            "date": rdate, "time_utc": (row.get("time_utc") or "").strip() or None,
            "home": row["home"], "away": row["away"],
            "league_hint": row.get("league", ""), "books": {}, "source": "manual",
            "id": None})
        book = row["book"].strip() or "manual"
        ev["books"].setdefault(book, {})[market] = {
            **ev["books"].get(book, {}).get(market, {}), sel: odds}
    return list(events.values())


def invert_books(books: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for b, mkts in books.items():
        for m, sels in mkts.items():
            out.setdefault(m, {})[b] = sels
    return out


def build_league_events(cfg: dict, league: dict, window_set: set[str], run_date: str,
                        odds_api, af, manual_events: list, matcher,
                        sync_ctx) -> tuple[dict, list]:
    """Merge The Odds API + API-Football + manual CSV for one league.
    Events span several days (feed = upcoming fixtures); the caller assigns
    day membership by actual kickoff timestamp."""
    events: dict[str, dict] = {}
    unmatched: list[str] = []
    exc = [x.lower() for x in cfg.get("odds_api", {}).get("exclude_books", [])]
    dropped_books: set[str] = set()

    def excluded(book: str) -> bool:
        if any(x in book.lower() for x in exc):
            dropped_books.add(book)
            return True
        return False

    def merge(ev, source):
        h, a = matcher.match(ev["home"]), matcher.match(ev["away"])
        for who, raw, m in (("home", ev["home"], h), ("away", ev["away"], a)):
            if m is None:
                unmatched.append(f"{league['name']}: could not match {who} team "
                                 f"'{raw}' ({source})")
        h = h or canonical(ev["home"])
        a = a or canonical(ev["away"])
        k = match_key(ev["date"], h, a)
        cur = events.setdefault(k, {
            "date": ev["date"], "time_utc": ev.get("time_utc"), "home": h, "away": a,
            "league": league["name"], "books": {}, "sources": [],
            "provider_event_id": None, "provider": None, "book_update": {}})
        cur["sources"].append(source)
        if ev.get("time_utc"):
            cur["time_utc"] = ev["time_utc"]
        if source == "the-odds-api" and ev.get("id"):
            cur["provider_event_id"] = ev["id"]
            cur["provider"] = "the-odds-api"
            for b, lu in (ev.get("book_update") or {}).items():
                if b not in cur["book_update"] or lu > cur["book_update"][b]:
                    cur["book_update"][b] = lu
        for b, mkts in ev["books"].items():
            if excluded(b):
                continue
            cur["books"].setdefault(b, {}).update(mkts)

    if odds_api:
        tier = league.get("tier", "core")
        markets = (cfg["odds_api"].get("markets_extended") if tier == "extended"
                   else cfg["odds_api"].get("markets_core")) or ["h2h"]
        guard = int(cfg["odds_api"].get("quota_guard_remaining", 0))
        remaining = getattr(odds_api, "last_remaining", None)
        if tier == "extended" and guard and remaining is not None:
            try:
                if int(remaining) < guard:
                    log(f"  [odds-api] SKIP {league['name']}: quota {remaining} below "
                        f"guard {guard} - extended tier deferred (budget protection)")
                    with db.conn() as c:
                        db.log_sync(c, "the-odds-api", f"odds/{league['odds_api']}", False,
                                    f"skipped by quota guard ({remaining} < {guard})")
                    odds_api = None
            except ValueError:
                pass
        if odds_api:
            try:
                evs = odds_api.league_odds(league["odds_api"], cfg["odds_api"]["regions"],
                                           markets)
                for ev in evs:
                    merge(ev, "the-odds-api")
                sync_ctx("the-odds-api", f"odds/{league['odds_api']}", True,
                         f"{len(evs)} events [{tier}:{','.join(markets)}]",
                         odds_api.last_remaining)
            except Exception as e:
                log(f"  [odds-api] {league['name']}: {e}")
                sync_ctx("the-odds-api", f"odds/{league['odds_api']}", False,
                         str(e)[:300], getattr(odds_api, "last_remaining", None))
    if af:
        for d in sorted(window_set):
            try:
                for ev in af.odds_for_date(league["af_id"], af_season_year(), d,
                                           cfg["api_football"]["extra_bets"]):
                    merge(ev, "api-football")
            except Exception as e:
                log(f"  [api-football] {league['name']} {d}: {e}")
                sync_ctx("api-football", f"odds/{league['af_id']}", False, str(e)[:300])
    for ev in manual_events:
        hint = (ev.get("league_hint") or "").lower()
        if league["name"].lower() in hint or hint in league["name"].lower() or not hint:
            merge(ev, "manual")
    for b in sorted(dropped_books):
        unmatched.append(f"{league['name']}: bookmaker '{b}' excluded entirely "
                         f"(config odds_api.exclude_books - exchange/bad-feed list)")
    return events, unmatched


def af_second_source(cfg, league, window_dates: list[str], run_date_obj: date,
                     demo: bool, sync_ctx):
    """Coverage-first 2nd-source cross-check (task B + C step: independent).

    Ask the provider what seasons/endpoints this subscription covers
    (cached 24h in af_coverage). Only call an endpoint when the provider
    CONFIRMS coverage - otherwise record the structured reason. Never faked.
    Returns (fixtures_list | None, short_status_for_display, log_detail)."""
    if demo or not cfg.get("api_football", {}).get("enabled"):
        return None, "second source: not configured (API-Football disabled)", None
    from src.api_football import (ApiFootball, APIFootballError, season_covered,
                                  covered_seasons_label)
    season = af_season_year(run_date_obj)
    try:
        afc = ApiFootball(api_key("API_FOOTBALL_KEY"))
    except SystemExit:
        return None, "second source: unavailable (API_FOOTBALL_KEY not set)", None
    with db.conn() as c:
        cov = db.get_af_coverage(c, league["af_id"])
    if cov is None:
        try:
            cov = afc.league_coverage(league["af_id"])
            with db.conn() as c:
                db.set_af_coverage(c, cov)
                db.log_sync(c, "api-football", f"leagues/{league['af_id']}", True,
                            f"coverage: seasons {covered_seasons_label(cov)}",
                            afc.remaining)
        except APIFootballError as e:
            sync_ctx("api-football", f"leagues/{league['af_id']}", False,
                     f"coverage query failed ({e.kind}): {e}")
            return None, f"second source: unavailable (coverage query failed: {e.kind})", None
    if not season_covered(cov, season):
        lbl = covered_seasons_label(cov)
        with db.conn() as c:
            db.log_sync(c, "api-football", f"fixtures/{league['af_id']}", False,
                        f"season {season} NOT in provider coverage ({lbl}) - "
                        f"cross-check skipped by plan scope (not an outage)")
        return None, ("second source: unavailable (plan covers seasons "
                      f"{lbl}; current season {season} not covered)"), None
    # provider confirms coverage of this season -> real cross-check
    try:
        out: list[dict] = []
        for d in window_dates:
            for r in afc.fixtures_on(league["af_id"], season, d):
                out.append({"date": r["date"], "home": canonical(r["home"]),
                            "away": canonical(r["away"]), "status": r.get("status", "")})
        sync_ctx("api-football", f"fixtures/{league['af_id']}", True,
                 f"{len(out)} fixtures over {len(window_dates)} days", afc.remaining)
        return out, f"ok ({len(out)} fixtures)", None
    except APIFootballError as e:
        sync_ctx("api-football", f"fixtures/{league['af_id']}", False,
                 f"{e.kind}: {e}")
        return None, f"second source: unavailable ({e.kind})", None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD base date (default: today, Lusaka time)")
    ap.add_argument("--demo", metavar="FD_CODE",
                    help="offline demo on real historical closing odds, e.g. --demo E0")
    ap.add_argument("--league", help="run only this league (football-data code)")
    args = ap.parse_args()

    cfg = load_config()
    load_env()
    tz = ZoneInfo(cfg.get("timezone", "Africa/Lusaka"))
    now_local = datetime.now(tz)
    run_date = args.date or now_local.date().isoformat()
    run_date_obj = date.fromisoformat(run_date)
    window_days = int(cfg.get("report_window_days", 4))
    from src.util import rolling_window_dates
    window_dates = rolling_window_dates(run_date_obj, window_days)
    window_set = set(window_dates)
    if args.demo:
        window_dates = [run_date]           # demo replays a single day
        window_set = {run_date}
    run_id = f"{run_date}_{datetime.now().strftime('%H%M%S')}"
    demo = bool(args.demo)
    susp = SUSPEND
    mver = model_version()
    now_utc = datetime.now(timezone.utc)

    leagues = [l for l in cfg["leagues"] if l.get("enabled")]
    if args.league:
        leagues = [l for l in leagues if l["fd"] == args.league]
    if demo:
        leagues = [l for l in cfg["leagues"] if l["fd"] == args.demo]

    odds_api = af = None
    if not demo:
        if cfg.get("odds_api", {}).get("enabled"):
            from src.odds_api import OddsApi
            odds_api = OddsApi(api_key("ODDS_API_KEY"))
        if cfg.get("api_football", {}).get("enabled") and cfg["api_football"].get("use_odds"):
            from src.api_football import ApiFootball
            af = ApiFootball(api_key("API_FOOTBALL_KEY"))

    verifier = Verifier(cfg, now_utc=now_utc)
    manual_events = [] if demo else parse_manual_odds(Path("data/manual_odds.csv"),
                                                      window_set)
    all_picks, all_fixtures, unmatched_all = [], [], []
    overA, overB = [], []                 # Over 2.5 / Over 3.5 strategy tables
    tier_rank = {"high": 3, "moderate": 2, "low": 1}
    n_fixtures_total = 0
    counts = {"predicted": 0, "withheld": 0, "failed": 0, "blocked": 0}
    summary_extra: list[str] = []

    feed_health = {"odds_ok": 0, "odds_fail": 0}

    def sync_ctx(source, endpoint, ok, detail="", quota=None):
        if source == "the-odds-api" and str(endpoint).startswith("odds/"):
            feed_health["odds_ok" if ok else "odds_fail"] += 1
        with db.conn() as c:
            db.log_sync(c, source, endpoint, ok, detail, quota)

    for league in leagues or []:
        log(f"[{league['name']}]")
        seasons = seasons_back(current_season_code(run_date_obj),
                               int(cfg["model"].get("history_seasons", 3)))
        hist = load_results(league["fd"], seasons)
        if hist.empty:
            log(f"  no historical data found for {league['fd']} ({seasons}) - skipped")
            continue
        model = fit_model(hist, cfg["model"], pd.Timestamp(run_date_obj), league["fd"])
        matcher = model.matcher()
        log(f"  model fitted on {model.dc['n_matches']} matches "
            f"(home adv {model.dc['home_adv']:.3f}, rho {model.dc['rho']:.3f}, "
            f"converged={model.dc['converged']})")

        # scores_map is filled AFTER events are merged (see below), because the
        # gating decision depends on whether events kick off soon.
        scores_map: dict[str, dict] = {}

        # ---------- second source: coverage-first cross-check (task B/C) -----
        second_source, second_status, _ = af_second_source(
            cfg, league, window_dates, run_date_obj, demo, sync_ctx)

        # ---------- build events -------------------------------------------
        if demo:
            got = latest_results_with_odds(league["fd"], current_season_code(run_date_obj))
            if not got:
                log("  no current-season file for demo")
                continue
            res, pack = got
            played = res.dropna(subset=["hg"])
            counts_d = played.groupby("date").size()
            recent = counts_d[counts_d.index >= counts_d.index.max() - pd.Timedelta(days=14)]
            full_days = recent[recent >= 5]
            day = full_days.index.max() if len(full_days) else counts_d.index.max()
            events = {}
            for i, r in res[res.date == day].iterrows():
                books = pack["odds"].get(int(i), {})
                if not books:
                    continue
                events[match_key(run_date, r.home, r.away)] = {
                    "date": run_date, "time_utc": None, "home": r.home, "away": r.away,
                    "league": league["name"], "books": books,
                    "sources": ["football-data (closing)"], "provider": "demo",
                    "provider_event_id": f"{r.home}-v-{r.away}-{day.date()}",
                    "book_update": {},
                    "_result": (int(r.hg), int(r.ag)) if pd.notna(r.hg) else None}
            log(f"  demo: using {len(events)} real fixtures from {day.date()} "
                f"with closing odds")
            agg_exclude = pack.get("aggregate_books", set()) | AGG_BOOK_NAMES
        else:
            events, un = build_league_events(cfg, league, window_set, run_date,
                                             odds_api, af, manual_events, matcher,
                                             sync_ctx)
            unmatched_all.extend(un)
            agg_exclude = set(AGG_BOOK_NAMES)
            log(f"  {len(events)} fixtures with odds (window {window_dates[0]}.."
                f"{window_dates[-1]})")

        # ---- live/completed status feed (FREE /scores endpoint) ------
        # Only fetched when useful: league has pending unsettled picks, or any
        # merged event kicks off within the next ~27h / started recently.
        if odds_api and not demo:
            n_pending = 0
            with db.conn() as c:
                n_pending = c.execute(
                    "SELECT COUNT(*) n FROM picks WHERE status='pending' AND league=?",
                    (league["name"],)).fetchone()["n"]
            horizon = any(
                (kickoff_dt(ev["date"], ev.get("time_utc")) or now_utc + timedelta(days=9))
                <= now_utc + timedelta(hours=27)
                for ev in events.values())
            if n_pending or horizon:
                try:
                    for s in odds_api.league_scores(league["odds_api"], days_from=3):
                        scores_map[s["id"]] = s
                    sync_ctx("the-odds-api", f"scores/{league['odds_api']}", True,
                             f"{len(scores_map)} rows (pending={n_pending})",
                             odds_api.last_remaining)
                except Exception as e:
                    sync_ctx("the-odds-api", f"scores/{league['odds_api']}", False,
                             str(e)[:250])

        # ---------- STEP 2+3: upsert fixtures, statuses, removed-from-feed ---
        day_index: dict[str, list[str]] = defaultdict(list)
        for k in events:
            day_index[k.split("|")[0]].append(k)

        with db.conn() as c:
            for k, ev in events.items():
                kt = kickoff_dt(ev["date"], ev.get("time_utc"))
                status = "scheduled"
                if demo:
                    # offline replay of a past matchday for layout demo only;
                    # the demo banner states results are already known. Live
                    # status protection is enforced in production & tests.
                    status = "scheduled"
                else:
                    sc = scores_map.get(ev.get("provider_event_id") or "")
                    if sc is not None:
                        if sc.get("completed"):
                            status = "completed"
                        elif kt and kt <= now_utc:
                            status = "live"
                    elif kt and kt <= now_utc:
                        status = "live"      # started (timestamp-based, labelled)
                fx = Fixture(ev.get("provider") or "manual",
                             ev.get("provider_event_id"), league["name"],
                             ev["date"], ev.get("time_utc"), ev["home"], ev["away"],
                             status, country=league.get("country", ""),
                             kickoff_utc=kt.strftime("%Y-%m-%dT%H:%M:%SZ") if kt else None)
                fx.books = ev["books"]
                fx.sources = ev["sources"]
                fx.book_update = ev.get("book_update", {})
                ev["_fixture"] = fx
                db.upsert_fixture(c, fx.to_row())
            if not demo:
                stale = c.execute(
                    """SELECT fid, date FROM fixtures WHERE league=? AND provider='the-odds-api'
                         AND date>=? AND date<=? AND last_seen < datetime('now','-30 hours')""",
                    (league["name"], run_date, window_dates[-1])).fetchall()
                todays_fids = {ev["_fixture"].fid for ev in events.values()
                               if ev.get("_fixture")}
                for row in stale:
                    if row["fid"] not in todays_fids:
                        db.set_fixture_status(c, row["fid"], "removed")
                        db.set_fixture_prediction(c, row["fid"], "excluded-status",
                            "vanished from provider feed (likely postponed/cancelled); "
                            "suppressed until provider resolves")
                        unmatched_all.append(
                            f"{league['name']}: fixture {row['fid']} ({row['date']}) "
                            f"was in the feed but vanished - marked 'removed' for review")
                c.commit()

        league_picks = []

        with db.conn() as c:
            if not demo:
                for k, ev in events.items():
                    for b, mkts in ev["books"].items():
                        for m, sels in mkts.items():
                            for s, o in sels.items():
                                db.log_quote(c, run_id, k, ev["date"], league["name"],
                                             ev["home"], ev["away"], "/".join(ev["sources"]),
                                             b, m, s, o)
            for k, ev in sorted(events.items(), key=lambda kv: (kv[1]["date"],
                                                                kv[1]["home"],
                                                                kv[1]["away"])):
                fx: Fixture = ev["_fixture"]
                # ---- window membership by ACTUAL kickoff timestamp ----
                kt = kickoff_dt(ev["date"], ev.get("time_utc"))
                local_date = kt.astimezone(tz).date().isoformat() if kt else ev["date"]
                in_window = local_date in window_set
                day_idx = window_dates.index(local_date) if in_window else None
                if not in_window:
                    continue  # tracked in DB only; not in this report
                n_fixtures_total += 1

                # ---- model + validation ----------------------------------
                pred = model.predict(ev["home"], ev["away"])
                fh = model.form(ev["home"], run_date_obj)
                fa = model.form(ev["away"], run_date_obj)
                form_ok = fh.get("played", 0) >= 4 and fa.get("played", 0) >= 4
                model_ok_flag = bool(pred["full_history"])
                pok, preason = probs_valid({m: pred["markets"][m]
                                            for m in MODEL_MKTS if m in pred["markets"]})
                if demo:
                    checks, comp, ok_v = [], 100.0, True
                    reason, vnotes = "verified (offline demo fixture)", []
                else:
                    checks = verifier.checks(fx, day_index, second_source, second_status)
                    comp = verifier.completeness(fx, checks, model_ok_flag, form_ok)
                    ok_v, reason, vnotes = verifier.verdict(fx, checks, comp)
                    db.set_fixture_verification(c, fx.fid, ok_v, comp, vnotes)
                    db.log_verification(c, fx.fid, checks)

                kick_local, day_label = "", ""
                if kt:
                    ldt = kt.astimezone(tz)
                    kick_local = ldt.strftime("%a %d %b · %H:%M") + " CAT"
                    day_label = {0: "Today", 1: "Tomorrow"}.get(
                        day_idx, ldt.strftime("%a %d %b")) if day_idx is not None else ""

                # ---- classify into report sections -----------------------
                blocked_map = {
                    "completed": "completed - result flows to results.html "
                                 "(never shown as upcoming)",
                    "postponed": "postponed - suppressed until provider supplies "
                                 "a new date",
                    "cancelled": "cancelled - suppressed (void; any unsettled "
                                 "picks become void)",
                    "abandoned": "abandoned - flagged for manual review",
                    "removed": "vanished from provider feed (likely postponed/"
                               "cancelled) - suppressed, flagged for review",
                }
                base_row = {
                    "league": league["name"], "country": league.get("country", ""),
                    "date": local_date, "date_utc": ev["date"],
                    "time_utc": ev.get("time_utc"), "kickoff_local": kick_local,
                    "day_label": day_label, "day_index": day_idx,
                    "fid": fx.fid, "home": ev["home"], "away": ev["away"],
                    "status": fx.status,
                    "verify": {"verified": bool(ok_v), "completeness": comp,
                               "status": fx.status},
                    "xg": (pred["lambda_home"], pred["lambda_away"]),
                    "form": {"home": fh, "away": fa},
                    "model_version": mver,
                    "markets": [], "markets_priced": [], "notes": list(vnotes),
                }
                if fx.status in BLOCKED_STATUSES:
                    base_row.update(section="blocked",
                                    status_note=blocked_map.get(fx.status, fx.status))
                    all_fixtures.append(base_row)
                    counts["blocked"] += 1
                    db.set_fixture_prediction(c, fx.fid, "excluded-status",
                                              base_row["status_note"])
                    continue
                if fx.status == "live":
                    base_row.update(section="blocked",
                                    status_note="in play (kickoff passed) - no pre-match "
                                                "predictions offered; pre-match and live "
                                                "stats are never mixed")
                    all_fixtures.append(base_row)
                    counts["blocked"] += 1
                    db.set_fixture_prediction(c, fx.fid, "excluded-status",
                                              base_row["status_note"])
                    continue

                # -------- over-goals EVIDENCE (task 3) - all real numbers ----
                ev_date = date.fromisoformat(local_date)
                ev_bundle = gan.fixture_evidence(model, hist, ev["home"], ev["away"],
                                                 ev_date, cfg)
                base_row["evidence"] = ev_bundle
                base_row["evidence_quality"] = gan.evidence_quality(ev_bundle)

                publish, pub_reason = publication_gate(ok_v, reason, model_ok_flag,
                                                       pok, preason, in_window)
                if not publish:
                    sec = "failed" if not ok_v else "withheld"
                    base_row.update(section=sec, withheld_reason=pub_reason,
                                    fail_reason=reason if not ok_v else None)
                    all_fixtures.append(base_row)
                    counts["failed" if not ok_v else "withheld"] += 1
                    db.set_fixture_prediction(c, fx.fid, "failed" if not ok_v else "withheld",
                                              pub_reason)
                    continue

                # ================= PUBLISHED fixture ======================
                n_books_1x2 = sum(1 for b, m in ev["books"].items() if "1X2" in m)
                dc_top = max(pred["markets"]["1X2"], key=pred["markets"]["1X2"].get)
                el = model.elo.prob_1x2(ev["home"], ev["away"], model.base_draw)
                agree = max(el, key=el.get) == dc_top
                conf = confidence_category(comp, agree, n_books_1x2)
                explain = model.explain(ev["home"], ev["away"], pred)
                base_row.update(
                    section="predicted", failed=False,
                    verify={**base_row["verify"], "confidence": conf,
                            "sources": ev["sources"]},
                    explain=explain)

                # staleness: old prices are not 'current' (task D)
                fresh_books, stale_books = odds_freshness(
                    ev.get("book_update", {}),
                    float(cfg.get("odds_stale_hours", 36)), now_utc)
                if stale_books:
                    base_row["notes"].append(
                        "stale prices (>{}h) excluded from value detection: {}".format(
                            int(float(cfg.get("odds_stale_hours", 36))),
                            ", ".join(sorted(set(stale_books))[:4])))
                books_fresh = {b: m for b, m in ev["books"].items()
                               if b not in set(stale_books)}

                by_market = invert_books(books_fresh)
                model_only: list[dict] = []
                base_row["top_score"] = pred.get("top_score")
                generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                snapshot_base = {
                    "lambda_home": round(pred["lambda_home"], 3),
                    "lambda_away": round(pred["lambda_away"], 3),
                    "top_score": list(pred.get("top_score", (0, 0))),
                    "elo": pred["elo"], "full_history": pred["full_history"],
                    "form_home": {kk: fh.get(kk) for kk in ("played", "pts", "gf", "ga")},
                    "form_away": {kk: fa.get(kk) for kk in ("played", "pts", "gf", "ga")},
                    "books_priced": sorted(ev["books"].keys()),
                    "kickoff_utc": fx.kickoff_utc,
                }
                for market in MODEL_MKTS:
                    model_p = pred["markets"].get(market)
                    if model_p is None:
                        continue
                    if market not in by_market:
                        # probability exists (verified model data) but without a
                        # real, current bookmaker price there is NO fair line,
                        # NO edge and NO EV - nothing is invented (task D).
                        model_only.append({"market": market, "probs": model_p})
                        continue
                    summary, rows = evaluate_market(market, by_market[market], model_p,
                                                    cfg, exclude_books=set(agg_exclude))
                    base_row["notes"].extend(summary.get("notes", []))
                    base_row["markets"].append({"market": market, "summary": summary,
                                                "rows": rows})
                    base_row["markets_priced"].append(market)
                    for r in rows:
                        pv2 = {"run_id": run_id, "fixture_id": fx.fid, "mkey": k,
                               "date": local_date, "league": league["name"],
                               "home": ev["home"], "away": ev["away"],
                               "market": market, "selection": r["selection"],
                               "p_model": r["p_model"], "p_fair": r["p_fair"],
                               "model_version": mver, "sources": ev["sources"],
                               "completeness": comp, "confidence": conf,
                               "generated_at": generated_at}
                        _pid, gen_at, reused = db.prediction_v2_insert(
                            c, pv2, {**snapshot_base,
                                     "fair_prob": round(r["p_fair"], 4),
                                     "best_price": r["odds"], "best_book": r["book"]},
                            recalc_reason=("manual recalculation (workflow input)"
                                           if RECALC else None))
                        if reused:
                            r["generated_at"] = gen_at
                            r["reused"] = True
                        if not demo:
                            db.log_prediction(c, run_id, k, local_date, league["name"],
                                              ev["home"], ev["away"], market,
                                              r["selection"], r["p_model"], r["p_fair"],
                                              r["odds"], r["book"])
                        if not r["is_value"] or susp:
                            continue
                        pick = {
                            "run_date": run_date, "date": local_date,
                            "time_utc": ev.get("time_utc"),
                            "league": league["name"], "country": league.get("country", ""),
                            "mkey": k, "home": ev["home"], "away": ev["away"],
                            "market": market, "selection": r["selection"],
                            "book": r["book"], "odds": r["odds"],
                            "p_model": r["p_model"], "p_fair": r["p_fair"],
                            "p_raw": r["p_raw"], "edge": r["edge"], "ev": r["ev"],
                            "kelly": r["stake"] / (cfg["bankroll"] * cfg["kelly_fraction"])
                                     if r["stake"] else 0,
                            "stake": round(r["stake"], 2),
                            "fixture_id": fx.fid, "confidence": conf, "completeness": comp,
                            "kickoff_local": kick_local, "day_label": day_label,
                            "notes": list(vnotes)}
                        if demo and ev.get("_result"):
                            hg, ag = ev["_result"]
                            won = bet_won(market, r["selection"], hg, ag)
                            pick["notes"].append(
                                f"already played: {hg}-{ag} -> {'WON' if won else 'LOST'}")
                        league_picks.append(pick)
                base_row["model_only"] = model_only
                if model_only:
                    base_row["notes"].append(
                        "not priced by configured sources right now - shown as "
                        "model probability only, no EV: "
                        + ", ".join(mo["market"] for mo in model_only))

                # ---- over-goals strategy tables (task 4): rank by tier +
                # ---- calibrated probability + evidence, NEVER raw EV ----
                for mkt in ("OU25", "OU35"):
                    mo = pred["markets"].get(mkt)
                    if not mo:
                        continue
                    p_over = mo["over"]
                    price_row = None
                    for m in base_row["markets"]:
                        if m["market"] == mkt:
                            for r in m["rows"]:
                                if r["selection"] == "over":
                                    price_row = r
                    sup, risk = gan.supporting_risks(ev_bundle, pred, mkt)
                    fh5, fa5 = ev_bundle["form5_home"], ev_bundle["form5_away"]
                    sh, sa = ev_bundle["split_home"], ev_bundle["split_away"]
                    sel_row = {
                        "fixture": f"{ev['home']} v {ev['away']}",
                        "league": league["name"], "country": league.get("country", ""),
                        "kickoff_local": kick_local, "day_label": day_label,
                        "date": local_date, "fid": fx.fid,
                        "p": p_over, "fair_odds": round(1.0 / p_over, 2),
                        "xg_total": round(pred["lambda_home"] + pred["lambda_away"], 2),
                        "top_score": pred.get("top_score"),
                        "conf": conf, "completeness": comp,
                        "quality": base_row["evidence_quality"],
                        "odds": (price_row["odds"] if price_row else None),
                        "book": (price_row["book"] if price_row else None),
                        "ev": (price_row["ev"] if price_row else None),
                        "is_value": bool(price_row and price_row["is_value"]),
                        "form_ev": (f"{ev['home'].title()} last {fh5.get('n',0)}: "
                                    f"{fh5.get('over25_pct','-')}% over2.5; "
                                    f"{ev['away'].title()} last {fa5.get('n',0)}: "
                                    f"{fa5.get('over25_pct','-')}%") ,
                        "venue_ev": (f"home games: {sh.get('over25_pct','-')}% over2.5 "
                                     f"({sh.get('n',0)} matches); away games: "
                                     f"{sa.get('over25_pct','-')}% ({sa.get('n',0)})"),
                        "h2h_ev": (f"{ev_bundle['h2h']['n']} meetings, avg "
                                   f"{ev_bundle['h2h']['avg_total']} goals"
                                   if ev_bundle["h2h"]["n"] else "no recent H2H"),
                        "news": ev_bundle["news"]["status"],
                        "support": sup, "risks": risk,
                        "model_version": mver,
                    }
                    (overA if mkt == "OU25" else overB).append(sel_row)

                all_fixtures.append(base_row)
                counts["predicted"] += 1
                db.set_fixture_prediction(c, fx.fid, "published", "")

            # picks now join ONE global slate; DB insertion happens after
            # exposure caps so what is recorded == what was published
            all_picks.extend(league_picks)

        log(f"  window fixtures: {counts} | value bets so far: {len(league_picks)}"
            + (" (SUSPEND mode: not recorded)" if susp else ""))

    if (not demo and any(l.get("enabled") and l.get("odds_api") for l in leagues or [])
            and feed_health["odds_ok"] == 0):
        summary_extra.append(
            "⚠ BOOKMAKER PRICE FEED UNAVAILABLE TODAY: every attempt to download "
            f"current odds from The Odds API failed ({feed_health['odds_fail']} league "
            "call(s) - usually a rejected/expired API key). This report therefore "
            "contains fixtures and model probabilities only from already-tracked, "
            "previously verified data - NO new value-bet opinions are offered because "
            "EV requires a real current price. Nothing has been relabelled or invented. "
            "The feed resumes automatically as soon as the API key is fixed "
            "(see sync log on the admin page for the exact error).")

    if susp:
        summary_extra.append("PREDICTIONS SUSPENDED by administrator "
                             "(workflow input suspend_predictions); markets analysed "
                             "but no picks published.")
    if RECALC:
        summary_extra.append("Deliberate recalculation requested (RECALC_PREDICTIONS): "
                             "revised predictions are stored as revisions, originals kept.")
    all_picks.sort(key=lambda p: p["edge"], reverse=True)
    for i, p in enumerate(all_picks):
        p["rank"] = i + 1
    if not demo and all_picks:
        from src.value import exposure_caps
        all_picks = exposure_caps(all_picks, float(cfg["bankroll"]),
                                  float(cfg.get("max_stake_fixture_pct", 0.03)),
                                  float(cfg.get("max_stake_total_pct", 0.10)))

    # --- over-goals ranking (documented rule; EV is shown but NEVER sorts) ---
    for tab in (overA, overB):
        tab.sort(key=lambda r: (tier_rank.get(r["conf"], 0), r["p"],
                                r["completeness"], r["quality"]), reverse=True)
        for i, r in enumerate(tab):
            r["rank"] = i + 1

    with db.conn() as c:
        if not demo and not susp:
            for p in all_picks:
                added = db.add_pick(c, p)
                if not added:
                    p.setdefault("notes", []).append(
                        "already published by an earlier run "
                        "(original pick kept - never duplicated)")
        db.log_run(c, run_id, n_fixtures_total, 0 if demo else len(all_picks),
                   meta=("DEMO " + args.demo) if demo else
                        f"live; model={mver}; window={window_days}d"
                        + ("; SUSPEND" if susp else "") + ("; RECALC" if RECALC else ""))

    prefix = "demo_" if demo else ""
    stamp = run_date
    html_path = REPORTS / f"{stamp}_{prefix}report.html"
    csv_path = REPORTS / f"{stamp}_{prefix}picks.csv"
    xlsx_path = REPORTS / f"{stamp}_{prefix}report.xlsx"
    run_info = {"date": run_date,
                "generated_at": now_local.strftime("%Y-%m-%d %H:%M") + " CAT",
                "generated_utc": now_utc.strftime("%Y-%m-%d %H:%M") + " UTC",
                "threshold": cfg["value_threshold"], "bankroll": cfg["bankroll"],
                "n_fixtures": n_fixtures_total, "model_version": mver,
                "window_dates": window_dates, "timezone": "Africa/Lusaka (CAT)",
                "counts": counts, "suspended": susp, "notices": summary_extra,
                "over_tables": {"A": overA, "B": overB}}
    html_path.write_text(render(run_info, all_picks, all_fixtures, unmatched_all,
                                demo=demo))
    write_picks_csv(all_picks, csv_path)
    from src.xlsx_report import build_xlsx
    build_xlsx(run_info, all_picks, all_fixtures, unmatched_all, xlsx_path, demo=demo)

    print("\n================== SUMMARY ==================")
    print(f"date           : {run_date}{'  (DEMO on real historical odds)' if demo else ''}")
    print(f"window         : {window_dates[0]} -> {window_dates[-1]} "
          f"({window_days} calendar days, Africa/Lusaka)")
    print(f"model version  : {mver}")
    print(f"fixtures       : {n_fixtures_total} in window "
          f"({counts['predicted']} predicted, {counts['withheld']} withheld, "
          f"{counts['failed']} failed verification, {counts['blocked']} live/closed)")
    print(f"value bets     : {len(all_picks)}"
          + ("  (suspended - not recorded)" if susp else ""))
    for p in all_picks:
        print(f"  #{p['rank']:>2} {p['home']} v {p['away']}  {p['market']}/{p['selection']} "
              f"@ {p['odds']:.2f} ({p['book']})  edge +{100*p['edge']:.1f}%  "
              f"conf {p.get('confidence','-')}  stake {p['stake']:.2f}")
    if odds_api and getattr(odds_api, "last_remaining", None) is not None:
        print(f"Odds API remaining quota (header): {odds_api.last_remaining}")
    for n in summary_extra:
        print(n)
    if not all_picks:
        print("No value bets met the threshold. That is a normal, honest outcome.")
    print(f"\nReport (HTML)  : {html_path}")
    print(f"Report (Excel) : {xlsx_path}")
    print(f"Picks  (CSV)   : {csv_path}")


if __name__ == "__main__":
    sys.exit(main())
