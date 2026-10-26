#!/usr/bin/env python3
"""Build the site HOME page (repo-root index.html) - the dynamic "Today" page.

Root fix for the stale-date problem: the page no longer redirects to a dated
report file. It computes the CURRENT date in Africa/Lusaka every time it loads
(in the visitor's browser, independently of any report's generation time),
and loads the matching report (reports/<lusaka_today>_report.html) with a
cache-buster. If that day's full report does not exist yet (the daily run is at
06:30 CAT), it openly says so (freshness warning) and embeds the LATEST one
found in data/latest.json - never pretending an old report is today's.

Also writes data/latest.json (pointer the page + tests can check):
  {"report_date", "url", "generated_at_utc"}
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPORTS = Path("reports")
LATEST_JSON = Path("data/latest.json")
# real dated reports only — demo/scratch files must never become "latest"
DATE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_report\.html$")


def today_report_rel(lusaka_today: str) -> str | None:
    """Relative path of the report generated for `lusaka_today`, if present."""
    p = REPORTS / f"{lusaka_today}_report.html"
    return f"reports/{p.name}" if p.exists() else None


def latest_report_rel() -> tuple[str | None, str | None]:
    files = sorted(f for f in REPORTS.glob("2*_report.html")
                   if DATE_NAME.match(f.name))
    if not files:
        return None, None
    f = files[-1]
    return f"reports/{f.name}", f.name.replace("_report.html", "")


def build_latest_json() -> dict:
    tz_lu = datetime.now(timezone.utc).astimezone(ZoneInfo("Africa/Lusaka")).date().isoformat()
    url, rep_date = latest_report_rel()
    payload = {
        "report_date": rep_date,
        "url": url,
        "lusaka_today_at_build": tz_lu,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    LATEST_JSON.write_text(json.dumps(payload, indent=1))
    return payload


def main() -> None:
    build_latest_json()
    files = sorted(f for f in REPORTS.glob("2*_report.html")
                   if DATE_NAME.match(f.name))
    rows = []
    for f in reversed(files[-40:]):
        day = f.name.replace("_report.html", "")
        xl = REPORTS / f"{day}_report.xlsx"
        cs = REPORTS / f"{day}_picks.csv"
        links = f'<a href="reports/{f.name}">HTML report</a>'
        if xl.exists():
            links += f' · <a href="reports/{xl.name}">Excel</a>'
        if cs.exists():
            links += f' · <a href="reports/{cs.name}">CSV</a>'
        rows.append(f"<li><b>{day}</b>: {links}</li>")
    archive = "".join(rows) or "<li>no reports yet</li>"

    page = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-store">
<title>Soccer value bets — today's report</title><style>
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1a1a2e}
.card{background:#f4f6ff;border:1px solid #dfe3f5;border-radius:8px;padding:10px 16px;font-size:13px;display:inline-block;margin:4px}
.btn{display:inline-block;background:#3949ab;color:#fff;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:13px;border:0;cursor:pointer}
.warn{background:#fff8e1;border:1px solid #ffe082;padding:10px 14px;border-radius:6px;font-size:13px;margin:12px 0}
.stale{background:#ffebee;border:1px solid #ef5350;padding:10px 14px;border-radius:6px;font-size:13px;margin:12px 0}
.small{font-size:12px;color:#555}
.summary{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0}
iframe{width:100%;border:0}h1{font-size:22px}
details{margin:14px 0}
</style></head><body>
<h1>⚽ Soccer Value Bets — today's report</h1>
<div class="summary">
  <div class="card">Now in Zambia (live clock)<b id="clock">…</b></div>
  <div class="card">Report date<b id="rdate">…</b></div>
  <div class="card">Fixture window (rolling)<b id="win" style="font-size:13px">…</b></div>
  <div class="card">Latest data update<b id="lastupd" style="font-size:13px">…</b></div>
</div>
<p><button class="btn" onclick="hardReload()">↻ Refresh</button>
&nbsp;<a class="btn" href="calendar.html" style="background:#2e7d32">📅 Match calendar</a>
&nbsp;<a class="btn" href="results.html" style="background:#00695c">✅ Results &amp; calibration</a>
&nbsp;<a class="btn" href="admin.html" style="background:#6d4c41">🛠 Admin</a></p>
<div id="freshbox"></div>
<iframe id="rep" title="daily report" onload="sizeFrame()"></iframe>
<details><summary><b>Report archive (every past day is kept)</b></summary>
<ul>__ARCHIVE__</ul></details>
<p class="small">Dates and the rolling window are computed live in <b>Africa/Lusaka (CAT, UTC+2)</b>
each time this page loads — never from a filename, a commit date, or yesterday's build.
Reports are generated daily at 06:30 CAT; fixture status &amp; results refresh every 6 hours;
both are cache-busted (<code>?v=&lt;now&gt;</code>) so Pages/CDN cannot serve you yesterday.
Probabilities are estimates, not guarantees.</p>
<script>
// ---- the single live clock: Africa/Lusaka, recomputed client-side ----
function luParts(d){
  var f=new Intl.DateTimeFormat('en-CA',{timeZone:'Africa/Lusaka',year:'numeric',
      month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  var p={}; f.formatToParts(d).forEach(function(x){p[x.type]=x.value;});
  return {date:p.year+'-'+p.month+'-'+p.day, time:p.hour+':'+p.minute+':'+p.second};
}
var openedDay=null;
function addDays(iso,n){var d=new Date(iso+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+n);
  return d.toISOString().slice(0,10);}
function fmtLong(iso){return new Date(iso+'T12:00:00Z').toLocaleDateString('en-GB',
  {weekday:'long',day:'numeric',month:'long',year:'numeric',timeZone:'Africa/Lusaka'});}

var LATEST=null;
function reportUrlFor(day){ return 'reports/'+day+'_report.html'; }

function loadReport(){
  var lu=luParts(new Date()), today=lu.date;
  openedDay=today;
  var v=Date.now();                       // cache-buster: never serve yesterday
  var url=reportUrlFor(today)+'?v='+v;
  // does TODAY's report exist? probe it (Pages returns 404 page otherwise)
  fetch(reportUrlFor(today),{method:'HEAD',cache:'no-store'}).then(function(r){
    if(r.ok){ showReport(url, today, true); }
    else { fallbackReport(today); }
  }).catch(function(){ fallbackReport(today); });
  document.getElementById('win').textContent =
      today+' → '+addDays(today,3)+' (4 calendar days, Africa/Lusaka CAT)';
  document.getElementById('rdate').textContent = today;
}
function fallbackReport(today){
  fetch('data/latest.json?v='+Date.now()).then(function(r){return r.json();}).then(function(j){
    LATEST=j;
    var url=(j.url||'calendar.html')+'?v='+Date.now();
    showReport(url, j.report_date, false, today);
  }).catch(function(){ showReport('calendar.html?v='+Date.now(), null, false, today); });
}
function showReport(url, repDate, isToday, today){
  var box=document.getElementById('freshbox');
  if(isToday){
    box.innerHTML='<div class="warn">✅ Showing <b>today\u2019s report</b> ('+fmtLong(repDate)+
      '). Generated automatically at 06:30 CAT and updated through the day.</div>';
  }else{
    box.innerHTML='<div class="stale">⚠ <b>Data-update warning:</b> the full report for '+
      fmtLong((today||repDate)||'')+' has not been generated yet (daily run at 06:30 CAT). '+
      'Below is the latest completed report'+(repDate?' ('+fmtLong(repDate)+')':'')+
      '. Fixture status &amp; results refresh every 6 h on the calendar page. '+
      'This is reported as stale - not relabelled as today.</div>';
  }
  document.getElementById('rep').src=url;
}
function sizeFrame(){
  try{var f=document.getElementById('rep');
    f.style.height=(f.contentWindow.document.body.scrollHeight+80)+'px';}catch(e){
    document.getElementById('rep').style.height='2400px';}
}
function hardReload(){ location.reload(); }
function tick(){
  var lu=luParts(new Date());
  document.getElementById('clock').textContent=lu.time+' CAT · '+fmtLong(lu.date);
  var lu_el=document.getElementById('lastupd');
  // midnight rollover: the Zambia date changed while the page was open -> reload
  if(openedDay && lu.date!==openedDay){
    document.getElementById('freshbox').innerHTML=
      '<div class="stale">🕛 The date changed to '+fmtLong(lu.date)+
      ' in Zambia while this page was open — reloading with fresh data…</div>';
    setTimeout(function(){location.reload();}, 1500);
  }
}
loadReport(); setInterval(tick, 1000);
fetch('data/calendar_data.json?v='+Date.now()).then(function(r){return r.json();}).then(function(j){
  document.getElementById('lastupd').textContent='calendar/results data: '+j.generated_at_cat;
}).catch(function(){});
</script></body></html>"""
    page = page.replace("__ARCHIVE__", archive)
    Path("index.html").write_text(page)
    print("index.html -> dynamic 'Today' page (live Lusaka date, cache-busted)")


if __name__ == "__main__":
    main()
