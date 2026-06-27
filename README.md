# Soccer Prediction System

Automated daily soccer predictions with performance tracking.

## Features

- Home win predictions (9-point rule system)
- Over/Under 2.5 goals predictions
- Automatic win/loss tracking and yesterday's results in daily reports
- Weekly and monthly performance reports
- Email and Telegram delivery (free + VIP channels)

## Production files (actively used)

| File | Role |
|------|------|
| `over25_soccerbase.py` | Over/Under prediction engine (Soccerbase) |
| `home_win_soccerbase.py` | Home win prediction engine (Soccerbase) |
| `fetch_results.py` | Settle picks from manual + Soccerbase + APIs |
| `prediction_tracker.py` | History, yesterday summary, weekly/monthly stats |
| `generate_weekly_report.py` | Weekly performance report |
| `generate_monthly_report.py` | Monthly performance report |
| `backfill_history.py` | Dedupe history and fix dates from saved reports |
| `update_results.py` | Interactive manual result entry |
| `manual_results.csv` | Manual scores + auto-filled from fetch |
| `.github/workflows/run_daily.yml` | Full automation schedule |

Legacy / dev only (not in CI): `*_hybrid.py`, `over25_predictor.py`, `*_backup.py`, `debug_scraper.py`, `weekly_report.py`.

## Data sources

### Predictions (Soccerbase only)

Both engines scrape fixtures and team form exclusively from Soccerbase. No API fallback in production.

### Result settling (priority order)

When `fetch_results.py` runs, the first source to return a score for each match wins:

1. **Manual** — `manual_results.csv` / `manual_results.json` (intentional overrides)
2. **Soccerbase** — same site as predictions; reliable for yesterday's results
3. **Football-Data.org** — optional (`FOOTBALL_DATA_KEY` in GitHub Secrets)
4. **API-Football** — optional (`API_FOOTBALL_KEY` in GitHub Secrets)

```bash
python fetch_results.py              # yesterday only
python fetch_results.py --days 7     # last 7 days
python backfill_history.py           # dedupe + fix dates from saved reports, then refresh
```

## Free vs VIP

| | Free (email + public Telegram) | VIP (Telegram VIP channel) |
|---|-------------------------------|----------------------------|
| **Picks** | Top + Good only (perfect / qualified) | All picks including decent/close |
| **Per pick** | Team, date, confidence, model % | + Kelly stake %, xG, rule score, check breakdown |
| **Yesterday** | Win/loss summary for that market | Same |
| **Reports** | Weekly/monthly on both channels | Same |

**VIP advantages for subscribers:** more picks, stake sizing (Kelly), full model transparency (which rules passed/failed), and enough detail to audit every call.

Free channel stays useful as a teaser; VIP is the actionable, full-analysis product.

## Automation schedule (UTC)

| Time | Job | What runs |
|------|-----|-----------|
| 01:00 daily | `daily-predictions` | `fetch_results --days 3` → predictors → email/Telegram |
| 20:00 daily | `fetch-results` | `fetch_results --days 3` → commit history → Telegram result summary |
| Mon 03:00 | `weekly-report` | fetch 10 days → weekly report → Telegram |
| 1st 03:00 | `monthly-report` | fetch 35 days → monthly report → Telegram |

Manual dispatch: workflow supports `results`, `predictions`, `weekly`, `monthly`, or `all`.

## Manual results

```bash
python update_results.py
```

Or edit `manual_results.csv`: `date,home_team,away_team,score`

## Notes

- Educational purposes only. Gamble responsibly.
- Never bet more than you can afford to lose.
