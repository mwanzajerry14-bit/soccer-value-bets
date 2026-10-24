"""Historical results & odds from football-data.co.uk (free, no key).

Main-league CSVs (E0, SP1, ...) contain full-time results plus many
bookmaker 1X2 and Over/Under 2.5 columns, including *closing* prices
(suffixed C: B365CH/PSCH/MaxCH/AvgCH, B365C>2.5 etc. in recent seasons).

NOTE (honesty): football-data has NO BTTS or Double-Chance odds columns,
so backtesting with real prices is only possible for 1X2 and O/U 2.5.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .settings import HIST, current_season_code
from .teamnames import canonical
from .util import http_get

FD_BASE = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
EXTRA_BASE = "https://www.football-data.co.uk/new/{code}.csv"  # results-only leagues

_AGG_BASES = {"Max", "Avg", "BbMx", "BbAv", "BFE"}
# Max/Avg/BbMx/BbAv are market aggregates, not a single book.
# BFE = Betfair Exchange prices: excluded because their odds aren't
# net of commission, so they're not directly comparable to book prices.


def seasons_back(code: str, n: int) -> list[str]:
    """['2526','2425',...] for the n seasons ending with `code`."""
    yy = int(code[:2])
    start = 2000 + yy if yy < 70 else 1900 + yy
    out = []
    for i in range(n):
        s = start - i
        out.append(f"{s % 100:02d}{(s + 1) % 100:02d}")
    return out


def download(code: str, season: str, cache_hours: int = 12) -> Path | None:
    """Download (with cache) one league-season CSV. Returns None on failure."""
    p = HIST / f"{code}_{season}.csv"
    is_current = season == current_season_code()
    if p.exists() and (not is_current or
                       datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)
                       < timedelta(hours=cache_hours)):
        return p
    base = FD_BASE if len(code) <= 3 else EXTRA_BASE
    r = http_get(base.format(season=season, code=code), retries=2)
    if r.status_code != 200 or not r.content.startswith(b"\xef\xbb\xbf") and b"Date" not in r.content[:400]:
        return None
    p.write_bytes(r.content)
    time.sleep(0.3)  # be polite to the free server
    return p


def load_results(code: str, seasons: list[str]) -> pd.DataFrame:
    """Results dataframe: date, home, away, hg, ag, ftr (canonical names)."""
    frames = []
    for s in seasons:
        p = download(code, s)
        if p is None:
            continue
        try:
            df = pd.read_csv(p, encoding="utf-8-sig", on_bad_lines="skip")
        except Exception:
            continue
        need = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"}
        if not need.issubset(df.columns):
            continue
        df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"])
        dt = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce", format="mixed")
        out = pd.DataFrame({
            "date": dt.dt.normalize(),
            "home": df["HomeTeam"].map(canonical),
            "away": df["AwayTeam"].map(canonical),
            "hg": pd.to_numeric(df["FTHG"], errors="coerce"),
            "ag": pd.to_numeric(df["FTAG"], errors="coerce"),
            "ftr": df.get("FTR"),
            "league": code,
            "season": s,
        }).dropna(subset=["date", "hg", "ag"])
        out["hg"] = out["hg"].astype(int)
        out["ag"] = out["ag"].astype(int)
        frames.append(out)
    if not frames:
        return pd.DataFrame(columns=["date", "home", "away", "hg", "ag", "ftr"])
    return pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------
# Odds extraction -> long format: (match index, book, market, selection, odds)
# ---------------------------------------------------------------------

def _book_triples(cols: list[str]) -> dict[str, dict[str, str]]:
    """Discover 1X2 column triples. Returns clean_book -> {H,D,A -> column}."""
    triples: dict[str, dict[str, str]] = {}
    for c in cols:
        m = re.match(r"^([A-Za-z0-9]+)([HDA])$", c)
        if not m:
            continue
        base, suffix = m.groups()
        if base.endswith(("AH",)):  # asian-handicap columns (B365AHH etc.)
            continue
        clean = base[:-1] if base.endswith("C") and len(base) > 1 else base
        closing = base.endswith("C")
        cur = triples.setdefault(clean, {})
        # prefer closing variant when both exist
        if closing or suffix not in cur:
            cur[suffix] = c
    return {b: t for b, t in triples.items() if {"H", "D", "A"} <= set(t)}


def _total_pairs(cols: list[str]) -> dict[str, dict[str, str]]:
    """Discover O/U 2.5 pairs. Returns clean_book -> {over,under -> column}."""
    pairs: dict[str, dict[str, str]] = {}
    for c in cols:
        if c.endswith(">2.5"):
            base, sel = c[:-4], "over"
        elif c.endswith("<2.5"):
            base, sel = c[:-4], "under"
        else:
            continue
        clean = base[:-1] if base.endswith("C") and len(base) > 1 else base
        closing = base.endswith("C")
        cur = pairs.setdefault(clean, {})
        if closing or sel not in cur:
            cur[sel] = c
    return {b: t for b, t in pairs.items() if {"over", "under"} <= set(t)}


def extract_book_odds(csv_path: Path) -> tuple[pd.DataFrame, dict]:
    """Returns (results_df, odds: match_index -> {book -> {market -> {sel -> odds}}}).

    Aggregate columns (Max/Avg/BbMx/BbAv) are kept but flagged in a parallel
    dict so callers can exclude them from consensus and best-price logic.
    """
    df = pd.read_csv(csv_path, encoding="utf-8-sig", on_bad_lines="skip")
    dt = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce", format="mixed")
    res = pd.DataFrame({
        "date": dt.dt.normalize(),
        "home": df["HomeTeam"].map(canonical),
        "away": df["AwayTeam"].map(canonical),
        "hg": pd.to_numeric(df.get("FTHG"), errors="coerce"),
        "ag": pd.to_numeric(df.get("FTAG"), errors="coerce"),
        "ftr": df.get("FTR"),
    }).dropna(subset=["date"])

    triples = _book_triples(list(df.columns))
    pairs = _total_pairs(list(df.columns))
    odds: dict[int, dict] = {}
    aggregates: set[str] = set()
    for i in res.index:
        books: dict[str, dict] = {}
        for book, cols in triples.items():
            try:
                o = {"home": float(df.at[i, cols["H"]]),
                     "draw": float(df.at[i, cols["D"]]),
                     "away": float(df.at[i, cols["A"]])}
            except Exception:
                continue
            if min(o.values()) > 1.0:
                clean = book[:-1] if book.endswith("C") else book
                if clean in _AGG_BASES:
                    aggregates.add(book)
                books.setdefault(book, {})["1X2"] = o
        for book, cols in pairs.items():
            try:
                o = {"over": float(df.at[i, cols["over"]]),
                     "under": float(df.at[i, cols["under"]])}
            except Exception:
                continue
            if min(o.values()) > 1.0:
                clean = book[:-1] if book.endswith("C") else book
                if clean in _AGG_BASES:
                    aggregates.add(book)
                books.setdefault(book, {})["OU25"] = o
        if books:
            odds[int(i)] = books
    return res, {"odds": odds, "aggregate_books": aggregates}


def latest_results_with_odds(code: str, season: str) -> tuple[pd.DataFrame, dict] | None:
    p = download(code, season)
    if p is None:
        return None
    return extract_book_odds(p)
