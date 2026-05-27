# Daily 25 Goals Prediction

A fully automated, GitHub Actions-powered Over 2.5 and Under 2.5 Goals prediction system using Soccerbase.

## Features

- 10-point algorithm for Over 2.5 Goals:
  - H1: Last 3 home matches ≥7 total goals
  - H2: Last 3 home matches ≥2 Over 2.5
  - H3: Last 6 home matches ≥4 Over 2.5
  - H4: Last 6 home matches ≥18 total goals
  - A1: Last 3 away matches ≥7 total goals
  - A2: Last away match ≥2 goals
  - A3: Last 3 away matches ≥2 scored in
  - A4: Last 3 away matches ≥2 Over 2.5
  - A5: Last 6 away matches ≥4 Over 2.5
  - A6: Last 6 away matches ≥18 total goals

- Sections:
  - Perfect Matches (10/10 checks, all applicable conditions hit perfectly)
  - Qualified Matches (10/10 checks)
  - Close Calls (9/10 checks)
  - Under 2.5 (0/10 checks)
  - Under 2.5 (1/10 checks)

## Local Usage

### Prerequisites

1. Install Python 3.10+
2. Create a virtual environment:
   ```bash
   python -m venv venv
   ```
3. Activate it:
   - Windows: `.\venv\Scripts\activate`
   - Linux/Mac: `source venv/bin/activate`
4. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### Run locally

```bash
# Today's date (all matches: scheduled + completed)
python over25_soccerbase.py

# Specific date
python over25_soccerbase.py 2026-05-25

# Only scheduled matches
python over25_soccerbase.py 2026-05-25 --scheduled

# Save JSON output to custom path
python over25_soccerbase.py 2026-05-25 --json-out output.json
```

## GitHub Actions Setup

### 1. Push the repo to GitHub
Make sure all files are committed and pushed to your repo.

### 2. Set up GitHub Secrets for Email Notifications
Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret** and add these secrets:
1. `EMAIL_USERNAME`: Your Gmail address (e.g., "you@gmail.com")
2. `EMAIL_PASSWORD`: A **Gmail App Password** (NOT your regular password! [Learn how to create one here](https://support.google.com/accounts/answer/185833))
3. `TO_EMAILS`: Email addresses to send to, **comma-separated** (e.g., "user1@example.com,user2@example.com,user3@example.com")

### 3. Manual trigger from GitHub
Go to your repo → **Actions** → **Daily Over/Under Goals Predictor** → **Run workflow**

## Files

- `over25_soccerbase.py`: Main prediction script with stats on separate lines (used in GitHub Actions)
- `soccerbase_predictor.py`: Same prediction script in original format
- `requirements.txt`: Dependencies list
- `.github/workflows/run_daily.yml`: GitHub Actions workflow for daily automation

## License
MIT
