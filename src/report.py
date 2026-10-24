"""Daily report: self-contained HTML (no external assets) + CSV export."""
from __future__ import annotations

import csv
import html
from pathlib import Path

SEL_LABEL = {"home": "Home (1)", "draw": "Draw (X)", "away": "Away (2)",
             "over": "Over 2.5", "under": "Under 2.5", "yes": "Yes", "no": "No",
             "1X": "1X", "12": "12", "X2": "X2"}
MKT_LABEL = {"1X2": "Match result (1X2)", "OU25": "Over/Under 2.5 goals",
             "BTTS": "Both teams to score", "DC": "Double chance"}

CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1a1a2e}
h1{font-size:22px}h2{font-size:17px;margin-top:28px;border-bottom:2px solid #3949ab;padding-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}
th{background:#3949ab;color:#fff;padding:6px 8px;text-align:left}
td{border-bottom:1px solid #e3e3ee;padding:5px 8px}
tr.v td{background:#e8f5e9}
.badge{background:#2e7d32;color:#fff;border-radius:4px;padding:1px 6px;font-size:11px}
.warn{background:#fff8e1;border:1px solid #ffe082;padding:10px 14px;border-radius:6px;font-size:13px;margin:12px 0}
.demo{background:#ffebee;border:1px solid #ef5350;padding:12px;border-radius:6px;font-weight:600}
.summary{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0}
.card{background:#f4f6ff;border:1px solid #dfe3f5;border-radius:8px;padding:10px 16px;font-size:13px}
.card b{font-size:18px;display:block}
.small{font-size:12px;color:#555}
td.r,th.r{text-align:right}
"""


def esc(x) -> str:
    return html.escape(str(x))


def pct(x, nd=1):
    return "n/a" if x is None else f"{100*x:.{nd}f}%"


def _picks_table(picks: list[dict]) -> str:
    if not picks:
        return ('<div class="warn"><b>No value bets today.</b> '
                'This is normal and honest: value is rare. '
                'Do not lower the threshold just to have action.</div>')
    rows = []
    for i, p in enumerate(picks, 1):
        notes = "; ".join(p.get("notes", []))
        rows.append(
            f"<tr class='v'><td>{i}</td><td>{esc(p['league'])}</td>"
            f"<td>{esc(p['home'])} v {esc(p['away'])}<br><span class='small'>{esc(p['date'])}"
            f"{' · '+esc(p['time_utc'])+' UTC' if p.get('time_utc') else ''}</span></td>"
            f"<td>{esc(MKT_LABEL[p['market']])}</td><td><b>{esc(SEL_LABEL[p['selection']])}</b></td>"
            f"<td>{esc(p['book'])}</td><td class='r'><b>{p['odds']:.2f}</b></td>"
            f"<td class='r'>{pct(p['p_model'])}</td><td class='r'>{pct(p['p_fair'])}</td>"
            f"<td class='r'>+{100*p['edge']:.1f}%</td><td class='r'>{100*p['ev']:.1f}%</td>"
            f"<td class='r'>{p['stake']:.2f}</td><td class='small'>{esc(notes)}</td></tr>")
    return ("<table><tr><th>#</th><th>League</th><th>Match</th><th>Market</th><th>Selection</th>"
            "<th>Best book</th><th class='r'>Odds</th><th class='r'>Model p</th>"
            "<th class='r'>Fair implied</th><th class='r'>Edge</th><th class='r'>EV</th>"
            "<th class='r'>Stake</th><th>Notes</th></tr>" + "".join(rows) + "</table>")


def _fixtures_tables(fixtures: list[dict]) -> str:
    blocks = []
    for f in fixtures:
        header = (f"<h3 style='margin:14px 0 4px'>{esc(f['league'])} — {esc(f['home'])} v {esc(f['away'])}"
                  f"<span class='small'> · {esc(f['date'])}"
                  f"{' · ' + esc(f['time_utc']) + ' UTC' if f.get('time_utc') else ''}"
                  f" · xG model: {f['xg'][0]:.2f} – {f['xg'][1]:.2f}</span></h3>")
        form = f.get("form") or {}
        fh, fa = form.get("home", {}), form.get("away", {})
        form_line = ""
        if fh:
            form_line = (f"<div class='small'>Last {fh.get('played', 0)}: {esc(f['home'])} "
                         f"{fh.get('pts', 0)} pts ({fh.get('gf', 0)} GF / {fh.get('ga', 0)} GA) · "
                         f"{esc(f['away'])} {fa.get('pts', 0)} pts ({fa.get('gf', 0)} GF / {fa.get('ga', 0)} GA)</div>")
        parts = [header, form_line]
        for m in f["markets"]:
            s = m["summary"]
            margin_txt = ("n/a" if s["avg_margin"] is None else f"{100*s['avg_margin']:.1f}%")
            parts.append(f"<div class='small' style='margin-top:6px'><b>{esc(MKT_LABEL[m['market']])}</b>"
                         f" · books: {s['n_books']} · avg bookmaker margin: {margin_txt}</div>")
            rows = []
            for r in m["rows"]:
                cls = " class='v'" if r["is_value"] else ""
                flag = " <span class='badge'>VALUE</span>" if r["is_value"] else ""
                mgn = "n/a" if r["book_margin"] is None else f"{100*r['book_margin']:.1f}%"
                rows.append(f"<tr{cls}><td>{esc(SEL_LABEL[r['selection']])}{flag}</td>"
                            f"<td class='r'>{pct(r['p_model'])}</td><td class='r'>{pct(r['p_fair'])}</td>"
                            f"<td class='r'>{r['odds']:.2f} <span class='small'>@ {esc(r['book'])}</span></td>"
                            f"<td class='r'>{100*r['edge']:+.1f}%</td><td class='r'>{mgn}</td></tr>")
            parts.append("<table><tr><th>Selection</th><th class='r'>Model p</th>"
                         "<th class='r'>Fair implied p</th><th class='r'>Best price</th>"
                         "<th class='r'>Edge</th><th class='r'>Book margin</th></tr>" + "".join(rows) + "</table>")
        if f.get("notes"):
            parts.append(f"<div class='small'>⚠ {'; '.join(map(esc, f['notes']))}</div>")
        blocks.append("".join(parts))
    return "".join(blocks)


def render(run: dict, picks: list[dict], fixtures: list[dict],
           unmatched: list[str], demo: bool = False) -> str:
    demo_banner = ("<div class='demo'>DEMONSTRATION REPORT — generated offline from real historical "
                   "closing odds (football-data.co.uk). Results are already known at these dates; "
                   "this shows report layout only. Live runs use your API keys.</div>") if demo else ""
    unmatch_block = ""
    if unmatched:
        unmatch_block = ("<h2>Skipped / unmatched names</h2><div class='warn'>"
                         + "<br>".join(esc(u) for u in unmatched)
                         + "</div><p class='small'>Add aliases to src/teamnames.py (ALIASES) to fix.</p>")
    cards = f"""
    <div class="summary">
      <div class="card">Fixtures analysed<b>{run['n_fixtures']}</b></div>
      <div class="card">Value bets (edge ≥ {100*run['threshold']:.0f}%)<b>{len(picks)}</b></div>
      <div class="card">Total suggested stake<b>{sum(p['stake'] for p in picks):.2f}</b></div>
      <div class="card">Bankroll in config<b>{run['bankroll']:.0f}</b></div>
      <div class="card">Staking<b>¼ Kelly, cap 2%</b></div>
    </div>"""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Value bets — {esc(run['date'])}</title><style>{CSS}</style></head><body>
<h1>⚽ Football value-bet report — {esc(run['date'])}</h1>
<p class="small">Generated {esc(run['generated_at'])} · Model: Dixon-Coles (time-decayed) blended with Elo ·
Probabilities are estimates, not guarantees. Betting involves risk.</p>
{demo_banner}
{cards}
<h2>Value bets</h2>
{_picks_table(picks)}
<p class="small">Edge = model probability − consensus fair probability (each bookmaker de-vigged, then averaged).
EV = p × best price − 1. Market margins shown per fixture below.</p>
<h2>All fixtures — model probabilities vs market</h2>
{_fixtures_tables(fixtures)}
{unmatch_block}
<hr><p class="small">Method: bookmaker overround shown per market (avg across books); fair probabilities are
overround-removed (proportional de-vig). Stake = quarter Kelly capped at 2% of bankroll.
Predictions here are probabilistic estimates only — no outcome is guaranteed.</p>
</body></html>"""


def write_picks_csv(picks: list[dict], path: Path) -> None:
    cols = ["date", "league", "home", "away", "market", "selection", "book", "odds",
            "p_model", "p_fair", "p_raw", "edge", "ev", "stake"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for p in picks:
            w.writerow(p)
