#!/usr/bin/env python3
"""Rebuild the website landing page (repo-root index.html).

Runs inside the daily GitHub Action, right after report generation:
  - index.html auto-redirects to the LATEST daily HTML report
  - below it, an archive list of every past report (html/xlsx/csv)
GitHub Pages (Settings -> Pages -> main / root) then serves this at
    https://<user>.github.io/soccer-value-bets/
"""
from pathlib import Path
import html

REPORTS = Path("reports")


def main() -> None:
    files = sorted(REPORTS.glob("*_report.html"))
    rows = []
    for f in reversed(files):
        day = f.name.replace("_report.html", "")
        xl = REPORTS / f"{day}_report.xlsx"
        cs = REPORTS / f"{day}_picks.csv"
        links = f'<a href="reports/{f.name}">HTML report</a>'
        if xl.exists():
            links += f' · <a href="reports/{xl.name}">Excel</a>'
        if cs.exists():
            links += f' · <a href="reports/{cs.name}">CSV</a>'
        rows.append(f"<li><b>{html.escape(day)}</b>: {links}</li>")
    target = f"reports/{files[-1].name}" if files else "#"
    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0; url={target}">
<title>Daily value bets</title></head>
<body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:720px;margin:40px auto;padding:0 16px">
<h2>&#9917; Daily football value-bet reports</h2>
    <p>📅 <b><a href="calendar.html">Match calendar (live)</a></b>
&nbsp;|&nbsp; <b>Latest report:</b> <a href="{target}">{html.escape(files[-1].name if files else "none yet")}</a>
&nbsp;|&nbsp; <b><a href="results.html">&#9989; Results &amp; prediction verification</a></b>
&nbsp;|&nbsp; <a href="admin.html">&#128736; Admin dashboard</a></p>
<ul>{"".join(rows) or "<li>no reports yet</li>"}</ul>
<p style="color:#666;font-size:12px">Probabilities are model estimates, not guarantees.
Stake only what you can afford to lose.</p>
</body></html>"""
    Path("index.html").write_text(page)
    print("index.html ->", target)


if __name__ == "__main__":
    main()
