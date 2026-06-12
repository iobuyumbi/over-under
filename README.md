# Soccer Prediction System
Automated daily soccer predictions with performance tracking for monetization.

## Features
- 🏠 Home win predictions with 9-point rule system
- 🔥 Over/Under 2.5 goals predictions
- 📊 Automatic performance tracking with win/loss history
- 📈 Monthly performance reports
- 💌 Daily email alerts
- 📱 Telegram notifications (Free and VIP channels)
- 💰 Affiliate-friendly design for monetization

## Quick Start

### 1. Daily Predictions (Automated)
Runs automatically every morning via GitHub Actions.

### 2. Updating Results
After matches finish, update results manually:

```bash
python update_results.py
```

This will show a menu:
1. Update pending results interactively
2. View overall history

### 3. Updating Results Manually
You can also edit `prediction_history.json` directly.
Change `"result": "pending"` to:
- `"win"` if prediction was correct
- `"loss"` if prediction was incorrect
- `"push"` for draws or exact 2 goals

## File Structure

| File | Description |
|------|-------------|
| home_win_soccerbase.py | Home win prediction engine |
| over25_soccerbase.py | Over/Under 2.5 goals prediction engine |
| prediction_tracker.py | History tracking system |
| update_results.py | User-friendly result updater |
| generate_monthly_report.py | Monthly performance report generator |
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
