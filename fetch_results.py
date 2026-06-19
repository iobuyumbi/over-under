#!/usr/bin/env python3
"""
Automatic Result Fetcher
Fetches yesterday's match results from Soccerbase and updates prediction history.
"""

import requests
import json
import logging
import sqlite3
import re
import hashlib
import time
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from fake_useragent import UserAgent

from prediction_tracker import load_history, save_history

# =============================================================================
# CONFIGURATION
# =============================================================================
CACHE_DB = "soccerbase_results_cache.db"
CACHE_TTL_HOURS = 24

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =============================================================================
# CACHE
# =============================================================================
def get_cache(key):
    try:
        conn = sqlite3.connect(CACHE_DB)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS cache
                     (key TEXT PRIMARY KEY, data TEXT, timestamp REAL)''')
        c.execute('SELECT data, timestamp FROM cache WHERE key = ?', (key,))
        row = c.fetchone()
        conn.close()
        
        if row and (time.time() - row[1] < CACHE_TTL_HOURS * 3600):
            return json.loads(row[0])
    except Exception as e:
        logger.warning(f"Cache read error: {e}")
    return None

def set_cache(key, data):
    try:
        conn = sqlite3.connect(CACHE_DB)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS cache
                     (key TEXT PRIMARY KEY, data TEXT, timestamp REAL)''')
        c.execute('REPLACE INTO cache VALUES (?, ?, ?)',
                  (key, json.dumps(data), time.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Cache write error: {e}")

# =============================================================================
# HTTP CLIENT
# =============================================================================
def get_session():
    session = requests.Session()
    retry = Retry(connect=3, backoff_factor=1)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({
        'User-Agent': UserAgent().random,
        'Accept-Language': 'en-US,en;q=0.9'
    })
    return session

# =============================================================================
# FETCH RESULTS
# =============================================================================
def fetch_match_results(date_str):
    """Fetch match results for a specific date (YYYY-MM-DD) from Soccerbase."""
    logger.info(f"Fetching results for {date_str}...")
    
    cache_key = f"results_{date_str}"
    cached = get_cache(cache_key)
    if cached:
        logger.info(f"Using cached results for {date_str}")
        return cached
    
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_param = dt.strftime("%d/%m/%Y")
        
        url = f"https://www.soccerbase.com/matches/results.sd?date={date_param}"
        session = get_session()
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        matches = []
        
        # Find all match rows
        match_rows = soup.select('tbody tr, tr.match')
        
        for row in match_rows:
            try:
                # Extract match info
                cells = row.find_all('td')
                if len(cells) < 4:
                    continue
                
                # Try to get home and away teams
                home_team = None
                away_team = None
                score = None
                
                for cell in cells:
                    text = cell.get_text(strip=True)
                    # Look for score pattern (e.g., 2-1)
                    if re.match(r'^\d+-\d+$', text):
                        score = text
                    # Try to identify team cells
                    elif text and not re.match(r'^\d+$', text) and 'FT' not in text:
                        if not home_team:
                            home_team = text
                        elif not away_team:
                            away_team = text
                
                if home_team and away_team and score:
                    matches.append({
                        'home_team': normalize_team_name(home_team),
                        'away_team': normalize_team_name(away_team),
                        'score': score
                    })
            except Exception as e:
                logger.debug(f"Error parsing match row: {e}")
                continue
        
        if matches:
            logger.info(f"Found {len(matches)} match results for {date_str}")
            set_cache(cache_key, matches)
        
        return matches
        
    except Exception as e:
        logger.error(f"Error fetching results for {date_str}: {e}")
        return []


def fetch_fixtures_for_date(date_str):
    """Return home/away pairs for all matches listed on Soccerbase for a date."""
    logger.info(f"Fetching fixtures for {date_str}...")

    cache_key = f"fixtures_{date_str}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_param = dt.strftime("%d/%m/%Y")
        url = f"https://www.soccerbase.com/matches/results.sd?date={date_param}"
        session = get_session()
        resp = session.get(url, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        matches = []

        for table in soup.find_all("table", class_="listWithCards"):
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 6:
                    continue

                home_raw = cells[3].get_text(strip=True)
                away_raw = cells[5].get_text(strip=True)
                home = re.sub(r"\s*\d+.*$", "", home_raw).strip()
                away = re.sub(r"\s*\d+.*$", "", away_raw).strip()
                if not home or not away:
                    continue

                matches.append({
                    "home_team": home,
                    "away_team": away,
                })

        set_cache(cache_key, matches)
        return matches
    except Exception as e:
        logger.error(f"Error fetching fixtures for {date_str}: {e}")
        return []


def find_match_date(home_team, away_team, start_date, end_date):
    """Find the calendar date for a fixture by scanning Soccerbase day pages."""
    home_norm = normalize_team_name(home_team)
    away_norm = normalize_team_name(away_team)
    current = start_date

    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        for match in fetch_fixtures_for_date(date_str):
            if (
                normalize_team_name(match["home_team"]) == home_norm
                and normalize_team_name(match["away_team"]) == away_norm
            ):
                return date_str
        current += timedelta(days=1)

    return None

def normalize_team_name(name):
    """Normalize team name for better matching."""
    name = name.strip().lower()
    # Remove common suffixes
    name = re.sub(r' fc$', '', name)
    name = re.sub(r' cf$', '', name)
    name = re.sub(r' city$', '', name)
    name = re.sub(r' united$', '', name)
    name = re.sub(r' athletic$', '', name)
    name = re.sub(r' afc$', '', name)
    return name.strip()

def parse_score(score_str):
    """Parse a score like "2-1" into home_goals, away_goals."""
    if not score_str or '-' not in score_str:
        return None, None
    
    try:
        parts = score_str.split('-')
        home_goals = int(parts[0])
        away_goals = int(parts[1])
        return home_goals, away_goals
    except Exception as e:
        logger.debug(f"Error parsing score {score_str}: {e}")
        return None, None

# =============================================================================
# UPDATE HISTORY
# =============================================================================
def update_history_with_results(date_str):
    """Fetch results for a date and update pending predictions."""
    logger.info(f"Updating history for {date_str}...")
    
    # Get results from Soccerbase
    results = fetch_match_results(date_str)
    
    if not results:
        logger.warning(f"No results found for {date_str}")
        return
    
    # Load our prediction history
    history = load_history()
    updated = 0
    
    # Check Home Win predictions
    for idx, pick in enumerate(history['home_win']):
        if pick['result'] == 'pending' and pick['date'] == date_str:
            result = determine_home_win_result(pick, results)
            if result:
                history['home_win'][idx]['result'] = result
                updated += 1
                logger.info(f"Updated Home Win: {pick['home_team']} vs {pick['away_team']} = {result}")
    
    # Check Over/Under predictions
    for idx, pick in enumerate(history['over_under']):
        if pick['result'] == 'pending' and pick['date'] == date_str:
            result = determine_over_under_result(pick, results)
            if result:
                history['over_under'][idx]['result'] = result
                updated += 1
                logger.info(f"Updated Over/Under: {pick['home_team']} vs {pick['away_team']} = {result}")
    
    # Save updated history
    if updated > 0:
        save_history(history)
        logger.info(f"Updated {updated} predictions with results!")
    else:
        logger.info("No pending predictions matched the results.")
    
    return updated

def determine_home_win_result(pick, results):
    """Determine if a home win prediction was correct."""
    home_team_norm = normalize_team_name(pick['home_team'])
    away_team_norm = normalize_team_name(pick['away_team'])
    
    for match in results:
        if (normalize_team_name(match['home_team']) == home_team_norm and
            normalize_team_name(match['away_team']) == away_team_norm):
            home_goals, away_goals = parse_score(match['score'])
            if home_goals is not None and away_goals is not None:
                if home_goals > away_goals:
                    return 'win'
                elif home_goals < away_goals:
                    return 'loss'
                else:
                    return 'push'
    
    return None

def determine_over_under_result(pick, results):
    """Determine if an over/under prediction was correct."""
    home_team_norm = normalize_team_name(pick['home_team'])
    away_team_norm = normalize_team_name(pick['away_team'])
    
    for match in results:
        if (normalize_team_name(match['home_team']) == home_team_norm and
            normalize_team_name(match['away_team']) == away_team_norm):
            home_goals, away_goals = parse_score(match['score'])
            if home_goals is not None and away_goals is not None:
                total_goals = home_goals + away_goals
                
                if pick['prediction'] == 'over':
                    if total_goals > 2:
                        return 'win'
                    elif total_goals < 2:
                        return 'loss'
                    else:
                        return 'push'
                else:  # under
                    if total_goals < 2:
                        return 'win'
                    elif total_goals > 2:
                        return 'loss'
                    else:
                        return 'push'
    
    return None

# =============================================================================
# MAIN
# =============================================================================
def main():
    """Main function - fetch yesterday's results by default."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Fetch match results and update prediction history.')
    parser.add_argument('--date', 
                        help='Date to fetch (YYYY-MM-DD). Defaults to yesterday.')
    
    args = parser.parse_args()
    
    if args.date:
        target_date = args.date
    else:
        yesterday = datetime.now() - timedelta(days=1)
        target_date = yesterday.strftime("%Y-%m-%d")
    
    print(f"FETCHING RESULTS FOR: {target_date}")
    
    updated = update_history_with_results(target_date)
    
    if updated > 0:
        print(f"Success! Updated {updated} prediction results!")
    else:
        print("No updates were needed.")

if __name__ == "__main__":
    main()
