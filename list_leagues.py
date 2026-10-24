#!/usr/bin/env python3
"""Utility: list all soccer leagues currently offered by The Odds API.

    python list_leagues.py

Use the printed sport keys in config.yaml (`odds_api:` field).
"""
from src.odds_api import OddsApi
from src.settings import api_key, load_env

load_env()
api = OddsApi(api_key("ODDS_API_KEY"))
for s in api.list_soccer_leagues():
    flag = "" if s.get("active") else "  (inactive)"
    print(f"{s['key']:45s} {s['title']}{flag}")
print(f"\nrequests remaining: {api.last_remaining}")
