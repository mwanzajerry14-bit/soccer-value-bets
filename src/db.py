"""SQLite tracking: every quote, prediction and pick is logged."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .settings import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, run_ts TEXT, n_fixtures INT, n_picks INT, meta TEXT);
CREATE TABLE IF NOT EXISTS quotes(
  run_id TEXT, mkey TEXT, date TEXT, league TEXT, home TEXT, away TEXT,
  source TEXT, book TEXT, market TEXT, selection TEXT, odds REAL,
  captured_at TEXT);
CREATE INDEX IF NOT EXISTS idx_quotes_mkey ON quotes(mkey);
CREATE TABLE IF NOT EXISTS predictions(
  run_id TEXT, mkey TEXT, date TEXT, league TEXT, home TEXT, away TEXT,
  market TEXT, selection TEXT, p_model REAL, p_fair REAL, best_odds REAL, book TEXT);
CREATE INDEX IF NOT EXISTS idx_pred_mkey ON predictions(mkey);
CREATE TABLE IF NOT EXISTS picks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date TEXT, date TEXT, league TEXT, mkey TEXT, home TEXT, away TEXT,
  market TEXT, selection TEXT, book TEXT, odds REAL,
  p_model REAL, p_fair REAL, p_raw REAL, edge REAL, ev REAL,
  kelly REAL, stake REAL,
  status TEXT DEFAULT 'pending',
  hg INTEGER, ag INTEGER, profit REAL, settled_at TEXT,
  UNIQUE(run_date, mkey, market, selection));
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
        yield c
        c.commit()
    finally:
        c.close()


def log_run(c, run_id: str, n_fixtures: int, n_picks: int, meta: str = "") -> None:
    c.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
              (run_id, datetime.now().isoformat(timespec="seconds"), n_fixtures, n_picks, meta))


def log_quote(c, run_id, mkey, date, league, home, away, source, book, market, selection, odds):
    c.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (run_id, mkey, date, league, home, away, source, book, market, selection, odds,
               datetime.now().isoformat(timespec="seconds")))


def log_prediction(c, run_id, mkey, date, league, home, away, market, selection,
                   p_model, p_fair, best_odds, book):
    c.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (run_id, mkey, date, league, home, away, market, selection,
               p_model, p_fair, best_odds, book))


def add_pick(c, p: dict) -> bool:
    cur = c.execute(
        """INSERT OR IGNORE INTO picks
           (run_date,date,league,mkey,home,away,market,selection,book,odds,
            p_model,p_fair,p_raw,edge,ev,kelly,stake)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p["run_date"], p["date"], p["league"], p["mkey"], p["home"], p["away"],
         p["market"], p["selection"], p["book"], p["odds"], p["p_model"], p["p_fair"],
         p["p_raw"], p["edge"], p["ev"], p["kelly"], p["stake"]))
    return cur.rowcount > 0


def pending_picks(c) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM picks WHERE status='pending'").fetchall()


def settle_pick(c, pick_id: int, hg: int, ag: int, won: bool, stake: float, odds: float) -> None:
    profit = stake * (odds - 1.0) if won else -stake
    c.execute("""UPDATE picks SET status=?, hg=?, ag=?, profit=?, settled_at=? WHERE id=?""",
              ("won" if won else "lost", hg, ag, round(profit, 2),
               datetime.now().isoformat(timespec="seconds"), pick_id))


def settled(c) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM picks WHERE status IN ('won','lost')").fetchall()


def all_picks(c) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM picks ORDER BY date").fetchall()
