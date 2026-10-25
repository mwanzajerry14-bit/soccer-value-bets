#!/usr/bin/env python3
"""Light status refresh - runs every 6h on its OWN workflow.

Uses ONLY free endpoints that don't consume the monthly odds quota:
  - The Odds API /scores (free per provider docs): live/completed status
  - football-data.co.uk CSVs (free): final results for recently completed days
Zero /odds calls => zero quota burn. Quota usage is logged to sync_log so the
admin page can always audit it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src import db
from src.settings import api_key, load_config, load_env

TZ = ZoneInfo("Africa/Lusaka")


def main() -> None:
    cfg = load_config()
    load_env()
    from src.odds_api import OddsApi
    api = OddsApi(api_key("ODDS_API_KEY"))
    now_utc = datetime.now(timezone.utc)
    today = now_utc.astimezone(TZ).date()
    n_updated = 0

    leagues = [l for l in cfg["leagues"] if l.get("enabled") and l.get("odds_api")]
    with db.conn() as c:
        for l in leagues:
            # only leagues with tracked fixtures near 'now' need a scores poll
            near = c.execute(
                "SELECT COUNT(*) n FROM fixtures WHERE league=? AND date BETWEEN ? AND ?",
                (l["name"], (today - timedelta(days=3)).isoformat(),
                 (today + timedelta(days=1)).isoformat())).fetchone()["n"]
            if not near:
                continue
            try:
                scores = api.league_scores(l["odds_api"], days_from=3)
            except Exception as e:
                db.log_sync(c, "the-odds-api", f"scores/{l['odds_api']}", False,
                            f"refresh: {e}"[:250], None)
                continue
            db.log_sync(c, "the-odds-api", f"scores/{l['odds_api']}", True,
                        f"refresh: {len(scores)} rows (free endpoint)",
                        api.last_remaining)
            by_id = {s["id"]: s for s in scores}
            rows = c.execute(
                """SELECT fid, provider_event_id, status FROM fixtures
                    WHERE league=? AND provider='the-odds-api' AND date BETWEEN ? AND ?""",
                (l["name"], (today - timedelta(days=3)).isoformat(),
                 (today + timedelta(days=1)).isoformat())).fetchall()
            for r in rows:
                s = by_id.get(r["provider_event_id"] or "")
                if not s:
                    continue
                new = "completed" if s.get("completed") else (
                    "live" if s.get("commence_time", "9") <=
                    now_utc.strftime("%Y-%m-%dT%H:%M:%S") else r["status"])
                if new != r["status"] and r["status"] not in ("removed", "postponed",
                                                              "cancelled", "abandoned"):
                    db.set_fixture_status(c, r["fid"], new)
                    n_updated += 1
        # fixtures that kicked off a while ago and still say scheduled -> live
        # (timestamp-based; honest label, /scores may miss some competitions)
        cutoff = (now_utc - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
        stale_live = c.execute(
            """SELECT fid FROM fixtures WHERE status='scheduled'
                 AND kickoff_utc IS NOT NULL AND kickoff_utc<=?""", (cutoff,)).fetchall()
        for r in stale_live:
            db.set_fixture_status(c, r["fid"], "live")
            n_updated += 1

    # rebuild the calendar + results pages so status/results show soon after
    import subprocess, sys
    for script in ("build_results_page.py", "build_calendar.py", "build_admin_page.py"):
        r = subprocess.run([sys.executable, script], capture_output=True, text=True)
        print(script, "->", (r.stdout or r.stderr).strip()[-160:])
    print(f"refresh done: {n_updated} fixture statuses updated; "
          f"quota remaining: {api.last_remaining}")


if __name__ == "__main__":
    main()
