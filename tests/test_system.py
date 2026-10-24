"""Automated tests - SYNTHETIC MOCK DATA ONLY (task section 11).
No network, no live fixtures: demo data never touches production data."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["SVB_DB_PATH"] = str(ROOT / "data" / "test_unit.db")  # isolated test DB

from src import db                                        # noqa: E402
from src.value import (evaluate_market, fair_probs, overround, bet_won,  # noqa: E402
                       kelly_fraction)
from src.verification import Fixture, Verifier, confidence_category      # noqa: E402
from src.dixon_coles import market_probs, score_matrix                    # noqa: E402
from src.util import match_key                                            # noqa: E402
from src.teamnames import LeagueMatcher                                   # noqa: E402

CFG = {"value_threshold": 0.05, "bankroll": 1000.0, "kelly_fraction": 0.25,
       "max_stake_pct": 0.02, "price_sanity_factor": 2.0,
       "min_books": 3, "min_completeness": 60}


def fx(home="alpha", away="beta", status="scheduled", books=None, days_ahead=1,
       pid="ev1"):
    books = books or {f"B{i}": {"1X2": {"home": 2.0, "draw": 3.4, "away": 4.0}}
                      for i in range(4)}
    d = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).date().isoformat()
    f = Fixture("test", pid, "Test League", d, "18:00", home, away, status)
    f.books = books
    return f


@pytest.fixture(autouse=True)
def clean_db():
    p = Path(os.environ["SVB_DB_PATH"])
    if p.exists():
        p.unlink()
    yield
    if p.exists():
        p.unlink()


# ---------------- probability validity ----------------

def test_1x2_probabilities_sum_to_100():
    P = score_matrix(1.6, 1.1, -0.1, 10)
    m = market_probs(P)
    s = sum(m["1X2"].values())
    assert abs(s - 1.0) < 1e-9, f"1X2 sums to {s}"            # market validity
    assert abs(sum(m["OU25"].values()) - 1.0) < 1e-9
    assert abs(sum(m["BTTS"].values()) - 1.0) < 1e-9
    assert abs(m["DC"]["1X"] - (m["1X2"]["home"] + m["1X2"]["draw"])) < 1e-9


def test_devig_math():
    odds = {"home": 2.0, "draw": 3.5, "away": 4.0}
    r = overround(odds)
    assert abs(r - (0.5 + 1 / 3.5 + 0.25 - 1)) < 1e-9
    fp = fair_probs(odds)
    assert abs(sum(fp.values()) - 1.0) < 1e-9
    assert abs(fp["home"] - 0.5 / (1 + r)) < 1e-9


# ---------------- price sanity + value rule ----------------

def test_insane_price_dropped_with_note():
    books = {"B1": {"home": 2.0, "draw": 3.4, "away": 4.0},
             "B2": {"home": 2.05, "draw": 3.3, "away": 3.9},
             "B3": {"home": 1.98, "draw": 3.5, "away": 4.1},
             "Bad": {"home": 25.0, "draw": 3.4, "away": 4.0}}
    summary, rows = evaluate_market("1X2", books, {"home": .45, "draw": .3, "away": .25}, CFG)
    assert all(r["book"] != "Bad" for r in rows)
    assert any("Bad" in n for n in summary["notes"])


# ---------------- verification gates ----------------

def test_valid_fixture_passes_when_verified():
    v = Verifier(CFG, now_utc=datetime.now(timezone.utc))
    f = fx()
    d = f.date
    idx = {d: [f.mkey]}
    checks = v.checks(f, idx, [{"date": d, "home": "alpha", "away": "beta"}], "ok")
    comp = v.completeness(f, checks, True, True)
    ok, reason, _ = v.verdict(f, checks, comp)
    assert ok, reason
    assert comp >= 60


def test_unmatched_team_names_flagged():
    v = Verifier(CFG)
    f = fx(home="", away="beta")
    checks = v.checks(f, {}, None, "n/a")
    ok, reason, _ = v.verdict(f, checks, v.completeness(f, checks, True, True))
    assert not ok and reason == "Match verification failed"


def test_completed_match_cannot_be_published():
    v = Verifier(CFG, now_utc=datetime.now(timezone.utc))
    f = fx(status="completed", days_ahead=-1)
    checks = v.checks(f, {f.date: [f.mkey]}, None, "n/a")
    ok, _, _ = v.verdict(f, checks, 95.0)
    assert not ok


def test_postponed_match_rejected():
    v = Verifier(CFG)
    f = fx(status="postponed")
    checks = v.checks(f, {f.date: [f.mkey]}, None, "n/a")
    ok, reason, _ = v.verdict(f, checks, 95.0)
    assert not ok and "verification failed" in reason.lower()


def test_conflicting_sources_require_review():
    v = Verifier(CFG, now_utc=datetime.now(timezone.utc))
    f = fx()
    # second source lists a DIFFERENT home team for this date -> conflict note
    checks = v.checks(f, {f.date: [f.mkey]},
                      [{"date": f.date, "home": "gamma", "away": "beta"}], "ok")
    sec = [c for c in checks if c.name == "second_source"][0]
    assert not sec.ok and "review" in sec.detail.lower()


def test_team_appearing_twice_same_day_flagged():
    v = Verifier(CFG, now_utc=datetime.now(timezone.utc))
    f = fx()
    other = match_key(f.date, "alpha", "gamma")       # alpha twice in one day
    checks = v.checks(f, {f.date: [f.mkey, other]}, None, "n/a")
    chk = [c for c in checks if c.name == "team_play_once_per_day"][0]
    assert not chk.ok


def test_low_completeness_withholds_prediction():
    v = Verifier(CFG)
    books = {"Only1": {"1X2": {"home": 2.0, "draw": 3.4, "away": 4.0}}}
    f = fx(books=books)
    checks = v.checks(f, {f.date: [f.mkey]}, None, "no second source")
    comp = v.completeness(f, checks, False, False)   # no history, no form, 1 book
    ok, reason, _ = v.verdict(f, checks, comp)
    assert not ok and "Insufficient data" in reason


# ---------------- grading + immutability ----------------

def test_bet_grading_all_markets():
    assert bet_won("1X2", "home", 2, 0) and not bet_won("1X2", "away", 2, 0)
    assert bet_won("OU25", "over", 2, 1) and bet_won("OU25", "under", 1, 0)
    assert bet_won("BTTS", "yes", 1, 1) and bet_won("BTTS", "no", 1, 0)
    assert bet_won("DC", "1X", 1, 1) and bet_won("DC", "X2", 1, 2) and bet_won("DC", "12", 2, 1)
    assert not bet_won("DC", "12", 0, 0)


def test_prediction_snapshot_immutable_after_settle(tmp_path):
    with db.conn() as c:
        p = dict(run_date="2026-01-01", date="2026-01-01", league="L", mkey="k",
                 home="a", away="b", market="1X2", selection="home", book="T",
                 odds=2.0, p_model=0.55, p_fair=0.5, p_raw=0.5, edge=0.05,
                 ev=0.1, kelly=0.05, stake=20.0)
        db.add_pick(c, p)
        pid = c.execute("SELECT id FROM picks").fetchone()["id"]
        db.settle_pick(c, pid, 3, 1, True, 20.0, 2.0)
        row = c.execute("SELECT * FROM picks WHERE id=?", (pid,)).fetchone()
        assert row["p_model"] == 0.55 and row["odds"] == 2.0   # inputs untouched
        assert row["status"] == "won" and row["profit"] == 20.0


def test_unique_constraints_prevent_duplicates():
    with db.conn() as c:
        f = fx().to_row()
        db.upsert_fixture(c, f)
        db.upsert_fixture(c, f)   # same fixture twice -> single row
        n = c.execute("SELECT COUNT(*) n FROM fixtures").fetchone()["n"]
        assert n == 1


# ---------------- timezone ----------------

def test_timezone_display_conversion():
    from zoneinfo import ZoneInfo
    dt = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)       # SAST = UTC+2 winter
    cat = dt.astimezone(ZoneInfo("Africa/Johannesburg"))
    assert cat.hour == 20 and cat.strftime("%H:%M") == "20:00"
    assert ZoneInfo("Africa/Johannesburg").key == "Africa/Johannesburg"


# ---------------- secrets must not leak ----------------

def test_no_secret_keys_in_tracked_files():
    bad = []
    for p in ROOT.rglob("*"):
        if p.is_dir() or ".git" in p.parts or p.suffix in (".pyc", ".db", ".png"):
            continue
        if p.name in ("test_system.py",):
            continue
        if p.suffix not in (".py", ".yml", ".yaml", ".md", ".txt", ".csv", ".html", ".example"):
            continue
        txt = p.read_text(errors="ignore")
        assert "password" not in txt.lower() or p.name in ("README.md", "daily.yml")
        for marker in ("x-apisports-key: ", "apiKey=sk", "ODDS_API_KEY=1", "BEGIN PRIVATE"):
            if marker in txt:
                bad.append((p, marker))
    assert not bad, f"possible secret leak in {bad}"
