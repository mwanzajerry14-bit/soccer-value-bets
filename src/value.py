"""Value detection: margins (overround), de-vigging, edge, Kelly stakes."""
from __future__ import annotations


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


def evaluate_market(market: str, books: dict[str, dict[str, float]],
                    model_p: dict[str, float], cfg: dict,
                    exclude_books: set[str] | None = None):
    """One market on one match -> (summary, candidate rows for each selection).

    VALUE RULE (strict): value iff
        edge = p_model - consensus_fair_prob >= value_threshold   (default 5%)
        AND expected value at the best available price > 0  (p*odds > 1).
    """
    cons, margins = consensus_fair(books, exclude_books)
    best = best_prices(books, exclude_books)
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
        "n_books": len([b for b in books if b not in (exclude_books or set())]),
        "avg_margin": sum(margins.values()) / len(margins) if margins else None,
        "margins": margins,
    }
    return summary, rows


def bet_won(market: str, selection: str, hg: int, ag: int) -> bool:
    """True if the bet wins given full-time goals."""
    if market == "1X2":
        r = "home" if hg > ag else "draw" if hg == ag else "away"
        return selection == r
    if market == "OU25":
        r = "over" if hg + ag >= 3 else "under"
        return selection == r
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
