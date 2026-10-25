#!/usr/bin/env python3
"""Build the match calendar (calendar.html + data/calendar_data.json).

The DATE problem, fixed at the root: the page never trusts the client clock or
a filename. The build stamps `lusaka_today` from the ACTUAL UTC time at build
moment (server-side clock), converts to Africa/Lusaka, and ships it inside the
JSON. The calendar anchors all navigation on that server date. A fresh status
workflow (every 6h, free endpoints only) rebuilds this file, and the page
re-fetches the JSON with a cache-buster so a stale Pages cache cannot leave
yesterday's date on screen. Freshness is displayed and colour-coded.

Data: tracked fixtures DB (real provider data) + football-data.co.uk results.
Past dates show results when the provider has published them; otherwise the
fixture stays visible marked "awaiting official result". Nothing invented.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src import db
from src.historical import load_results
from src.settings import current_season_code, load_config
from src.report import CSS

TZ = ZoneInfo("Africa/Lusaka")
DATA_JSON = Path("data/calendar_data.json")
PAGE = Path("calendar.html")

# how far the calendar serves (past anchored on tracking start, future anchored
# on what providers have published)
PAST_DAYS = 90
FUTURE_DAYS = 10


def esc_js(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def build_data(today: date | None = None) -> dict:
    """Assemble the calendar payload. `today` is injectable for tests."""
    cfg = load_config()
    now_utc = datetime.now(timezone.utc)
    lusaka_today = (today or now_utc.astimezone(TZ).date())
    min_d = lusaka_today - timedelta(days=PAST_DAYS)
    max_d = lusaka_today + timedelta(days=FUTURE_DAYS)

    fd_by_name = {l["name"]: l["fd"] for l in cfg["leagues"] if l.get("fd")}

    with db.conn() as c:
        fx_rows = c.execute(
            """SELECT fid, mkey, date, time_utc, kickoff_utc, league, country,
                      home, away, status, verified, completeness,
                      prediction_status, prediction_reason
                 FROM fixtures WHERE date>=? AND date<=? ORDER BY date, time_utc""",
            (min_d.isoformat(), max_d.isoformat())).fetchall()
        pred_map = {r["fixture_id"]: dict(r) for r in c.execute(
            """SELECT fixture_id,
                      MAX(CASE WHEN market='OU25' AND selection='over' THEN p_model END) o25,
                      MAX(CASE WHEN market='OU35' AND selection='over' THEN p_model END) o35,
                      MAX(confidence) conf, MAX(completeness) comp
                 FROM predictions_v2 WHERE status='active' OR status IS NULL
                 GROUP BY fixture_id""").fetchall()}
        pick_map = defaultdict(list)
        for r in c.execute("SELECT fixture_id, market, selection, odds, book, status "
                           "FROM picks WHERE fixture_id IS NOT NULL").fetchall():
            pick_map[r["fixture_id"]].append({"market": r["market"], "sel": r["selection"],
                                              "odds": r["odds"], "book": r["book"],
                                              "status": r["status"]})

    # official results merge (football-data) for past dates
    wanted = defaultdict(set)
    for f in fx_rows:
        fd = fd_by_name.get(f["league"])
        if fd and f["date"] < lusaka_today.isoformat():
            wanted[fd].add(f["date"])
    results: dict[tuple, tuple[int, int]] = {}
    for fd, dts in wanted.items():
        seasons = sorted({current_season_code(date.fromisoformat(d)) for d in dts} |
                         {current_season_code()})
        hist = load_results(fd, seasons)
        for r in hist.itertuples():
            results[(r.date.date().isoformat(), r.home, r.away)] = (int(r.hg), int(r.ag))

    days: dict[str, list] = defaultdict(list)
    for f in fx_rows:
        # local day from the ACTUAL kickoff timestamp (fallback: provider date)
        local_day = f["date"]
        kickoff_local = ""
        if f["kickoff_utc"]:
            try:
                dt = datetime.fromisoformat(f["kickoff_utc"].replace("Z", "+00:00"))
                local_day = dt.astimezone(TZ).date().isoformat()
                kickoff_local = dt.astimezone(TZ).strftime("%H:%M")
            except ValueError:
                pass
        res = results.get((f["date"], f["home"], f["away"])) or \
            results.get((local_day, f["home"], f["away"]))
        pm = pred_map.get(f["fid"], {})
        if res:
            status, score = "completed", f"{res[0]}-{res[1]}"
        else:
            score = None
            status = f["status"]
            if f["date"] < lusaka_today.isoformat() and status == "scheduled":
                status = "awaiting-result"      # honest: finished, result not yet published
        days[local_day].append({
            "fid": f["fid"], "league": f["league"], "country": f["country"] or "",
            "home": f["home"].title(), "away": f["away"].title(),
            "kickoff": kickoff_local, "status": status, "score": score,
            "verified": bool(f["verified"]),
            "completeness": f["completeness"],
            "pub": f["prediction_status"] or "",
            "note": (f["prediction_reason"] or "")[:200],
            "o25": pm.get("o25"), "o35": pm.get("o35"),
            "conf": pm.get("conf"), "picks": pick_map.get(f["fid"], []),
        })
    for d in days:
        days[d].sort(key=lambda x: (x["kickoff"] or "99:99", x["league"], x["home"]))

    payload = {
        "generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_cat": now_utc.astimezone(TZ).strftime("%d %b %Y, %H:%M CAT"),
        "lusaka_today": lusaka_today.isoformat(),
        "min_date": min_d.isoformat(), "max_date": max_d.isoformat(),
        "window4": [(lusaka_today + timedelta(days=i)).isoformat() for i in range(4)],
        "days": {d: v for d, v in days.items()},
    }
    return payload


HTML_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Match calendar — soccer-value-bets</title><style>{CSS}
.ctrl{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0}
.livecard{background:#ffebee!important;border-color:#ef5350!important}
.fresh-ok{color:#2e7d32;font-weight:700}.fresh-warn{color:#ef6c00;font-weight:700}
.fresh-bad{color:#c62828;font-weight:700}
.st{font-size:11px;border-radius:4px;padding:1px 6px;font-weight:700}
.st-scheduled{background:#e3f2fd;color:#1565c0}.st-live{background:#ffebee;color:#c62828}
.st-completed{background:#e8f5e9;color:#2e7d32}.st-awaiting-result{background:#fff8e1;color:#ef6c00}
.st-removed,.st-postponed,.st-cancelled,.st-abandoned{background:#efebe9;color:#5d4037}
.st-unknown{background:#eceff1;color:#455a64}
.fr{display:flex;gap:8px;align-items:baseline;border-bottom:1px solid #ececf4;padding:6px 2px;font-size:13px}
.fr .t{width:52px;font-weight:700}.fr .lg{width:190px;color:#555;font-size:12px}
.fr .mt{flex:1}.fr .pr{text-align:right;font-size:12px;color:#555;white-space:nowrap}
h3.dhead{background:#3949ab;color:#fff;border-radius:6px;padding:4px 12px;font-size:14px;margin:16px 0 6px}
</style></head><body>
<h1>📅 Match calendar — verified fixtures & over-goals analysis</h1>
<p class="small">Server-generated <b id="genat">…</b> · <span id="fresh"></span> ·
data re-fetched automatically every 30 min and after each scheduled run ·
source: API providers + football-data results · nothing cached locally as truth.</p>
<div class="ctrl">
  <button class="btn" onclick="shift(-1)">◀ Previous day</button>
  <input type="date" id="dp" onchange="pick(this.value)">
  <button class="btn" onclick="shift(1)">Next day ▶</button>
  <button class="btn" onclick="goToday()">📍 Today</button>
  <button class="btn" style="background:#6d4c41" onclick="fourday()">4-day upcoming view</button>
  <span class="small">Range (past review):
    <input type="date" id="dfrom"> → <input type="date" id="dto">
    <button class="btn" onclick="rangeView()">Load range</button></span>
  <select id="mode" onchange="draw()" style="padding:6px">
    <option value="all">All fixtures</option>
    <option value="upcoming">Upcoming (scheduled only)</option>
    <option value="live">Live matches</option>
    <option value="results">Previous matches &amp; results</option>
  </select>
</div>
<h3 class="dhead" id="selhead">…</h3>
<div id="list"></div>
<p class="small"><a href="index.html">Daily reports archive</a> ·
<a href="results.html">Results &amp; calibration</a> · <a href="admin.html">Admin</a></p>
<script>
var DATA=null, SEL=null, SELTO=null;
function fmtDay(iso){var d=new Date(iso+"T12:00:00Z");
  return d.toLocaleDateString("en-GB",{weekday:"long",day:"numeric",month:"long",year:"numeric",timeZone:"Africa/Lusaka"});}
function freshness(){var g=new Date(DATA.generated_at_utc), age=(Date.now()-g.getTime())/36e5;
  var el=document.getElementById('fresh');document.getElementById('genat').textContent=DATA.generated_at_cat;
  var cls=age<8?'fresh-ok':age<30?'fresh-warn':'fresh-bad';
  el.innerHTML='<span class="'+cls+'">'+(age<1?'updated just now':'updated '+age.toFixed(1)+'h ago')
    +(age>30?' — STALE: the automatic refresh may be paused; data is '+age.toFixed(0)+'h old':'')+'</span>';}
function anchor(sel){SEL=sel;SELTO=sel;document.getElementById('dp').value=sel;draw();}
function isoPlus(iso,n){var d=new Date(iso+"T12:00:00Z");d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10);}
function shift(n){anchor(isoPlus(SEL,n));}
function pick(v){if(v)anchor(v);}
function goToday(){anchor(DATA.lusaka_today);}
function fourday(){SEL=DATA.lusaka_today;SELTO=DATA.window4[3];document.getElementById('dp').value=SEL;draw();}
function rangeView(){var a=document.getElementById('dfrom').value,b=document.getElementById('dto').value;
  if(a&&b){SEL=a;SELTO=b;draw();}}
function stChip(f){var s=f.status,lab=s==='awaiting-result'?'finished (awaiting official result)':s;
  return '<span class="st st-'+s+'">'+lab+'</span>';}
function row(f){var pr='';
  if(f.o25!=null){pr='O2.5 '+Math.round(f.o25*100)+'%'+(f.o35!=null?' · O3.5 '+Math.round(f.o35*100)+'%':'')
     +(f.conf?' · '+f.conf:'');}
  if(f.picks&&f.picks.length){pr+=(pr?' · ':'')+'pick: '+f.picks.map(function(p){return p.market+'/'+p.sel+' @'+p.odds+(p.status!=='pending'?' ('+p.status+')':'')}).join(', ');}
  var nb=f.note&&f.pub!=='published'?'<span class="small" style="color:#b71c1c"> — '+f.note+'</span>':'';
  return '<div class="fr"><span class="t">'+(f.kickoff||'--:--')+'</span>'
    +'<span class="lg">'+f.league+'</span>'
    +'<span class="mt"><b>'+f.home+' v '+f.away+'</b> '+stChip(f)+(f.score?' <b>'+f.score+'</b>':'')
    +(f.verified?' <span class="small" style="color:#2e7d32">✓</span>':'')+nb
    +'<br><span class="small" style="color:#8a8aa0">'+f.fid+'</span></span>'
    +'<span class="pr">'+pr+'</span></div>';}
function draw(){if(!DATA)return;freshness();
  var mode=document.getElementById('mode').value, out=[], any=false;
  var from=SEL, to=SELTO||SEL;
  document.getElementById('selhead').textContent =
    (from===to ? fmtDay(from) : fmtDay(from)+'  →  '+fmtDay(to))
    +(DATA.window4.indexOf(from)>=0&&from===to?'  (4-day window: '+DATA.window4.join(', ')+')':'');
  var d=from;
  while(d<=to){var list=(DATA.days[d]||[]).filter(function(f){
      if(mode==='upcoming')return f.status==='scheduled';
      if(mode==='live')return f.status==='live';
      if(mode==='results')return f.status==='completed'||f.status==='awaiting-result';
      return true;});
    if(list.length){any=true;out.push('<h3 class="dhead" style="background:#5c6bc0">'+fmtDay(d)+'</h3>');
      list.forEach(function(f){out.push(row(f));});}
    if(isoPlus(d,1)===d)break; d=isoPlus(d,1); if(d>DATA.max_date&&from>d)break;
    if(d>to)break;}
  document.getElementById('list').innerHTML = out.join('') ||
    '<div class="warn">No tracked fixtures for this selection. Coverage begins 24 Oct 2026 '
    +'(when tracking started). Future fixtures appear as providers publish them '
    +'(typically 2-4 days ahead for odds-covered leagues).</div>';}
function loadData(){fetch('data/calendar_data.json?v='+Date.now()).then(function(r){return r.json();})
  .then(function(j){DATA=j;if(!SEL)SEL=j.lusaka_today,SELTO=SEL,document.getElementById('dp').value=SEL;draw();})
  .catch(function(){document.getElementById('list').innerHTML='<div class="warn">calendar data not available yet - run the workflow once.</div>';});}
loadData(); setInterval(loadData, 30*60*1000);
</script></body></html>
"""


def main() -> None:
    payload = build_data()
    DATA_JSON.write_text(json.dumps(payload, indent=1))
    html = HTML_PAGE.replace("{CSS}", CSS)
    PAGE.write_text(html)
    nd = len(payload["days"])
    nf = sum(len(v) for v in payload["days"].values())
    print(f"calendar built: today={payload['lusaka_today']} "
          f"(server UTC {payload['generated_at_utc']}), {nf} fixtures over {nd} days")


if __name__ == "__main__":
    main()
