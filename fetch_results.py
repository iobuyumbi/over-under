#!/usr/bin/env python3
"""
Automatic Result Fetcher - v4
Primary: Football-Data.org (free tier covers Chile, Argentina, World Cup)
Fallback: API-Football
Manual: CSV/JSON override for when APIs fail
"""

import requests
import json
import logging
import sqlite3
import re
import time
import os
import sys
import csv
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from prediction_tracker import load_history, save_history

# =============================================================================
# CONFIGURATION
# =============================================================================
CACHE_DB = "results_cache.db"
CACHE_TTL_HOURS = 24

# API Keys from environment
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# =============================================================================
# TEAM NAME MAPPINGS
# =============================================================================
TEAM_NAME_MAP = {
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
    "middlesbrough": "middlesbrough",
    # International
    "usa": "united states",
    "czechia": "czech republic",
    "ivory coast": "cote d'ivoire",
    "south korea": "korea republic",
    "north korea": "korea dpr",
    "coquimbo unido": "coquimbo",
    "colo-colo": "colo colo",
    "o'higgins": "o'higgins",
    "audax italiano": "audax italiano",
    "la serena": "la serena",
    "cobresal": "cobresal",
    "gimnasia y tiro": "gimnasia y tiro",
    "gimnasia de jujuy": "gimnasia jujuy",
    "san martin de san juan": "san martin san juan",
    "temperley": "temperley",
    "guemes": "guemes",
    "almirante brown": "almirante brown",
    "godoy cruz": "godoy cruz",
    "ferrocarril midland": "ferrocarril midland",
    "atlanta": "atlanta",
    "ferro carril oeste": "ferro carril oeste",
    "acassuso": "acassuso",
    "central norte": "central norte",
    "san telmo": "san telmo",
    "nueva chicago": "nueva chicago",
    "chacarita": "chacarita juniors",
    "almagro": "almagro",
    "agropecuario": "agropecuario",
    "quilmes": "quilmes",
    "patronato": "patronato",
    "atletico rafaela": "atletico rafaela",
    "sundsvall": "sundsvall",
    "osters": "osters",
    "paraguay": "paraguay",
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
# TEAM NAME NORMALIZATION
# =============================================================================
def normalize_team_name(name):
    name = name.strip().lower()
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
    name = re.sub(r"[^a-z0-9\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    if name in TEAM_NAME_MAP:
        name = TEAM_NAME_MAP[name]
    return name

def team_names_match(name1, name2):
    n1 = normalize_team_name(name1)
    n2 = normalize_team_name(name2)
    if n1 == n2:
        return True
    if len(n1) > 3 and len(n2) > 3:
        if n1 in n2 or n2 in n1:
            return True
    return False

# =============================================================================
# SCORE PARSING
# =============================================================================
def parse_score(score_str):
    if not score_str:
        return None, None
    score_str = score_str.strip()
    score_str = re.sub(r"^(FT\s*|AET\s*|Pens\s*)", "", score_str, flags=re.IGNORECASE)
    match = re.search(r"(\d+)\s*[-:]\s*(\d+)", score_str)
    if match:
        try:
            return int(match.group(1)), int(match.group(2))
        except ValueError:
            pass
    return None, None

# =============================================================================
# SOURCE 1: FOOTBALL-DATA.ORG (PRIMARY)
# =============================================================================
def fetch_football_data_org(date_str):
    if not FOOTBALL_DATA_KEY:
        logger.info("[Football-Data.org] No API key configured.")
        return []

    logger.info(f"[Football-Data.org] Fetching results for {date_str}...")
    cache_key = f"fdo_results_{date_str}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    try:
        url = "https://api.football-data.org/v4/matches"
        headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
        params = {"dateFrom": date_str, "dateTo": date_str, "status": "FINISHED"}

        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        matches = []
        for match in data.get("matches", []):
            home = match["homeTeam"]["name"]
            away = match["awayTeam"]["name"]
            score = match.get("score", {}).get("fullTime", {})
            if score and score.get("home") is not None and score.get("away") is not None:
                matches.append({
                    "home_team": home,
                    "away_team": away,
                    "score": f"{score['home']}-{score['away']}",
                    "source": "football-data-org"
                })

        logger.info(f"[Football-Data.org] Found {len(matches)} matches")
        set_cache(cache_key, matches)
        return matches

    except Exception as e:
        logger.error(f"[Football-Data.org] Error: {e}")
        return []

# =============================================================================
# SOURCE 2: API-FOOTBALL (FALLBACK)
# =============================================================================
def fetch_api_football(date_str):
    if not API_FOOTBALL_KEY:
        logger.info("[API-Football] No API key configured.")
        return []

    logger.info(f"[API-Football] Fetching results for {date_str}...")
    cache_key = f"af_results_{date_str}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    try:
        url = "https://v3.football.api-sports.io/fixtures"
        headers = {
            "x-rapidapi-key": API_FOOTBALL_KEY,
            "x-rapidapi-host": "v3.football.api-sports.io",
        }
        params = {"date": date_str, "status": "FT"}

        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        matches = []
        for fixture in data.get("response", []):
            home = fixture["teams"]["home"]["name"]
            away = fixture["teams"]["away"]["name"]
            gh = fixture["goals"]["home"]
            ga = fixture["goals"]["away"]
            if gh is not None and ga is not None:
                matches.append({
                    "home_team": home,
                    "away_team": away,
                    "score": f"{gh}-{ga}",
                    "source": "api-football"
                })

        logger.info(f"[API-Football] Found {len(matches)} matches")
        set_cache(cache_key, matches)
        return matches

    except Exception as e:
        logger.error(f"[API-Football] Error: {e}")
        return []

# =============================================================================
# SOURCE 3: MANUAL OVERRIDE (CSV/JSON FILE)
# =============================================================================
def fetch_manual_override(date_str):
    """Read results from manual_results.json or manual_results.csv"""
    results = []

    # Try JSON
    if os.path.exists("manual_results.json"):
        try:
            with open("manual_results.json", "r") as f:
                data = json.load(f)
                for match in data.get("results", []):
                    if match.get("date") == date_str:
                        results.append({
                            "home_team": match["home_team"],
                            "away_team": match["away_team"],
                            "score": match["score"],
                            "source": "manual"
                        })
            if results:
                logger.info(f"[Manual] Found {len(results)} matches in manual_results.json")
                return results
        except Exception as e:
            logger.warning(f"[Manual] Error reading JSON: {e}")

    # Try CSV
    if os.path.exists("manual_results.csv"):
        try:
            with open("manual_results.csv", "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("date") == date_str:
                        results.append({
                            "home_team": row["home_team"],
                            "away_team": row["away_team"],
                            "score": row["score"],
                            "source": "manual"
                        })
            if results:
                logger.info(f"[Manual] Found {len(results)} matches in manual_results.csv")
                return results
        except Exception as e:
            logger.warning(f"[Manual] Error reading CSV: {e}")

    return []

# =============================================================================
# COMBINED FETCH
# =============================================================================
def fetch_match_results(date_str):
    all_results = []
    seen = set()

    sources = [
        ("Football-Data.org", fetch_football_data_org),
        ("API-Football", fetch_api_football),
        ("Manual Override", fetch_manual_override),
    ]

    for source_name, source_func in sources:
        try:
            results = source_func(date_str)
            for match in results:
                key = (normalize_team_name(match["home_team"]), normalize_team_name(match["away_team"]))
                if key not in seen:
                    seen.add(key)
                    all_results.append(match)
        except Exception as e:
            logger.error(f"Error with {source_name}: {e}")

    logger.info(f"Total unique results: {len(all_results)}")
    return all_results

# =============================================================================
# UPDATE HISTORY
# =============================================================================
def update_history_with_results(date_str):
    logger.info(f"Updating history for {date_str}...")

    results = fetch_match_results(date_str)

    if not results:
        logger.warning(f"No results found for {date_str}")
        return 0, []

    logger.info(f"Results to check: {len(results)}")
    for r in results[:5]:
        logger.info(f"  {r['home_team']} vs {r['away_team']} = {r['score']} ({r['source']})")
    if len(results) > 5:
        logger.info(f"  ... and {len(results) - 5} more")

    history = load_history()
    updated = 0
    unmatched = []

    for idx, pick in enumerate(history["home_win"]):
        if pick["result"] == "pending" and pick["date"] == date_str:
            result = determine_home_win_result(pick, results)
            if result:
                history["home_win"][idx]["result"] = result
                history["home_win"][idx]["updated_at"] = datetime.now().isoformat()
                updated += 1
                logger.info(f"✅ HW: {pick['home_team']} vs {pick['away_team']} = {result}")
            else:
                unmatched.append(f"HW: {pick['home_team']} vs {pick['away_team']}")

    for idx, pick in enumerate(history["over_under"]):
        if pick["result"] == "pending" and pick["date"] == date_str:
            result = determine_over_under_result(pick, results)
            if result:
                history["over_under"][idx]["result"] = result
                history["over_under"][idx]["updated_at"] = datetime.now().isoformat()
                updated += 1
                logger.info(f"✅ OU: {pick['home_team']} vs {pick['away_team']} = {result}")
            else:
                unmatched.append(f"OU: {pick['home_team']} vs {pick['away_team']}")

    if updated > 0:
        save_history(history)
        logger.info(f"Updated {updated} predictions!")
    else:
        logger.warning("No predictions matched.")

    if unmatched:
        logger.warning(f"{len(unmatched)} unmatched:")
        for u in unmatched[:10]:
            logger.warning(f"  - {u}")

    return updated, unmatched

def determine_home_win_result(pick, results):
    for match in results:
        if (team_names_match(match["home_team"], pick["home_team"]) and
                team_names_match(match["away_team"], pick["away_team"])):
            hg, ag = parse_score(match["score"])
            if hg is not None and ag is not None:
                if hg > ag:
                    return "win"
                elif hg < ag:
                    return "loss"
                else:
                    return "push"
    return None

def determine_over_under_result(pick, results):
    for match in results:
        if (team_names_match(match["home_team"], pick["home_team"]) and
                team_names_match(match["away_team"], pick["away_team"])):
            hg, ag = parse_score(match["score"])
            if hg is not None and ag is not None:
                total = hg + ag
                if pick["prediction"] == "over":
                    if total > 2:
                        return "win"
                    elif total < 2:
                        return "loss"
                    else:
                        return "push"
                else:
                    if total < 2:
                        return "win"
                    elif total > 2:
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
    parser.add_argument("--days", type=int, default=1, help="Number of past days to check")
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving")
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
        print(f"Unmatched: {len(all_unmatched)}")
    print(f"{'='*60}")

    return total_updated

if __name__ == "__main__":
    sys.exit(main())
