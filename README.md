# Soccer Prediction System
Automated daily soccer predictions with performance tracking for monetization.

## Features
- 🏠 Home win predictions with rule system, H2H bogey detection, and Kelly staking
- 🔥 Over/Under 2.5 goals predictions with Poisson modeling and Dixon-Coles correction
- 🎯 BTTS (Both Teams To Score) Yes / No predictions
- 🧠 Head-to-Head (H2H) vetoes across all three markets to catch opponent-specific bogeys missed by general form
- 🚦 Two-tier league filtering:
  - **Static blocks** (integrity/region) via `blacklist.json` — fully skipped
  - **Statistical blocks** (weak ROI, poor win-rate) — tracked as `published=false` so ROI data builds and they auto-unblock when improving
- 📊 Automatic performance tracking with win/loss history and rolling auto-block statistics
- 📈 Monthly/weekly performance reports
- 💌 Daily email alerts
- 📱 Telegram notifications (Free and VIP channels)
- 📋 Auto-populated `manual_results.csv` (all recorded fixtures, including shadow picks, await scores)
- 💰 Affiliate-friendly design for monetization

## Quick Start

### 1. Daily Predictions (Automated)
Runs automatically every morning via GitHub Actions.

### 2. Updating Results
After matches finish, update results using the automated fetcher:

```bash
python fetch_results.py
```

This will try multiple data sources in order to get match results:
1. Football-Data.org (primary, free tier covers Chile, Argentina, World Cup)
2. API-Football (fallback)
3. Manual override (CSV/JSON)

You can also use manual entry directly:
```bash
python update_results.py
```

### 3. Auto-populate manual entry sheet
Predictions are auto-added to `manual_results.csv` each time a predictor runs, including shadow picks from statistically-blocked leagues. To back-fill history entries into the CSV:

```bash
python update_manual_results.py
```

### 4. Manual Results Entry
If APIs don't cover your matches, add results directly to `manual_results.csv` (columns: `date,home_team,away_team,score`):

```csv
date,home_team,away_team,score
2026-06-15,Team A,Team B,2-1
```

Or create `manual_results.json` with structure:
  ```json
  {
    "results": [
      {
        "date": "2026-06-15",
        "home_team": "Team A",
        "away_team": "Team B",
        "score": "2-1"
      }
    ]
  }
  ```

You can also edit `prediction_history.json` directly.
Change `"result": "pending"` to:
- `"win"` if prediction was correct
- `"loss"` if prediction was incorrect
- `"push"` for draws or exact 2 goals

## Data Sources

### API Keys (Optional but Recommended)
Set these environment variables in GitHub Secrets for better reliability:

- `FOOTBALL_DATA_KEY`: Get free from https://www.football-data.org/client/register
- `API_FOOTBALL_KEY`: Get from https://www.api-football.com/

## File Structure

| File | Description |
|------|-------------|
| home_win_soccerbase.py | Home win prediction engine (rules + H2H vetoes + Kelly) |
| over25_soccerbase.py | Over/Under 2.5 goals prediction engine (Poisson + Dixon-Coles) |
| btts_soccerbase.py | BTTS Yes / No prediction engine |
| prediction_tracker.py | History tracking system, auto-block stats, report builders, `record_predictions()` with `published` flag |
| fetch_results.py | Automated result fetcher with multiple data sources |
| update_results.py | User-friendly result updater |
| update_manual_results.py | Auto-populate `manual_results.csv` from `prediction_history.json` |
| generate_monthly_report.py | Monthly performance report generator |
| generate_weekly_report.py | Weekly performance report generator |
| blacklist.json | Static / integrity region/team blocklist (optional - empty = auto-only mode) |
| prediction_history.json | Full history store: home_win / over_under / btts picks with `result`, `final_score`, and `published: false` for shadow (stat-blocked) picks |
| manual_results.csv | Auto-populated CSV - add `score` values for leagues not covered by free APIs |
| .github/workflows/run_daily.yml | GitHub Actions workflow |

## Monetization Ideas
1. **Affiliate Marketing**: Promote bookmaker affiliate links in your reports
2. **Freemium Model**: Offer free daily picks and detailed VIP picks with stake recommendations
3. **Sponsored Posts**: Work directly with bookmakers once you have an audience
4. **Subscription Service**: Charge monthly for access to your VIP channel

## Notes
- This is for educational purposes only
- Gambling has risks, gamble responsibly
- Never bet more than you can afford to lose

## System Architecture

### Two-Tier League Filtering
Leagues are evaluated across two independent filter layers. A pick must pass **only the static filter** to be processed and tracked.

| Layer | Source | Effect |
|-------|--------|--------|
| **Static / Integrity block** | `blacklist.json` → `BLOCKED_REGIONS` | Fixture fully skipped. Not processed, not tracked, not in CSV. Used for region / ethical exclusions. |
| **Statistical auto-block** | Rolling 120-day win-rate from `prediction_history.json` | Pick is **processed and recorded** with `"published": false` (a "shadow entry"). Result is counted toward future ROI stats so the league can auto-unblock. Never appears in user-facing reports. |

### `published` Flag Lifecycle
Each entry in `prediction_history.json` stores `"published": true | false`:
- **`published: true`** → Pick appears in Telegram / email reports and yesterday results summaries.
- **`published: false`** → Tracked for ROI calculations only. Invisible in public reports. Never inflates the visible record.

### Data-Driven Feedback Loop
The auto-unblock cycle runs as follows (code in `prediction_tracker.py`):

1. **Predict phase**: Each predictor skips only static blocks. Qualifying picks from statistically-blocked leagues are collected normally.
2. **Record phase**: `record_predictions()` calls `is_statistical_block_only()` → sets `"published": false` for statistical-only blocks.
3. **Result phase**: Results are populated via APIs or `manual_results.csv` regardless of `published` flag.
4. **Stats phase**: `_compute_poor_league_tables()` reads ALL settled picks (no `published` filter) to compute per-market bucket win-rates.
5. **Auto-block gates**:
   - General poor leagues: `≥10 decided` with win-rate `<50%` → blocked (or low-sample: `≥3 decided` with win-rate `<35%`)
   - Weak-ROI buckets (Allsvenskan, Ireland, K-League, MLS, etc.): blocked until `≥2 decided` AND win-rate `≥60%`
6. **Repeat**: Once a weak league hits the allow-threshold, new picks automatically flow through as `published: true`.

### H2H Veto Rules
Applied across all three markets. H2H is capped at 6 most-recent meetings between the specific pair.

| Market | Threshold | Rule |
|--------|-----------|------|
| Home Win | ≥2 meetings home vs away | Veto if away wins ≥ 2 **AND** away wins ≥ 2.0 × max(1, home wins) |
| Over 2.5 | ≥3 H2H meetings | Veto Over if ≤33% went Over 2.5 |
| Under 2.5 | ≥3 H2H meetings | Veto Under if ≥67% went Over 2.5 |
| BTTS Yes / No | ≥3 H2H meetings | Symmetric 33% / 67% veto on each side |

### Goal Market Gate Proportional Fallback (Thin Data)
New seasons often have fewer than 6 venue form matches. All gates fall back proportionally:
- 6 matches full data → standard thresholds (e.g. 3/6 Over, 3/6 BTTS)
- <6 available → scale to ≥50–67% of available (capped at minimum 2 matches to pass)
- Min form `len` guards are relaxed at season start to maximize fixture volume.

## 📊 Weekly Performance Report

Regenerate fresh reports at any time (reads live from `prediction_history.json`):

```bash
python generate_weekly_report.py
python generate_monthly_report.py
```

**Last Snapshot:** 2026-08-03 06:29

### Over 2.5 Goals

| Week | Matches | Wins | Losses | Win Rate |
|------|---------|------|--------|----------|
| 2026-W30 | 54 | 28 | 26 | 51.9% |
| 2026-W29 | 41 | 27 | 14 | 65.9% |
| 2026-W28 | 13 | 9 | 4 | 69.2% |
| 2026-W27 | 10 | 4 | 6 | 40.0% |
| 2026-W26 | 23 | 13 | 10 | 56.5% |
| 2026-W25 | 8 | 6 | 2 | 75.0% |

### Home Win

| Week | Matches | Wins | Losses | Win Rate |
|------|---------|------|--------|----------|
| 2026-W30 | 18 | 13 | 5 | 72.2% |
| 2026-W29 | 20 | 12 | 8 | 60.0% |
| 2026-W28 | 6 | 4 | 2 | 66.7% |
| 2026-W27 | 4 | 2 | 2 | 50.0% |
| 2026-W26 | 10 | 6 | 4 | 60.0% |
| 2026-W25 | 3 | 3 | 0 | 100.0% |

### BTTS (Yes + No)

Pending — data builds as `btts_soccerbase.py` runs. Generate fresh tables via `generate_weekly_report.py` once settled picks accumulate.
