"""Automated tests - SYNTHETIC MOCK DATA ONLY (task section 11).
No network, no live fixtures: demo data never touches production data."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
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


# ============== H: window, midnight crossing, gates v3, identity ==============

def test_rolling_four_day_window_dynamic():
    from src.util import rolling_window_dates
    got = rolling_window_dates(date(2026, 10, 24), 4)
    assert got == ["2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27"]
    # month/year rollover handled by real dates, never hardcoded
    assert rolling_window_dates(date(2026, 12, 31), 2)[1] == "2027-01-01"


def test_kickoff_midnight_crossing_utc_to_lusaka():
    # 22:30 UTC on the 24th == 00:30 CAT on the 25th -> window day 1, not 0
    from run_daily import kickoff_dt
    from zoneinfo import ZoneInfo
    kt = kickoff_dt("2026-10-24", "22:30")
    local = kt.astimezone(ZoneInfo("Africa/Lusaka"))
    assert local.date().isoformat() == "2026-10-25"
    # 23:59 UTC on the 26th -> 01:59 CAT on the 27th = last window day
    kt2 = kickoff_dt("2026-10-26", "23:59")
    assert kt2.astimezone(ZoneInfo("Africa/Lusaka")).date().isoformat() == "2026-10-27"


def test_lusaka_no_dst_utc_plus_2_all_year():
    from zoneinfo import ZoneInfo
    for month in (1, 7):
        dt = datetime(2026, month, 15, 12, 0, tzinfo=timezone.utc)
        assert dt.astimezone(ZoneInfo("Africa/Lusaka")).utcoffset() == timedelta(hours=2)


def test_probability_validation_rejects_bad_models():
    from src.value import probs_valid
    ok, _ = probs_valid({"1X2": {"home": .5, "draw": .3, "away": .2},
                         "OU25": {"over": .6, "under": .4}})
    assert ok
    ok, why = probs_valid({"1X2": {"home": .5, "draw": .3, "away": .1}})      # sum 90%
    assert not ok and "sum" in why
    ok, why = probs_valid({"OU25": {"over": 1.2, "under": -.2}})              # out of range
    assert not ok
    ok, why = probs_valid({"BTTS": {"yes": float("nan"), "no": .5}})
    assert not ok
    # rounding tolerance: 1.004 passes
    ok, _ = probs_valid({"1X2": {"home": .5, "draw": .302, "away": .202}})
    assert ok


def test_model_markets_cover_ou15_ou35_and_scoreline():
    P = score_matrix(1.8, 1.2, -0.08, 10)
    m = market_probs(P)
    for k in ("1X2", "OU15", "OU25", "OU35", "BTTS"):
        assert k in m and abs(sum(m[k].values()) - 1.0) < 1e-9
    # DC legs overlap (each covers two 1X2 outcomes): check consistency, not sum
    assert abs(m["DC"]["1X"] - (m["1X2"]["home"] + m["1X2"]["draw"])) < 1e-9
    assert abs(m["DC"]["X2"] - (m["1X2"]["draw"] + m["1X2"]["away"])) < 1e-9
    from src.dixon_coles import top_scoreline
    h, a = top_scoreline(P)
    assert 0 <= h <= 10 and 0 <= a <= 10
    # full set passes publication validation
    from src.value import probs_valid
    ok, why = probs_valid(m)
    assert ok, why


def test_stale_prices_excluded_from_ev():
    from src.value import odds_freshness
    now = datetime.now(timezone.utc)
    bu = {"Fresh": (now - timedelta(hours=2)).isoformat(),
          "Stale": (now - timedelta(hours=72)).isoformat()}
    fresh, stale = odds_freshness(bu, 36, now)
    assert fresh == ["Fresh"] and stale == ["Stale"]


def test_af_error_classification_and_coverage():
    from src.api_football import classify_errors, season_covered, covered_seasons_label
    kind, msg = classify_errors({"plan": "Free plans do not have access to this "
                                         "season, try from 2022 to 2024."})
    assert kind == "plan_scope" and "{" not in msg          # no raw dumps to users
    cov = {"seasons": [{"year": y} for y in (2022, 2023, 2024)],
           "coverage": {"fixtures": True, "odds": False}}
    assert not season_covered(cov, 2026) and season_covered(cov, 2023)
    assert covered_seasons_label(cov) == "2022-2024"


def test_publication_gate_blocks_fallback_and_bad_probs():
    from src.verification import publication_gate
    pub, why = publication_gate(True, "verified", False, True, "valid")
    assert not pub and "withheld" in why.lower() and "heuristic" in why.lower()
    pub, why = publication_gate(True, "verified", True, False, "1X2 sums to 0.90")
    assert not pub and "validation" in why.lower()
    pub, _ = publication_gate(False, "Match verification failed", True, True, "valid")
    assert not pub
    pub, _ = publication_gate(True, "verified", True, True, "valid")
    assert pub
    pub, _ = publication_gate(True, "verified", True, True, "valid", window_ok=False)
    assert not pub


def test_prediction_identity_reuse_and_revision():
    with db.conn() as c:
        p = dict(run_id="r1", fixture_id="f1", mkey="k", date="2026-10-24", league="L",
                 home="alpha", away="beta", market="1X2", selection="home",
                 p_model=.5, p_fair=.45, model_version="m1", sources=["t"],
                 completeness=80, confidence="moderate", generated_at="2026-10-24T06:00:00+00:00")
        pid, gen_at, reused = db.prediction_v2_insert(c, p, {"x": 1})
        assert not reused
        p2 = dict(p, run_id="r2", generated_at="2026-10-25T06:00:00+00:00")
        pid2, gen_at2, reused2 = db.prediction_v2_insert(c, p2, {"x": 1})
        assert reused2 and pid2 == pid and gen_at2 == "2026-10-24T06:00:00+00:00"
        n = c.execute("SELECT COUNT(*) n FROM predictions_v2 WHERE fixture_id='f1'").fetchone()["n"]
        assert n == 1
        # deliberate recalculation: new row, original superseded - history kept
        pid3, _, reused3 = db.prediction_v2_insert(c, p2, {"x": 1},
                                                   recalc_reason="manual recalc")
        assert not reused3 and pid3 != pid
        rows = c.execute("SELECT * FROM predictions_v2 WHERE fixture_id='f1' "
                         "ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[0]["status"] == "superseded" and rows[1]["status"] == "active"
        assert rows[1]["revision_of"] == rows[0]["id"]


def test_multiple_markets_same_fixture_not_duplicates():
    with db.conn() as c:
        base = dict(run_id="r1", fixture_id="f9", mkey="k", date="2026-10-24",
                    league="L", home="alpha", away="beta", p_model=.5, p_fair=.45,
                    model_version="m1", sources=["t"], completeness=80,
                    confidence="moderate", generated_at="2026-10-24T06:00:00+00:00")
        db.prediction_v2_insert(c, dict(base, market="1X2", selection="home"), {})
        db.prediction_v2_insert(c, dict(base, market="OU25", selection="over"), {})
        n = c.execute("SELECT COUNT(*) n FROM predictions_v2 WHERE fixture_id='f9'"
                      ).fetchone()["n"]
        assert n == 2                                     # different markets: allowed


def test_pending_pick_identity_across_runs():
    with db.conn() as c:
        p = dict(run_date="2026-10-24", date="2026-10-27", league="L",
                 mkey="2026-10-27|alpha|beta", home="alpha", away="beta",
                 market="1X2", selection="home", book="T", odds=2.0, p_model=.55,
                 p_fair=.5, p_raw=.5, edge=.05, ev=.1, kelly=.05, stake=20.0)
        assert db.add_pick(c, p)
        assert not db.add_pick(c, dict(p, run_date="2026-10-25"))   # reuse, not dupe


def test_report_sections_and_filters_render():
    from src.report import render
    from src.verification import Fixture as Fx
    f_pred = dict(league="England Premier League", country="England",
                  date="2026-10-25", date_utc="2026-10-25", time_utc="16:30",
                  kickoff_local="Sun 25 Oct · 18:30 CAT", day_label="Tomorrow",
                  day_index=1, fid="the-odds-api:abc123", home="alpha", away="beta",
                  status="scheduled", section="predicted", failed=False,
                  verify={"verified": True, "completeness": 85.0, "status": "scheduled",
                          "confidence": "moderate", "sources": ["the-odds-api"]},
                  explain="model xG 1.5-1.0", top_score=(2, 1),
                  xg=(1.5, 1.0), form={"home": {"played": 5, "pts": 9, "gf": 8, "ga": 3},
                                       "away": {"played": 5, "pts": 4, "gf": 5, "ga": 7}},
                  model_version="dc-elo-1.2@test", markets=[], model_only=[], notes=[])
    f_with = dict(f_pred, section="withheld", home="gamma", away="delta",
                  fid="the-odds-api:def456", verify={"verified": True,
                  "completeness": 40.0, "status": "scheduled"},
                  withheld_reason="Prediction withheld - insufficient verified data")
    f_live = dict(f_pred, section="blocked", home="eps", away="zeta",
                  fid="the-odds-api:ghi789", verify={"verified": False,
                  "completeness": 0, "status": "live"},
                  status_note="in play - no pre-match predictions offered")
    run = {"date": "2026-10-24", "generated_at": "2026-10-24 06:30 CAT",
           "generated_utc": "2026-10-24 04:30 UTC", "threshold": .05,
           "bankroll": 1000.0, "n_fixtures": 3, "model_version": "dc-elo-1.2@test",
           "window_dates": ["2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27"],
           "counts": {"predicted": 1, "withheld": 1, "failed": 0, "blocked": 1},
           "notices": []}
    page = render(run, [], [f_pred, f_with, f_live], [])
    for token in ("Verified upcoming fixtures", "prediction withheld", "Verification failed",
                  "Not upcoming", "fixture id:", "the-odds-api:abc123", "most likely score 2-1",
                  "Africa/Lusaka", "all days", "all countries", "all competitions",
                  "all markets", "Show more", "insufficient verified data",
                  "in play - no pre-match predictions offered"):
        assert token in page, token
    # withheld fixture must NOT expose probabilities/margins
    assert "54.0%" not in page.split("prediction withheld")[1][:800]


# ============== acceptance: calendar, over-goals, staking caps ==============

def test_calendar_today_and_window_from_server_utc():
    # acceptance: today=25 Oct 2026 -> 4-day view covers 25-28 inclusive
    from build_calendar import build_data
    import pandas as _pd
    with db.conn() as c:
        f = fx(pid="w1", days_ahead=1).to_row()
        f["kickoff_utc"] = "2026-10-26T16:30:00Z"
        db.upsert_fixture(c, f)
    data = build_data(today=date(2026, 10, 25))
    assert data["lusaka_today"] == "2026-10-25"
    assert data["window4"] == ["2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28"]
    assert "2026-10-26" in data["days"]
    row = data["days"]["2026-10-26"][0]
    assert row["kickoff"] == "18:30"          # 16:30 UTC -> 18:30 CAT
    assert row["fid"].startswith("test:")


def test_calendar_past_date_keeps_result_row():
    from build_calendar import build_data
    with db.conn() as c:
        f = fx(pid="p1").to_row()
        f["date"] = "2026-10-24"
        f["mkey"] = "2026-10-24|alpha|beta"
        f["kickoff_utc"] = "2026-10-24T13:00:00Z"
        db.upsert_fixture(c, f)
        db.set_fixture_prediction(c, f["fid"], "withheld", "no prices")
    data = build_data(today=date(2026, 10, 25))
    row = data["days"]["2026-10-24"][0]
    # no provider result yet -> honest awaiting label, never a fake score
    assert row["status"] == "awaiting-result" and row["score"] is None


def test_ou_lines_grading():
    assert bet_won("OU25", "over", 2, 1) and not bet_won("OU25", "under", 2, 1)
    assert bet_won("OU35", "over", 2, 2) and not bet_won("OU35", "over", 2, 1)
    assert bet_won("OU15", "under", 1, 0) and not bet_won("OU15", "under", 1, 1)


def test_exposure_caps_correlated_and_total():
    from src.value import exposure_caps
    picks = [
        {"fixture_id": "A", "mkey": "a", "stake": 25.0, "notes": []},
        {"fixture_id": "A", "mkey": "a", "stake": 20.0, "notes": []},  # same fixture
        {"fixture_id": "B", "mkey": "b", "stake": 20.0, "notes": []},
        {"fixture_id": "C", "mkey": "c", "stake": 20.0, "notes": []},
        {"fixture_id": "D", "mkey": "d", "stake": 20.0, "notes": []},
    ]
    out = exposure_caps(picks, bankroll=1000.0, per_fixture_pct=0.03, total_pct=0.10)
    by_a = sum(p["stake"] for p in out if p["fixture_id"] == "A")
    assert by_a <= 30.0 + 1e-9                      # 3% per-fixture cap
    assert sum(p["stake"] for p in out) <= 100.0 + 1e-9   # 10% slate cap
    # stake never appears without those caps being respected
    assert all(p["stake"] <= 30.0 + 1e-9 for p in out)


def test_goals_evidence_real_numbers(tmp_path, monkeypatch):
    import pandas as pd
    monkeypatch.chdir(tmp_path)          # no data/team_news.csv here -> unverified
    from src.goals_analysis import (head_to_head, fixture_evidence, evidence_quality,
                                    supporting_risks, team_news)
    rows = []
    d0 = date(2026, 9, 1)
    for i in range(12):
        d = d0 + timedelta(days=7 * i)
        rows.append(dict(date=pd.Timestamp(d), home="alpha", away="gamma",
                         hg=3, ag=1))                 # alpha home wins 3-1 (over)
        rows.append(dict(date=pd.Timestamp(d + timedelta(days=2)), home="beta",
                         away="alpha", hg=1, ag=2))   # alpha away wins (3 total: over2.5)
    hist = pd.DataFrame(rows)

    class _E:                                   # tiny elo stub
        def r(self, t):
            return 1500
    model = type("M", (), {"elo": _E()})
    ev = fixture_evidence(model(), hist, "alpha", "beta", date(2026, 12, 1),
                          {"model": {"form_last_n": 5}})
    f5 = ev["form5_home"]
    assert f5["n"] == 5 and f5["over25_n"] + f5["n"] >= f5["over25_n"]
    assert f5["over25_pct"] >= 80               # these synthetic games are high-scoring
    assert ev["split_home"]["n"] >= 4           # home-only sample
    q = evidence_quality(ev)
    assert 0.5 <= q <= 1.0
    h = head_to_head(hist, "alpha", "beta", date(2026, 12, 1))
    assert h["n"] == 5 and h["over25_n"] == 5 and h["over35_n"] == 0   # latest 5, totals=3
    news = team_news("alpha", "beta", date(2026, 12, 1))
    assert news["status"] == "unverified"       # no feed + no CSV: never invented
    sup, risk = supporting_risks(
        ev, {"lambda_home": 1.9, "lambda_away": 1.1,
             "markets": {"OU35": {"over": 0.31}}}, "OU35")
    assert any("expected total goals 3.00" in s for s in sup)
    assert any("news" in r.lower() for r in risk)


def test_over_tables_separate_and_ranked_not_by_ev():
    from src.report import render
    mk = lambda rank, p, ev, conf: {"rank": rank, "fixture": "a t%d v b t%d" % (rank, rank),
        "league": "L", "country": "C", "kickoff_local": "Sat · 18:00 CAT",
        "day_label": "Today", "date": "2026-10-25", "fid": "f%d" % rank, "p": p,
        "fair_odds": round(1 / p, 2), "xg_total": 3.1, "top_score": (2, 1),
        "conf": conf, "completeness": 85, "quality": .8, "odds": 2.0 if ev else None,
        "book": "B365" if ev else None, "ev": ev, "is_value": bool(ev),
        "form_ev": "x", "venue_ev": "y", "h2h_ev": "z", "news": "unverified",
        "support": ["s1"], "risks": ["r1"], "model_version": "m"}
    # rows pre-ranked by run_daily rule; render must keep tables separate and ordered
    A = [mk(1, .62, .3, "high"), mk(2, .58, -.1, "high")]
    B = [mk(1, .33, None, "moderate")]
    run = {"date": "2026-10-25", "generated_at": "x", "generated_utc": "y",
           "threshold": .05, "bankroll": 1000.0, "n_fixtures": 0,
           "model_version": "m", "window_dates": ["2026-10-25"], "counts": {},
           "notices": [], "over_tables": {"A": A, "B": B}}
    page = render(run, [], [], [])
    assert "TABLE A — BEST OVER 2.5" in page and "TABLE B — BEST OVER 3.5" in page
    ia = page.index("TABLE A"); ib = page.index("TABLE B")
    assert ia < ib                                    # separate tables
    assert "62.0%" in page[ia:ib] and "33.0%" in page[ib:]
    assert "EV never determines rank" in page


def test_calendar_html_nav_controls_and_cachebuster():
    from build_calendar import HTML_PAGE
    page = HTML_PAGE.replace("{CSS}", "")
    for token in ("Previous day", "Next day", "Today", "4-day upcoming view",
                  "type=\"date\"", "dfrom", "Load range", "upcoming", "live",
                  "results", "calendar_data.json?v=", "lusaka_today",
                  "generated_at_utc", "fresh"):
        assert token in page, token


class TestAutoDating:
    """Task 3 acceptance: the site must compute the Zambia date live, every page
    load — a stale build/commit/file must NEVER masquerade as 'today'."""

    def test_midnight_lusaka_boundary(self):
        from zoneinfo import ZoneInfo
        lu = ZoneInfo("Africa/Lusaka")
        before = datetime(2026, 10, 25, 21, 59, 59,
                          tzinfo=timezone.utc).astimezone(lu)
        after = datetime(2026, 10, 25, 22, 0, 0,
                         tzinfo=timezone.utc).astimezone(lu)
        assert before.date().isoformat() == "2026-10-25"
        assert after.date().isoformat() == "2026-10-26"   # flips at midnight CAT

    def test_lusaka_today_utc_derived(self):
        from src.util import lusaka_today
        from zoneinfo import ZoneInfo
        expect = datetime.now(timezone.utc).astimezone(
            ZoneInfo("Africa/Lusaka")).date()
        assert lusaka_today() == expect

    def test_today_report_rel_and_latest_json(self, tmp_path, monkeypatch):
        from zoneinfo import ZoneInfo
        import update_site_index as usi
        monkeypatch.chdir(tmp_path)
        (tmp_path / "reports").mkdir(); (tmp_path / "data").mkdir()
        (tmp_path / "reports" / "2026-10-25_report.html").write_text("x")
        day = datetime.now(timezone.utc).astimezone(
            ZoneInfo("Africa/Lusaka")).date().isoformat()
        assert usi.today_report_rel("2026-10-25") == "reports/2026-10-25_report.html"
        # an old report is found by its OWN date, never relabelled as today
        url, rep_date = usi.latest_report_rel()
        assert rep_date == "2026-10-25" and url.endswith("2026-10-25_report.html")
        payload = usi.build_latest_json()
        assert payload["report_date"] == "2026-10-25"
        assert (tmp_path / "data" / "latest.json").exists()
        assert payload["lusaka_today_at_build"] == day    # clock-derived, not filename
        # 2026-10-26 report absent -> tomorrow the index must fall back, not lie
        assert usi.today_report_rel("2026-10-26") is None

    def test_index_page_dynamic_tokens(self, tmp_path, monkeypatch):
        import update_site_index as usi
        monkeypatch.chdir(tmp_path)
        (tmp_path / "reports").mkdir(); (tmp_path / "data").mkdir()
        (tmp_path / "reports" / "2026-10-25_report.html").write_text("x")
        usi.main()
        page = (tmp_path / "index.html").read_text()
        for token in ("timeZone:'Africa/Lusaka'", "method:'HEAD'",
                      "data/latest.json", "4 calendar days, Africa/Lusaka CAT",
                      "location.reload", "?v=", "midnight"):
            assert token in page, token
        # the iframe starts EMPTY — JS decides the URL from the live clock;
        # the old report may appear only inside the (labelled) archive list
        assert '<iframe id="rep" title="daily report" onload="sizeFrame()"></iframe>' in page
        assert 'src="reports/' not in page.split("Report archive")[0]

    def test_dated_report_live_tokens(self):
        from src.report import render
        run = {"date": "2026-10-25", "generated_at": "25 Oct, 06:30 CAT",
               "generated_utc": "2026-10-25 04:30 UTC", "threshold": .05,
               "bankroll": 1000.0, "n_fixtures": 0, "model_version": "m",
               "window_dates": ["2026-10-25"], "counts": {}, "notices": []}
        page = render(run, [], [], [])
        for token in ("timeZone:'Africa/Lusaka'", "lvclock", "lage",
                      "This is the report for", "it is now", "in Zambia",
                      "location.reload", "↻ Refresh"):
            assert token in page, token
        assert "2026-10-25T04:30:00Z" in page           # gen ts fed to JS clock

    def test_calendar_live_today_and_stale_banner(self):
        from build_calendar import HTML_PAGE
        for token in ("lusakaNow", "timeZone:'Africa/Lusaka'",
                      "the Zambia date has moved on", "location.reload",
                      "stale", "calendar_data.json?v="):
            assert token in HTML_PAGE, token

    def test_window_rolls_with_today(self):
        from build_calendar import build_data
        from src.util import lusaka_today
        data = build_data()
        day = lusaka_today().isoformat()
        assert data["lusaka_today"] == day
        assert data["window4"][0] == day
        assert data["window4"][3] == (lusaka_today() + timedelta(days=3)).isoformat()
