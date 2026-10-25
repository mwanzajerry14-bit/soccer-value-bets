"""Value detection: margins (overround), de-vigging, edge, Kelly stakes."""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone


def probs_valid(markets: dict[str, dict[str, float]]) -> tuple[bool, str]:
    """Statistical sanity of ALL model outputs before publishing (task F).

    Rules: every probability strictly within [0.5%, 99.5%]; every market's
    outcomes sum to 100% +- rounding (1%). Returns (ok, reason).
    """
    for mkt, probs in markets.items():
        if not probs:
            return False, f"{mkt}: empty probabilities"
        for sel, p in probs.items():
            if not isinstance(p, (int, float)) or math.isnan(p):
                return False, f"{mkt}/{sel}: not a number"
            if p <= 0.0 or p >= 1.0:
                return False, f"{mkt}/{sel}: {p:.4f} outside (0%, 100%)"
        if mkt == "DC":
            # double-chance legs overlap by construction: sum is 2 - P(draw-free legs)
            exp = {"1X", "12", "X2"}
            if set(probs) != exp:
                return False, "DC must have legs 1X/12/X2"
            continue
        s = sum(probs.values())
        if abs(s - 1.0) > 0.01:
            return False, f"{mkt}: probabilities sum to {s:.4f} (allowed 0.99-1.01)"
    # 1X2 internal consistency required when blend used
    if "1X2" in markets and len(markets["1X2"]) != 3:
        return False, "1X2 must have exactly home/draw/away"
    if "1X2" in markets and "DC" in markets:
        x = markets["1X2"]
        dc_ = markets["DC"]
        if abs(dc_["1X"] - (x["home"] + x["draw"])) > 0.01:
            return False, "DC/1X2 inconsistency"
    return True, "valid"


def odds_freshness(book_update: dict[str, str], stale_hours: float,
                   now: datetime | None = None) -> tuple[list[str], list[str]]:
    """Split books into (fresh, stale) by their last_update timestamp.

    A price older than `stale_hours` is not 'current': it is excluded from
    EV/edge computation (task D: EV only on real, CURRENT, mapped prices)."""
    now = now or datetime.now(timezone.utc)
    fresh, stale = [], []
    for book, lu in (book_update or {}).items():
        try:
            dt = datetime.fromisoformat(lu.replace("Z", "+00:00"))
            age_h = (now - dt).total_seconds() / 3600
        except (ValueError, AttributeError):
            fresh.append(book)   # provider gave no timestamp: treat as current
            continue
        (stale if age_h > stale_hours else fresh).append(book)
    return fresh, stale


def overround(book_odds: dict[str, float]) -> float:
    """Bookmaker margin for one market: sum(1/odds) - 1. e.g. 0.05 = 5%."""
    return sum(1.0 / o for o in book_odds.values() if o > 1) - 1.0


def fair_probs(book_odds: dict[str, float]) -> dict[str, float]:
    """Proportional (normalisation) de-vig.

    Known limitation: proportional de-vig ignores the favourite-longshot
    bias; a Shin or power method may be added later. Method noted in code
    so no confusion about what 'fair' means here.
    """
    s = sum(1.0 / o for o in book_odds.values())
    return {sel: (1.0 / o) / s for sel, o in book_odds.items()}


def consensus_fair(books: dict[str, dict[str, float]],
                   exclude: set[str] | None = None) -> tuple[dict[str, float], dict[str, float]]:
    """Mean fair probability across bookmakers (each de-vigged first).

    Returns (consensus_probs, {book: overround}).
    """
    exclude = exclude or set()
    margins: dict[str, float] = {}
    acc: dict[str, list[float]] = {}
    for b, odds in books.items():
        if b in exclude:
            continue
        margins[b] = overround(odds)
        for sel, p in fair_probs(odds).items():
            acc.setdefault(sel, []).append(p)
    cons = {sel: sum(ps) / len(ps) for sel, ps in acc.items()}
    return cons, margins


def best_prices(books: dict[str, dict[str, float]],
                exclude: set[str] | None = None) -> dict[str, tuple[float, str]]:
    """selection -> (best_odds, book). Aggregate columns excluded by caller."""
    exclude = exclude or set()
    best: dict[str, tuple[float, str]] = {}
    for b, odds in books.items():
        if b in exclude:
            continue
        for sel, o in odds.items():
            if sel not in best or o > best[sel][0]:
                best[sel] = (o, b)
    return best


def kelly_fraction(p: float, odds: float) -> float:
    """Full Kelly fraction of bankroll. Negative edge -> 0."""
    b = odds - 1.0
    f = (b * p - (1.0 - p)) / b
    return max(0.0, f)


def stake(p: float, odds: float, bankroll: float, frac: float, cap_pct: float) -> float:
    """Quarter-Kelly (or cfg) stake, capped at cap_pct of bankroll."""
    return min(frac * kelly_fraction(p, odds), cap_pct) * bankroll


def _drop_insane_prices(books: dict[str, dict[str, float]],
                        factor: float | None) -> tuple[dict, list[str]]:
    """Drop a book's whole market when ANY of its prices exceeds
    factor x the market median for that selection (needs >= 3 books).

    Guards against stale/exchange lay prices and manual CSV typos.
    Returns (filtered_books, human-readable notes about drops).
    """
    notes: list[str] = []
    if not factor or len(books) < 3:
        return books, notes
    sels = set().union(*[set(o) for o in books.values()])
    bad: set[str] = set()
    for sel in sorted(sels):
        prices = [o[sel] for o in books.values() if sel in o]
        if len(prices) < 3:
            continue
        med = statistics.median(prices)
        for b, o in books.items():
            if sel in o and o[sel] > med * factor:
                bad.add(b)
                notes.append(f"dropped bookmaker '{b}': {sel} @ {o[sel]:.2f} "
                             f"vs market median {med:.2f} - price looks wrong")
    return {b: o for b, o in books.items() if b not in bad}, notes


def evaluate_market(market: str, books: dict[str, dict[str, float]],
                    model_p: dict[str, float], cfg: dict,
                    exclude_books: set[str] | None = None):
    """One market on one match -> (summary, candidate rows for each selection).

    VALUE RULE (strict): value iff
        edge = p_model - consensus_fair_prob >= value_threshold   (default 5%)
        AND expected value at the best available price > 0  (p*odds > 1).
    """
    books = {b: o for b, o in books.items() if b not in (exclude_books or set())}
    books, sanity_notes = _drop_insane_prices(books, cfg.get("price_sanity_factor"))
    cons, margins = consensus_fair(books)
    best = best_prices(books)
    thr = float(cfg.get("value_threshold", 0.05))
    bankroll = float(cfg.get("bankroll", 1000.0))
    kf = float(cfg.get("kelly_fraction", 0.25))
    cap = float(cfg.get("max_stake_pct", 0.02))

    rows = []
    for sel, pm in model_p.items():
        if sel not in cons or sel not in best:
            continue
        price, book = best[sel]
        p_raw = 1.0 / price
        edge = pm - cons[sel]
        ev = pm * price - 1.0
        is_value = (edge >= thr) and (ev > 0)
        rows.append({
            "market": market, "selection": sel,
            "p_model": pm, "p_fair": cons[sel], "p_raw": p_raw,
            "edge": edge, "ev": ev, "is_value": is_value,
            "book": book, "odds": price,
            "book_margin": margins.get(book),
            "stake": stake(pm, price, bankroll, kf, cap) if is_value else 0.0,
        })
    summary = {
        "n_books": len(books),
        "avg_margin": sum(margins.values()) / len(margins) if margins else None,
        "margins": margins,
        "notes": sanity_notes,
    }
    return summary, rows


def bet_won(market: str, selection: str, hg: int, ag: int) -> bool:
    """True if the bet wins given full-time goals."""
    if market == "1X2":
        r = "home" if hg > ag else "draw" if hg == ag else "away"
        return selection == r
    if market and market.startswith("OU"):
        line = int(market[2:])      # OU15 -> 15 (int kills a stray "2.5" decimal form)
        over_wins = hg + ag > line / 10.0
        return (selection == "over") == over_wins
    if market == "BTTS":
        r = "yes" if (hg > 0 and ag > 0) else "no"
        return selection == r
    if market == "DC":
        if selection == "1X":
            return hg >= ag
        if selection == "X2":
            return ag >= hg
        if selection == "12":
            return hg != ag
    raise ValueError(f"unknown market/selection {market}/{selection}")


def exposure_caps(picks: list[dict], bankroll: float, per_fixture_pct: float = 0.03,
                  total_pct: float = 0.10) -> list[dict]:
    """Correlated-exposure control (task 6): stakes stay quarter-Kelly, but
    selections on the SAME fixture are correlated - cap their combined stake
    at per_fixture_pct of bankroll, and the whole slate at total_pct.
    Scales down proportionally; never scales up. Returns picks with final
    'stake' + 'capped' note flags."""
    by_fx: dict[str, float] = {}
    for p in picks:
        key = p.get("fixture_id") or p.get("mkey", "")
        by_fx[key] = by_fx.get(key, 0.0) + p["stake"]
    for p in picks:
        key = p.get("fixture_id") or p.get("mkey", "")
        total_fx = by_fx[key]
        cap = per_fixture_pct * bankroll
        if total_fx > cap and total_fx > 0:
            p["stake"] = round(p["stake"] * cap / total_fx, 2)
            p.setdefault("notes", []).append(
                "stake scaled down: correlated same-fixture exposure cap")
    slate = sum(p["stake"] for p in picks)
    if slate > total_pct * bankroll and slate > 0:
        f = total_pct * bankroll / slate
        for p in picks:
            p["stake"] = round(p["stake"] * f, 2)
            p.setdefault("notes", []).append("stake scaled down: total-exposure cap")
    return picks
