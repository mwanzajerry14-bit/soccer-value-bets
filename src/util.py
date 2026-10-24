"""Shared helpers: HTTP with retry, canonical match keys, misc."""
from __future__ import annotations

import json
import pathlib
import time

import requests

from .teamnames import canonical

UA = {"User-Agent": "Mozilla/5.0 (value-bets research; contact: local)"}


def http_get(url: str, params: dict | None = None, headers: dict | None = None,
             retries: int = 3, timeout: int = 30) -> requests.Response:
    hdrs = {**UA, **(headers or {})}
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=hdrs, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                last = r
                time.sleep(2 * (i + 1))
                continue
            return r
        except requests.RequestException as e:  # network hiccup
            last = e
            time.sleep(2 * (i + 1))
    if isinstance(last, requests.Response):
        return last
    raise RuntimeError(f"GET failed {url}: {last}")


def match_key(date_str: str, home: str, away: str) -> str:
    return f"{date_str}|{canonical(home)}|{canonical(away)}"


def rolling_window_dates(today: "date", days: int) -> list[str]:
    """Rolling N-calendar-day window, dynamically computed - never hardcoded
    (task A). `today` must already be the local date in the display timezone
    (Africa/Lusaka)."""
    from datetime import timedelta
    return [(today + timedelta(days=i)).isoformat() for i in range(days)]


def cache_path(cache_dir: pathlib.Path, name: str) -> pathlib.Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / name


def read_json(p: pathlib.Path):
    return json.loads(p.read_text()) if p.exists() else None


def write_json(p: pathlib.Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))


def fmt_pct(x: float, nd: int = 1) -> str:
    return f"{100 * x:.{nd}f}%"
