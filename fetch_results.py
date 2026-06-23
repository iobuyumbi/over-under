#!/usr/bin/env python3
"""
Automatic Result Fetcher - v3
Fetches match results and updates prediction history.
Tries multiple data sources for better reliability:
1. Soccerbase (scraping)
2. API-Football (API)
3. Football-Data.org (API)
4. TheSportsDB (API)
"""

import requests
import json
import logging
import sqlite3
import re
import time
import os
import sys
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from prediction_tracker import load_history, save_history

# =============================================================================
# CONFIGURATION
# =============================================================================
CACHE_DB = "soccerbase_results_cache.db"
CACHE_TTL_HOURS = 24

# API Keys from environment variables
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")
API_FOOTBALL_HOST = "v3.football.api-sports.io"

FOOTBALL_DATA_ORG_KEY = os.environ.get("FOOTBALL_DATA_ORG_KEY", "")
FOOTBALL_DATA_ORG_HOST = "api.football-data.org"

# TheSportsDB is free, no key needed for basic use
THESPORTSDB_HOST = "www.thesportsdb.com"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# =============================================================================
# TEAM NAME MAPPINGS
# Soccerbase often uses abbreviations or different spellings.
# Add mappings here as you discover mismatches.
# =============================================================================
TEAM_NAME_MAP = {
    # Soccerbase name -> prediction name (or vice versa)
    "man utd": "manchester united",
    "man city": "manchester city",
    "tottenham": "tottenham hotspur",
    "west ham": "west ham united",
    "newcastle": "newcastle united",
    "nottm forest": "nottingham forest",
    "brighton": "brighton & hove albion",
    "wolves": "wolverhampton wanderers",
    "leicester": "leicester city",
    "ipswich": "ipswich town",
    "sheff utd": "sheffield united",
    "cardiff": "cardiff city",
    "swansea": "swansea city",
    "stoke": "stoke city",
    "norwich": "norwich city",
    "qpr": "queens park rangers",
    "boro": "middlesbrough",
    "boro'": "middlesbrough",
    "derby": "derby county",
    "hull": "hull city",
    "blackburn": "blackburn rovers",
    "rotherham": "rotherham united",
    "bristol city": "bristol city",
    "millwall": "millwall",
    "preston": "preston north end",
    "blackpool": "blackpool",
    "bournemouth": "afc bournemouth",
    "brentford": "brentford",
    "fulham": "fulham",
    "luton": "luton town",
    "coventry": "coventry city",
    "plymouth": "plymouth argyle",
    "oxford": "oxford united",
    "portsmouth": "portsmouth",
    "charlton": "charlton athletic",
    "bolton": "bolton wanderers",
    "peterborough": "peterborough united",
    "shrewsbury": "shrewsbury town",
    "wycombe": "wycombe wanderers",
    "bristol rovers": "bristol rovers",
    "wigan": "wigan athletic",
    "burton": "burton albion",
    "lincoln": "lincoln city",
    "stevenage": "stevenage",
    "leyton orient": "leyton orient",
    "salford": "salford city",
    "stockport": "stockport county",
    "wrexham": "wrexham",
    "mansfield": "mansfield town",
    "barrow": "barrow",
    "morecambe": "morecambe",
    "accrington": "accrington stanley",
    "crawley": "crawley town",
    "tranmere": "tranmere rovers",
    "doncaster": "doncaster rovers",
    "harrogate": "harrogate town",
    "crewe": "crewe alexandra",
    "grimsby": "grimsby town",
    "newport": "newport county",
    "colchester": "colchester united",
    "bradford": "bradford city",
    "swindon": "swindon town",
    "sutton": "sutton united",
    "afc wimbledon": "afc wimbledon",
    "walsall": "walsall",
    "gillingham": "gillingham",
    "fleetwood": "fleetwood town",
    "exeter": "exeter city",
    "bristol city": "bristol city",
    "huddersfield": "huddersfield town",
    "sheffield wed": "sheffield wednesday",
    "sheff wed": "sheffield wednesday",
    "birmingham": "birmingham city",
    "sunderland": "sunderland",
    "leeds": "leeds united",
    "west brom": "west bromwich albion",
    "burnley": "burnley",
    "watford": "watford",
    "reading": "reading",
    "cardiff": "cardiff city",
    "swansea": "swansea city",
    "stoke": "stoke city",
    "norwich": "norwich city",
    "qpr": "queens park rangers",
    "preston": "preston north end",
    "blackburn": "blackburn rovers",
    "blackpool": "blackpool",
    "rotherham": "rotherham united",
    "millwall": "millwall",
    "bristol city": "bristol city",
    "luton": "luton town",
    "coventry": "coventry city",
    "middlesbrough": "middlesbrough",
    "hull": "hull city",
    "derby": "derby county",
    "bristol rovers": "bristol rovers",
    "wigan": "wigan athletic",
    "burton": "burton albion",
    "lincoln": "lincoln city",
    "shrewsbury": "shrewsbury town",
    "wycombe": "wycombe wanderers",
    "peterborough": "peterborough united",
    "charlton": "charlton athletic",
    "bolton": "bolton wanderers",
    "portsmouth": "portsmouth",
    "oxford": "oxford united",
    "plymouth": "plymouth argyle",
    "cambridge": "cambridge united",
    "stevenage": "stevenage",
    "leyton orient": "leyton orient",
    "salford": "salford city",
    "stockport": "stockport county",
    "wrexham": "wrexham",
    "mansfield": "mansfield town",
    "barrow": "barrow",
    "morecambe": "morecambe",
    "accrington": "accrington stanley",
    "crawley": "crawley town",
    "tranmere": "tranmere rovers",
    "doncaster": "doncaster rovers",
    "harrogate": "harrogate town",
    "crewe": "crewe alexandra",
    "grimsby": "grimsby town",
    "newport": "newport county",
    "colchester": "colchester united",
    "bradford": "bradford city",
    "swindon": "swindon town",
    "sutton": "sutton united",
    "afc wimbledon": "afc wimbledon",
    "walsall": "walsall",
    "gillingham": "gillingham",
    "fleetwood": "fleetwood town",
    "exeter": "exeter city",
    "bristol city": "bristol city",
    "huddersfield": "huddersfield town",
    "sheffield wed": "sheffield wednesday",
    "sheff wed": "sheffield wednesday",
    "birmingham": "birmingham city",
    "sunderland": "sunderland",
    "leeds": "leeds united",
    "west brom": "west bromwich albion",
    "burnley": "burnley",
    "watford": "watford",
    "reading": "reading",
    # Common international
    "usa": "united states",
    "czechia": "czech republic",
    "ivory coast": "cote d'ivoire",
    "south korea": "korea republic",
    "north korea": "korea dpr",
}

# =============================================================================
# CACHE
# =============================================================================
def get_cache(key):
    try:
        conn = sqlite3.connect(CACHE_DB)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS cache
                     (key TEXT PRIMARY KEY, data TEXT, timestamp REAL)""")
        c.execute("SELECT data, timestamp FROM cache WHERE key = ?", (key,))
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
        c.execute("""CREATE TABLE IF NOT EXISTS cache
                     (key TEXT PRIMARY KEY, data TEXT, timestamp REAL)""")
        c.execute("REPLACE INTO cache VALUES (?, ?, ?)",
                   (key, json.dumps(data, default=str), time.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Cache write error: {e}")

# =============================================================================
# HTTP CLIENT
# =============================================================================
def get_session():
    session = requests.Session()
    retry = Retry(connect=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
    })
    return session

# =============================================================================
# TEAM NAME NORMALIZATION
# =============================================================================
def normalize_team_name(name):
    """Normalize team name for better matching."""
    name = name.strip().lower()
    # Remove common suffixes
    name = re.sub(r"\s*fc$", "", name)
    name = re.sub(r"\s*cf$", "", name)
    name = re.sub(r"\s*city$", "", name)
    name = re.sub(r"\s*united$", "", name)
    name = re.sub(r"\s*athletic$", "", name)
    name = re.sub(r"\s*afc$", "", name)
    name = re.sub(r"\s*sc$", "", name)
    name = re.sub(r"\s*deportivo$", "", name)
    name = re.sub(r"\s*club$", "", name)
    name = re.sub(r"\s*esporte$", "", name)
    name = re.sub(r"[^a-z0-9\s]", "", name)  # remove special chars
    name = re.sub(r"\s+", " ", name).strip()
    # Apply manual mappings
    if name in TEAM_NAME_MAP:
        name = TEAM_NAME_MAP[name]
    return name

def team_names_match(name1, name2):
    """Fuzzy match two team names."""
    n1 = normalize_team_name(name1)
    n2 = normalize_team_name(name2)
    if n1 == n2:
        return True
    # Check if one is a substring of the other (e.g. "man utd" vs "manchester united")
    if len(n1) > 3 and len(n2) > 3:
        if n1 in n2 or n2 in n1:
            return True
    return False

# =============================================================================
# SCORE PARSING
# =============================================================================
def parse_score(score_str):
    """Parse a score like \"2-1\" or \"2 - 1\" into home_goals, away_goals."""
    if not score_str:
        return None, None
    # Remove extra whitespace and common prefixes
    score_str = score_str.strip()
    score_str = re.sub(r"^(FT\s*|AET\s*|Pens\s*)", "", score_str, flags=re.IGNORECASE)
    # Match patterns like "2-1", "2 - 1", "2:1"
    match = re.search(r"(\d+)\s*[-:]\s*(\d+)", score_str)
    if match:
        try:
            return int(match.group(1)), int(match.group(2))
        except ValueError:
            pass
    return None, None

# =============================================================================
# SOCCERBASE SCRAPER
# =============================================================================
def fetch_match_results_soccerbase(date_str):
    """Fetch match results for a specific date from Soccerbase."""
    logger.info(f"[Soccerbase] Fetching results for {date_str}...")

    cache_key = f"sb_results_{date_str}"
    cached = get_cache(cache_key)
    if cached:
        logger.info(f"[Soccerbase] Using cached results for {date_str}")
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

        # Soccerbase results are often in tables with class "listWithCards"
        # or in divs with match data. Try multiple selectors.
        selectors = [
            "table.listWithCards tbody tr",
            "table tbody tr",
            ".match-list tbody tr",
            ".results-table tbody tr",
            "tr.match",
            ".fixture-row",
            ".match-row",
        ]

        rows = []
        for selector in selectors:
            rows = soup.select(selector)
            if rows:
                logger.info(f"[Soccerbase] Found {len(rows)} rows with selector: {selector}")
                break

        if not rows:
            # Log a snippet of HTML for debugging
            html_snippet = soup.get_text(separator="\n", strip=True)[:2000]
            logger.warning(f"[Soccerbase] No match rows found. Page text snippet:\n{html_snippet}")
            return []

        for row in rows:
            try:
                cells = row.find_all(["td", "th"])
                if len(cells) < 4:
                    continue

                home_team = None
                away_team = None
                score = None

                for cell in cells:
                    text = cell.get_text(strip=True)
                    if not text:
                        continue

                    # Look for score pattern
                    if re.search(r"\d+\s*[-:]\s*\d+", text):
                        score = text
                        continue

                    # Skip non-team cells
                    if re.match(r"^\d+$", text):
                        continue
                    if text.upper() in ("FT", "HT", "AET", "PENS", "VS", "V"):
                        continue
                    if any(x in text.lower() for x in ["bet", "odds", "live", "preview", "stats"]):
                        continue

                    # Heuristic: first long text = home, second = away
                    if len(text) > 1 and not home_team:
                        home_team = text
                    elif len(text) > 1 and not away_team:
                        away_team = text

                if home_team and away_team and score:
                    matches.append({
                        "home_team": home_team,
                        "away_team": away_team,
                        "score": score,
                        "source": "soccerbase"
                    })
            except Exception as e:
                logger.debug(f"[Soccerbase] Error parsing row: {e}")
                continue

        if matches:
            logger.info(f"[Soccerbase] Parsed {len(matches)} match results for {date_str}")
            set_cache(cache_key, matches)
        else:
            logger.warning(f"[Soccerbase] Found rows but parsed 0 matches for {date_str}")

        return matches

    except Exception as e:
        logger.error(f"[Soccerbase] Error fetching results for {date_str}: {e}")
        return []

# =============================================================================
# API-FOOTBALL FALLBACK
# =============================================================================
def fetch_match_results_api_football(date_str):
    """Fetch match results using API-Football (free tier)."""
    if not API_FOOTBALL_KEY:
        logger.info("[API-Football] No API key configured, skipping.")
        return []

    logger.info(f"[API-Football] Fetching results for {date_str}...")

    cache_key = f"api_football_results_{date_str}"
    cached = get_cache(cache_key)
    if cached:
        logger.info(f"[API-Football] Using cached results for {date_str}")
        return cached

    try:
        url = f"https://{API_FOOTBALL_HOST}/fixtures"
        headers = {
            "x-rapidapi-key": API_FOOTBALL_KEY,
            "x-rapidapi-host": API_FOOTBALL_HOST,
        }
        params = {"date": date_str, "status": "FT"}

        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        matches = []
        for fixture in data.get("response", []):
            home = fixture["teams"]["home"]["name"]
            away = fixture["teams"]["away"]["name"]
            goals_home = fixture["goals"]["home"]
            goals_away = fixture["goals"]["away"]

            if goals_home is not None and goals_away is not None:
                matches.append({
                    "home_team": home,
                    "away_team": away,
                    "score": f"{goals_home}-{goals_away}",
                    "source": "api-football"
                })

        logger.info(f"[API-Football] Found {len(matches)} match results for {date_str}")
        set_cache(cache_key, matches)
        return matches

    except Exception as e:
        logger.error(f"[API-Football] Error: {e}")
        return []


# =============================================================================
# FOOTBALL-DATA.ORG FALLBACK
# =============================================================================
def fetch_match_results_football_data_org(date_str):
    """Fetch match results using Football-Data.org."""
    if not FOOTBALL_DATA_ORG_KEY:
        logger.info("[Football-Data.org] No API key configured, skipping.")
        return []

    logger.info(f"[Football-Data.org] Fetching results for {date_str}...")

    cache_key = f"football_data_org_results_{date_str}"
    cached = get_cache(cache_key)
    if cached:
        logger.info(f"[Football-Data.org] Using cached results for {date_str}")
        return cached

    try:
        url = f"https://{FOOTBALL_DATA_ORG_HOST}/v4/matches"
        headers = {"X-Auth-Token": FOOTBALL_DATA_ORG_KEY}
        params = {"dateFrom": date_str, "dateTo": date_str, "status": "FINISHED"}

        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        matches = []
        for match in data.get("matches", []):
            home = match["homeTeam"]["name"]
            away = match["awayTeam"]["name"]
            score = match["score"]["fullTime"]
            if score and score["home"] is not None and score["away"] is not None:
                matches.append({
                    "home_team": home,
                    "away_team": away,
                    "score": f"{score['home']}-{score['away']}",
                    "source": "football-data-org"
                })

        logger.info(f"[Football-Data.org] Found {len(matches)} match results for {date_str}")
        set_cache(cache_key, matches)
        return matches

    except Exception as e:
        logger.error(f"[Football-Data.org] Error: {e}")
        return []


# =============================================================================
# THESPORTSDB FALLBACK
# =============================================================================
def fetch_match_results_thesportsdb(date_str):
    """Fetch match results using TheSportsDB (free, no key needed for basic use)."""
    logger.info(f"[TheSportsDB] Fetching results for {date_str}...")

    cache_key = f"thesportsdb_results_{date_str}"
    cached = get_cache(cache_key)
    if cached:
        logger.info(f"[TheSportsDB] Using cached results for {date_str}")
        return cached

    try:
        # TheSportsDB uses date format DD/MM/YYYY
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        tsdb_date = dt.strftime("%d/%m/%Y")
        
        url = f"https://{THESPORTSDB_HOST}/api/v1/json/1/eventsday.php?d={tsdb_date}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        matches = []
        for event in data.get("events", []):
            if event.get("intHomeScore") is not None and event.get("intAwayScore") is not None:
                matches.append({
                    "home_team": event["strHomeTeam"],
                    "away_team": event["strAwayTeam"],
                    "score": f"{event['intHomeScore']}-{event['intAwayScore']}",
                    "source": "thesportsdb"
                })

        logger.info(f"[TheSportsDB] Found {len(matches)} match results for {date_str}")
        set_cache(cache_key, matches)
        return matches

    except Exception as e:
        logger.error(f"[TheSportsDB] Error: {e}")
        return []


# =============================================================================
# COMBINED FETCH - TRIES ALL SOURCES
# =============================================================================
def fetch_match_results(date_str):
    """Fetch match results, trying multiple sources in order."""
    all_results = []
    seen = set()

    # Try all sources and collect unique results
    sources = [
        ("Soccerbase", fetch_match_results_soccerbase),
        ("API-Football", fetch_match_results_api_football),
        ("Football-Data.org", fetch_match_results_football_data_org),
        ("TheSportsDB", fetch_match_results_thesportsdb)
    ]

    for source_name, source_func in sources:
        try:
            results = source_func(date_str)
            for match in results:
                key = (match["home_team"], match["away_team"])
                if key not in seen:
                    seen.add(key)
                    all_results.append(match)
        except Exception as e:
            logger.error(f"Error with {source_name}: {e}")
            continue

    logger.info(f"Total unique results found from all sources: {len(all_results)}")
    return all_results

# =============================================================================
# UPDATE HISTORY
# =============================================================================
def update_history_with_results(date_str):
    """Fetch results for a date and update pending predictions."""
    logger.info(f"Updating history for {date_str}...")

    results = fetch_match_results(date_str)

    if not results:
        logger.warning(f"No results found for {date_str} from any source")
        return 0, []

    logger.info(f"Total results to check against: {len(results)}")
    for r in results[:5]:
        logger.info(f"  Result: {r['home_team']} vs {r['away_team']} = {r['score']} ({r['source']})")
    if len(results) > 5:
        logger.info(f"  ... and {len(results) - 5} more")

    history = load_history()
    updated = 0
    unmatched = []

    # Check Home Win predictions
    for idx, pick in enumerate(history["home_win"]):
        if pick["result"] == "pending" and pick["date"] == date_str:
            result = determine_home_win_result(pick, results)
            if result:
                history["home_win"][idx]["result"] = result
                history["home_win"][idx]["updated_at"] = datetime.now().isoformat()
                updated += 1
                logger.info(f"✅ Home Win: {pick['home_team']} vs {pick['away_team']} = {result}")
            else:
                unmatched.append(f"HW: {pick['home_team']} vs {pick['away_team']}")

    # Check Over/Under predictions
    for idx, pick in enumerate(history["over_under"]):
        if pick["result"] == "pending" and pick["date"] == date_str:
            result = determine_over_under_result(pick, results)
            if result:
                history["over_under"][idx]["result"] = result
                history["over_under"][idx]["updated_at"] = datetime.now().isoformat()
                updated += 1
                logger.info(f"✅ Over/Under: {pick['home_team']} vs {pick['away_team']} = {result}")
            else:
                unmatched.append(f"OU: {pick['home_team']} vs {pick['away_team']}")

    if updated > 0:
        save_history(history)
        logger.info(f"Updated {updated} predictions with results!")
    else:
        logger.warning("No pending predictions matched the results.")

    if unmatched:
        logger.warning(f"{len(unmatched)} predictions could not be matched:")
        for u in unmatched[:10]:
            logger.warning(f"  - {u}")
        if len(unmatched) > 10:
            logger.warning(f"  ... and {len(unmatched) - 10} more")

    return updated, unmatched

def determine_home_win_result(pick, results):
    """Determine if a home win prediction was correct."""
    for match in results:
        if (team_names_match(match["home_team"], pick["home_team"]) and
                team_names_match(match["away_team"], pick["away_team"])):
            home_goals, away_goals = parse_score(match["score"])
            if home_goals is not None and away_goals is not None:
                if home_goals > away_goals:
                    return "win"
                elif home_goals < away_goals:
                    return "loss"
                else:
                    return "push"
    return None

def determine_over_under_result(pick, results):
    """Determine if an over/under prediction was correct."""
    for match in results:
        if (team_names_match(match["home_team"], pick["home_team"]) and
                team_names_match(match["away_team"], pick["away_team"])):
            home_goals, away_goals = parse_score(match["score"])
            if home_goals is not None and away_goals is not None:
                total_goals = home_goals + away_goals
                if pick["prediction"] == "over":
                    if total_goals > 2:
                        return "win"
                    elif total_goals < 2:
                        return "loss"
                    else:
                        return "push"
                else:  # under
                    if total_goals < 2:
                        return "win"
                    elif total_goals > 2:
                        return "loss"
                    else:
                        return "push"
    return None

# =============================================================================
# MAIN
# =============================================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fetch match results and update prediction history.")
    parser.add_argument("--date", help="Date to fetch (YYYY-MM-DD). Defaults to yesterday.")
    parser.add_argument("--days", type=int, default=1, help="Number of past days to check (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without saving")
    args = parser.parse_args()

    if args.date:
        dates_to_check = [args.date]
    else:
        dates_to_check = []
        for i in range(1, args.days + 1):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            dates_to_check.append(d)

    total_updated = 0
    all_unmatched = []

    for date_str in dates_to_check:
        print(f"\n{'='*60}")
        print(f"FETCHING RESULTS FOR: {date_str}")
        print(f"{'='*60}")

        if args.dry_run:
            print("[DRY RUN] Not saving changes")

        updated, unmatched = update_history_with_results(date_str)
        total_updated += updated
        all_unmatched.extend(unmatched)

    print(f"\n{'='*60}")
    print(f"SUMMARY: Updated {total_updated} predictions across {len(dates_to_check)} day(s)")
    if all_unmatched:
        print(f"Unmatched predictions: {len(all_unmatched)}")
    print(f"{'='*60}")

    return total_updated

if __name__ == "__main__":
    sys.exit(main())
