"""Synthetic tests for the additive tracked-league totals analysis only."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["SVB_DB_PATH"] = str(ROOT / "data" / "test_analysis_unit.db")

from src import db  # noqa: E402
from src import analysis_core as ac  # noqa: E402
from src.analysis_report import build_html  # noqa: E402
from src.util import lusaka_today  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    p = Path(os.environ["SVB_DB_PATH"])
    p.unlink(missing_ok=True)
    yield
    p.unlink(missing_ok=True)


def history() -> pd.DataFrame:
    rows = []
    teams = ["Alpha", "Beta", "Gamma", "Delta"]
    d = pd.Timestamp("2025-01-01")
    for i in range(96):
        h, a = teams[i % 4], teams[(i + 1) % 4]
        rows.append({"date": d + pd.Timedelta(days=i * 2), "home": h, "away": a,
                     "hg": (i * 3) % 4, "ag": (i * 5 + 1) % 3,
                     "ftr": "H", "comp": "Demo League"})
    return pd.DataFrame(rows)


def test_totals_are_ordered_and_in_unit_interval():
    p = ac.goal_probabilities(1.8, 1.35, -0.05, 10)
    assert 0 < p["OU55"] < p["OU25"] < 1


def test_snapshots_are_immutable_but_can_be_superseded():
    now = datetime.now(timezone.utc)
    fixture = {"mkey": "2099-01-01|alpha|beta", "fid": "test:1", "date": "2099-01-01",
               "kickoff_utc": "2099-01-01T18:00:00Z", "league": "Demo League", "home": "Alpha", "away": "Beta",
               "lambda_home": 1.6, "lambda_away": 1.2, "verification": 75.0, "confidence": "moderate",
               "evidence": {}, "notes": []}
    market = {"market": "OU25", "line": 2.5, "p_over": 0.55, "fair_over": 1.818,
              "label": "Moderate statistical candidate", "value": {"available": False}}
    with db.conn() as c:
        ac.ensure_schema(c)
        assert ac._snapshot(c, "r1", fixture, market, now) == "inserted"
        row = c.execute("SELECT id FROM analysis_snapshots").fetchone()
        with pytest.raises(Exception):
            c.execute("UPDATE analysis_snapshots SET p_over=.99 WHERE id=?", (row["id"],))
        with pytest.raises(Exception):
            c.execute("DELETE FROM analysis_snapshots WHERE id=?", (row["id"],))
        assert ac._snapshot(c, "r2", fixture, market, now + timedelta(hours=25)) == "revised"
        assert c.execute("SELECT COUNT(*) n FROM analysis_snapshots").fetchone()["n"] == 2


def test_site_only_run_and_html_are_honest(monkeypatch):
    h = history()
    monkeypatch.setattr(ac.historical, "load_results", lambda *args, **kwargs: h)
    tomorrow = lusaka_today() + timedelta(days=1)
    kickoff = datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc).replace(hour=17)
    cfg = {"timezone": "Africa/Lusaka", "report_window_days": 4, "odds_stale_hours": 36,
           "model": {"history_seasons": 2, "decay_per_day": .004, "max_goals": 10, "form_last_n": 5},
           "leagues": [{"name": "Demo League", "fd": "DEMO", "enabled": True}]}
    with db.conn() as c:
        db.upsert_fixture(c, {"fid": "test:fx", "mkey": f"{tomorrow}|alpha|beta", "date": tomorrow.isoformat(),
                              "time_utc": "17:00", "league": "Demo League", "home": "Alpha", "away": "Beta",
                              "provider": "test", "provider_event_id": "1", "status": "scheduled",
                              "country": "Testland", "kickoff_utc": kickoff.strftime("%Y-%m-%dT%H:%M:%SZ")})
        db.set_fixture_verification(c, "test:fx", True, 90, [])
        db.log_quote(c, "quote-run", f"{tomorrow}|alpha|beta", tomorrow.isoformat(), "Demo League", "Alpha", "Beta",
                     "synthetic", "Book A", "OU25", "over", 1.95)
        db.log_quote(c, "quote-run", f"{tomorrow}|alpha|beta", tomorrow.isoformat(), "Demo League", "Alpha", "Beta",
                     "synthetic", "Book A", "OU25", "under", 1.95)
        result = ac.run(c, cfg, now=datetime.now(timezone.utc))
    assert result["counts"]["fixtures_discovered"] == 1
    assert result["counts"]["analysed"] == 1
    a = result["analyses"][0]
    assert {x["market"] for x in a["markets"]} == {"OU25", "OU55"}
    assert a["markets"][0]["value"]["available"] is True
    assert a["markets"][1]["value"]["available"] is False
    page = build_html(result)
    assert "Tracked-league totals analysis" in page
    assert "worldwide 100+ match scan" in page
    assert "No value or EV is shown unless a real current bookmaker price was recorded" in page
