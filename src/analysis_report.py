"""Self-contained HTML renderer for the additive tracked-leagues analysis page."""
from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from .report import CSS


def e(value) -> str:
    return html.escape("" if value is None else str(value))


def pct(value, digits: int = 1) -> str:
    return "n/a" if value is None else f"{100 * float(value):.{digits}f}%"


def _market(a: dict, key: str) -> dict:
    return next(x for x in a["markets"] if x["market"] == key)


def _label(label: str) -> str:
    colour = {"Moderate statistical candidate": "#9a6700", "Risky candidate": "#cf222e",
              "No recommendation": "#6e7781"}.get(label, "#555")
    return f'<span class="pill" style="border-color:{colour};color:{colour}">{e(label)}</span>'


def _value(v: dict) -> str:
    if not v.get("available"):
        return f'<span class="small">{e(v.get("reason"))}</span>'
    return (f'{v["best_over_odds"]:.2f} at {e(v["best_book"])} · edge {v["edge"] * 100:+.1f} pts '
            f'· EV {v["ev"] * 100:+.1f}%')


def _rank(rows: list[dict], market: str) -> str:
    if not rows:
        name = "Over 2.5" if market == "OU25" else "Over 5.5"
        return f'<p class="warn">No qualifying {name} statistical candidates today. The system does not pad rankings.</p>'
    bits = ['<div class="scroll"><table><tr><th>#</th><th>Match</th><th>Competition</th><th>Kickoff (CAT)</th>'
            '<th>P(Over)</th><th>Fair odds</th><th>Verification</th><th>Label</th><th>Real price / value</th></tr>']
    for i, a in enumerate(rows, 1):
        m = _market(a, market)
        bits.append(f'<tr><td>{i}</td><td>{e(a["home"])} v {e(a["away"])}</td>'
                    f'<td>{e(a["league"])}</td><td>{e(a["kickoff_cat"])}</td>'
                    f'<td>{pct(m["p_over"])}</td><td>{e(m["fair_over"])}</td>'
                    f'<td>{a["verification"]:.0f}%</td><td>{_label(m["label"])}</td>'
                    f'<td>{_value(m["value"])}</td></tr>')
    bits.append('</table></div>')
    return ''.join(bits)


def _summary(ev: dict, key: str) -> str:
    x = "over25" if key == "OU25" else "over55"
    h, a, vh, va, h2h = ev["form10_home"], ev["form10_away"], ev["venue10_home"], ev["venue10_away"], ev["h2h"]
    return (f'Form L10: home {pct(h.get(x),0)} ({h["n"]}), away {pct(a.get(x),0)} ({a["n"]}) · '
            f'venue: home-at-home {pct(vh.get(x),0)} ({vh["n"]}), away-away {pct(va.get(x),0)} ({va["n"]}) · '
            f'H2H: {pct(h2h.get(x),0)} ({h2h["n"]})')


def _details(a: dict) -> str:
    top = []
    for key, title in (("OU25", "Over 2.5"), ("OU55", "Over 5.5")):
        m = _market(a, key)
        top.append(f'<h4>{title}: {pct(m["p_over"])} · fair {e(m["fair_over"])} · {_label(m["label"])}</h4>'
                   f'<p class="small">{e(m["label_reason"])}. {_summary(a["evidence"], key)}.</p>'
                   f'<p class="small"><b>Prices:</b> {_value(m["value"])}</p>')
    ev = a["evidence"]
    facts = (f'Home last 10: average {e(ev["form10_home"]["avg_total"])} goals, O2.5 {pct(ev["form10_home"]["over25"],0)}, '
             f'O5.5 {pct(ev["form10_home"]["over55"],0)}. Away last 10: average {e(ev["form10_away"]["avg_total"])} goals, '
             f'O2.5 {pct(ev["form10_away"]["over25"],0)}, O5.5 {pct(ev["form10_away"]["over55"],0)}.')
    notes = '; '.join(a["notes"])
    return (f'<details><summary><b>{e(a["home"])} v {e(a["away"])}</b> · {e(a["league"])} · '
            f'{e(a["kickoff_cat"])} · total xG {a["expected_total"]:.2f} · verification {a["verification"]:.0f}%</summary>'
            '<div class="detail"><h4>Verified facts</h4>'
            f'<p class="small">Fixture/status data is from the main site pipeline. Historical statistics use only recorded results before kickoff. {e(facts)}</p>'
            f'<p class="small"><b>Verification:</b> {a["verification"]:.0f}/100 — {e(a["verification_category"])}. '
            'This is data quality, not a probability or an accuracy score.</p>'
            f'<p class="small"><b>Limitations:</b> {e(notes)}</p>'
            '<h4>Independent totals analysis</h4>' + ''.join(top) + '</div></details>')


def _excluded(rows: list[dict]) -> str:
    if not rows:
        return '<p>Nothing was excluded.</p>'
    out = ['<div class="scroll"><table><tr><th>Match</th><th>League</th><th>Reason</th></tr>']
    for x in rows:
        f = x["fixture"]
        out.append(f'<tr><td>{e(f.get("home"))} v {e(f.get("away"))}</td><td>{e(f.get("league"))}</td><td>{e(x["reason"])}</td></tr>')
    return ''.join(out) + '</table></div>'


def build_html(result: dict) -> str:
    c, m = result["counts"], result["metrics"]
    style = CSS + """
.scroll{overflow-x:auto}.cards{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.card{border:1px solid #d0d7de;border-radius:8px;padding:8px 12px;min-width:125px}.card b{display:block;font-size:1.35em}.pill{display:inline-block;border:1px solid #777;border-radius:12px;padding:1px 7px;font-size:11px}.warn{background:#fff8e1;border-left:4px solid #f0ad00;padding:9px 12px}.detail{margin:8px 0 4px;padding:4px 10px;background:#f8fafc;border:1px solid #e3e8ef}.small{font-size:12px;color:#444}details{border:1px solid #d0d7de;border-radius:7px;padding:7px 10px;margin:9px 0}summary{cursor:pointer}
"""
    cards = [("Fixtures found", c["fixtures_discovered"]), ("Analysed", c["analysed"]),
             ("Excluded", c["excluded"]), ("O2.5 candidates", c["ou25_candidates"]),
             ("O5.5 candidates", c["ou55_candidates"]), ("Settled since last run", c["settled"])]
    card_html = ''.join(f'<div class="card"><b>{e(v)}</b>{e(k)}</div>' for k, v in cards)
    metrics = ''.join(f'<li><b>{"Over 2.5" if key == "OU25" else "Over 5.5"}:</b> {x["n"]} settled · '
                      f'Brier {e(x.get("brier", "n/a"))} · ECE {e(x.get("ece", "n/a"))}. {e(x.get("note", ""))}</li>'
                      for key, x in m.items())
    notes = ''.join(f'<li>{e(x)}</li>' for x in result["notes"])
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Football totals analysis</title><style>{style}</style></head><body>
<p class="small"><a href="index.html">← Home</a> · <a href="calendar.html">Calendar</a> · <a href="results.html">Results</a></p>
<h1>Tracked-league totals analysis — Over 2.5 / Over 5.5</h1>
<p><b>Window:</b> {e(result["window"][0])} → {e(result["window"][-1])} · <b>Generated:</b> {e(result["generated_utc"])} UTC · model {e(result["version"])}</p>
<div class="cards">{card_html}</div>
<div class="warn"><b>Scope:</b> this is an additive, independent analysis of the fixtures already tracked by this site — not a claim of a worldwide 100+ match scan. Probabilities are estimates, not guarantees. No value or EV is shown unless a real current bookmaker price was recorded.</div>
<h2>Top Over 2.5 statistical candidates</h2>{_rank(result["rank_ou25"], "OU25")}
<h2>Top Over 5.5 statistical candidates</h2><p class="small">Over 5.5 means six or more goals. It has its own probability and structural evidence gate; it is never inferred from an O2.5 recommendation.</p>{_rank(result["rank_ou55"], "OU55")}
<h2>All analysed matches</h2>{''.join(_details(a) for a in result["analyses"]) or '<p class="warn">No tracked fixture passed the pre-kickoff analysis gates today.</p>'}
<h2>Excluded / insufficient-data matches</h2>{_excluded(result["excluded"])}
<h2>Performance and calibration</h2><p class="small">Only immutable snapshots stored before kickoff are evaluated. Results without a trustworthy match are left unresolved; they are never guessed.</p><ul>{metrics}</ul>
<h2>Data limitations</h2><ul>{notes}</ul>
</body></html>'''
