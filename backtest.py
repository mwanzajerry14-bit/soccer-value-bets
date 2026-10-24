#!/usr/bin/env python3
"""Walk-forward backtest of the full pipeline on past seasons.

Data   : football-data.co.uk (real bookmaker prices incl. closing odds).
Markets: 1X2 and Over/Under 2.5 only - those are the ONLY markets with real
         prices in this free source. (No public free BTTS / Double-Chance
         history exists, so those markets cannot be honestly backtested here.)
Method : refit the model every --refit-days days using ONLY matches strictly
         before each window, then apply exactly the same value rules as live.

It prints/plots honest numbers, including if the model does NOT beat the market.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from src.historical import download, extract_book_odds, load_results, seasons_back
from src.model import fit as fit_model
from src.settings import REPORTS, load_config
from src.value import bet_won, evaluate_market

AGG = {"Max", "Avg", "BbMx", "BbAv", "BFE"}


def invert(books: dict) -> dict:
    out = {}
    for b, mkts in books.items():
        for m, sels in mkts.items():
            out.setdefault(m, {})[b] = sels
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="E0")
    ap.add_argument("--target", default="2526", help="season code backtested in full")
    ap.add_argument("--train", type=int, default=2, help="prior seasons in history")
    ap.add_argument("--refit-days", type=int, default=14)
    ap.add_argument("--min-edge", type=float, default=None)
    args = ap.parse_args()

    cfg = load_config()
    if args.min_edge is not None:
        cfg["value_threshold"] = args.min_edge
    model_cfg = cfg["model"]
    seasons = list(reversed(seasons_back(args.target, args.train + 1)))
    print(f"loading {args.league} seasons {seasons} ...")
    hist = load_results(args.league, seasons)

    p = download(args.league, args.target)
    res, pack = extract_book_odds(p)
    odds_map = {}
    for i, books in pack["odds"].items():
        r = res.loc[i]
        odds_map[(r["date"], r["home"], r["away"])] = books

    target_season = seasons[-1]
    tm = hist[hist.season == target_season].copy()
    dates = sorted(tm.date.unique())
    print(f"target season: {len(tm)} matches, {pd.Timestamp(dates[0]).date()} -> {pd.Timestamp(dates[-1]).date()}")

    bets: list[dict] = []
    model_brier, mkt_brier, n_brier = [], [], 0
    fitted = 0
    start = pd.Timestamp(dates[0])
    end = pd.Timestamp(dates[-1])
    w = start
    while w <= end:
        w_end = w + pd.Timedelta(days=args.refit_days)
        train_slice = hist[hist.date < w]
        model = fit_model(train_slice, model_cfg, w, args.league)
        fitted += 1
        print(f"  fit @ {w.date()}: {model.dc['n_matches']} matches, "
              f"{model.dc['n_teams']} teams, converged={model.dc['converged']}", flush=True)
        window_matches = tm[(tm.date >= w) & (tm.date < w_end)]
        for m in window_matches.itertuples():
            books = odds_map.get((m.date, m.home, m.away))
            if not books:
                continue
            pred = model.predict(m.home, m.away)
            by_market = invert(books)
            # ---- baseline skill comparison on 1X2, every match ----
            r1 = by_market.get("1X2")
            if r1:
                summary, rows = evaluate_market("1X2", r1, pred["markets"]["1X2"], cfg, AGG)
                if rows:
                    outc = np.array([float(m.hg > m.ag), float(m.hg == m.ag), float(m.hg < m.ag)])
                    order = ["home", "draw", "away"]
                    pm = np.array([pred["markets"]["1X2"][s] for s in order])
                    pf = np.array([next((r["p_fair"] for r in rows if r["selection"] == s), np.nan)
                                   for s in order])
                    if not np.isnan(pf).any():
                        model_brier.append(float(np.sum((pm - outc) ** 2)))
                        mkt_brier.append(float(np.sum((pf - outc) ** 2)))
                        n_brier += 1
            # ---- value betting simulation ----
            for market in ("1X2", "OU25"):
                if market not in by_market:
                    continue
                summary, rows = evaluate_market(market, by_market[market],
                                                pred["markets"][market], cfg, AGG)
                for r in rows:
                    if not r["is_value"]:
                        continue
                    won = bet_won(market, r["selection"], int(m.hg), int(m.ag))
                    bets.append({
                        "date": m.date.date().isoformat(), "home": m.home, "away": m.away,
                        "market": market, "selection": r["selection"],
                        "odds": r["odds"], "book": r["book"], "p_model": r["p_model"],
                        "p_fair": r["p_fair"], "edge": r["edge"], "ev": r["ev"],
                        "stake": round(r["stake"], 2), "won": won,
                        "profit": round(r["stake"] * (r["odds"] - 1) if won else -r["stake"], 2),
                        "result": f"{m.hg}-{m.ag}",
                    })
        w = w_end

    # ---------------- reporting ----------------
    df = pd.DataFrame(bets)
    lines = [f"\n========== BACKTEST {args.league} {args.target} =========="]
    lines.append(f"refits: {fitted} (every {args.refit_days} days), matches tested: {len(tm)}")
    if n_brier:
        lines.append(f"All-match 1X2 Brier score (lower=better): model {np.mean(model_brier):.4f} "
                     f"vs market-fair {np.mean(mkt_brier):.4f}  (n={n_brier})")
    if df.empty:
        lines.append("NO value bets met the threshold across the whole season. "
                     "The model found nothing to beat the market with. That is a real, common result.")
    else:
        n = len(df); won = int(df.won.sum())
        turnover = df.stake.sum(); profit = df.profit.sum()
        lines.append(f"value bets        : {n} ({won} won, hit rate {won/n*100:.1f}%)")
        lines.append(f"avg odds          : {df.odds.mean():.2f}")
        lines.append(f"turnover (Kelly)  : {turnover:.1f}   profit: {profit:+.1f}   "
                     f"ROI: {profit/turnover*100:+.2f}%")
        flat = sum((o - 1) if wn else -1 for o, wn in zip(df.odds, df.won))
        lines.append(f"flat 1u staking   : profit {flat:+.2f}u over {n} bets  ROI {flat/n*100:+.2f}%")
        eq = 1000 + df.sort_values('date').profit.cumsum()
        dd = (eq - eq.cummax()).min()
        lines.append(f"Kelly equity curve: start 1000 -> end {eq.iloc[-1]:.1f} (max drawdown {dd:.1f})")
        lines.append("by market:")
        for mkt, g in df.groupby("market"):
            pr, to = g.profit.sum(), g.stake.sum()
            lines.append(f"  {mkt:5s}: {len(g):3d} bets, hit {g.won.mean()*100:.1f}%, ROI {pr/to*100:+.2f}%")
        # calibration of value bets
        df["bin"] = pd.cut(df.p_model, [0,.3,.4,.5,.6,.7,.8,1.0])
        lines.append("calibration (value bets; want actual >= predicted):")
        for b, g in df.groupby("bin", observed=True):
            lines.append(f"  p in {str(b):11s}: n={len(g):3d}, mean pred {g.p_model.mean()*100:.1f}%, "
                         f"actual win {g.won.mean()*100:.1f}%")
        verdict = "BEAT the market" if profit > 0 else "DID NOT beat the market"
        lines.append(f"VERDICT: {verdict} after margin on {n} bets. "
                     f"(One season is small evidence either way - see README limitations.)")

    text = "\n".join(lines)
    print(text)
    out_txt = REPORTS / f"backtest_{args.league}_{args.target}_summary.txt"
    out_txt.write_text(text + "\n")
    if not df.empty:
        df.to_csv(REPORTS / f"backtest_{args.league}_{args.target}_bets.csv", index=False)
    print(f"\nfiles: {out_txt}")


if __name__ == "__main__":
    main()
