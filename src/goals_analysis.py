"""Over-goals evidence engine (task 3): verified facts for O2.5/O3.5 analysis.

Computes ONLY from the real match history already loaded from
football-data.co.uk (results the provider actually reported). Team news is
not inventable: without a verified feed it is reported as unavailable, or read
from an optional user-maintained data/team_news.csv (with source + timestamp).
"""
from __future__ import annotations

import csv
import math
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

# same decay constant family as the model (recent matches weigh more)
TREND_DECAY_PER_DAY = 0.004
NEWS_MAX_AGE_H = 72


def _team_rows(hist: pd.DataFrame, team: str, venue: str | None, before: date) -> pd.DataFrame:
    m = hist
    if venue == "home":
        m = m[m.home == team]
    elif venue == "away":
        m = m[m.away == team]
    else:
        m = m[(m.home == team) | (m.away == team)]
    return m[m.date < pd.Timestamp(before)].sort_values("date", ascending=False)


def _agg(rows: pd.DataFrame, team: str, before: date, elo_r=None) -> dict:
    """Aggregate goals evidence for `team` over `rows`."""
    n = len(rows)
    if not n:
        return {"n": 0}
    gf = ga = over25 = over35 = clean = blank = 0
    wgf = wga = 0.0
    opp_elos = []
    for r in rows.itertuples():
        is_home = r.home == team
        f, a = (r.hg, r.ag) if is_home else (r.ag, r.hg)
        gf += f; ga += a
        tot = r.hg + r.ag
        over25 += tot >= 3
        over35 += tot >= 4
        clean += a == 0
        blank += f == 0
        days = max(0, (pd.Timestamp(before) - r.date).days)
        w = math.exp(-TREND_DECAY_PER_DAY * days)     # recency weighting
        wgf += w * f; wga += w * a
        opp = r.away if is_home else r.home
        if elo_r is not None:
            try:
                opp_elos.append(elo_r(opp))
            except Exception:
                pass
    return {
        "n": n,
        "gf": gf, "ga": ga,
        "avg_gf": round(gf / n, 2), "avg_ga": round(ga / n, 2),
        "avg_total": round((gf + ga) / n, 2),
        "over25_n": over25, "over25_pct": round(over25 / n * 100),
        "over35_n": over35, "over35_pct": round(over35 / n * 100),
        "clean_sheets": clean, "failed_to_score": blank,
        # recency-weighted trend (decay): per-weighted-day rates
        "trend_gf": round(wgf, 2), "trend_ga": round(wga, 2),
        "opp_mean_elo": round(sum(opp_elos) / len(opp_elos)) if opp_elos else None,
        "small_sample": n < 5,
    }


def head_to_head(hist: pd.DataFrame, home: str, away: str, before: date,
                 last: int = 5) -> dict:
    """Latest competitive meetings (max `last`) with age + totals. Never
    invents meetings; small/old samples are labelled, not conclusive."""
    rows = hist[((hist.home == home) & (hist.away == away)) |
                ((hist.home == away) & (hist.away == home))]
    rows = rows[rows.date < pd.Timestamp(before)].sort_values("date", ascending=False).head(last)
    games = []
    over25 = over35 = 0
    for r in rows.itertuples():
        age_m = round((pd.Timestamp(before) - r.date).days / 30.44)
        tot = r.hg + r.ag
        over25 += tot >= 3
        over35 += tot >= 4
        games.append({"date": r.date.date().isoformat(),
                      "match": f"{r.home.title()} {r.hg}-{r.ag} {r.away.title()}",
                      "total": int(tot), "over25": tot >= 3, "over35": tot >= 4,
                      "age_months": age_m, "old": age_m > 18})
    n = len(games)
    return {"n": n, "games": games,
            "over25_n": over25, "over35_n": over35,
            "avg_total": round(sum(g["total"] for g in games) / n, 2) if n else None,
            "small_sample": n < 3,
            "note": "" if n >= 3 else f"only {n} recorded meeting(s) - small sample, not conclusive"}


def team_news(home: str, away: str, kickoff: date) -> dict:
    """Team news = injuries/suspensions/lineups/rotation.

    Free data plan reality: no injuries feed is available. We therefore look
    ONLY at the optional, user-maintained data/team_news.csv (columns:
    date,home,away,note,source,checked_at). Entries older than 72h or without
    a source are ignored. If nothing is verified, say so - never invent news.
    """
    p = Path("data/team_news.csv")
    if not p.exists():
        return {"status": "unverified",
                "note": "injuries/lineups/rotation: no verified free feed on current plan",
                "source": "", "checked_at": ""}
    out = []
    with open(p) as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#") and ln.strip()]
    for row in csv.DictReader(lines):
        if (row.get("date") or "") != kickoff.isoformat():
            continue
        rh, ra = (row.get("home") or "").strip().lower(), (row.get("away") or "").strip().lower()
        if rh not in home.lower() and ra not in away.lower() \
                and rh not in away.lower() and ra not in home.lower():
            continue
        src, ts = (row.get("source") or "").strip(), (row.get("checked_at") or "").strip()
        if not src or not ts:
            continue                      # untraceable -> ignored
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 3600
            if age_h > NEWS_MAX_AGE_H:
                continue
        except ValueError:
            continue
        out.append({"note": row.get("note", "").strip(), "source": src,
                    "checked_at": ts, "confirmed":
                    (row.get("confirmed", "").strip().lower() in ("1", "yes", "true"))})
    if not out:
        return {"status": "unverified",
                "note": "injuries/lineups/rotation: no verified free feed on current plan",
                "source": "", "checked_at": ""}
    return {"status": "csv",
            "entries": out,
            "note": "user-maintained CSV - label predicted lineups as unconfirmed",
            "source": "; ".join(sorted({e["source"] for e in out})),
            "checked_at": max(e["checked_at"] for e in out)}


def fixture_evidence(model, hist: pd.DataFrame, home: str, away: str,
                     before: date, cfg: dict) -> dict:
    """Full evidence bundle for one fixture (all real numbers)."""
    elo = getattr(model, "elo", None)
    elo_r = (lambda t: elo.r(t)) if elo is not None else None
    fn = int(cfg.get("model", {}).get("form_last_n", 5))
    return {
        "form5_home": _agg(_team_rows(hist, home, None, before).head(5), home, before, elo_r),
        "form5_away": _agg(_team_rows(hist, away, None, before).head(5), away, before, elo_r),
        "form10_home": _agg(_team_rows(hist, home, None, before).head(10), home, before, elo_r),
        "form10_away": _agg(_team_rows(hist, away, None, before).head(10), away, before, elo_r),
        "split_home": _agg(_team_rows(hist, home, "home", before).head(fn), home, before, elo_r),
        "split_away": _agg(_team_rows(hist, away, "away", before).head(fn), away, before, elo_r),
        "split10_home": _agg(_team_rows(hist, home, "home", before).head(10), home, before, elo_r),
        "split10_away": _agg(_team_rows(hist, away, "away", before).head(10), away, before, elo_r),
        "h2h": head_to_head(hist, home, away, before),
        "news": team_news(home, away, before),
    }


def evidence_quality(ev: dict) -> float:
    """0..1 multiplier - how REPRESENTATIVE is the evidence (documented):
    50% from last-10 sample coverage (both teams, /20 matches total),
    25% from venue-split coverage, 25% from H2H availability (0.5 base when
    no H2H - H2H is a bonus, not a requirement)."""
    f10 = min(20, ev["form10_home"].get("n", 0) + ev["form10_away"].get("n", 0)) / 20
    splits = min(20, ev["split10_home"].get("n", 0) + ev["split10_away"].get("n", 0)) / 20
    h2h = 0.5 + 0.5 * min(1.0, ev["h2h"]["n"] / 3)
    return round(0.5 * f10 + 0.25 * splits + 0.25 * h2h, 3)


def supporting_risks(ev: dict, pred: dict, market: str) -> tuple[list[str], list[str]]:
    """Main supporting factors and main risks - generated from REAL numbers."""
    sup, risk = [], []
    total_xg = pred["lambda_home"] + pred["lambda_away"]
    sup.append(f"expected total goals {total_xg:.2f} "
               f"({pred['lambda_home']:.2f} + {pred['lambda_away']:.2f})")
    for side, key in (("home", "form5_home"), ("away", "form5_away")):
        f5 = ev[key]
        if f5.get("n", 0) >= 3:
            sup.append(f"{side} team last {f5['n']}: {f5['over25_pct']}% went over 2.5 "
                       f"(avg {f5['avg_total']} total goals)")
        if f5.get("small_sample"):
            risk.append(f"{side} recent sample small ({f5.get('n', 0)} matches)")
    h2h = ev["h2h"]
    if h2h["n"] and h2h["avg_total"] is not None and h2h["avg_total"] >= 2.6:
        sup.append(f"head-to-head avg {h2h['avg_total']} goals over {h2h['n']} meetings")
    if h2h["small_sample"]:
        risk.append(f"head-to-head: {h2h['note']}")
    if any(g["old"] for g in h2h["games"]):
        risk.append("some head-to-head meetings are >18 months old")
    if market == "OU35" and pred["markets"]["OU35"]["over"] < 0.35:
        risk.append("model sees limited over-3.5 support (this market needs "
                    "~4+ goals; higher variance)")
    if ev["news"]["status"] == "unverified":
        risk.append("team news (injuries/rotation) unverified on current plan")
    return sup, risk
