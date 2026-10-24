#!/usr/bin/env python3
"""Build admin.html - static admin/ops dashboard (read-only analytics).

NOTE on admin actions (task section 8): GitHub Pages is static hosting, so an
in-page "trigger refresh / disable predictions" button cannot be securely
authenticated here. Administrative actions are therefore implemented where
strong auth already exists: inside GitHub -
  * trigger refresh  -> Actions -> "Daily value-bet report" -> Run workflow
  * suspend picks    -> same, with the suspend_predictions input checked
  * inspect evidence -> this page + verification_log rows in data/value_bets.db
That use of GitHub's authenticated UI is the security boundary; this page
only reports state.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src import db
from src.report import CSS

def esc(x) -> str:
    import html as H
    return H.escape(str(x))


def card(label, val):
    return f'<div class="card">{esc(label)}<b>{esc(val)}</b></div>'


def main() -> None:
    with db.conn() as c:
        fx = c.execute("SELECT status, verified, COUNT(*) n FROM fixtures GROUP BY status, verified").fetchall()
        fx_total = sum(r["n"] for r in fx)
        by_status: dict[str, int] = {}
        verified_n = rejected = 0
        for r in fx:
            by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
            if r["verified"]:
                verified_n += r["n"]
            if r["status"] in ("removed", "cancelled", "postponed", "abandoned"):
                rejected += r["n"]
        last_sync = c.execute("SELECT ts,source,endpoint,ok,quota_remaining FROM sync_log "
                              "ORDER BY ts DESC LIMIT 1").fetchone()
        syncs = c.execute("SELECT ts,source,endpoint,ok,detail,quota_remaining FROM sync_log "
                          "ORDER BY ts DESC LIMIT 25").fetchall()
        preds = c.execute("SELECT confidence, COUNT(*) n FROM predictions_v2 GROUP BY confidence").fetchall()
        pv2_total = sum(r["n"] for r in preds)
        psets = {r["confidence"]: r["n"] for r in preds}
        runs = c.execute("SELECT COUNT(*) n FROM runs").fetchone()["n"]
        picks = c.execute("SELECT status, COUNT(*) n FROM picks GROUP BY status").fetchall()
        perf_m = c.execute(
            """SELECT market, COUNT(*) n, SUM(status='won') wins, ROUND(100.0*SUM(status='won')/COUNT(*),1) hit,
                      ROUND(SUM(profit),2) profit, ROUND(SUM(stake),2) turnover
                 FROM picks WHERE status IN ('won','lost') GROUP BY market""").fetchall()
        perf_l = c.execute(
            """SELECT league, COUNT(*) n, SUM(status='won') wins, ROUND(SUM(profit),2) profit,
                      ROUND(SUM(stake),2) turnover
                 FROM picks WHERE status IN ('won','lost') GROUP BY league""").fetchall()
        stale = c.execute(
            """SELECT fid, date, home, away FROM fixtures
                 WHERE status='scheduled' AND last_seen < ?
                 AND date >= date('now') ORDER BY date""",
            (datetime.utcnow().isoformat(timespec="seconds"),)).fetchall()

    out = [f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Admin</title><style>{CSS}</style></head><body>
<h1>🛠 Admin dashboard</h1>
<p class="small">Generated {datetime.utcnow().isoformat(timespec='seconds')} UTC · read-only analytics.
Admin actions (refresh / suspend picks) are done in GitHub &rarr; Actions with your logged-in account -
that is the authentication boundary on this static site.</p>
<div class="summary">
{card("Fixtures tracked", fx_total)}
{card("Verified", verified_n)}
{card("Awaiting/rejected review", rejected)}
{card("Scheduled", by_status.get("scheduled", 0))}
{card("Live", by_status.get("live", 0))}
{card("Completed", by_status.get("completed", 0))}
{card("Removed-from-feed", by_status.get("removed", 0))}
{card("Runs logged", runs)}
{card("Predictions (v2 snapshots)", pv2_total)}
{card("Confidence high/mod/low", f"{psets.get('high',0)}/{psets.get('moderate',0)}/{psets.get('low',0)}")}
</div>"""]
    if last_sync:
        out.append(f"<p class='small'>Last successful sync: <b>{esc(last_sync['ts'])} UTC</b> "
                   f"({esc(last_sync['source'])} {esc(last_sync['endpoint'])})"
                   + (f" · quota remaining: {esc(last_sync['quota_remaining'])}"
                      if last_sync["quota_remaining"] else "") + "</p>")

    # picks status
    pstat = ", ".join(f"{r['status']}: {r['n']}" for r in picks) or "none yet"
    out.append(f"<h2>Picks</h2><p class='small'>{esc(pstat)}</p>")

    for title, rows, cols in (
        ("Prediction performance by market (settled only)", perf_m,
         ["Market", "n", "wins", "hit %", "turnover", "profit"]),
        ("By league (settled only)", perf_l,
         ["League", "n", "wins", "turnover", "profit"])):
        out.append(f"<h2>{esc(title)}</h2>")
        if rows:
            out.append("<table><tr>" + "".join(f"<th>{c2}</th>" for c2 in cols) + "</tr>")
            for r in rows:
                if len(cols) == 6:
                    out.append(f"<tr><td>{esc(r['market'])}</td><td class='r'>{r['n']}</td>"
                               f"<td class='r'>{r['wins']}</td><td class='r'>{r['hit']}%</td>"
                               f"<td class='r'>{r['turnover']}</td><td class='r'>{r['profit']:+.2f}</td></tr>")
                else:
                    roi = (r["profit"] / r["turnover"] * 100) if r["turnover"] else 0
                    out.append(f"<tr><td>{esc(r['league'])}</td><td class='r'>{r['n']}</td>"
                               f"<td class='r'>{r['wins']}</td><td class='r'>{r['turnover']}</td>"
                               f"<td class='r'>{r['profit']:+.2f} ({roi:+.1f}%)</td></tr>")
            out.append("</table>")
        else:
            out.append("<p class='small'>no settled bets yet.</p>")

    out.append("<h2>Stale / vanished data warnings</h2>")
    if stale:
        out.append("<table><tr><th>Fixture</th><th>Date</th></tr>")
        for r in stale[:50]:
            out.append(f"<tr><td>{esc(r['home'])} v {esc(r['away'])} <span class='small'>({esc(r['fid'])})</span></td>"
                       f"<td>{esc(r['date'])}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class='small'>none.</p>")

    out.append("<h2>Recent API synchronization log</h2><table><tr><th>ts (UTC)</th><th>source</th>"
               "<th>endpoint</th><th>ok</th><th>detail</th><th>quota left</th></tr>")
    for r in syncs:
        tick = "✅" if r["ok"] else "❌"
        out.append(f"<tr><td>{esc(r['ts'])}</td><td>{esc(r['source'])}</td><td>{esc(r['endpoint'])}</td>"
                   f"<td>{tick}</td><td class='small'>{esc(r['detail'])}</td>"
                   f"<td>{esc(r['quota_remaining'] or '')}</td></tr>")
    out.append("</table>")
    out.append("</body></html>")
    Path("admin.html").write_text("".join(out))
    print(f"admin.html built: {fx_total} fixtures, {verified_n} verified, {rejected} review")


if __name__ == "__main__":
    main()
