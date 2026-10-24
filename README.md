# ⚽ soccer-value-bets

Daily pipeline that fetches real fixtures + multi-bookmaker odds, fits a
time-decayed **Dixon-Coles goals model** (blended with Elo) per league,
de-vigs the market, and flags **value bets** where the model probability
exceeds the de-vigged market probability by a configured edge (default 5%).
Every prediction is expressed as a probability. Nothing is guaranteed.

> **Honesty up front:** the out-of-the-box configuration did **NOT** beat
> closing-line market prices in two freely-run backtests on the completed
> 2025-26 season (see [Backtest results](#backtest-results-run-on-real-historical-odds)).
> This tool is a framework + measurement harness, not a money printer.

---

## 1. Quick start

```bash
cd soccer-value-bets
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env       # then edit .env and paste your API keys (see §2)

# 0) prove it to yourself offline first (no keys needed, real historical data):
python run_daily.py --demo E0        # builds a demo report from real closing odds
python backtest.py --league E0 --target 2526   # walk-forward on a completed season

# 1) live run for today:
python run_daily.py

# 2) next day, settle picks and see performance:
python settle.py
```

Outputs land in `reports/` (every run, including the scheduled morning run):
- `YYYY-MM-DD_report.html` - self-contained HTML report (all matches, all
  markets, odds, margins, model probabilities, flagged value bets with stakes)
- `YYYY-MM-DD_report.xlsx` - Excel workbook, 3 sheets: **Value bets** (ranked,
  with edge/EV/stake), **All fixtures** (every selection of every market:
  model p vs de-vigged fair p vs best price, margins, filter + freeze panes),
  **Summary** (run parameters, totals, disclaimers, skipped/unmatched names)
- `YYYY-MM-DD_picks.csv` - the flagged picks as flat data
- everything is also logged to `data/value_bets.db` (SQLite)

## 2. Where to add your API keys

Edit `.env` (created from `.env.example`):

| Variable | Get it at | Free tier | Used for |
|---|---|---|---|
| `ODDS_API_KEY` | https://the-odds-api.com/ (The Odds API) | ~500 requests/month | 1X2 (`h2h`) and Over/Under 2.5 (`totals`) across EU books (Pinnacle etc.) |
| `API_FOOTBALL_KEY` | https://dashboard.api-football.com/register | 100 requests/day, **seasons 2022-2024 only** | BTTS + Double Chance odds **(paid plan needed for the current season)**; fallback for results/settling |

**Quota math** (The Odds API charges `regions × markets` units per league request):
the 8 leagues enabled by default cost ~16 units/day ≈ 480/month — exactly the free
budget. Enable more leagues in `config.yaml` only if you also fetch less often,
drop a market, or pay. API-Football odds calls are **disk-cached per day** so
re-running the pipeline doesn't burn your 100/day.

### Betting with local books (Betway Zambia, Betpawa, 1xBet, …)

These aren't on either API. Two purposes get separated:

1. **Market consensus (benchmark):** API odds give the de-vigged "fair" line.
2. **Your actual prices:** paste them into `data/manual_odds.csv`
   (format documented in the file header). They are merged into the same
   best-price hunt and are what Kelly sizing uses when they're best.

## 3. How "value" is computed

For every selection in every market, per match:

```
overround (margin)  = Σ 1/odds − 1                  (shown per market in report)
fair prob per book  = (1/odds) / Σ(1/odds)          (proportional de-vig)
consensus fair      = mean of de-vigged probs across books
edge                = model_prob − consensus_fair
EV                  = model_prob × best_available_price − 1

VALUE iff  edge ≥ value_threshold (default 0.05)  AND  EV > 0

stake = min( ¼ × f_Kelly , 2% ) × bankroll     where f_Kelly = (b·p − (1−p))/b
```

Best price is searched across all bookmakers (API + your manual CSV).
Aggregate/exchange columns (Max/Avg/Bb*/Betfair Exchange) are excluded from
both consensus and best price (exchange odds aren't net of commission).

## 4. Daily automation

### GitHub Actions (your choice)
`.github/workflows/daily.yml` runs **06:30 CAT daily**:
1. Push this folder to a GitHub repo.
2. Repo → **Settings → Secrets and variables → Actions**: add `ODDS_API_KEY`
   and `API_FOOTBALL_KEY`.
3. The HTML + Excel + CSV reports land in `reports/` and are uploaded as
   workflow **artifacts** (30-day retention); the run also commits
   `data/value_bets.db` back to the repo so your bet history persists.

Note: GitHub hosted runners send traffic from EU/US IP ranges; both APIs
are fine with that. Cron times in GH Actions are UTC (06:30 CAT = 04:30 UTC).

### Cron alternative (own machine/VPS, stays online)
```cron
30 6 * * *  cd /home/you/soccer-value-bets && .venv/bin/python run_daily.py >> logs/daily.log 2>&1
0 12 * * *  cd /home/you/soccer-value-bets && .venv/bin/python settle.py >> logs/settle.log 2>&1
```

## 5. Teams / leagues

Edit `config.yaml → leagues`. Enabled by default (all have full free history
+ odds coverage): EPL, Championship, La Liga, Serie A, Bundesliga, Ligue 1,
Eredivisie, Primeira Liga. Eight more are listed ready to toggle on.
Run `python list_leagues.py` to print every soccer league The Odds API
currently offers (uses 1 free request).

Team-name mismatches between sources are handled in `src/teamnames.py`
(normalize → alias table → fuzzy match). Anything unmatchable is **listed in
the report's "Skipped" section — never guessed**.

## 6. Backtest results (run on real historical odds)

Walk-forward: refit every 14 days using only earlier matches
(Dixon-Coles + Elo blend, same value rules as live). Prices = real bookmaker
closing odds from football-data.co.uk. Markets tested: 1X2 and O/U 2.5
(the only ones with real free history).

| Season 2025-26 | League | Brier model vs market | Value bets | Hit rate | Kelly ROI | Flat ROI |
|---|---|---|---|---|---|---|
| EPL | 380 matches | 0.6183 vs **0.6078** (market better) | 415 | 39.0% | **−4.6%** | −4.5% |
| Bundesliga | 306 matches | 0.5825 vs **0.5622** (market better) | 323 | 44.3% | **−9.4%** | −9.6% |

Threshold sensitivity (EPL 25-26):

| min edge | bets | flat ROI |
|---|---|---|
| 5% | 415 | −4.5% |
| 8% | 233 | −4.2% |
| 12% | 102 | **+2.2%** |

Reading: flagged edges below ~10% were mostly **noise**; the biggest edges
landed near breakeven in *one* season (n=102 — could easily be luck).
Calibration also shows the model is systematically **overconfident** in the
40-80% probability range (predicted > actual). Reproduce/extend anytime:

```bash
python backtest.py --league SP1 --target 2526 --train 2 --refit-days 14
python backtest.py --league E0  --target 2425  --min-edge 0.12
```

Treat this system as a measurement tool: log picks for weeks, compare against
`settle.py` calibration output, and only scale up if *your* live tracked record
(not one backtest) supports it. Protective moves to consider: raise
`value_threshold` to 0.10-0.12, require minimum 3 bookmakers, or restrict to 1X2.

## 7. File map

```
config.yaml              <- leagues, threshold, bankroll, Kelly settings
run_daily.py             <- the daily pipeline (fetch -> model -> value -> report)
backtest.py              <- walk-forward backtester on past seasons
settle.py                <- grade settled picks; hit rate / ROI / calibration
list_leagues.py          <- print all soccer leagues on The Odds API
src/
  settings.py            <- config + .env loading, season codes
  historical.py          <- football-data.co.uk download + odds parsing
  dixon_coles.py         <- the goals model (fit, score matrix, markets)
  elo.py                 <- margin-aware Elo (blend partner)
  model.py               <- fit orchestration, promoted-team heuristic, form
  odds_api.py            <- The Odds API v4 client (1X2, O/U 2.5)
  api_football.py        <- API-Football client (BTTS, Double Chance, results)
  teamnames.py           <- name normalization + alias/fuzzy matching
  value.py               <- overround, de-vig, edge, Kelly, bet grading
  db.py                  <- SQLite logging of quotes/predictions/picks
  report.py              <- HTML + CSV report generation
  xlsx_report.py         <- Excel workbook report (3 sheets)
data/manual_odds.csv     <- YOUR local bookmakers' prices
.github/workflows/daily.yml          <- 06:30 CAT automatic run
.github/workflows/list_leagues.yml   <- manual "List soccer leagues" run
```

## 8. Known limitations (read these)

1. **The baseline model did not beat closing prices in backtests** (see §6).
   Any profit would need better inputs (player availability, xG data, sharper
   blending) than free data provides.
2. **Single-season evidence.** One season ≈ a few hundred bets; ±5% ROI swings
   are normal luck either way. Never conclude from n < ~1000.
3. **Proportional de-vig** ignores favourite-longshot bias; "fair" probabilities
   are slightly wrong, mostly at longshot prices (Shin/power methods unimplemented).
4. **Closing odds ≈ bettable.** Historical backtests use closing prices; in
   real life you might not always get that number seconds before kickoff.
5. **No BTTS / Double-Chance historical prices** exist free, so those markets
   are live-only and cannot be honestly backtested here.
6. **Promoted/new teams** are estimated by heuristic (mean of the 6 weakest
   fitted teams) and are always flagged "limited history" in the report.
   Early-season ratings on them are the least reliable output of the system.
7. **No team news**: injuries, rotation, motivation, derby effects, cup
   congestion, weather - none of it is in the model.
8. **API constraints**: The Odds API has no BTTS/Double-Chance markets;
   API-Football's free tier limits daily volume and some books; free-tier
   bookmaker rosters change over time (run `list_leagues.py` / check your
   dashboard). Quotas are read from response headers and printed each run.
9. **Local-book odds are manual** - stale pasted prices will produce phantom
   "value". Paste them the same morning you bet.
10. **Kelly assumes the probability is right.** Given limitation 1-3, treat the
    stake column as an upper bound, and the 2% cap as the real protection.
11. **Correlation between selections in the same match** is modelled coherently
    (all markets come from one scoreline matrix), but correlation *across your
    bankroll* when you hold multiple simultaneous bets is not managed beyond
    the per-bet cap.
12. **Live-run lesson (first real run, 24 Oct 2026): exchange feeds quote
    phantom prices.** EU-region exchange entries (Betfair/Matchbook-type)
    contained absurd offers (e.g. a home win at 55.0, a draw at 21.0) that
    simulate as huge "value" but are lay offers / stale feed artefacts - they
    are NOT bettable bookmaker prices. These books are now excluded by default
    (`odds_api.exclude_books`) and any single-book price >2x the market median
    for a selection is auto-dropped (`price_sanity_factor`), with a note in the
    report whenever that happens.
13. **API-Football free plan covers only seasons 2022-2024** (verified
    24 Oct 2026). On free, it cannot supply CURRENT-season BTTS / Double
    Chance odds or fixtures - those markets then come only from your
    `manual_odds.csv`, or enable `api_football.use_odds` on a paid plan.
    Settlement still works on free via football-data.co.uk for the enabled
    leagues. Also: `soccer_efl_championship` 404'd on 24 Oct 2026 (key renamed
    upstream) - run the manual GitHub Action **"List soccer leagues"** to print
    the current sport key, paste it into config.yaml, re-enable the league.
14. This is **not financial advice**. Only stake money you can afford to lose;
    betting laws and taxes are your responsibility (Zambia: licensed operators
    and 18+ rules apply).

---

## Fixture verification & trust system (added 2026-10-24)

Every prediction now passes a **verify-before-publish** gate (`src/verification.py`):

1. Provider fixture record required (stable `provider:event_id`; The Odds API IDs + manual/demo ids).
2. Checks: teams present & distinct, kickoff parseable & not started, non-terminal status
   (completed/cancelled/postponed/abandoned/removed are blocked), >=1 real 1X2 price,
   team-mapping sanity (a team can't play twice in one day).
3. **Duplicate control**: fixtures are keyed `date|home|away` + provider; DB uses
   UNIQUE constraints + upserts (idempotent pipeline reruns).
4. **Second independent source**: API-Football fixture lists are consulted when the plan
   allows. Free plan = seasons 2022-2024 only, so on the free tier every fixture notes
   "second source unavailable" instead of pretending. A disagreement = "requires review".
5. **Data completeness score (0-100%)**: books priced, model history present, last-5 form
   available, second-source confirmation, extra markets, feed freshness. Predictions are
   withheld below `min_completeness` (default 60%) with the message
   "Insufficient data for a reliable prediction."
6. **Freshness guard**: fixtures that disappear from the feed get status `removed`
   (postponed/cancelled proxy - the free feeds don't send an explicit signal, so removal
   from the odds feed + scores feed = our honest detection) and appear in admin warnings.
   Live/completed matches (The Odds API /scores, free) are never presented as upcoming.
7. **Prediction snapshots** (`predictions_v2`): every published probability is stored with
   fixture id, model version (`dc-elo-1.1@<git sha>`), sources, data completeness,
   confidence tier, UTC generation time and the stats used (xG, Elo, last-5 form, books).
   Snapshots are INSERT-only/settle-only job - historical inputs are never edited.
8. **Confidence tiers** are a published rule, not an accuracy claim:
   high = completeness>=90 + DC/Elo agreement + >=6 books; moderate = >=70 + >=3 books;
   else low. Whether the tiers MEAN anything is measured on results.html (calibration).
9. Timestamps stored in UTC; display in `timezone` from config (Africa/Johannesburg,
   DST-safe via zoneinfo; SAST has no DST).

### Admin & ops

- **admin.html** on the site: fixtures by status, verified counts, sync log with errors
  and quota, stale-data warnings, prediction counts by confidence, settled performance
  by market and league.
- Admin ACTIONS happen behind GitHub's own authentication (the real boundary on a static
  host): Actions -> "Daily value-bet report" -> Run workflow (fresh refresh), or tick
  **suspend_predictions** to publish no picks while data sources look unreliable.

### Automated tests (tests/, CI on every push)

`python -m pytest tests/ -q` - 15 tests, MOCK DATA ONLY: probability validity, de-vig,
insane-price guard, all verification gates (invalid teams, duplicates, conflicts,
postponed/completed, insufficient data), bet grading incl. voids, snapshot immutability,
unique constraints, timezone conversion, secret-leak scan.

### Honest gaps (explicitly NOT claimed)

- Second-source verification is **informational only on free tiers** (API-Football plan
  walls the current season); fixtures are verified by single-provider integrity rules.
- Postponed/cancelled fixtures are detected as *vanished-from-feed*, not via an official
  status field (both free providers lack one): treated as `removed` = predictions suppressed, review flagged.
- No injuries/xG/external team news exists in the free inputs; confidence reflects
  data completeness + model agreement only.
- In-play predictions are not built; live fixtures are labelled and excluded from pre-match stats.
