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
-- API-Football subscription coverage cache (asked from the provider itself,
-- refreshed every 24h, NOT assumed - task B "discover available seasons")
CREATE TABLE IF NOT EXISTS af_coverage(
  league_id INTEGER PRIMARY KEY, fetched_at TEXT, seasons TEXT,
  coverage TEXT, name TEXT, country TEXT);
"""

# columns added after initial release (ALTER migrations, idempotent)
_COLUMN_MIGRATIONS = [("picks", "fixture_id TEXT"),
                      ("picks", "confidence TEXT"),
                      ("picks", "completeness REAL"),
                      ("fixtures", "country TEXT"),
                      ("fixtures", "kickoff_utc TEXT"),        # full ISO timestamp
                      ("fixtures", "prediction_status TEXT"),   # published|withheld|failed|excluded
                      ("fixtures", "prediction_reason TEXT"),
                      # -- prediction revision tracking (task E) --
                      ("predictions_v2", "revision_of INTEGER"),
                      ("predictions_v2", "status TEXT DEFAULT 'active'"),
                      ("predictions_v2", "revision_reason TEXT")]


def db_path() -> Path:
    return Path(os.environ.get("SVB_DB_PATH", str(_DEFAULT_DB)))


def _migrate_pred_v2_identity(c) -> None:
    """Task E: stable prediction identity = (fixture_id, market, selection,
    model_version). Exactly ONE active row per identity; older duplicates are
    marked superseded (history preserved, never deleted/overwritten)."""
    dupes = c.execute(
        """SELECT fixture_id, market, selection, model_version, COUNT(*) n
             FROM predictions_v2
            WHERE status IS NULL OR status='active'
            GROUP BY 1,2,3,4 HAVING n>1""").fetchall()
    for d in dupes:
        rows = c.execute(
            """SELECT id FROM predictions_v2
                WHERE fixture_id=? AND market=? AND selection=? AND model_version=?
                  AND (status IS NULL OR status='active') ORDER BY id""",
            (d["fixture_id"], d["market"], d["selection"], d["model_version"])).fetchall()
        keep = rows[0]["id"]
        for r in rows[1:]:
            c.execute("UPDATE predictions_v2 SET status='superseded', revision_of=?, "
                      "revision_reason=COALESCE(revision_reason,'identity dedupe') WHERE id=?",
                      (keep, r["id"]))
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_pred_v2_active
                   ON predictions_v2(fixture_id, market, selection, model_version)
                   WHERE status IS NULL OR status='active'""")
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_picks_pending
                   ON picks(mkey, market, selection) WHERE status='pending'""")


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
        _migrate_pred_v2_identity(c)
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
    """Identity = (mkey, market, selection) while pending: a pick published by
    an earlier run is REUSED, never duplicated (task E). The partial unique
    index idx_picks_pending enforces it at the storage level too."""
    dup = c.execute(
        "SELECT id FROM picks WHERE mkey=? AND market=? AND selection=? AND status='pending'",
        (p["mkey"], p["market"], p["selection"])).fetchone()
    if dup:
        return False
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
                                provider_event_id,status,first_seen,last_seen,
                                country,kickoff_utc)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(fid) DO UPDATE SET last_seen=excluded.last_seen,
                 time_utc=excluded.time_utc, status=excluded.status,
                 country=COALESCE(excluded.country, fixtures.country),
                 kickoff_utc=COALESCE(excluded.kickoff_utc, fixtures.kickoff_utc)""",
        (f["fid"], f["mkey"], f["date"], f.get("time_utc"), f["league"],
         f["home"], f["away"], f["provider"], f["provider_event_id"],
         f.get("status", "unknown"), ts, ts, f.get("country"), f.get("kickoff_utc")))


def set_fixture_prediction(c, fid: str, status: str, reason: str = "") -> None:
    """Record the publication decision (published / withheld / failed /
    excluded-status / no-fixture-data) with its human-readable reason."""
    c.execute("UPDATE fixtures SET prediction_status=?, prediction_reason=? WHERE fid=?",
              (status, reason[:400], fid))


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


def prediction_v2_get_active(c, fixture_id: str, market: str, selection: str,
                             model_version: str):
    """The single active snapshot for this identity (or None)."""
    return c.execute(
        """SELECT * FROM predictions_v2
            WHERE fixture_id=? AND market=? AND selection=? AND model_version=?
              AND (status IS NULL OR status='active')""",
        (fixture_id, market, selection, model_version)).fetchone()


def prediction_v2_insert(c, p: dict, snapshot: dict, recalc_reason: str | None = None):
    """Task E: reuse-or-revise.

    - If an active row exists for (fixture, market, selection, version) and no
      deliberate recalc was requested: REUSE it (returns its id) and never
      touch its stored probability/timestamp - the pre-match view is preserved.
    - If recalc_reason is given: supersede the old row and insert a NEW row
      carrying revision_of + revision_reason. Both rows survive (audit trail).
    """
    import json
    existing = prediction_v2_get_active(c, p["fixture_id"], p["market"],
                                        p["selection"], p["model_version"])
    if existing is not None and not recalc_reason:
        return existing["id"], existing["generated_at"], True
    if existing is not None:
        c.execute("UPDATE predictions_v2 SET status='superseded', revision_reason=? "
                  "WHERE id=?", (recalc_reason, existing["id"]))
    cur = c.execute(
        """INSERT INTO predictions_v2
           (run_id,fixture_id,mkey,date,league,home,away,market,selection,p_model,p_fair,
            model_version,sources,completeness,confidence,generated_at,snapshot,
            revision_of,status,revision_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p["run_id"], p["fixture_id"], p["mkey"], p["date"], p["league"], p["home"],
         p["away"], p["market"], p["selection"], p["p_model"], p["p_fair"],
         p["model_version"], json.dumps(p["sources"]), p["completeness"],
         p["confidence"], p["generated_at"], json.dumps(snapshot),
         existing["id"] if existing is not None else None, "active", recalc_reason))
    return cur.lastrowid, p["generated_at"], False


def get_af_coverage(c, league_id: int, max_age_hours: int = 24):
    import json
    row = c.execute("SELECT * FROM af_coverage WHERE league_id=?", (league_id,)).fetchone()
    if not row:
        return None
    try:
        age_h = (datetime.utcnow() - datetime.fromisoformat(row["fetched_at"])).total_seconds() / 3600
    except ValueError:
        return None
    if age_h > max_age_hours:
        return None
    return {"league_id": league_id, "seasons": json.loads(row["seasons"]),
            "coverage": json.loads(row["coverage"]), "name": row["name"],
            "country": row["country"], "fetched_at": row["fetched_at"]}


def set_af_coverage(c, cov: dict) -> None:
    import json
    c.execute("INSERT OR REPLACE INTO af_coverage VALUES (?,?,?,?,?,?)",
              (cov["league_id"], datetime.utcnow().isoformat(timespec="seconds"),
               json.dumps(cov.get("seasons", [])), json.dumps(cov.get("coverage", {})),
               cov.get("name", ""), cov.get("country", "")))
