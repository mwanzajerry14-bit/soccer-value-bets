#!/usr/bin/env python3
"""Daily pipeline: fetch odds -> VERIFY fixtures -> fit model -> predict ->
flag value -> report + log.

Verification-first (task spec): no prediction is generated for a fixture that
fails verification or has insufficient data - it is reported as
'Match verification failed' / 'Insufficient data...' instead.

Usage:
    python run_daily.py                     # live run for today
    python run_daily.py --date 2026-10-25   # live run for another date
    python run_daily.py --demo E0           # OFFLINE demo on real historical
                                            # closing odds (no API keys needed)
Env:  SUSPEND_PREDICTIONS=1  -> admin kill-switch: verify & report only,
                                publish no picks (set by manual workflow input).
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src import db
from src.historical import load_results, seasons_back, latest_results_with_odds
from src.model import MODEL_VERSION, fit as fit_model
from src.settings import (REPORTS, api_key, af_season_year, current_season_code,
                          load_config, load_env)
from src.teamnames import canonical
from src.util import match_key
from src.value import evaluate_market, bet_won
from src.verification import BLOCKED_STATUSES, Fixture, Verifier, confidence_category
from src.report import render, write_picks_csv

AGG_BOOK_NAMES = {"Max", "Avg", "BbMx", "BbAv", "BFE"}
MARKET_ORDER = ["1X2", "OU25", "BTTS", "DC"]
SUSPEND = os.environ.get("SUSPEND_PREDICTIONS", "").lower() in ("1", "true", "yes")


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


def parse_manual_odds(path: Path, wanted_date: str) -> list[dict]:
    if not path.exists():
        return []
    events: dict[tuple, dict] = {}
    with open(path) as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#") and ln.strip()]
    for row in csv.DictReader(lines):
        if (row.get("date") or "").strip() != wanted_date:
            continue
        try:
            odds = float(row["odds"])
        except (TypeError, ValueError):
            log(f"  [manual] bad odds in row: {row}")
            continue
        if odds <= 1.0:
            continue
        market = row["market"].strip().upper().replace(" ", "")
        sel = (row["selection"].strip().lower() if market in ("1X2", "OU25", "BTTS")
               else row["selection"].strip().upper())
        if market not in ("1X2", "OU25", "BTTS", "DC"):
            log(f"  [manual] unknown market '{market}' -> row skipped")
            continue
        key = (canonical(row["home"]), canonical(row["away"]))
        ev = events.setdefault(key, {
            "date": wanted_date, "time_utc": None, "home": row["home"], "away": row["away"],
            "league_hint": row.get("league", ""), "books": {}, "source": "manual", "id": None})
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


def build_league_events(cfg: dict, league: dict, run_date: str,
                        odds_api, af, manual_events: list, matcher) -> tuple[dict, list]:
    """Merge The Odds API + API-Football + manual CSV for one league.
    Events may span several days (feed gives upcoming fixtures); the caller
    filters to run_date for predictions but verification tracks all."""
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
                unmatched.append(f"{league['name']}: could not match {who} team '{raw}' ({source})")
        h = h or canonical(ev["home"])
        a = a or canonical(ev["away"])
        k = match_key(ev["date"], h, a)
        cur = events.setdefault(k, {"date": ev["date"], "time_utc": ev.get("time_utc"),
                                    "home": h, "away": a, "league": league["name"],
                                    "books": {}, "sources": [], "provider_event_id": None,
                                    "provider": None})
        cur["sources"].append(source)
        if ev.get("time_utc"):
            cur["time_utc"] = ev["time_utc"]
        if source == "the-odds-api" and ev.get("id"):
            cur["provider_event_id"] = ev["id"]
            cur["provider"] = "the-odds-api"
        for b, mkts in ev["books"].items():
            if excluded(b):
                continue
            cur["books"].setdefault(b, {}).update(mkts)

    if odds_api:
        try:
            evs = odds_api.league_odds(league["odds_api"], cfg["odds_api"]["regions"],
                                       cfg["odds_api"]["markets"])
            for ev in evs:
                merge(ev, "the-odds-api")           # all days tracked; day filter later
            with db.conn() as c:
                db.log_sync(c, "the-odds-api", f"odds/{league['odds_api']}", True,
                            f"{len(evs)} events", odds_api.last_remaining)
        except Exception as e:
            log(f"  [odds-api] {league['name']}: {e}")
            with db.conn() as c:
                db.log_sync(c, "the-odds-api", f"odds/{league['odds_api']}", False,
                            str(e)[:300], getattr(odds_api, "last_remaining", None))
    if af:
        try:
            for ev in af.odds_for_date(league["af_id"], af_season_year(), run_date,
                                       cfg["api_football"]["extra_bets"]):
                merge(ev, "api-football")
        except Exception as e:
            log(f"  [api-football] {league['name']}: {e}")
            with db.conn() as c:
                db.log_sync(c, "api-football", f"odds/{league['af_id']}", False, str(e)[:300])
    for ev in manual_events:
        hint = (ev.get("league_hint") or "").lower()
        if league["name"].lower() in hint or hint in league["name"].lower() or not hint:
            merge(ev, "manual")
    for b in sorted(dropped_books):
        unmatched.append(f"{league['name']}: bookmaker '{b}' excluded entirely "
                         f"(config odds_api.exclude_books - exchange/bad-feed list)")
    return events, unmatched


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, config timezone)")
    ap.add_argument("--demo", metavar="FD_CODE",
                    help="offline demo on real historical closing odds, e.g. --demo E0")
    ap.add_argument("--league", help="run only this league (football-data code, e.g. E0)")
    args = ap.parse_args()

    cfg = load_config()
    load_env()
    tz = ZoneInfo(cfg.get("timezone", "Africa/Johannesburg"))
    run_date = args.date or datetime.now(tz).date().isoformat()
    run_date_obj = date.fromisoformat(run_date)
    run_id = f"{run_date}_{datetime.now().strftime('%H%M%S')}"
    demo = bool(args.demo)
    susp = SUSPEND
    mver = model_version()

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

    verifier = Verifier(cfg, now_utc=datetime.now(timezone.utc))
    manual_events = parse_manual_odds(Path("data/manual_odds.csv"), run_date)
    all_picks, all_fixtures, unmatched_all = [], [], []
    n_fixtures_total = 0
    summary_extra = []

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

        # ---------- STEP 1: fixtures with status (real provider data) ----------
        scores_map: dict[str, dict] = {}
        if odds_api:
            try:
                for s in odds_api.league_scores(league["odds_api"], days_from=3):
                    scores_map[s["id"]] = s
                with db.conn() as c:
                    db.log_sync(c, "the-odds-api", f"scores/{league['odds_api']}", True,
                                f"{len(scores_map)} rows", odds_api.last_remaining)
            except Exception as e:
                with db.conn() as c:
                    db.log_sync(c, "the-odds-api", f"scores/{league['odds_api']}", False, str(e)[:250])

        # ---------- second independent source for cross-verification ----------
        second_source, second_status = None, "not configured (API-Football odds disabled)"
        if cfg.get("api_football", {}).get("enabled") and not demo:
            try:
                from src.api_football import ApiFootball
                afc = ApiFootball(api_key("API_FOOTBALL_KEY"))
                raw = afc.fixtures_on(league["af_id"], af_season_year(run_date_obj),
                                      run_date)
                second_source = [{"date": r["date"], "home": canonical(r["home"]),
                                  "away": canonical(r["away"])} for r in raw]
                second_status = f"ok ({len(raw)} fixtures)"
                with db.conn() as c:
                    db.log_sync(c, "api-football", f"fixtures/{league['af_id']}", True,
                                second_status, afc.remaining)
            except Exception as e:
                second_status = str(e)[:180]
                with db.conn() as c:
                    db.log_sync(c, "api-football", f"fixtures/{league['af_id']}", False,
                                second_status)

        # ---------- build events ----------
        if demo:
            got = latest_results_with_odds(league["fd"], current_season_code(run_date_obj))
            if not got:
                log("  no current-season file for demo"); continue
            res, pack = got
            played = res.dropna(subset=["hg"])
            counts = played.groupby("date").size()
            recent = counts[counts.index >= counts.index.max() - pd.Timedelta(days=14)]
            full_days = recent[recent >= 5]
            day = full_days.index.max() if len(full_days) else counts.index.max()
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
                    "_result": (int(r.hg), int(r.ag)) if pd.notna(r.hg) else None}
            log(f"  demo: using {len(events)} real fixtures from {day.date()} with closing odds")
            agg_exclude = pack.get("aggregate_books", set()) | AGG_BOOK_NAMES
        else:
            events, un = build_league_events(cfg, league, run_date, odds_api, af,
                                             manual_events, matcher)
            unmatched_all.extend(un)
            agg_exclude = set(AGG_BOOK_NAMES)
            log(f"  {len(events)} fixtures with odds around {run_date}")

        # ---------- STEP 2+3: upsert fixtures, mark removed-from-feed ----------
        # duplicate-detection index: date -> [mkeys] (same team twice in one day
        # = suspicious mapping, flagged in checks)
        from collections import defaultdict
        day_index: dict[str, list[str]] = defaultdict(list)
        for k in events:
            day_index[k.split("|")[0]].append(k)

        with db.conn() as c:
            for k, ev in events.items():
                status = "scheduled"
                if demo:
                    status = "completed" if ev.get("_result") else "scheduled"
                else:
                    sc = scores_map.get(ev.get("provider_event_id") or "")
                    if sc is not None:
                        status = "completed" if sc.get("completed") else (
                            "live" if sc.get("commence_time", "") <=
                            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") else "scheduled")
                fx = Fixture(ev.get("provider") or "manual",
                             ev.get("provider_event_id"), league["name"],
                             ev["date"], ev.get("time_utc"), ev["home"], ev["away"], status)
                fx.books = ev["books"]; fx.sources = ev["sources"]
                ev["_fixture"] = fx
                db.upsert_fixture(c, fx.to_row())
            if not demo:
                stale = c.execute(
                    """SELECT fid, date FROM fixtures WHERE league=? AND provider='the-odds-api'
                         AND date>=? AND date<=date(?, '+5 day') AND last_seen < datetime('now','-30 hours')""",
                    (league["name"], run_date, run_date)).fetchall()
                todays_fids = {ev["_fixture"].fid for ev in events.values() if ev.get("_fixture")}
                for row in stale:
                    if row["fid"] not in todays_fids:
                        db.set_fixture_status(c, row["fid"], "removed")
                        unmatched.append(f"{league['name']}: fixture {row['fid']} ({row['date']}) "
                                         f"was in the feed but vanished - marked for review")
                c.commit()

        n_fixtures_total += len([e for e in events.values() if e["date"] == run_date])
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
            for k, ev in sorted(events.items(), key=lambda kv: (kv[1]["home"], kv[1]["away"])):
                if ev["date"] != run_date and not demo:
                    continue  # predictions only for the run date
                fx: Fixture = ev["_fixture"]

                # ---------- STEP 2/3/4/6: verification checks ----------
                pred = model.predict(ev["home"], ev["away"])
                fh = model.form(ev["home"], run_date_obj)
                fa = model.form(ev["away"], run_date_obj)
                form_ok = fh.get("played", 0) >= 4 and fa.get("played", 0) >= 4
                if demo:
                    checks, comp, ok_v = [], 100.0, True
                    reason, vnotes = "verified (offline demo fixture)", []
                else:
                    checks = verifier.checks(fx, day_index, second_source, second_status)
                    comp = verifier.completeness(fx, checks, pred["full_history"], form_ok)
                    ok_v, reason, vnotes = verifier.verdict(fx, checks, comp)
                    db.set_fixture_verification(c, fx.fid, ok_v, comp, vnotes)
                    db.log_verification(c, fx.fid, checks)

                kick_cat = ""
                if ev.get("time_utc"):
                    try:
                        dt = datetime.fromisoformat(f"{ev['date']}T{ev['time_utc']}:00").replace(
                            tzinfo=timezone.utc).astimezone(tz)
                        kick_cat = dt.strftime("%H:%M") + f" {cfg.get('timezone','CAT')}"
                    except ValueError:
                        pass

                notes = list(vnotes)
                if not pred["full_history"]:
                    notes.append("limited history for a promoted/new team: "
                                 "weak-team prior assumed (heuristic)")
                if fx.status in BLOCKED_STATUSES or fx.status == "live":
                    notes.append(f"status '{fx.status}': not presented as upcoming")

                # ---------- STEP 7: gate ----------
                if not ok_v:
                    all_fixtures.append({
                        "league": league["name"], "date": ev["date"],
                        "time_utc": ev.get("time_utc"), "kickoff_local": kick_cat,
                        "home": ev["home"], "away": ev["away"],
                        "failed": True, "fail_reason": reason,
                        "verify": {"verified": False, "completeness": comp,
                                   "status": fx.status},
                        "markets": [], "xg": (pred["lambda_home"], pred["lambda_away"]),
                        "form": {"home": fh, "away": fa}, "notes": notes})
                    continue

                n_books_1x2 = sum(1 for b, m in ev["books"].items() if "1X2" in m)
                dc_top = max(pred["markets"]["1X2"], key=pred["markets"]["1X2"].get)
                el = model.elo.prob_1x2(ev["home"], ev["away"], model.base_draw)
                agree = max(el, key=el.get) == dc_top
                conf = confidence_category(comp, agree, n_books_1x2)
                explain = model.explain(ev["home"], ev["away"], pred)

                fx_row = {"league": league["name"], "date": ev["date"],
                          "time_utc": ev.get("time_utc"), "kickoff_local": kick_cat,
                          "home": ev["home"], "away": ev["away"], "failed": False,
                          "verify": {"verified": True, "completeness": comp,
                                     "status": fx.status, "confidence": conf,
                                     "sources": ev["sources"]},
                          "explain": explain, "model_version": mver,
                          "xg": (pred["lambda_home"], pred["lambda_away"]),
                          "form": {"home": fh, "away": fa}, "markets": [], "notes": notes}
                by_market = invert_books(ev["books"])
                generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                snapshot_base = {
                    "lambda_home": round(pred["lambda_home"], 3),
                    "lambda_away": round(pred["lambda_away"], 3),
                    "elo": pred["elo"], "full_history": pred["full_history"],
                    "form_home": {k: fh.get(k) for k in ("played", "pts", "gf", "ga")},
                    "form_away": {k: fa.get(k) for k in ("played", "pts", "gf", "ga")},
                    "books_priced": sorted(ev["books"].keys()),
                }
                for market in MARKET_ORDER:
                    if market not in by_market:
                        continue
                    model_p = pred["markets"][market]
                    summary, rows = evaluate_market(market, by_market[market], model_p,
                                                    cfg, exclude_books=set(agg_exclude))
                    fx_row["notes"].extend(summary.get("notes", []))
                    fx_row["markets"].append({"market": market, "summary": summary, "rows": rows})
                    for r in rows:
                        # ----- STEP 8: snapshot prediction before publication -----
                        pv2 = {"run_id": run_id, "fixture_id": fx.fid, "mkey": k,
                               "date": ev["date"], "league": league["name"],
                               "home": ev["home"], "away": ev["away"],
                               "market": market, "selection": r["selection"],
                               "p_model": r["p_model"], "p_fair": r["p_fair"],
                               "model_version": mver, "sources": ev["sources"],
                               "completeness": comp, "confidence": conf,
                               "generated_at": generated_at}
                        db.log_prediction_v2(c, pv2, {**snapshot_base,
                                                      "fair_prob": round(r["p_fair"], 4),
                                                      "best_price": r["odds"],
                                                      "best_book": r["book"]})
                        if not demo:
                            db.log_prediction(c, run_id, k, ev["date"], league["name"],
                                              ev["home"], ev["away"], market, r["selection"],
                                              r["p_model"], r["p_fair"], r["odds"], r["book"])
                        if not r["is_value"] or susp:
                            continue
                        pick = {
                            "run_date": run_date, "date": ev["date"], "time_utc": ev.get("time_utc"),
                            "league": league["name"], "mkey": k, "home": ev["home"], "away": ev["away"],
                            "market": market, "selection": r["selection"], "book": r["book"],
                            "odds": r["odds"], "p_model": r["p_model"], "p_fair": r["p_fair"],
                            "p_raw": r["p_raw"], "edge": r["edge"], "ev": r["ev"],
                            "kelly": r["stake"] / (cfg["bankroll"] * cfg["kelly_fraction"]) if r["stake"] else 0,
                            "stake": round(r["stake"], 2),
                            "fixture_id": fx.fid, "confidence": conf, "completeness": comp,
                            "notes": list(notes)}
                        if demo and ev.get("_result"):
                            hg, ag = ev["_result"]
                            won = bet_won(market, r["selection"], hg, ag)
                            pick["notes"].append(f"already played: {hg}-{ag} -> {'WON' if won else 'LOST'}")
                        league_picks.append(pick)
                all_fixtures.append(fx_row)

            if demo:
                all_picks.extend(league_picks)
            elif not susp:
                for p in league_picks:
                    added = db.add_pick(c, p)
                    all_picks.append(p)
                    if not added:
                        p["notes"].append("already picked in an earlier run today")

        log(f"  verified+predicted: value bets found: {len(league_picks)}"
            + (" (SUSPEND mode: not recorded)" if susp else ""))

    if susp:
        summary_extra.append("PREDICTIONS SUSPENDED by administrator "
                             "(workflow input suspend_predictions); markets analysed "
                             "but no picks published.")
    all_picks.sort(key=lambda p: p["edge"], reverse=True)
    for i, p in enumerate(all_picks):
        p["rank"] = i + 1

    with db.conn() as c:
        db.log_run(c, run_id, n_fixtures_total, 0 if demo else len(all_picks),
                   meta=("DEMO " + args.demo) if demo else
                        f"live; model={mver}" + ("; SUSPEND" if susp else ""))

    prefix = "demo_" if demo else ""
    stamp = run_date
    html_path = REPORTS / f"{stamp}_{prefix}report.html"
    csv_path = REPORTS / f"{stamp}_{prefix}picks.csv"
    xlsx_path = REPORTS / f"{stamp}_{prefix}report.xlsx"
    run_info = {"date": run_date, "generated_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z"),
                "threshold": cfg["value_threshold"], "bankroll": cfg["bankroll"],
                "n_fixtures": n_fixtures_total, "model_version": mver,
                "suspended": susp, "notices": summary_extra}
    html_path.write_text(render(run_info, all_picks, all_fixtures, unmatched_all, demo=demo))
    write_picks_csv(all_picks, csv_path)
    from src.xlsx_report import build_xlsx
    build_xlsx(run_info, all_picks, all_fixtures, unmatched_all, xlsx_path, demo=demo)

    print("\n================== SUMMARY ==================")
    print(f"date           : {run_date}{'  (DEMO on real historical odds)' if demo else ''}")
    print(f"model version  : {mver}")
    print(f"fixtures       : {n_fixtures_total}")
    print(f"value bets     : {len(all_picks)}" + ("  (suspended - not recorded)" if susp else ""))
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
