"""Excel (.xlsx) daily report: value bets + full model-vs-market grid + summary.

Three sheets:
  1. Value bets    - ranked picks with edge, EV and suggested stake
  2. All fixtures  - one row per selection of every market of every match:
                     model probability vs de-vigged fair prob vs best price
  3. Summary       - run parameters, counts, sources, disclaimers (+ skipped names)
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .report import MKT_LABEL, SEL_LABEL

HEADER_FILL = PatternFill("solid", fgColor="3949AB")
HEADER_FONT = Font(color="FFFFFF", bold=True)
VALUE_FILL = PatternFill("solid", fgColor="E8F5E9")
DEMO_FILL = PatternFill("solid", fgColor="FFEBEE")

PCT = "0.0%"
ODDS = "0.00"
STAKE = "0.00"


def _style_header(ws, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(ncols)}1"


def _widths(ws, widths: list[float]) -> None:
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _sheet_value_bets(wb: Workbook, picks: list[dict]) -> None:
    ws = wb.active
    ws.title = "Value bets"
    cols = ["Rank", "Date", "Time UTC", "League", "Home", "Away", "Market", "Selection",
            "Best book", "Best odds", "Model p", "Fair implied p", "Edge",
            "EV", "Suggested stake", "Notes"]
    ws.append(cols)
    _style_header(ws, len(cols))
    for p in picks:
        ws.append([
            p.get("rank"), p["date"], p.get("time_utc") or "", p["league"], p["home"],
            p["away"], MKT_LABEL[p["market"]], SEL_LABEL[p["selection"]], p["book"],
            p["odds"], p["p_model"], p["p_fair"], p["edge"], p["ev"], p["stake"],
            "; ".join(p.get("notes", [])),
        ])
        r = ws.max_row
        for c in range(1, len(cols) + 1):
            ws.cell(row=r, column=c).fill = VALUE_FILL
        for col, fmt in (("J", ODDS), ("K", PCT), ("L", PCT), ("M", PCT), ("N", PCT), ("O", STAKE)):
            ws[f"{col}{r}"].number_format = fmt
    if not picks:
        ws.append(["", "", "", "", "", "", "No value bets today - normal outcome, "
                   "do not lower the threshold to force action."])
    _widths(ws, [6, 11, 9, 22, 20, 20, 24, 12, 16, 9, 9, 13, 8, 8, 13, 55])


def _sheet_fixtures(wb: Workbook, fixtures: list[dict]) -> None:
    ws = wb.create_sheet("All fixtures")
    cols = ["League", "Date", "Match", "Model xG home", "Model xG away",
            "Home last-5 pts", "Away last-5 pts", "Market", "Selection",
            "Model p", "Fair implied p", "Best odds", "Best book", "Edge",
            "Value?", "Books quoted", "Avg book margin"]
    ws.append(cols)
    _style_header(ws, len(cols))
    for f in fixtures:
        fh = (f.get("form") or {}).get("home") or {}
        fa = (f.get("form") or {}).get("away") or {}
        for m in f["markets"]:
            s = m["summary"]
            for r in m["rows"]:
                ws.append([
                    f["league"], f["date"], f"{f['home']} v {f['away']}",
                    round(f["xg"][0], 2), round(f["xg"][1], 2),
                    fh.get("pts", ""), fa.get("pts", ""),
                    MKT_LABEL[m["market"]], SEL_LABEL[r["selection"]],
                    r["p_model"], r["p_fair"], r["odds"], r["book"], r["edge"],
                    "VALUE" if r["is_value"] else "",
                    s["n_books"], s["avg_margin"] if s["avg_margin"] is not None else None,
                ])
                row = ws.max_row
                for col, fmt in (("D", ODDS), ("E", ODDS), ("J", PCT), ("K", PCT),
                                 ("L", ODDS), ("N", PCT), ("Q", PCT)):
                    ws[f"{col}{row}"].number_format = fmt
                if r["is_value"]:
                    for c in range(1, len(cols) + 1):
                        ws.cell(row=row, column=c).fill = VALUE_FILL
    _widths(ws, [22, 11, 34, 12, 12, 13, 13, 24, 12, 9, 13, 9, 16, 8, 8, 12, 14])


def _sheet_summary(wb: Workbook, run: dict, picks: list[dict],
                   unmatched: list[str], demo: bool) -> None:
    ws = wb.create_sheet("Summary")
    rows = [
        ["Report date", run["date"]],
        ["Generated at", run["generated_at"]],
        ["Mode", "DEMO (offline, historical closing odds)" if demo else "live"],
        ["", ""],
        ["Fixtures analysed", run["n_fixtures"]],
        ["Value bets (edge >= %.0f%%)" % (100 * run["threshold"]), len(picks)],
        ["Total suggested stake", round(sum(p["stake"] for p in picks), 2)],
        ["Bankroll (config)", run["bankroll"]],
        ["Staking rule", "quarter Kelly, capped at 2% of bankroll"],
        ["Model", "Dixon-Coles (time-decayed) blended with Elo"],
        ["", ""],
        ["How value is defined", "edge = model p - consensus fair p (each book de-vigged, averaged); requires edge >= threshold AND model p x best price > 1"],
        ["Margins", "overround per market shown on the HTML report and 'All fixtures' sheet (avg across books)"],
        ["", ""],
        ["DISCLAIMER", "All outputs are probability estimates, not guarantees. Past backtests of this baseline did NOT beat closing prices - track live before staking real money."],
    ]
    for r in rows:
        ws.append(r)
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 110
    ws["A1"].font = Font(bold=True)
    for rr in (5, 6, 7):
        ws.cell(row=rr, column=1).font = Font(bold=True)
    ws.cell(row=14, column=1).font = Font(bold=True, color="B71C1C")
    ws.cell(row=14, column=2).alignment = Alignment(wrap_text=True)
    if demo:
        ws.append(["", ""])
        ws.append(["DEMO NOTICE", "This workbook was generated offline from real historical "
                                   "closing odds. Matches are already completed."])
        for c in ("A", "B"):
            ws[f"{c}{ws.max_row}"].fill = DEMO_FILL
    if unmatched:
        ws.append(["", ""])
        ws.append(["Skipped / unmatched", "; ".join(unmatched)][:2])
        ws.cell(row=ws.max_row, column=2).alignment = Alignment(wrap_text=True)


def build_xlsx(run_info: dict, picks: list[dict], fixtures: list[dict],
               unmatched: list[str], path: Path, demo: bool = False) -> Path:
    wb = Workbook()
    _sheet_value_bets(wb, picks)
    _sheet_fixtures(wb, fixtures)
    _sheet_summary(wb, run_info, picks, unmatched, demo)
    wb.save(path)
    return path
