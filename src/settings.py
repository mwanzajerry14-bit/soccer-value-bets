"""Configuration loading: config.yaml + .env (API keys)."""
from __future__ import annotations

import os
import pathlib
from datetime import date

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HIST = DATA / "historical"
CACHE = DATA / "cache"
REPORTS = ROOT / "reports"
DB_PATH = DATA / "value_bets.db"

for d in (DATA, HIST, CACHE, REPORTS):
    d.mkdir(parents=True, exist_ok=True)


def load_env(path: pathlib.Path | None = None) -> None:
    """Minimal .env loader (no external dependency)."""
    p = path or ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: pathlib.Path | None = None) -> dict:
    p = path or ROOT / "config.yaml"
    cfg = yaml.safe_load(p.read_text())
    cfg.setdefault("value_threshold", 0.05)
    cfg.setdefault("kelly_fraction", 0.25)
    cfg.setdefault("max_stake_pct", 0.02)
    cfg.setdefault("bankroll", 1000.0)
    cfg.setdefault("timezone", "Africa/Johannesburg")
    return cfg


def api_key(name: str, required: bool = True) -> str | None:
    load_env()
    key = os.environ.get(name, "").strip()
    if required and (not key or key.startswith("put_your_")):
        raise SystemExit(
            f"\nERROR: {name} is not set.\n"
            f"  1) cp .env.example .env\n"
            f"  2) edit .env and paste your key for {name}\n"
            f"  See README.md section 'API keys'.\n"
        )
    return key or None


def current_season_code(d: date | None = None) -> str:
    """football-data season code, e.g. 2026-07..2027-06 -> '2627'."""
    d = d or date.today()
    start = d.year if d.month >= 7 else d.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def af_season_year(d: date | None = None) -> int:
    """API-Football season year (= year the season starts)."""
    d = d or date.today()
    return d.year if d.month >= 7 else d.year - 1
