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
from bs4 import BeautifulSoup

from prediction_tracker import (
    load_history,
    save_history,
    format_safer_result_line,
    get_pending_predictions,
    pick_is_due,
    pick_is_overdue,
    pick_is_stale_pending,
    resolve_pick_result,
    is_blocked_pick,
    format_pick_result_lines,
    format_result_badge,
    format_result_tag,
)

# =============================================================================
# CONFIGURATION
# =============================================================================
CACHE_DB = "results_cache.db"
CACHE_TTL_HOURS = 24
SELECTED_RESULTS_REPORT = "selected_results_report.txt"

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
    "cape verde is": "cape verde",
    "bosnia hz": "bosnia herzegovina",
    "bosniaherzegovina": "bosnia herzegovina",
    "dr congo": "congo",
    "n ireland": "northern ireland",
    "dominican rep": "dominican republic",
    "trinidad": "trinidad tobago",
    "korea republic": "south korea",
    "croatia": "croatia",
    "mexico": "mexico",
    "ecuador": "ecuador",
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
    """Read results from manual_results.json or manual_results.csv (only matches with scores)"""
    results = []

    # Try JSON
    if os.path.exists("manual_results.json"):
        try:
            with open("manual_results.json", "r") as f:
                data = json.load(f)
                for match in data.get("results", []):
                    if match.get("date") == date_str and match.get("score", "").strip():
                        results.append({
                            "home_team": match["home_team"],
                            "away_team": match["away_team"],
                            "score": match["score"],
                            "source": "manual"
                        })
            if results:
                logger.info(f"[Manual] Found {len(results)} matches with scores in manual_results.json")
                return results
        except Exception as e:
            logger.warning(f"[Manual] Error reading JSON: {e}")

    # Try CSV
    if os.path.exists("manual_results.csv"):
        try:
            with open("manual_results.csv", "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("date") == date_str and row.get("score", "").strip():
                        results.append({
                            "home_team": row["home_team"],
                            "away_team": row["away_team"],
                            "score": row["score"],
                            "source": "manual"
                        })
            if results:
                logger.info(f"[Manual] Found {len(results)} matches with scores in manual_results.csv")
                return results
        except Exception as e:
            logger.warning(f"[Manual] Error reading CSV: {e}")

    return []

# =============================================================================
# SOURCE 4: SOCCERBASE SCRAPE (FALLBACK FOR LEAGUES NOT COVERED BY APIs)
# =============================================================================
def fetch_soccerbase_results(date_str):
    logger.info(f"[Soccerbase] Fetching results for {date_str}...")
    cache_key = f"soccerbase_results_{date_str}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    try:
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        session.mount("https://", HTTPAdapter(max_retries=retry_strategy))
        resp = session.get(
            f"https://www.soccerbase.com/matches/results.sd?date={date_str}",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
            timeout=30,
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        matches = []
        for table in soup.find_all("table", class_="listWithCards"):
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 6:
                    continue

                home = re.sub(r"\s*\d+.*$", "", cells[3].get_text(strip=True)).strip()
                score = cells[4].get_text(" ", strip=True)
                away = re.sub(r"\s*\d+.*$", "", cells[5].get_text(strip=True)).strip()
                hg, ag = parse_score(score)

                if home and away and hg is not None and ag is not None:
                    matches.append({
                        "home_team": home,
                        "away_team": away,
                        "score": f"{hg}-{ag}",
                        "source": "soccerbase",
                    })

        logger.info(f"[Soccerbase] Found {len(matches)} matches")
        set_cache(cache_key, matches)
        return matches
    except Exception as e:
        logger.error(f"[Soccerbase] Error: {e}")
        return []

# =============================================================================
# COMBINED FETCH
# =============================================================================
def fetch_match_results(date_str):
    all_results = []
    seen = set()

    sources = [
        ("Soccerbase", fetch_soccerbase_results),
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
def update_history_with_results(date_str, dry_run=False):
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
    settled = []

    for idx, pick in enumerate(history["home_win"]):
        if pick["result"] == "pending" and pick["date"] == date_str and pick_is_due(pick):
            match = find_matching_result(pick, results)
            result = determine_home_win_result(pick, [match]) if match else None
            if result:
                if not dry_run:
                    history["home_win"][idx]["result"] = result
                    history["home_win"][idx]["updated_at"] = datetime.now().isoformat()
                    history["home_win"][idx]["final_score"] = match["score"]
                    history["home_win"][idx]["result_source"] = match.get("source")
                updated += 1
                pick_home = pick.get("home_team", pick.get("home"))
                pick_away = pick.get("away_team", pick.get("away"))
                settled.append({
                    "type": "HOME WIN",
                    "league": pick.get("league"),
                    "home_team": pick_home,
                    "away_team": pick_away,
                    "prediction": "Home Win",
                    "confidence": pick.get("confidence"),
                    "score": match["score"],
                    "result": result,
                    "source": match.get("source"),
                })
                logger.info(f"[WIN] HW: {pick_home} vs {pick_away} = {match['score']} = {result}")
            else:
                pick_home = pick.get("home_team", pick.get("home"))
                pick_away = pick.get("away_team", pick.get("away"))
                unmatched.append(f"HW: {pick_home} vs {pick_away}")

    for idx, pick in enumerate(history["over_under"]):
        if pick["result"] == "pending" and pick["date"] == date_str and pick_is_due(pick):
            match = find_matching_result(pick, results)
            result = determine_over_under_result(pick, [match]) if match else None
            if result:
                if not dry_run:
                    history["over_under"][idx]["result"] = result
                    history["over_under"][idx]["updated_at"] = datetime.now().isoformat()
                    history["over_under"][idx]["final_score"] = match["score"]
                    history["over_under"][idx]["result_source"] = match.get("source")
                updated += 1
                pick_home = pick.get("home_team", pick.get("home"))
                pick_away = pick.get("away_team", pick.get("away"))
                prediction = pick.get("prediction", "over")
                market = "Over 2.5" if prediction in ("over", "Over 2.5") else "Under 2.5"
                settled.append({
                    "type": "OVER/UNDER",
                    "league": pick.get("league"),
                    "home_team": pick_home,
                    "away_team": pick_away,
                    "prediction": market,
                    "confidence": pick.get("confidence"),
                    "score": match["score"],
                    "result": result,
                    "source": match.get("source"),
                })
                logger.info(f"[WIN] OU: {pick_home} vs {pick_away} = {match['score']} = {result}")
            else:
                pick_home = pick.get("home_team", pick.get("home"))
                pick_away = pick.get("away_team", pick.get("away"))
                unmatched.append(f"OU: {pick_home} vs {pick_away}")

    if updated > 0 and not dry_run:
        save_history(history)
        logger.info(f"Updated {updated} predictions!")
    elif updated > 0:
        logger.info(f"[DRY RUN] Would update {updated} predictions.")
    else:
        logger.warning("No predictions matched.")

    if unmatched:
        logger.warning(f"{len(unmatched)} unmatched:")
        for u in unmatched[:10]:
            logger.warning(f"  - {u}")

    return updated, unmatched


def pick_to_report_item(pick, ptype):
    """Convert a settled history pick into a report line item."""
    home = pick.get("home_team", pick.get("home"))
    away = pick.get("away_team", pick.get("away"))
    if ptype == "home_win":
        prediction = "Home Win"
    else:
        prediction = (
            "Over 2.5"
            if pick.get("prediction", "over").lower() in ("over", "over 2.5")
            else "Under 2.5"
        )
    return {
        "home_team": home,
        "away_team": away,
        "prediction": prediction,
        "score": pick.get("final_score", ""),
        "result": pick.get("result"),
    }


def collect_settled_picks_for_date(history, date_str):
    """All decided picks for a date, whether settled this run or earlier."""
    items = []
    for pick in history.get("home_win", []):
        if pick.get("date", "")[:10] != date_str:
            continue
        if pick.get("result") in ("win", "loss", "push") and pick.get("final_score"):
            items.append(pick_to_report_item(pick, "home_win"))
    for pick in history.get("over_under", []):
        if pick.get("date", "")[:10] != date_str:
            continue
        if pick.get("result") in ("win", "loss", "push") and pick.get("final_score"):
            items.append(pick_to_report_item(pick, "over_under"))
    return items


def collect_unmatched_pending_for_date(history, date_str):
    """Past-date picks still pending after settlement (missing scores). Stale (>3d) hidden."""
    unmatched = []
    for pick in history.get("home_win", []):
        if pick.get("date", "")[:10] != date_str:
            continue
        if pick.get("result") == "pending" and pick_is_overdue(pick) and not pick_is_stale_pending(pick):
            home = pick.get("home_team", pick.get("home"))
            away = pick.get("away_team", pick.get("away"))
            unmatched.append(f"HW: {home} vs {away}")
    for pick in history.get("over_under", []):
        if pick.get("date", "")[:10] != date_str:
            continue
        if pick.get("result") == "pending" and pick_is_overdue(pick) and not pick_is_stale_pending(pick):
            home = pick.get("home_team", pick.get("home"))
            away = pick.get("away_team", pick.get("away"))
            unmatched.append(f"OU: {home} vs {away}")
    return unmatched


def build_yesterday_results_block(history):
    """Detailed yesterday block for selected-results Telegram."""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    lines = []

    for pick in history.get("home_win", []):
        if pick.get("date", "")[:10] != yesterday:
            continue
        lines.extend(format_pick_result_lines(pick, "Home Win"))

    for pick in history.get("over_under", []):
        if pick.get("date", "")[:10] != yesterday:
            continue
        market = (
            "Over 2.5"
            if pick.get("prediction", "over").lower() in ("over", "over 2.5")
            else "Under 2.5"
        )
        lines.extend(format_pick_result_lines(pick, market))

    while lines and lines[-1] == "":
        lines.pop()
    return yesterday, lines


def write_selected_results_report(dates_to_check, history, dry_run=False):
    """Rebuild selected-results report from history for the date window."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    mode = "[DRY RUN] " if dry_run else ""

    with open(SELECTED_RESULTS_REPORT, "w", encoding="utf-8") as f:
        yesterday_str, yesterday_lines = build_yesterday_results_block(history)
        if yesterday_lines:
            f.write(f"\n{mode}📌 YESTERDAY\n")
            f.write(f"Date: {yesterday_str}\n")
            f.write("-" * 30 + "\n")
            max_lines = 20
            for line in yesterday_lines[:max_lines]:
                f.write(f"{line}\n")
            remaining = len(yesterday_lines) - max_lines
            if remaining > 0:
                f.write(f"...and {remaining} more\n")

        for date_str in dates_to_check:
            if date_str == yesterday_str:
                continue

            settled = collect_settled_picks_for_date(history, date_str)
            unmatched = collect_unmatched_pending_for_date(history, date_str)
            if not settled and not unmatched:
                continue
            if date_str == today_str:
                if settled:
                    f.write(f"\n{mode}📅 TODAY\n")
                    f.write(f"Date: {date_str}\n")
                    f.write("-" * 30 + "\n")
                    for item in settled:
                        result = resolve_pick_result(
                            {"result": item["result"], "final_score": item["score"]}
                        )
                        tag = format_result_tag(result)
                        f.write(f"[{tag}] {item['home_team']} vs {item['away_team']}\n")
                        f.write(f"   {item['prediction']} — {format_result_badge(result)} ({item['score']})\n")
                        safer_line = format_safer_result_line(item["prediction"], item["score"])
                        if safer_line:
                            f.write(f"{safer_line}\n")
                        f.write("\n")

                if unmatched and settled:
                    f.write(f"Still unmatched: {len(unmatched)}\n")
                    for item in unmatched[:5]:
                        f.write(f"   - {item}\n")
                    if len(unmatched) > 5:
                        f.write(f"   ...and {len(unmatched) - 5} more\n")
                continue

            if unmatched and date_str < today_str:
                f.write(f"\n{mode}⏳ MISSING SCORES\n")
                f.write(f"Date: {date_str}\n")
                f.write("-" * 30 + "\n")
                f.write(f"Still unmatched: {len(unmatched)}\n")
                for item in unmatched[:5]:
                    f.write(f"   - {item}\n")
                if len(unmatched) > 5:
                    f.write(f"   ...and {len(unmatched) - 5} more\n")


def collect_settlement_dates(days_back, history=None):
    """Calendar lookback plus every date that still has pending picks."""
    if history is None:
        history = load_history()

    dates = set()
    for i in range(1, days_back + 1):
        dates.add((datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"))

    today = datetime.now().date()
    for ptype in ("home_win", "over_under"):
        for pick in history.get(ptype, []):
            if pick.get("result") != "pending":
                continue
            date_str = pick["date"][:10]
            try:
                pick_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if pick_date < today:
                dates.add(date_str)

    return sorted(dates, reverse=True)


def append_still_pending_report(history):
    """Note past-date picks that remain pending after a settlement run. Stale (>3d) hidden."""
    pending = get_pending_predictions()
    pending = [p for p in pending if not pick_is_stale_pending(p)]
    if not pending:
        return

    with open(SELECTED_RESULTS_REPORT, "a", encoding="utf-8") as f:
        f.write("\nSTILL PENDING\n")
        f.write("-" * 30 + "\n")
        for pick in pending[:20]:
            home = pick.get("home_team", pick.get("home"))
            away = pick.get("away_team", pick.get("away"))
            market = "HW" if pick.get("type") == "home_win" else "OU"
            f.write(f"[PENDING] {market}: {home} vs {away} ({pick['date']})\n")
        if len(pending) > 20:
            f.write(f"   ...and {len(pending) - 20} more\n")

def find_matching_result(pick, results):
    pick_home = pick.get("home_team", pick.get("home"))
    pick_away = pick.get("away_team", pick.get("away"))
    for match in results:
        if (team_names_match(match["home_team"], pick_home) and
                team_names_match(match["away_team"], pick_away)):
            return match
    return None

def determine_home_win_result(pick, results):
    pick_home = pick.get("home_team", pick.get("home"))
    pick_away = pick.get("away_team", pick.get("away"))
    for match in results:
        if (team_names_match(match["home_team"], pick_home) and
                team_names_match(match["away_team"], pick_away)):
            hg, ag = parse_score(match["score"])
            if hg is not None and ag is not None:
                if hg > ag:
                    return "win"
                elif hg < ag:
                    return "loss"
                else:
                    return "loss"
    return None

def determine_over_under_result(pick, results):
    pick_home = pick.get("home_team", pick.get("home"))
    pick_away = pick.get("away_team", pick.get("away"))
    for match in results:
        if (team_names_match(match["home_team"], pick_home) and
                team_names_match(match["away_team"], pick_away)):
            hg, ag = parse_score(match["score"])
            if hg is not None and ag is not None:
                total = hg + ag
                prediction = pick.get("prediction", "over")
                if prediction in ("over", "Over 2.5"):
                    return "win" if total > 2 else "loss"
                else:
                    return "win" if total < 3 else "loss"
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
        history = load_history()
        dates_to_check = collect_settlement_dates(args.days, history)

    total_updated = 0
    all_unmatched = []

    for date_str in dates_to_check:
        print(f"\n{'='*60}")
        print(f"FETCHING RESULTS FOR: {date_str}")
        print(f"{'='*60}")

        if args.dry_run:
            print("[DRY RUN] Not saving changes")

        updated, unmatched = update_history_with_results(date_str, dry_run=args.dry_run)
        total_updated += updated
        all_unmatched.extend(unmatched)

    history = load_history()
    write_selected_results_report(dates_to_check, history, dry_run=args.dry_run)

    if not args.dry_run:
        append_still_pending_report(history)

    pending_left = len(get_pending_predictions())
    print(f"\n{'='*60}")
    print(f"SUMMARY: Updated {total_updated} predictions across {len(dates_to_check)} day(s)")
    print(f"Still pending: {pending_left}")
    if all_unmatched:
        print(f"Unmatched: {len(all_unmatched)}")
    print(f"{'='*60}")

    return 0

def update_all_pending_results(days_back=7):
    """Backward compatibility: Update all pending predictions from last N days"""
    from prediction_tracker import load_history, save_history
    history = load_history()
    updated = 0
    today = datetime.now()
    
    dates_to_check = []
    for i in range(1, days_back + 1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        dates_to_check.append(d)
        
    for date_str in dates_to_check:
        results = fetch_match_results(date_str)
        
        for idx, pick in enumerate(history["home_win"]):
            if pick["result"] == "pending" and pick["date"] == date_str:
                match = find_matching_result(pick, results)
                result = determine_home_win_result(pick, [match]) if match else None
                if result:
                    history["home_win"][idx]["result"] = result
                    history["home_win"][idx]["updated_at"] = datetime.now().isoformat()
                    if match:
                        history["home_win"][idx]["final_score"] = match["score"]
                        history["home_win"][idx]["result_source"] = match.get("source")
                    updated += 1
                    pick_home = pick.get("home_team", pick.get("home"))
                    pick_away = pick.get("away_team", pick.get("away"))
                    logger.info(f"Updated: {pick_home} vs {pick_away} → {result}")
        
        for idx, pick in enumerate(history["over_under"]):
            if pick["result"] == "pending" and pick["date"] == date_str:
                match = find_matching_result(pick, results)
                result = determine_over_under_result(pick, [match]) if match else None
                if result:
                    history["over_under"][idx]["result"] = result
                    history["over_under"][idx]["updated_at"] = datetime.now().isoformat()
                    if match:
                        history["over_under"][idx]["final_score"] = match["score"]
                        history["over_under"][idx]["result_source"] = match.get("source")
                    updated += 1
                    pick_home = pick.get("home_team", pick.get("home"))
                    pick_away = pick.get("away_team", pick.get("away"))
                    logger.info(f"Updated: {pick_home} vs {pick_away} → {result}")
    
    if updated > 0:
        save_history(history)
        logger.info(f"[SUCCESS] Successfully updated {updated} match results")
    else:
        logger.info("No pending results to update.")
    
    return updated


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "update":
        update_all_pending_results(days_back=int(sys.argv[2]) if len(sys.argv) > 2 else 14)
        sys.exit(0)
    else:
        sys.exit(main())
