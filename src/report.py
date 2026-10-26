"""Daily report: self-contained HTML (no external assets) + CSV export.

Layout (task A/E/D):
  1. Value bets (only published picks; stable identity = fixture+market+selection)
  2. Verified upcoming fixtures WITH predictions - cards grouped by Lusaka day
  3. Verified fixtures - prediction WITHHELD (visible, with the exact reason)
  4. Verification FAILED (visible, with reason)
  5. Not upcoming: live / completed / postponed / cancelled / removed fixtures
     (clearly separated; never presented as upcoming - task 6)
Filters: text + day + country + competition + market + verification status.
Large lists: pagination via 'show more' buttons (client-side, static site).
Manual refresh: link into the GitHub Actions run page (auth lives there).
"""
from __future__ import annotations

import csv
import html
from pathlib import Path

SEL_LABEL = {"home": "Home (1)", "draw": "Draw (X)", "away": "Away (2)",
             "over": "Over", "under": "Under", "yes": "Yes", "no": "No",
             "1X": "1X", "12": "12", "X2": "X2"}
MKT_LABEL = {"1X2": "Match result (1X2)", "OU15": "Over/Under 1.5 goals",
             "OU25": "Over/Under 2.5 goals", "OU35": "Over/Under 3.5 goals",
             "BTTS": "Both teams to score", "DC": "Double chance"}
MKT_SHORT = {"1X2": "1X2", "OU15": "O/U1.5", "OU25": "O/U2.5", "OU35": "O/U3.5",
             "BTTS": "BTTS", "DC": "Double-chance"}
REFRESH_URL = ("https://github.com/mwanzajerry14-bit/soccer-value-bets/"
               "actions/workflows/daily.yml")

CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1a1a2e}
h1{font-size:22px}h2{font-size:17px;margin-top:28px;border-bottom:2px solid #3949ab;padding-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}
th{background:#3949ab;color:#fff;padding:6px 8px;text-align:left}
td{border-bottom:1px solid #e3e3ee;padding:5px 8px}
tr.v td{background:#e8f5e9}
.badge{background:#2e7d32;color:#fff;border-radius:4px;padding:1px 6px;font-size:11px}
.chip{display:inline-block;background:#eef0fb;border:1px solid #cfd5f2;border-radius:10px;padding:1px 8px;font-size:11px;margin-right:4px}
.fid{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10px;color:#777}
.warn{background:#fff8e1;border:1px solid #ffe082;padding:10px 14px;border-radius:6px;font-size:13px;margin:12px 0}
.demo{background:#ffebee;border:1px solid #ef5350;padding:12px;border-radius:6px;font-weight:600}
.summary{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0}
.card{background:#f4f6ff;border:1px solid #dfe3f5;border-radius:8px;padding:10px 16px;font-size:13px}
.card b{font-size:18px;display:block}
.small{font-size:12px;color:#555}
td.r,th.r{text-align:right}
.dayhdr{background:#dfe3f5;border-radius:6px;padding:4px 10px;font-weight:700;font-size:13px;margin:18px 0 6px}
.fx{border-left:3px solid #3949ab;padding-left:10px;margin-bottom:14px}
.fx.w-l{border-left-color:#ef6c00}.fx.f-l{border-left-color:#c62828}.fx.b-l{border-left-color:#9e9e9e}
.btn{display:inline-block;background:#3949ab;color:#fff!important;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:13px;border:0;cursor:pointer}
.more{margin:8px 0 18px}
select,input{padding:4px 8px;font-size:13px}
"""


def esc(x) -> str:
    return html.escape(str(x))


def pct(x, nd=1):
    return "n/a" if x is None else f"{100*x:.{nd}f}%"


def disp(name: str) -> str:
    """Pretty display name from the canonical lowercase key."""
    return html.escape(name.title())


def _conf_span(conf: str | None) -> str:
    if not conf:
        return ""
    cc = {"high": "#2e7d32", "moderate": "#ef6c00", "low": "#b71c1c"}.get(conf, "#555")
    tip = ("Rule: high = completeness>=90% + models agree + >=6 books; "
           "moderate = >=70% + >=3 books. Data-quality tier, NOT an accuracy claim.")
    return (f"<span style='color:{cc}'><b>{esc(conf)}</b></span> "
            f"<span class='small' title=\"{esc(tip)}\">(rule)</span>")


def _picks_table(picks: list[dict]) -> str:
    if not picks:
        return ('<div class="warn"><b>No value bets in this window.</b> '
                'This is normal and honest: value is rare. '
                'Do not lower the threshold just to have action.</div>')
    rows = []
    for i, p in enumerate(picks, 1):
        notes = "; ".join(p.get("notes", []))
        rows.append(
            f"<tr class='v pagrow'><td>{i}</td>"
            f"<td>{disp(p['home'])} v {disp(p['away'])}</td>"
            f"<td><span class='chip'>{esc(p.get('day_label') or p['date'])}</span>"
            f"<span class='small'>{esc(p.get('kickoff_local') or p['date'])}</span></td>"
            f"<td>{esc(p.get('country',''))} · {esc(p['league'])}</td>"
            f"<td>{esc(MKT_LABEL[p['market']])}</td><td><b>{esc(SEL_LABEL[p['selection']])}</b></td>"
            f"<td>{esc(p['book'])}</td><td class='r'><b>{p['odds']:.2f}</b></td>"
            f"<td class='r'>{pct(p['p_model'])}</td><td class='r'>{pct(p['p_fair'])}</td>"
            f"<td class='r'>+{100*p['edge']:.1f}%</td><td class='r'>{100*p['ev']:.1f}%</td>"
            f"<td>{_conf_span(p.get('confidence'))}</td>"
            f"<td class='r'>{p['stake']:.2f}</td>"
            f"<td class='small'>{esc(notes)}<br><span class='fid'>{esc(p.get('fixture_id',''))}</span></td></tr>")
    return ("<table><tr><th>#</th><th>Match</th><th>Kickoff (CAT)</th><th>Competition</th>"
            "<th>Market</th><th>Selection</th><th>Best book</th><th class='r'>Odds</th>"
            "<th class='r'>Model p</th><th class='r'>Fair implied</th><th class='r'>Edge</th>"
            "<th class='r'>EV</th><th>Conf.</th><th class='r'>Stake</th><th>Notes / fixture id</th></tr>"
            + "".join(rows) + "</table>")


def _market_table(m: dict) -> str:
    s = m["summary"]
    margin_txt = ("n/a" if s["avg_margin"] is None else f"{100*s['avg_margin']:.1f}%")
    rows = []
    for r in m["rows"]:
        cls = " class='v'" if r["is_value"] else ""
        flag = " <span class='badge'>VALUE</span>" if r["is_value"] else ""
        mgn = "n/a" if r["book_margin"] is None else f"{100*r['book_margin']:.1f}%"
        rows.append(f"<tr{cls}><td>{esc(SEL_LABEL[r['selection']])}{flag}</td>"
                    f"<td class='r'>{pct(r['p_model'])}</td><td class='r'>{pct(r['p_fair'])}</td>"
                    f"<td class='r'>{r['odds']:.2f} <span class='small'>@ {esc(r['book'])}</span></td>"
                    f"<td class='r'>{100*r['edge']:+.1f}%</td><td class='r'>{mgn}</td></tr>")
    return (f"<div class='small' style='margin-top:6px'><b>{esc(MKT_LABEL[m['market']])}</b>"
            f" · books: {s['n_books']} · avg bookmaker margin: {margin_txt}</div>"
            "<table><tr><th>Selection</th><th class='r'>Model p</th>"
            "<th class='r'>Fair implied p</th><th class='r'>Best price</th>"
            "<th class='r'>Edge</th><th class='r'>Book margin</th></tr>" + "".join(rows) + "</table>")


def _evidence_html(ev: dict) -> str:
    """VERIFIED FACTS section: recent form, venue splits, H2H, team-news
    status - every number from real recorded match data."""
    out = ["<div style='background:#f6f8ee;border:1px solid #e0e6c8;border-radius:6px;"
           "padding:8px 12px;margin:8px 0'>",
           "<b>VERIFIED FACTS</b> <span class='small'>(real recorded results; "
           "sources: provider feed + football-data.co.uk results)</span>"]

    def frow(lbl, a):
        if not a or not a.get("n"):
            return ""
        sample = " ⚠ small sample" if a.get("small_sample") else ""
        return (f"<tr><td>{esc(lbl)}</td><td class='r'>{a['n']}</td>"
                f"<td class='r'>{a['avg_gf']:.2f} / {a['avg_ga']:.2f}</td>"
                f"<td class='r'>{a['over25_n']} ({a['over25_pct']}%)</td>"
                f"<td class='r'>{a['over35_n']} ({a['over35_pct']}%)</td>"
                f"<td class='r'>{a['clean_sheets']}</td><td class='r'>{a['failed_to_score']}</td>"
                f"<td class='r'>{('Elo ' + str(a['opp_mean_elo'])) if a.get('opp_mean_elo') else '-'}"
                f"</td><td class='small'>{esc(sample)}</td></tr>")

    out.append("<table><tr><th>Recent form / home-away record</th><th class='r'>n</th>"
               "<th class='r'>avg GF / GA</th><th class='r'>Over 2.5</th>"
               "<th class='r'>Over 3.5</th><th class='r'>clean sheets</th>"
               "<th class='r'>failed to score</th><th class='r'>opponent strength</th>"
               "<th>sample</th></tr>")
    for lbl, key in (("Home team - last 5 overall", "form5_home"),
                     ("Home team - last 10 overall", "form10_home"),
                     ("Home team - last 5 at home", "split_home"),
                     ("Home team - last 10 at home", "split10_home"),
                     ("Away team - last 5 overall", "form5_away"),
                     ("Away team - last 10 overall", "form10_away"),
                     ("Away team - last 5 away", "split_away"),
                     ("Away team - last 10 away", "split10_away")):
        out.append(frow(lbl, ev.get(key)))
    out.append("</table>")
    out.append(f"<div class='small'>Recency-weighted trend (decay): "
               f"home trend GF/GA {ev['form5_home'].get('trend_gf','-')}/{ev['form5_home'].get('trend_ga','-')}"
               f" · away {ev['form5_away'].get('trend_gf','-')}/{ev['form5_away'].get('trend_ga','-')}</div>")
    h2h = ev.get("h2h") or {}
    if h2h.get("n"):
        legs = "; ".join(f"{g['date']}: {g['match']} "
                         f"({'O2.5' if g['over25'] else 'U2.5'}{' O3.5' if g['over35'] else ''}"
                         f"{', old>' + '18m' if g['old'] else ''})" for g in h2h["games"])
        out.append(f"<div class='small'><b>Head-to-head</b> ({h2h['n']} latest meetings, avg "
                   f"{h2h['avg_total']} goals): {esc(legs)}"
                   f"{' <b>⚠ ' + esc(h2h['note']) + '</b>' if h2h.get('note') else ''}</div>")
    else:
        out.append("<div class='small'><b>Head-to-head:</b> no recorded meetings in the "
                   "loaded history - not used, not invented.</div>")
    news = ev.get("news") or {}
    if news.get("status") == "csv":
        entries = "; ".join(f"{e['note']} ({e['source']}, {e['checked_at']}"
                            f"{'' if e['confirmed'] else ' - unconfirmed'})"
                            for e in news["entries"])
        out.append(f"<div class='small'><b>Team news</b> (user-maintained CSV, "
                   f"checked {esc(news['checked_at'])}): {esc(entries)}</div>")
    else:
        out.append(f"<div class='small'><b>Team news:</b> {esc(news.get('note','unverified'))}"
                   " - reported as unavailable, not invented.</div>")
    out.append("</div>")
    return "".join(out)


def _pred_card(f: dict) -> str:
    v = f.get("verify") or {}
    conf = v.get("confidence")
    cc = {"high": "#2e7d32", "moderate": "#ef6c00", "low": "#b71c1c"}.get(conf, "#555")
    ts = f.get("top_score")
    score_txt = f" · most likely score {ts[0]}-{ts[1]}" if ts else ""
    header = (f"<h3 style='margin:14px 0 4px'>{disp(f['home'])} v {disp(f['away'])} "
              f"<span class='badge'>✓ VERIFIED</span></h3>"
              f"<div class='small'><span class='chip'>{esc(f['country'])}</span>"
              f"<span class='chip'>{esc(f['league'])}</span> "
              f"{esc(f.get('kickoff_local') or f['date'])} · status {esc(v.get('status','?'))} · "
              f"data completeness <b>{v.get('completeness', 0):.0f}%</b> · "
              f"confidence <b style='color:{cc}'>{esc(conf or 'n/a')}</b> <span class='small'>(rule-based tier, not accuracy)</span> · "
              f"model {esc(f.get('model_version',''))}<br>"
              f"<span class='fid'>fixture id: {esc(f.get('fid',''))} · sources: {esc(', '.join(v.get('sources', [])))}</span></div>")
    parts = [header]
    # ---- section 1: verified facts (real results only) ----
    if f.get("evidence"):
        parts.append(_evidence_html(f["evidence"]))
    # ---- section 2: model prediction (never presented as fact) ----
    parts.append("<div style='background:#eef4fd;border:1px solid #cddcfa;border-radius:6px;"
                 "padding:8px 12px;margin:8px 0'><b>MODEL PREDICTION</b> "
                 "<span class='small'>(estimates, not confirmed outcomes; Dixon-Coles "
                 "Poisson goal model blended with Elo)</span>")
    if f.get("explain"):
        parts.append(f"<div class='small'>{esc(f['explain'])}{esc(score_txt)}</div>")
    if f.get("model_only"):
        bits = []
        for mo in f["model_only"]:
            pr = " / ".join(f"{SEL_LABEL.get(s, s)} {pct(p)}" for s, p in mo["probs"].items())
            bits.append(f"<b>{esc(MKT_SHORT.get(mo['market'], mo['market']))}</b>: {pr}")
        parts.append(f"<div class='small' style='margin-top:6px'>📊 Model probabilities, "
                     f"markets with no current bookmaker price (no fair line, no EV): "
                     + " · ".join(bits) + "</div>")
    parts.append("</div>")
    # ---- section 3: betting market information (real prices only) ----
    parts.append("<div style='background:#fdf6ec;border:1px solid #f0dcba;border-radius:6px;"
                 "padding:8px 12px;margin:8px 0'><b>BETTING MARKET INFORMATION</b> "
                 "<span class='small'>(real retrieved prices only; timestamps shown in "
                 "the sync log; stale prices excluded)</span>")
    if f["markets"]:
        for m in f["markets"]:
            parts.append(_market_table(m))
    else:
        parts.append("<div class='small'>No bookmaker prices retrieved for this fixture "
                     "from configured sources - no fair line and no EV published.</div>")
    parts.append("</div>")
    if f.get("notes"):
        parts.append(f"<div class='small'>⚠ {'; '.join(map(esc, f['notes']))}</div>")
    mkts = " ".join(m["market"] for m in f["markets"]) + " " + \
           " ".join(mo["market"] for mo in f.get("model_only", []))
    q = esc(f"{f['league']} {f['country']} {f['home']} {f['away']} {f['date']} verified")
    return (f"<div class='fx' data-q=\"{q}\" data-day='{f.get('day_index')}' "
            f"data-country=\"{esc(f['country'])}\" data-league=\"{esc(f['league'])}\" "
            f"data-markets='{mkts}' data-verify='verified'>" + "".join(parts) + "</div>")


def _over_table(rows: list[dict], line: str, letter: str) -> str:
    """Ranked over-goals selections. Ranked by confidence tier -> calibrated
    model probability -> data completeness -> evidence quality (documented
    rule). EV is displayed but NEVER determines rank."""
    if not rows:
        return (f"<div class='warn'><b>TABLE {letter}: no eligible Over {line} "
                f"selections published.</b> The system does not force a daily "
                f"shortlist - insufficient data or failed verification means "
                f"no entry.</div>")
    conf_c = {"high": "#2e7d32", "moderate": "#ef6c00", "low": "#b71c1c"}
    out = ["<table><tr><th>#</th><th>Fixture</th><th>Competition</th>"
           "<th>Kickoff (CAT)</th><th class='r'>Model p</th><th class='r'>Fair odds</th>"
           "<th>Recent form</th><th>Home/Away</th><th>H2H</th><th>Team news</th>"
           "<th class='r'>Best odds</th><th class='r'>EV</th>"
           "<th>Data / conf</th><th>Supports / risks</th></tr>"]
    for r in rows:
        cc = conf_c.get(r["conf"], "#555")
        odds_txt = (f"{r['odds']:.2f}<br><span class='small'>{esc(r['book'])}</span>"
                    if r.get("odds") else
                    "<span class='small'>not retrieved<br>(no EV computed)</span>")
        ev_txt = f"{100*r['ev']:.1f}%" if r.get("ev") is not None else "—"
        sup = "<br>".join("＋ " + esc(s) for s in r["support"][:3])
        ris = "<br>".join("－ " + esc(s) for s in r["risks"][:3])
        out.append(
            f"<tr class='pagrow'><td>{r['rank']}</td>"
            f"<td>{esc(r['fixture'].title())}<br><span class='fid'>{esc(r['fid'])}</span></td>"
            f"<td class='small'>{esc(r['country'])} · {esc(r['league'])}</td>"
            f"<td class='small'>{esc(r['kickoff_local'] or r['date'])}</td>"
            f"<td class='r'><b>{pct(r['p'])}</b></td>"
            f"<td class='r'>{r['fair_odds']:.2f}</td>"
            f"<td class='small'>{esc(r['form_ev'])}</td>"
            f"<td class='small'>{esc(r['venue_ev'])}</td>"
            f"<td class='small'>{esc(r['h2h_ev'])}</td>"
            f"<td class='small'>{esc(r['news'])}</td>"
            f"<td class='r'>{odds_txt}</td><td class='r'>{ev_txt}</td>"
            f"<td class='small'>completeness {r['completeness']:.0f}% · "
            f"<b style='color:{cc}'>{esc(r['conf'])}</b> · evidence {100*r['quality']:.0f}%</td>"
            f"<td class='small'>{sup}{'<br>' if sup and ris else ''}{ris}</td></tr>")
    out.append("</table>")
    if all(not r.get("odds") for r in rows):
        out.append("<p class='small'>No current bookmaker prices were retrieved for this "
                   "line (O/U 3.5 is rarely in the free feed) - fair odds shown are pure "
                   "model (1/p) and NO expected value is claimed. Local-book prices can be "
                   "added to data/manual_odds.csv (market OU35).</p>")
    return "".join(out)


def _row(f: dict, css: str, why_label: str, why: str) -> str:
    v = f.get("verify") or {}
    q = esc(f"{f['league']} {f.get('country','')} {f['home']} {f['away']} {f['date']} "
            f"{css}status-{v.get('status','')}")
    return (f"<tr class='fx pagrow {css}' data-q=\"{q}\" data-day='{f.get('day_index')}' "
            f"data-country=\"{esc(f.get('country',''))}\" data-league=\"{esc(f['league'])}\" "
            f"data-markets='' data-verify='{'verified' if v.get('verified') else 'not-verified'}'>"
            f"<td><span class='chip'>{esc(f.get('day_label') or f['date'])}</span></td>"
            f"<td>{disp(f['home'])} v {disp(f['away'])}</td>"
            f"<td class='small'>{esc(f.get('kickoff_local') or f['date'])}</td>"
            f"<td>{esc(f.get('country',''))} · {esc(f['league'])}</td>"
            f"<td class='small'>{esc(why)}</td>"
            f"<td class='fid'>{esc(f.get('fid',''))}</td></tr>")


def _fixtures(fixtures: list[dict], window_dates: list[str]) -> str:
    pred = [f for f in fixtures if f.get("section") == "predicted"]
    with_ = [f for f in fixtures if f.get("section") == "withheld"]
    fail = [f for f in fixtures if f.get("section") == "failed"]
    block = [f for f in fixtures if f.get("section") == "blocked"]
    pred.sort(key=lambda f: (f["date"], f["home"]))
    out = []

    out.append("<h2>✅ Verified upcoming fixtures with predictions "
               f"({len(pred)})</h2>")
    out.append("<div class='pag' id='predCards'>")
    cur_day = None
    for f in pred:
        if f["date"] != cur_day:
            cur_day = f["date"]
            out.append(f"<div class='dayhdr'>{esc(f.get('day_label') or cur_day)}"
                       f" — {esc(cur_day)}</div>")
        out.append(_pred_card(f))
    if not pred:
        out.append("<p class='small'>No published predictions in this window.</p>")
    out.append("</div>")

    out.append(f"<h2>⚠️ Verified fixtures — prediction withheld ({len(with_)})</h2>"
               "<p class='small'>These matches are verified as real and upcoming, but the "
               "system refuses to fabricate a prediction (usually: one/both teams have no "
               "history in the training window, or bookmaker prices are not available yet). "
               "They may gain predictions on a later run once prices appear.</p>")
    if with_:
        out.append("<div class='pag'><table><tr><th>Day</th><th>Fixture</th>"
                   "<th>Kickoff (CAT)</th><th>Competition</th><th>Why withheld</th>"
                   "<th>fixture id</th></tr>")
        for f in sorted(with_, key=lambda x: (x["date"], x["league"], x["home"])):
            out.append(_row(f, "fx w-l", "withheld", f.get("withheld_reason", "")))
        out.append("</table></div>")
    else:
        out.append("<p class='small'>none - every verified fixture earned a prediction.</p>")

    out.append(f"<h2>⛔ Verification failed ({len(fail)})</h2>")
    if fail:
        out.append("<div class='pag'><table><tr><th>Day</th><th>Fixture</th>"
                   "<th>Kickoff (CAT)</th><th>Competition</th><th>Failure reason</th>"
                   "<th>fixture id</th></tr>")
        for f in sorted(fail, key=lambda x: (x["date"], x["league"], x["home"])):
            out.append(_row(f, "fx f-l", "failed", f.get("fail_reason", "")))
        out.append("</table></div>")
    else:
        out.append("<p class='small'>none.</p>")

    out.append(f"<details><summary><b>Not upcoming — live / finished / removed "
               f"({len(block)})</b> (never presented as upcoming)</summary>")
    if block:
        out.append("<div class='pag'><table><tr><th>Day</th><th>Fixture</th>"
                   "<th>Kickoff (CAT)</th><th>Competition</th><th>Status</th>"
                   "<th>fixture id</th></tr>")
        for f in sorted(block, key=lambda x: (x["date"], x["league"], x["home"])):
            st = (f.get("verify") or {}).get("status", "?")
            out.append(_row(f, "fx b-l", "status", f"{st}: {f.get('status_note','')}"))
        out.append("</table></div>")
    else:
        out.append("<p class='small'>none.</p>")
    out.append("</details>")
    return "".join(out)


def _filter_bar(window_dates: list[str], fixtures: list[dict]) -> str:
    countries = sorted({f.get("country", "") for f in fixtures if f.get("country")})
    leagues = sorted({f["league"] for f in fixtures})
    day_opts = ["<option value=''>all days</option>"] + [
        f"<option value='{i}'>{html.escape(lbl)}</option>" for i, lbl in enumerate(
            ["Today", "Tomorrow", "In 2 days", "In 3 days", "In 4 days"][:len(window_dates)])]
    country_opts = ["<option value=''>all countries</option>"] + [
        f"<option>{html.escape(c)}</option>" for c in countries]
    league_opts = ["<option value=''>all competitions</option>"] + [
        f"<option>{html.escape(l)}</option>" for l in leagues]
    return f"""
<div class="small" style="margin:6px 0">
  🔎 <input id="fq" placeholder="team / league / keyword…" oninput="fxFilter()" style="width:200px">
  📅 <select id="fd" onchange="fxFilter()">{''.join(day_opts)}</select>
  🌍 <select id="fc" onchange="fxFilter()">{''.join(country_opts)}</select>
  🏆 <select id="fl" onchange="fxFilter()">{''.join(league_opts)}</select>
  🎯 <select id="fm" onchange="fxFilter()"><option value=''>all markets</option>
     <option>1X2</option><option>OU15</option><option>OU25</option><option>OU35</option>
     <option>BTTS</option><option>DC</option></select>
  ✓ <select id="fs" onchange="fxFilter()"><option value=''>any status</option>
     <option value='verified'>verified only</option>
     <option value='not-verified'>withheld / failed only</option></select>
</div>"""


def render(run: dict, picks: list[dict], fixtures: list[dict],
           unmatched: list[str], demo: bool = False) -> str:
    from datetime import datetime as _dt
    gen_js = "null"
    try:
        _gu = (run.get("generated_utc") or "").replace(" UTC", "")
        gen_js = f'new Date("{_dt.strptime(_gu, "%Y-%m-%d %H:%M").strftime("%Y-%m-%dT%H:%M:00Z")}")'
    except (ValueError, TypeError):
        pass
    demo_banner = ("<div class='demo'>DEMONSTRATION REPORT — generated offline from real historical "
                   "closing odds (football-data.co.uk). Results are already known at these dates; "
                   "this shows report layout only. Live runs use your API keys.</div>") if demo else ""
    unmatch_block = ""
    if unmatched:
        unmatch_block = ("<h2>Skipped / unmatched / integrity notes</h2><div class='warn'>"
                         + "<br>".join(esc(u) for u in dict.fromkeys(unmatched))
                         + "</div><p class='small'>Add aliases to src/teamnames.py (ALIASES) to fix "
                           "team-name mapping issues.</p>")
    ct = run.get("counts") or {}
    cards = f"""
    <div class="summary">
      <div class="card">Fixtures in window<b>{run['n_fixtures']}</b></div>
      <div class="card">With predictions<b>{ct.get('predicted', '-')}</b></div>
      <div class="card">Withheld (insufficient data)<b>{ct.get('withheld', '-')}</b></div>
      <div class="card">Verification failed<b>{ct.get('failed', '-')}</b></div>
      <div class="card">Live / finished / removed<b>{ct.get('blocked', '-')}</b></div>
      <div class="card">Value bets (edge ≥ {100*run['threshold']:.0f}%)<b>{len(picks)}</b></div>
      <div class="card">Total suggested stake<b>{sum(p['stake'] for p in picks):.2f}</b></div>
      <div class="card">Bankroll in config<b>{run['bankroll']:.0f}</b></div>
      <div class="card">Staking<b>¼ Kelly, cap 2%</b></div>
    </div>"""
    wd = run.get("window_dates") or [run["date"]]
    window_txt = f"{wd[0]} → {wd[-1]} ({len(wd)} calendar days, Africa/Lusaka CAT)"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="7200">
<title>Value bets — {esc(run['date'])}</title><style>{CSS}</style></head><body>
<h1>⚽ Football value-bet report — {esc(run['date'])}</h1>
<p class="small">Window: <b>{esc(window_txt)}</b> · Generated {esc(run['generated_at'])}
({esc(run.get('generated_utc',''))}) · Model: {esc(run.get('model_version','DC+Elo'))} — Dixon-Coles
(time-decayed) blended with Elo · Every fixture verified before prediction.</p>
<div class="card" style="display:inline-block;margin:4px">Now in Zambia (live clock)&nbsp;<b id="lvclock">…</b></div>
<div class="card" style="display:inline-block;margin:4px">Report age&nbsp;<b id="lage">…</b></div>
<div id="lvlive"></div>
<p><button class="btn" onclick="location.reload()">↻ Refresh</button></p>
<script>
(function(){{
  var genUtc = {gen_js};
  function luParts(d){{var f=new Intl.DateTimeFormat('en-CA',{{timeZone:'Africa/Lusaka',
    year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',
    second:'2-digit',hour12:false}});var p={{}};f.formatToParts(d).forEach(function(x){{p[x.type]=x.value;}});
    return p;}}
  var opened=luParts(new Date()).year+'-'+luParts(new Date()).month+'-'+luParts(new Date()).day;
  function tick(){{
    var p=luParts(new Date()), today=p.year+'-'+p.month+'-'+p.day;
    var nm=fmtDayName(p);
    document.getElementById('lvclock').textContent=p.hour+':'+p.minute+' CAT · '+nm;
    if(genUtc){{var age=(Date.now()-genUtc.getTime())/36e5;
      var el=document.getElementById('lage');
      el.textContent=age<1?'fresh this hour':age.toFixed(1)+'h';
      if(age>26){{el.style.color='#c62828';}}}}
    // this is a DATED report; if the real Zambia date moved on, say so honestly
    if(today!==opened){{ setTimeout(function(){{location.reload();}},1500); return; }}
    if(today!=='{esc(run['date'])}'){{
      var b=document.getElementById('lvlive');
      if(!b.innerHTML) b.innerHTML='<div class="warn" style="border-color:#ef5350;background:#ffebee">'+
        '🕛 This is the report for <b>{esc(run['date'])}</b>, but it is now <b>'+today+
        '</b> in Zambia. <a href="../index.html">Open today\\u2019s page</a> (auto-updates daily).</div>';
    }} else {{
      document.getElementById('lvlive').innerHTML='';
    }}
  }}
  function fmtDayName(p){{return new Date(p.year+'-'+p.month+'-'+p.day+'T12:00:00Z')
    .toLocaleDateString('en-GB',{{weekday:'short',day:'numeric',month:'short',
    timeZone:'Africa/Lusaka'}});}}
  tick(); setInterval(tick, 1000);
}})();
</script>
<a class="btn" href="{REFRESH_URL}" target="_blank">↻ Run pipeline refresh now (GitHub sign-in)</a>
{''.join(f"<div class='demo'>{esc(n)}</div>" for n in run.get('notices', []))}
{demo_banner}
{cards}
<h2>Value bets</h2>
{_picks_table(picks)}
<h2>🎯 Over-goals strategy - ranked selections</h2>
<p class="small">Your preferred markets, evaluated independently for every verified
upcoming fixture. Ranking rule (documented): confidence tier → calibrated model
probability → data completeness → evidence quality. <b>EV never determines rank.</b>
A match strong on Over 2.5 need not qualify for Over 3.5.</p>
<h3 style="margin-top:6px">TABLE A — BEST OVER 2.5 GOALS SELECTIONS</h3>
{_over_table((run.get('over_tables') or {}).get('A', []), '2.5', 'A')}
<h3>TABLE B — BEST OVER 3.5 GOALS SELECTIONS</h3>
{_over_table((run.get('over_tables') or {}).get('B', []), '3.5', 'B')}
<p class="small">Edge = model probability − consensus fair probability (each bookmaker de-vigged, then
averaged). EV = p × best price − 1, computed ONLY when a real, current bookmaker price exists.
Positive estimated EV depends on the model being well calibrated and does NOT guarantee profit -
calibration is measured on <a href="../results.html">results.html</a>. Confidence = data-completeness
+ model-agreement tier (rule published in README - it is NOT an accuracy claim). Stake =
¼ Kelly capped at 2% of bankroll. Bookmaker margin (overround) shown per market.</p>
{_filter_bar(wd, fixtures)}
{_fixtures(fixtures, wd)}
{unmatch_block}
<hr><p class="small">Predictions are probabilistic estimates only — no outcome is guaranteed; betting
involves real risk. Only pre-match snapshots are used for grading; predictions made after kickoff
would be invalid and are never created here. Result tracking, calibration, Brier/log-loss:
see results.html · Ops: admin.html.</p>
<script>
function fxFilter(){{
  var q=document.getElementById('fq').value.toLowerCase();
  var d=document.getElementById('fd').value, c=document.getElementById('fc').value,
      l=document.getElementById('fl').value, m=document.getElementById('fm').value,
      s=document.getElementById('fs').value;
  var filtered = q||d||c||l||m||s;
  document.querySelectorAll('.fx').forEach(function(x){{
    var ok=(x.getAttribute('data-q').toLowerCase().includes(q))
      &&(!d||x.getAttribute('data-day')==d)
      &&(!c||x.getAttribute('data-country')==c)
      &&(!l||x.getAttribute('data-league')==l)
      &&(!m||(x.getAttribute('data-markets')||'').split(' ').includes(m))
      &&(!s||x.getAttribute('data-verify')==s);
    x.style.display=ok?'':'none'; x.dataset.filt=ok?'1':'';
  }});
  if(filtered){{document.querySelectorAll('.pag .morebtn').forEach(function(b){{b.style.display='none'}});
    document.querySelectorAll('.fx[data-filt="1"]').forEach(function(x){{x.style.display='';}});}}
  else{{setupPags();}}
}}
function setupPags(){{
  document.querySelectorAll('.pag').forEach(function(pg){{
    var kids=[].slice.call(pg.querySelectorAll(':scope > .fx, :scope > table > tr.pagrow, :scope > table tr.pagrow'));
    kids=kids.filter(function(k){{return k.querySelectorAll('th').length===0;}});
    var per=12, btn=pg.nextSibling;
    if(!btn||!btn.classList||!btn.classList.contains('morebtn')){{
      btn=document.createElement('button');btn.className='btn morebtn more';
      pg.parentNode.insertBefore(btn, pg.nextSibling);
      btn.onclick=function(){{pg.dataset.shown=(+pg.dataset.shown)+per;applyPag(pg);}};
    }}
    pg.dataset.shown=per; applyPag(pg);
  }});
}}
function applyPag(pg){{
  var kids=[].slice.call(pg.querySelectorAll('tr.pagrow, .fx'));
  kids=kids.filter(function(k){{return k.querySelectorAll('th').length===0;}});
  var shown=+pg.dataset.shown||12, hidden=0;
  kids.forEach(function(k,i){{k.style.display=i<shown?'':'none';if(i>=shown)hidden++;}});
  var btn=pg.parentNode.querySelector('.morebtn');
  if(btn){{btn.textContent='Show more ('+hidden+' remaining)';btn.style.display=hidden?'':'none';}}
}}
setupPags();
</script>
</body></html>"""


def write_picks_csv(picks: list[dict], path: Path) -> None:
    cols = ["date", "day_label", "kickoff_local", "league", "country", "fixture_id",
            "home", "away", "market", "selection", "book", "odds",
            "p_model", "p_fair", "p_raw", "edge", "ev", "confidence", "completeness", "stake"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for p in picks:
            w.writerow(p)
