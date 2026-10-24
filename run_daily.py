#!/usr/bin/env python3
"""Daily pipeline: fetch odds -> fit model per league -> find value bets ->
write reports + log everything to SQLite.

Usage:
    python run_daily.py                     # live run for today
    python run_daily.py --date 2026-10-25   # live run for another date
    python run_daily.py --demo E0           # OFFLINE demo on real historical
                                            # closing odds (no API keys needed)
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src import db
from src.historical import load_results, seasons_back, latest_results_with_odds
from src.model import fit as fit_model
from src.settings import (REPORTS, api_key, af_season_year, current_season_code,
                          load_config, load_env)
from src.teamnames import canonical
from src.util import match_key
from src.value import evaluate_market, bet_won
from src.report import render, write_picks_csv

AGG_BOOK_NAMES = {"Max", "Avg", "BbMx", "BbAv", "BFE"}
MARKET_ORDER = ["1X2", "OU25", "BTTS", "DC"]


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_manual_odds(path: Path, wanted_date: str) -> list[dict]:
    """Manual odds from your local bookmakers (data/manual_odds.csv)."""
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
        sel = row["selection"].strip().lower() if market in ("1X2", "OU25", "BTTS") else row["selection"].strip().upper()
        if market not in ("1X2", "OU25", "BTTS", "DC"):
            log(f"  [manual] unknown market '{market}' -> row skipped")
            continue
        key = (canonical(row["home"]), canonical(row["away"]))
        ev = events.setdefault(key, {
            "date": wanted_date, "time_utc": None, "home": row["home"], "away": row["away"],
            "league_hint": row.get("league", ""), "books": {}, "source": "manual"})
        ev["books"].setdefault(row["book"].strip() or "manual", {})[market] = {
            **ev["books"].get(row["book"].strip() or "manual", {}).get(market, {}),
            sel: odds}
    return list(events.values())


def invert_books(books: dict) -> dict[str, dict]:
    """{book:{market:{sel:odds}}} -> {market:{book:{sel:odds}}}"""
    out: dict[str, dict] = {}
    for b, mkts in books.items():
        for m, sels in mkts.items():
            out.setdefault(m, {})[b] = sels
    return out


def build_league_events(cfg: dict, league: dict, run_date: str,
                        odds_api, af, manual_events: list, matcher) -> tuple[dict, list]:
    """Merge The Odds API + API-Football + manual CSV for one league."""
    events: dict[str, dict] = {}
    unmatched: list[str] = []

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
                                    "books": {}, "sources": []})
        cur["sources"].append(source)
        if ev.get("time_utc"):
            cur["time_utc"] = ev["time_utc"]
        for b, mkts in ev["books"].items():
            bb = cur["books"].setdefault(b, {})
            bb.update(mkts)

    if odds_api:
        try:
            for ev in odds_api.league_odds(league["odds_api"], cfg["odds_api"]["regions"],
                                           cfg["odds_api"]["markets"]):
                if ev["date"] == run_date:   # UTC date filter
                    merge(ev, "the-odds-api")
        except Exception as e:
            log(f"  [odds-api] {league['name']}: {e}")
    if af:
        try:
            for ev in af.odds_for_date(league["af_id"], af_season_year(), run_date,
                                       cfg["api_football"]["extra_bets"]):
                merge(ev, "api-football")
        except Exception as e:
            log(f"  [api-football] {league['name']}: {e}")
    for ev in manual_events:
        hint = (ev.get("league_hint") or "").lower()
        if league["name"].lower() in hint or hint in league["name"].lower() or not hint:
            merge(ev, "manual")
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

    leagues = [l for l in cfg["leagues"] if l.get("enabled")]
    if args.league:
        leagues = [l for l in leagues if l["fd"] == args.league]
    if demo:
        leagues = [l for l in cfg["leagues"] if l["fd"] == args.demo]
        if not leagues:
            leagues = []

    odds_api = af = None
    if not demo:
        if cfg.get("odds_api", {}).get("enabled"):
            from src.odds_api import OddsApi
            odds_api = OddsApi(api_key("ODDS_API_KEY"))
        if cfg.get("api_football", {}).get("enabled") and cfg["api_football"].get("use_odds"):
            from src.api_football import ApiFootball
            af = ApiFootball(api_key("API_FOOTBALL_KEY"))

    manual_events = parse_manual_odds(Path("data/manual_odds.csv"), run_date)
    all_picks, all_fixtures, unmatched_all = [], [], []
    n_fixtures_total = 0

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
                    "league": league["name"], "books": books, "sources": ["football-data (closing)"],
                    "_result": (int(r.hg), int(r.ag)) if pd.notna(r.hg) else None}
            log(f"  demo: using {len(events)} real fixtures from {day.date()} with closing odds")
            agg_exclude = pack.get("aggregate_books", set()) | AGG_BOOK_NAMES
        else:
            events, un = build_league_events(cfg, league, run_date, odds_api, af,
                                             manual_events, matcher)
            unmatched_all.extend(un)
            agg_exclude = set(AGG_BOOK_NAMES)
            log(f"  {len(events)} fixtures with odds for {run_date}")

        n_fixtures_total += len(events)
        league_picks = []

        with db.conn() as c:
            if not demo:
                for k, ev in events.items():
                    for b, mkts in ev["books"].items():
                        for m, sels in mkts.items():
                            for s, o in sels.items():
                                db.log_quote(c, run_id, k, ev["date"], league["name"],
                                             ev["home"], ev["away"], "demo" if demo else "/".join(ev["sources"]),
                                             b, m, s, o)

            for k, ev in sorted(events.items(), key=lambda kv: (kv[1]["home"], kv[1]["away"])):
                pred = model.predict(ev["home"], ev["away"])
                notes = []
                if not pred["full_history"]:
                    notes.append("limited history for a promoted/new team: "
                                 "league-average strength assumed")
                by_market = invert_books(ev["books"])
                fx_row = {"league": league["name"], "date": ev["date"],
                          "time_utc": ev.get("time_utc"), "home": ev["home"], "away": ev["away"],
                          "xg": (pred["lambda_home"], pred["lambda_away"]),
                          "form": {"home": model.form(ev["home"], run_date_obj),
                                   "away": model.form(ev["away"], run_date_obj)},
                          "markets": [], "notes": notes}
                for market in MARKET_ORDER:
                    if market not in by_market:
                        continue
                    model_p = pred["markets"][market]
                    summary, rows = evaluate_market(market, by_market[market], model_p,
                                                    cfg, exclude_books=set(agg_exclude))
                    fx_row["markets"].append({"market": market, "summary": summary, "rows": rows})
                    if not demo:
                        for r in rows:
                            db.log_prediction(c, run_id, k, ev["date"], league["name"],
                                              ev["home"], ev["away"], market, r["selection"],
                                              r["p_model"], r["p_fair"], r["odds"], r["book"])
                    for r in rows:
                        if not r["is_value"]:
                            continue
                        pick = {
                            "run_date": run_date, "date": ev["date"], "time_utc": ev.get("time_utc"),
                            "league": league["name"], "mkey": k, "home": ev["home"], "away": ev["away"],
                            "market": market, "selection": r["selection"], "book": r["book"],
                            "odds": r["odds"], "p_model": r["p_model"], "p_fair": r["p_fair"],
                            "p_raw": r["p_raw"], "edge": r["edge"], "ev": r["ev"],
                            "kelly": r["stake"] / (cfg["bankroll"] * cfg["kelly_fraction"]) if r["stake"] else 0,
                            "stake": round(r["stake"], 2),
                            "notes": list(notes)}
                        if demo and ev.get("_result"):
                            hg, ag = ev["_result"]
                            won = bet_won(market, r["selection"], hg, ag)
                            pick["notes"].append(f"already played: {hg}-{ag} -> {'WON' if won else 'LOST'}")
                        league_picks.append(pick)
                all_fixtures.append(fx_row)

            if demo:
                all_picks.extend(league_picks)
            else:
                for p in league_picks:
                    added = db.add_pick(c, p)
                    all_picks.append(p)
                    if not added:
                        p["notes"].append("already picked in an earlier run today")

        log(f"  value bets found: {len(league_picks)}")

    all_picks.sort(key=lambda p: p["edge"], reverse=True)
    for i, p in enumerate(all_picks):
        p["rank"] = i + 1

    with db.conn() as c:
        db.log_run(c, run_id, n_fixtures_total, 0 if demo else len(all_picks),
                   meta=("DEMO " + args.demo) if demo else "live")

    prefix = "demo_" if demo else ""
    stamp = run_date
    html_path = REPORTS / f"{stamp}_{prefix}report.html"
    csv_path = REPORTS / f"{stamp}_{prefix}picks.csv"
    xlsx_path = REPORTS / f"{stamp}_{prefix}report.xlsx"
    run_info = {"date": run_date, "generated_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z"),
                "threshold": cfg["value_threshold"], "bankroll": cfg["bankroll"],
                "n_fixtures": n_fixtures_total}
    html_path.write_text(render(run_info, all_picks, all_fixtures, unmatched_all, demo=demo))
    write_picks_csv(all_picks, csv_path)
    from src.xlsx_report import build_xlsx
    build_xlsx(run_info, all_picks, all_fixtures, unmatched_all, xlsx_path, demo=demo)

    print("\n================== SUMMARY ==================")
    print(f"date           : {run_date}{'  (DEMO on real historical odds)' if demo else ''}")
    print(f"fixtures       : {n_fixtures_total}")
    print(f"value bets     : {len(all_picks)}")
    for p in all_picks:
        print(f"  #{p['rank']:>2} {p['home']} v {p['away']}  {p['market']}/{p['selection']} "
              f"@ {p['odds']:.2f} ({p['book']})  edge +{100*p['edge']:.1f}%  stake {p['stake']:.2f}")
    if odds_api and getattr(odds_api, "last_remaining", None) is not None:
        print(f"Odds API remaining quota (header): {odds_api.last_remaining}")
    if af and getattr(af, "remaining", None) is not None:
        print(f"API-Football remaining (header)  : {af.remaining}")
    if not all_picks:
        print("No value bets met the threshold. That is a normal, honest outcome.")
    print(f"\nReport (HTML)  : {html_path}")
    print(f"Report (Excel) : {xlsx_path}")
    print(f"Picks  (CSV)   : {csv_path}")


if __name__ == "__main__":
    sys.exit(main())
