"""SQLite tracking: quotes, predictions, picks + v2 fixture verification,
prediction snapshots, sync logs. Schema is additive/migration-safe."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .settings import DB_PATH as _DEFAULT_DB

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

-- =========================== schema v2 ===========================
CREATE TABLE IF NOT EXISTS fixtures(
  fid TEXT PRIMARY KEY,              -- "provider:event_id" (stable across runs)
  mkey TEXT,                         -- canonical date|home|away
  date TEXT, time_utc TEXT, league TEXT, home TEXT, away TEXT,
  provider TEXT, provider_event_id TEXT,
  status TEXT DEFAULT 'unknown',     -- scheduled|live|completed|postponed|cancelled|abandoned|removed|unknown
  first_seen TEXT, last_seen TEXT,
  verified INTEGER DEFAULT 0,
  completeness REAL DEFAULT 0,
  verification_notes TEXT,
  UNIQUE(mkey, provider)
);
CREATE INDEX IF NOT EXISTS idx_fixtures_date ON fixtures(date);
CREATE TABLE IF NOT EXISTS verification_log(
  fid TEXT, ts TEXT, check_name TEXT, ok INTEGER, critical INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS sync_log(
  ts TEXT, source TEXT, endpoint TEXT, ok INTEGER, detail TEXT, quota_remaining TEXT);
CREATE TABLE IF NOT EXISTS predictions_v2(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT, fixture_id TEXT, mkey TEXT, date TEXT, league TEXT, home TEXT, away TEXT,
  market TEXT, selection TEXT, p_model REAL, p_fair REAL,
  model_version TEXT, sources TEXT, completeness REAL, confidence TEXT,
  generated_at TEXT, snapshot TEXT,
  UNIQUE(run_id, fixture_id, market, selection));
"""

# columns added after initial release (ALTER migrations, idempotent)
_COLUMN_MIGRATIONS = [("picks", "fixture_id TEXT"),
                      ("picks", "confidence TEXT"),
                      ("picks", "completeness REAL")]


def db_path() -> Path:
    return Path(os.environ.get("SVB_DB_PATH", str(_DEFAULT_DB)))


@contextmanager
def conn():
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
        for table, coldef in _COLUMN_MIGRATIONS:
            col = coldef.split()[0]
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise
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
            p_model,p_fair,p_raw,edge,ev,kelly,stake,fixture_id,confidence,completeness)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p["run_date"], p["date"], p["league"], p["mkey"], p["home"], p["away"],
         p["market"], p["selection"], p["book"], p["odds"], p["p_model"], p["p_fair"],
         p["p_raw"], p["edge"], p["ev"], p["kelly"], p["stake"],
         p.get("fixture_id"), p.get("confidence"), p.get("completeness")))
    return cur.rowcount > 0


def pending_picks(c) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM picks WHERE status='pending'").fetchall()


def settle_pick(c, pick_id: int, hg: int, ag: int, won: bool, stake: float, odds: float) -> None:
    """Grades a pick. Immutable by design: only result/status fields change,
    never the prediction inputs (p_model/odds/stake)."""
    profit = stake * (odds - 1.0) if won else -stake
    c.execute("""UPDATE picks SET status=?, hg=?, ag=?, profit=?, settled_at=? WHERE id=?""",
              ("won" if won else "lost", hg, ag, round(profit, 2),
               datetime.now().isoformat(timespec="seconds"), pick_id))


def settled(c) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM picks WHERE status IN ('won','lost')").fetchall()


def all_picks(c) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM picks ORDER BY date").fetchall()


# ----------------------- v2 helpers -----------------------

def upsert_fixture(c, f: dict) -> None:
    ts = datetime.utcnow().isoformat(timespec="seconds")
    c.execute(
        """INSERT INTO fixtures(fid,mkey,date,time_utc,league,home,away,provider,
                                provider_event_id,status,first_seen,last_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(fid) DO UPDATE SET last_seen=excluded.last_seen,
                 time_utc=excluded.time_utc, status=excluded.status""",
        (f["fid"], f["mkey"], f["date"], f.get("time_utc"), f["league"],
         f["home"], f["away"], f["provider"], f["provider_event_id"],
         f.get("status", "unknown"), ts, ts))


def set_fixture_verification(c, fid: str, verified: bool, completeness: float,
                             notes: list[str]) -> None:
    c.execute("UPDATE fixtures SET verified=?, completeness=?, verification_notes=? WHERE fid=?",
              (1 if verified else 0, completeness, "; ".join(notes), fid))


def set_fixture_status(c, fid: str, status: str) -> None:
    c.execute("UPDATE fixtures SET status=? WHERE fid=?", (status, fid))


def log_verification(c, fid: str, checks) -> None:
    ts = datetime.utcnow().isoformat(timespec="seconds")
    c.executemany("INSERT INTO verification_log VALUES (?,?,?,?,?,?)",
                  [(fid, ts, ch.name, 1 if ch.ok else 0, 1 if ch.critical else 0, ch.detail)
                   for ch in checks])


def log_sync(c, source: str, endpoint: str, ok: bool, detail: str = "",
             quota: str | None = None) -> None:
    c.execute("INSERT INTO sync_log VALUES (?,?,?,?,?,?)",
              (datetime.utcnow().isoformat(timespec="seconds"), source, endpoint,
               1 if ok else 0, detail[:400], quota))


def log_prediction_v2(c, p: dict, snapshot: dict) -> None:
    import json
    c.execute(
        """INSERT OR REPLACE INTO predictions_v2
           (run_id,fixture_id,mkey,date,league,home,away,market,selection,p_model,p_fair,
            model_version,sources,completeness,confidence,generated_at,snapshot)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p["run_id"], p["fixture_id"], p["mkey"], p["date"], p["league"], p["home"], p["away"],
         p["market"], p["selection"], p["p_model"], p["p_fair"], p["model_version"],
         json.dumps(p["sources"]), p["completeness"], p["confidence"], p["generated_at"],
         json.dumps(snapshot)))
