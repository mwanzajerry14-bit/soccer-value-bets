#!/usr/bin/env python3
"""Build the additive tracked-league Over 2.5 / Over 5.5 analysis page.

This command never writes to the main prediction/pick tables.  If it fails the
workflow leaves the already-published analysis.html in place.
"""
from __future__ import annotations

import json
import sys

from src import db
from src.analysis_core import run
from src.analysis_report import build_html
from src.settings import REPORTS, ROOT, load_config, load_env


def main() -> int:
    load_env()
    with db.conn() as c:
        result = run(c, load_config())
    html = build_html(result)
    (ROOT / "analysis.html").write_text(html, encoding="utf-8")
    (REPORTS / f"{result['run_date']}_analysis.html").write_text(html, encoding="utf-8")
    slim = {key: result[key] for key in ("run_id", "run_date", "generated_utc", "version", "window", "counts", "notes", "metrics")}
    (ROOT / "data" / "analysis_latest.json").write_text(json.dumps(slim, indent=1), encoding="utf-8")
    counts = result["counts"]
    print(f"analysis {result['run_date']}: {counts['analysed']} analysed / {counts['fixtures_discovered']} fixtures; "
          f"O2.5 {counts['ou25_candidates']}, O5.5 {counts['ou55_candidates']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
