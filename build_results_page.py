#!/usr/bin/env python3
"""Build results.html - the public "Results & verification" page.

Shows, from REAL recorded data (SQLite DB + football-data.co.uk results):
  1. Scoreboard: settled value bets, hit rate, ROI (Kelly stakes & flat 1u)
  2. Calibration: predicted probability vs actual win frequency
  3. Settled picks table: every pick, odds, stake, score, WON/LOST, profit
  4. Pending picks table (not yet settled)
  5. Prediction verification on ENDED matches: model 1X2 probabilities
     vs actual result (correct if model's top-probability outcome happened),
     plus model vs market Brier scores.
Football-data updates roughly Mon/Thu, so very recent matches may still show
as pending - that delay is honest, not a bug.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src import db
from src.historical import load_results
from src.settings import current_season_code, load_config
from src.report import CSS, MKT_LABEL, SEL_LABEL
from src.util import lusaka_today

TODAY = lusaka_today()

HTML_HEAD = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Results & verification</title><style>{CSS}</style></head><body>
<h1>&#9989; Results &amp; prediction verification</h1>
<p class="small">Updated automatically by the daily workflow. Everything here
comes from logged predictions vs real full-time results - nothing is edited by hand.</p>
"""
FOOT = """<hr><p class="small">Results source: football-data.co.uk (updated ~Mon/Thu;
matches from the last 1-3 days may still show as pending). Probabilities are
estimates, not guarantees. Past performance does not predict future results.</p>
</body></html>"""


def esc(x) -> str:
    import html as H
    return H.escape(str(x))


def pct(x, nd=1):
    return "n/a" if x is None else f"{100*x:.{nd}f}%"


def card(label, val):
    return f'<div class="card">{esc(label)}<b>{esc(val)}</b></div>'


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="verification window for predictions")
    args = ap.parse_args()
    cfg = load_config()

    with db.conn() as c:
        picks = c.execute("SELECT * FROM picks ORDER BY date DESC, id DESC").fetchall()
        preds = c.execute(
            "SELECT DISTINCT date, league, home, away, selection, p_model, p_fair "
            "FROM predictions WHERE market='1X2' ORDER BY date DESC").fetchall()

    settled = [p for p in picks if p["status"] in ("won", "lost")]
    pending = [p for p in picks if p["status"] == "pending"]

    # --------------------------------------------------------------
    # results lookup for verification of predictions on ended matches
    # --------------------------------------------------------------
    fd_by_name = {l["name"]: l["fd"] for l in cfg["leagues"] if l.get("fd")}
    cutoff = TODAY - timedelta(days=args.days)
    wanted = defaultdict(set)          # fd code -> set of dates
    for p in preds:
        d = date.fromisoformat(p["date"])
        if d >= cutoff and d <= TODAY and p["league"] in fd_by_name:
            wanted[fd_by_name[p["league"]]].add(p["date"])
    results: dict[tuple, tuple[int, int]] = {}
    for fd, dts in wanted.items():
        seasons = sorted({current_season_code(date.fromisoformat(d)) for d in dts} |
                         {current_season_code()})
        hist = load_results(fd, seasons)
        for r in hist.itertuples():
            results[(r.date.date().isoformat(), r.home, r.away)] = (int(r.hg), int(r.ag))

    # ------------------------------------------------------------------
    # verification stats over ended matches only (never guess outcomes)
    # ------------------------------------------------------------------
    per_match: dict[tuple, dict] = {}
    for p in preds:
        key = (p["date"], p["league"], p["home"], p["away"])
        res = results.get((p["date"], p["home"], p["away"]))
        e = per_match.setdefault(key, {"prob": {}, "fair": {}, "result": res})
        e["prob"][p["selection"]] = p["p_model"]
        e["fair"][p["selection"]] = p["p_fair"]

    import math
    verified = []
    brier_m, brier_f = [], []
    ll_m, ll_f = [], []
    correct = 0
    for key, e in sorted(per_match.items(), reverse=True):
        if not e["result"]:
            continue
        hg, ag = e["result"]
        actual = "home" if hg > ag else "draw" if hg == ag else "away"
        if len(e["prob"]) != 3:
            continue
        top = max(e["prob"], key=e["prob"].get)
        ok = top == actual
        correct += ok
        order = ["home", "draw", "away"]
        outc = [1.0 if s == actual else 0.0 for s in order]
        pm = [e["prob"][s] for s in order]
        brier_m.append(sum((a - b) ** 2 for a, b in zip(pm, outc)))
        ll_m.append(-sum(o * math.log(max(p, 1e-9)) for p, o in zip(pm, outc)))
        if len(e["fair"]) == 3:
            pf = [e["fair"][s] for s in order]
            brier_f.append(sum((a - b) ** 2 for a, b in zip(pf, outc)))
            ll_f.append(-sum(o * math.log(max(p, 1e-9)) for p, o in zip(pf, outc)))
        verified.append({**dict(zip(["date", "league", "home", "away"], key)),
                         "top": top, "actual": actual, "ok": ok,
                         "res": f"{hg}-{ag}", "prob": e["prob"]})

    n_settled = len(settled)
    wins = sum(1 for p in settled if p["status"] == "won")
    turnover = sum(p["stake"] for p in settled)
    profit = sum(p["profit"] for p in settled)
    roi = profit / turnover if turnover else None
    flat = sum((p["odds"] - 1) if p["status"] == "won" else -1 for p in settled)
    flat_roi = flat / n_settled if n_settled else None

    parts = [HTML_HEAD, "<h2>Scoreboard (value bets, settled)</h2><div class='summary'>",
             card("Settled bets", n_settled or 0),
             card("Hit rate", f"{wins}/{n_settled} ({wins/n_settled*100:.1f}%)" if n_settled else "n/a"),
             card("Kelly ROI", f"{roi*100:+.2f}%" if roi is not None else "n/a"),
             card("Flat 1u ROI", f"{flat_roi*100:+.2f}%" if flat_roi is not None else "n/a"),
             card("Profit", f"{profit:+.2f}" if n_settled else "n/a"),
             card("Verified predictions (30d)", len(verified)),
             card("Top-prob correct", f"{correct/len(verified)*100:.1f}%" if verified else "n/a"),
             card("Brier model vs market",
                  f"{sum(brier_m)/len(brier_m):.4f} vs {sum(brier_f)/len(brier_f):.4f}" if brier_f else "n/a"),
             card("Log-loss model vs market",
                  f"{sum(ll_m)/len(ll_m):.4f} vs {sum(ll_f)/len(ll_f):.4f}" if ll_f else "n/a"),
             "</div>"]

    # ---------------- performance by month (date range breakdown) --------
    if verified:
        bym: dict[str, list] = {}
        for v in verified:
            bym.setdefault(v["date"][:7], []).append(v)
        parts.append("<h2>Prediction verification by month (out-of-sample, pre-match only)</h2>"
                     "<table><tr><th>Month</th><th class='r'>evaluated</th>"
                     "<th class='r'>top-prob correct</th></tr>")
        for mth in sorted(bym, reverse=True):
            g = bym[mth]
            acc = sum(1 for x in g if x["ok"]) / len(g)
            parts.append(f"<tr><td>{esc(mth)}</td><td class='r'>{len(g)}</td>"
                         f"<td class='r'>{acc*100:.1f}%</td></tr>")
        parts.append("</table>")

    # ---------------- calibration on settled picks ----------------
    if n_settled:
        parts.append("<h2>Calibration (settled value bets)</h2><table><tr><th>predicted p</th>"
                     "<th class='r'>bets</th><th class='r'>mean predicted</th><th class='r'>actual win</th></tr>")
        bins = [(0, .3), (.3, .4), (.4, .5), (.5, .6), (.6, .7), (.7, .8), (.8, 1.01)]
        for lo, hi in bins:
            g = [p for p in settled if lo < p["p_model"] <= hi]
            if g:
                mp = sum(x["p_model"] for x in g) / len(g)
                aw = sum(1 for x in g if x["status"] == "won") / len(g)
                flag = " ✅" if aw >= mp else " ⚠️ overconfident"
                parts.append(f"<tr><td>{lo:.1f}-{min(hi,1.0):.1f}</td><td class='r'>{len(g)}</td>"
                             f"<td class='r'>{pct(mp)}</td><td class='r'>{pct(aw)}{flag}</td></tr>")
        parts.append("</table>")

    # ---------------- settled picks table ----------------
    parts.append("<h2>Settled picks</h2>")
    if settled:
        parts.append("<table><tr><th>Date</th><th>Match</th><th>Market</th><th>Pick</th>"
                     "<th>Book</th><th class='r'>Odds</th><th class='r'>Model p</th><th class='r'>Stake</th>"
                     "<th>Result</th><th>Outcome</th><th class='r'>Profit</th></tr>")
        for p in settled:
            w = p["status"] == "won"
            parts.append(f"<tr><td>{esc(p['date'])}</td><td>{esc(p['home'])} v {esc(p['away'])}</td>"
                         f"<td>{esc(MKT_LABEL[p['market']])}</td><td><b>{esc(SEL_LABEL[p['selection']])}</b></td>"
                         f"<td>{esc(p['book'])}</td><td class='r'>{p['odds']:.2f}</td>"
                         f"<td class='r'>{pct(p['p_model'])}</td><td class='r'>{p['stake']:.2f}</td>"
                         f"<td>{p['hg']}-{p['ag']}</td>"
                         f"<td style='color:{'#2e7d32' if w else '#c62828'};font-weight:700'>"
                         f"{'WON' if w else 'LOST'}</td><td class='r'>{p['profit']:+.2f}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<div class='warn'>No settled bets yet - first results appear here "
                     "after the next football-data update.</div>")

    # ---------------- pending picks ----------------
    voided = [p for p in picks if p["status"] == "void"]
    if voided:
        parts.append("<h2>Voided entries (never bettable - excluded from stats)</h2>"
                     "<p class='small'>Logged from a bad exchange odds feed before the "
                     "price-sanity guard existed (24 Oct 2026). Not real offers.</p>"
                     "<table><tr><th>Date</th><th>Match</th><th>Market</th><th>Pick</th>"
                     "<th>Feed book</th><th class='r'>Feed odds</th></tr>")
        for p in voided:
            parts.append(f"<tr class='small' style='color:#888'><td>{esc(p['date'])}</td>"
                         f"<td>{esc(p['home'])} v {esc(p['away'])}</td>"
                         f"<td>{esc(MKT_LABEL[p['market']])}</td><td>{esc(SEL_LABEL[p['selection']])}</td>"
                         f"<td>{esc(p['book'])} (bad feed)</td><td class='r'>{p['odds']:.2f}</td></tr>")
        parts.append("</table>")

    if pending:
        parts.append("<h2>Pending picks (not settled yet)</h2><table><tr><th>Date</th><th>Match</th>"
                     "<th>Market</th><th>Pick</th><th>Book</th><th class='r'>Odds</th>"
                     "<th class='r'>Model p</th><th class='r'>Stake</th></tr>")
        for p in pending:
            parts.append(f"<tr><td>{esc(p['date'])}</td><td>{esc(p['home'])} v {esc(p['away'])}</td>"
                         f"<td>{esc(MKT_LABEL[p['market']])}</td><td><b>{esc(SEL_LABEL[p['selection']])}</b></td>"
                         f"<td>{esc(p['book'])}</td><td class='r'>{p['odds']:.2f}</td>"
                         f"<td class='r'>{pct(p['p_model'])}</td><td class='r'>{p['stake']:.2f}</td></tr>")
        parts.append("</table>")

    # ---------------- prediction verification ----------------
    parts.append("<h2>Prediction verification - ended matches (last 30 days)</h2>")
    if verified:
        parts.append("<p class='small'>A prediction counts as correct when the model's "
                     "highest-probability 1X2 outcome happened.</p>"
                     "<table><tr><th>Date</th><th>League</th><th>Match</th><th>Result</th>"
                     "<th class='r'>P(home)</th><th class='r'>P(draw)</th><th class='r'>P(away)</th>"
                     "<th>Model top</th><th>Check</th></tr>")
        for v in verified[:80]:
            pr = v["prob"]
            tick = "&#9989;" if v["ok"] else "&#10060;"
            parts.append(f"<tr><td>{esc(v['date'])}</td><td>{esc(v['league'])}</td>"
                         f"<td>{esc(v['home'])} v {esc(v['away'])}</td><td><b>{v['res']}</b></td>"
                         f"<td class='r'>{pct(pr['home'])}</td><td class='r'>{pct(pr['draw'])}</td>"
                         f"<td class='r'>{pct(pr['away'])}</td><td>{esc(SEL_LABEL[v['top']])}</td>"
                         f"<td>{tick}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<div class='warn'>No ended matches with logged predictions yet.</div>")

    parts.append(FOOT)
    Path("results.html").write_text("".join(parts))
    print(f"results.html built: {n_settled} settled, {len(verified)} verified matches")


if __name__ == "__main__":
    main()
