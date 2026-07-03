# Soccer Prediction System
Automated daily soccer predictions with performance tracking for monetization.

## Features
- 🏠 Home win predictions with 9-point rule system
- 🔥 Over/Under 2.5 goals predictions
- 📊 Automatic performance tracking with win/loss history
- 📈 Monthly/weekly performance reports
- 💌 Daily email alerts
- 📱 Telegram notifications (Free and VIP channels)
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

### 3. Manual Results Entry
If APIs don't cover your matches, you can add results manually:
- Create `manual_results.csv` with columns: `date,home_team,away_team,score`
- Or create `manual_results.json` with structure:
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
| home_win_soccerbase.py | Home win prediction engine |
| over25_soccerbase.py | Over/Under 2.5 goals prediction engine |
| fetch_results.py | Automated result fetcher with multiple data sources |
| prediction_tracker.py | History tracking system |
| update_results.py | User-friendly result updater |
| generate_monthly_report.py | Monthly performance report generator |
| generate_weekly_report.py | Weekly performance report generator |
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

## 📊 Weekly Performance Report

**Last Updated:** 2026-06-26 21:37

### Over 2.5 Goals

| Week | Matches | Wins | Losses | Win Rate |
|------|---------|------|--------|----------|

### Home Win

| Week | Matches | Wins | Losses | Win Rate |
|------|---------|------|--------|----------|


## Weekly Performance Report

**Last Updated:** 2026-07-03 03:22

### Over 2.5 Goals

| Week | Matches | Wins | Losses | Win Rate |
|------|---------|------|--------|----------|
| 2026-W26 | 3 | 2 | 1 | 66.7% |
| 2026-W25 | 8 | 6 | 2 | 75.0% |
| 2026-W24 | 2 | 2 | 0 | 100.0% |
| 2026-W23 | 13 | 8 | 5 | 61.5% |

### Home Win

| Week | Matches | Wins | Losses | Win Rate |
|------|---------|------|--------|----------|
| 2026-W26 | 2 | 2 | 0 | 100.0% |
| 2026-W25 | 3 | 3 | 0 | 100.0% |
| 2026-W24 | 1 | 1 | 0 | 100.0% |
| 2026-W23 | 4 | 3 | 0 | 75.0% |
| 2026-W22 | 1 | 0 | 0 | 0.0% |


**Last Updated:** 2026-07-03 04:26

### Over 2.5 Goals

| Week | Matches | Wins | Losses | Win Rate |
|------|---------|------|--------|----------|
| 2026-W25 | 5 | 4 | 1 | 80.0% |
| 2026-W24 | 2 | 2 | 0 | 100.0% |
| 2026-W23 | 13 | 8 | 5 | 61.5% |

### Home Win

| Week | Matches | Wins | Losses | Win Rate |
|------|---------|------|--------|----------|
| 2026-W25 | 2 | 2 | 0 | 100.0% |
| 2026-W24 | 1 | 1 | 0 | 100.0% |
| 2026-W23 | 4 | 3 | 0 | 75.0% |
