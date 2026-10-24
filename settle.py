#!/usr/bin/env python3
"""Settle pending picks using real results, then report performance.

Result sources, in order:
  1) football-data.co.uk current-season CSV (free, no key) for league-coded picks
  2) API-Football /fixtures (if API_FOOTBALL_KEY set) for anything else

Reports: hit rate, ROI, by-market break-down and calibration
(predicted probability vs actual win frequency).
"""
from __future__ import annotations

import argparse
from datetime import date

import pandas as pd

from src import db
from src.historical import load_results
from src.settings import af_season_year, api_key, current_season_code, load_config, load_env
from src.teamnames import canonical
from src.value import bet_won


def settle_demo_note():
    pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    load_env()

    fd_by_name = {l["name"]: l["fd"] for l in cfg["leagues"] if l.get("fd")}
    af_by_name = {l["name"]: l["af_id"] for l in cfg["leagues"] if l.get("af_id")}

    with db.conn() as c:
        pend = db.pending_picks(c)
        settled_now = 0
        if pend:
            # group by league + date to minimise lookups
            groups: dict[tuple, list] = {}
            for r in pend:
                if date.fromisoformat(r["date"]) >= date.today():
                    continue  # future match, nothing to settle
                groups.setdefault((r["league"], r["date"]), []).append(r)

            af = None
            for (league, d), picks in sorted(groups.items()):
                found = {}
                fd = fd_by_name.get(league)
                if fd:
                    dd = date.fromisoformat(d)
                    seasons = sorted({current_season_code(dd),
                                      current_season_code()})
                    hist = load_results(fd, seasons)
                    day = hist[hist.date == pd.Timestamp(d)]
                    for r in day.itertuples():
                        found[(r.home, r.away)] = (int(r.hg), int(r.ag))
                missing = [p for p in picks if (p["home"], p["away"]) not in found]
                if missing and af_by_name.get(league):
                    try:
                        from src.api_football import ApiFootball
                        key = api_key("API_FOOTBALL_KEY", required=False)
                        if key:
                            af = af or ApiFootball(key)
                            for res in af.results_range(af_by_name[league],
                                                        af_season_year(date.fromisoformat(d)), d, d):
                                found[(canonical(res["home"]), canonical(res["away"]))] = \
                                    (res["hg"], res["ag"])
                    except Exception as e:
                        if not args.quiet:
                            print(f"  [settle] API-Football fallback failed for {league} {d}: {e}")

                for p in picks:
                    res = found.get((p["home"], p["away"]))
                    if not res:
                        if not args.quiet:
                            print(f"  still pending (no result found): "
                                  f"{p['date']} {p['home']} v {p['away']}")
                        continue
                    hg, ag = res
                    won = bet_won(p["market"], p["selection"], hg, ag)
                    db.settle_pick(c, p["id"], hg, ag, won, float(p["stake"]), float(p["odds"]))
                    settled_now += 1
                    if not args.quiet:
                        print(f"  settled {p['date']} {p['home']} v {p['away']} "
                              f"{p['market']}/{p['selection']} -> {hg}-{ag} "
                              f"{'WON' if won else 'LOST'}")

        rows = db.settled(c)

    print("\n========== PERFORMANCE ==========")
    print(f"settled now: {settled_now}   total settled: {len(rows)}")
    if rows:
        n = len(rows)
        wins = sum(1 for r in rows if r["status"] == "won")
        turnover = sum(r["stake"] for r in rows)
        profit = sum(r["profit"] for r in rows)
        print(f"bets: {n}  won: {wins}  hit rate: {wins/n*100:.1f}%")
        print(f"turnover: {turnover:.2f}  profit: {profit:+.2f}  ROI: {profit/turnover*100:+.2f}%")
        flat = sum((r["odds"] - 1) if r["status"] == "won" else -1 for r in rows)
        print(f"flat 1u: {flat:+.2f}u  (ROI {flat/n*100:+.2f}%)")
        print("\nby market:")
        mkts: dict[str, list] = {}
        for r in rows:
            mkts.setdefault(r["market"], []).append(r)
        for m, g in mkts.items():
            wn = sum(1 for r in g if r["status"] == "won")
            to = sum(r["stake"] for r in g)
            pf = sum(r["profit"] for r in g)
            print(f"  {m:5s}: {len(g):3d} bets, hit {wn/len(g)*100:.1f}%, ROI {pf/to*100:+.2f}%")
        print("\ncalibration (predicted vs actual; want actual >= predicted):")
        bins = [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.01]
        for lo, hi in zip(bins[:-1], bins[1:]):
            g = [r for r in rows if lo < r["p_model"] <= hi]
            if not g:
                continue
            mp = sum(r["p_model"] for r in g) / len(g)
            aw = sum(1 for r in g if r["status"] == "won") / len(g)
            print(f"  p {lo:.1f}-{min(hi,1.0):.1f}: n={len(g):3d}  pred {mp*100:.1f}%  actual {aw*100:.1f}%")
    else:
        print("no settled bets yet.")


if __name__ == "__main__":
    main()
