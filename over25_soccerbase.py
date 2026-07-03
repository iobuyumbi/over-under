#!/usr/bin/env python3
"""
OVER/UNDER 2.5 GOALS PREDICTOR - UNIFIED v5
==============================================
Over 2.5: High-scoring rules (from over25tips.com)
Under 2.5: Low-scoring mirror rules (defensive caps)
Shrinkage xG | Portfolio Kelly | SQLite Cache
"""

import requests
import json
import re
import argparse
import time
import random
import math
import logging
import sqlite3
import hashlib
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from fake_useragent import UserAgent
from concurrent.futures import ThreadPoolExecutor, as_completed

# Import prediction tracker
from prediction_tracker import record_predictions, get_yesterday_results, format_yesterday_header

# =============================================================================
# CONFIGURATION
# =============================================================================
CACHE_DB = "soccerbase_cache.db"
CACHE_TTL_HOURS = 24
MAX_WORKERS = 4
REQUEST_DELAY_MIN = 2.5
REQUEST_DELAY_MAX = 5.0
MAX_TOTAL_EXPOSURE = 0.25
SHRINKAGE_WEIGHT = 0.60

# Under 2.5 thresholds (from over25tips.com official algorithm)
UNDER_HOME_SCORED_CAP = 1.2      # Max avg goals scored by home team in last 6 home
UNDER_HOME_CONCEDED_CAP = 1.2    # Max avg goals conceded by home team in last 6 home
UNDER_AWAY_SCORED_CAP = 1.0      # Max avg goals scored by away team in last 6 away
UNDER_AWAY_CONCEDED_CAP = 1.0    # Max avg goals conceded by away team in last 6 away
UNDER_HOME_TOTAL_6_CAP = 10.0    # Legacy cap for backwards compatibility
UNDER_AWAY_TOTAL_6_CAP = 10.0    # Legacy cap for backwards compatibility
UNDER_HOME_OVER25_MAX = 3        # Legacy max for backwards compatibility
UNDER_AWAY_OVER25_MAX = 3        # Legacy max for backwards compatibility

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Self-contained UserAgent - works offline, no remote dependency
ua = UserAgent(
    fallback="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "DNT": "1",
    "Connection": "keep-alive",
}

retry_strategy = Retry(
    total=4,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504]
)
adapter = HTTPAdapter(
    max_retries=retry_strategy,
    pool_connections=10,
    pool_maxsize=10
)
session = requests.Session()
session.mount("https://", adapter)
session.mount("http://", adapter)


# =============================================================================
# CACHE LAYER
# =============================================================================
class Cache:
    def __init__(self, db_path=CACHE_DB, ttl_hours=CACHE_TTL_HOURS):
        self.db_path = db_path
        self.ttl = timedelta(hours=ttl_hours)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_created ON cache(created_at)
            """)

    def _make_key(self, url):
        return hashlib.sha256(url.encode()).hexdigest()

    def get(self, url):
        key = self._make_key(url)
        cutoff = (datetime.now() - self.ttl).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM cache WHERE key = ? AND created_at > ?",
                (key, cutoff)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, url, value):
        key = self._make_key(url)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value) VALUES (?, ?)",
                (key, json.dumps(value))
            )

    def clear(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM cache")
        logger.info("Cache cleared.")


cache = Cache()


# =============================================================================
# HTTP HELPERS
# =============================================================================
def get_random_headers():
    headers = HEADERS.copy()
    headers["User-Agent"] = ua.random
    return headers


def random_delay():
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


def fetch(url, use_cache=True):
    if use_cache:
        cached = cache.get(url)
        if cached is not None:
            logger.debug(f"Cache hit: {url[:80]}...")
            return cached

    random_delay()
    try:
        response = session.get(url, headers=get_random_headers(), timeout=20)
        response.raise_for_status()
        data = response.text
        if use_cache:
            cache.set(url, data)
        return data
    except Exception as e:
        logger.error(f"Fetch failed for {url[:80]}: {e}")
        return None


# =============================================================================
# SCRAPING
# =============================================================================
def fetch_soccerbase_fixtures(date_str):
    url = f"https://www.soccerbase.com/matches/results.sd?date={date_str}"
    html = fetch(url)
    if html is None:
        return []

    soup = BeautifulSoup(html, "html.parser")
    matches = []

    tables = soup.find_all("table", class_="listWithCards")
    if not tables:
        logger.warning(f"No fixture tables found for {date_str}")
        return matches

    for table in tables:
        current_league = "Unknown League"
        for row in table.find_all("tr"):
            league_link = row.find("a", href=lambda h: h and "comp_id=" in h)
            if league_link:
                current_league = league_link.get_text(strip=True)
                continue

            cells = row.find_all("td")
            if len(cells) < 6:
                continue

            home_raw = cells[3].get_text(strip=True)
            score_or_v = cells[4].get_text(strip=True)
            away_raw = cells[5].get_text(strip=True)

            home = re.sub(r"\s*\d+.*$", "", home_raw).strip()
            away = re.sub(r"\s*\d+.*$", "", away_raw).strip()

            if not home or not away:
                continue

            team_links = row.find_all("a", href=lambda h: h and "team_id=" in h)
            if len(team_links) < 2:
                continue

            try:
                home_id = team_links[0]["href"].split("team_id=")[1].split("&")[0]
                away_id = team_links[1]["href"].split("team_id=")[1].split("&")[0]
            except (KeyError, IndexError):
                continue

            matches.append({
                "league": current_league,
                "home": home,
                "away": away,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "date": date_str,
                "status": "Scheduled" if score_or_v.lower() == "v" else "Completed"
            })

    return matches


def fetch_soccerbase_team_results(team_id):
    url = f"https://www.soccerbase.com/teams/team.sd?team_id={team_id}&teamTabs=results"
    html = fetch(url)
    if html is None:
        return []

    soup = BeautifulSoup(html, "html.parser")
    matches = []

    for table in soup.find_all("table", class_="soccerGrid"):
        for row in table.find_all("tr")[2:]:
            cells = row.find_all("td")
            if len(cells) < 8:
                continue

            score = cells[4].get_text(strip=True)
            if "-" not in score:
                continue

            try:
                gf_h, gf_a = map(int, score.split("-"))
            except ValueError:
                continue

            home_link = cells[3].find("a", href=lambda h: h and "team_id=" in h)
            if not home_link:
                continue

            try:
                home_id_in_row = home_link["href"].split("team_id=")[1].split("&")[0]
            except (KeyError, IndexError):
                continue

            is_home = str(home_id_in_row) == str(team_id)
            gf = gf_h if is_home else gf_a
            ga = gf_a if is_home else gf_h

            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(cells[1]))
            date_str = date_match.group(1) if date_match else None

            matches.append({
                "gf": gf,
                "ga": ga,
                "total": gf + ga,
                "is_home": is_home,
                "date_str": date_str
            })

    matches.sort(key=lambda x: x.get("date_str") or "0000-00-00", reverse=True)
    return matches


# =============================================================================
# FORM & DATA HELPERS
# =============================================================================
def parse_date(date_str):
    """
    Parse date string with multiple format fallbacks.
    Handles Soccerbase variations: 2026-06-15, 15-Jun-26, 2026/06/15, etc.
    """
    if not date_str:
        return None

    date_str = str(date_str).strip()

    for fmt in ("%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str, fmt)
        except (ValueError, TypeError):
            continue

    # Try to extract any date-like pattern as last resort
    import re
    match = re.search(r'(\d{4})[-/](\d{2})[-/](\d{2})', date_str)
    if match:
        try:
            return datetime.strptime(match.group(0), "%Y-%m-%d")
        except ValueError:
            pass

    logger.warning(f"Could not parse date: {date_str}")
    return None


def get_team_form(team_id, is_home=True, num_matches=6, target_date_str=None):
    all_matches = fetch_soccerbase_team_results(team_id)
    target_dt = parse_date(target_date_str) if target_date_str else None
    form = []

    for match in all_matches:
        match_dt = parse_date(match.get("date_str"))
        if target_dt and match_dt and match_dt >= target_dt:
            continue
        if match["is_home"] == is_home:
            form.append((match["gf"], match["ga"]))
            if len(form) >= num_matches:
                break

    return form


# =============================================================================
# OVER 2.5 ALGORITHM (Your Original 10-Check Rules)
# =============================================================================
def apply_over_algorithm(home_3, away_3, home_6, away_6):
    if len(home_3) < 3 or len(away_3) < 3:
        return None, None, {"error": "Insufficient data"}, False

    passed, failed, details = [], [], {}
    is_perfect = True

    # Home 3-game
    h_total_3 = sum(gf + ga for gf, ga in home_3)
    if h_total_3 >= 7:
        passed.append("Home total goals (last 3)"); details["Home total goals (last 3)"] = f"PASS ({h_total_3})"
    else:
        failed.append("Home total goals (last 3)"); details["Home total goals (last 3)"] = f"FAIL ({h_total_3})"; is_perfect = False

    h_over_3 = sum(1 for gf, ga in home_3 if gf + ga > 2.5)
    if h_over_3 >= 2:
        passed.append("Home over 2.5 (last 3)"); details["Home over 2.5 (last 3)"] = f"PASS ({h_over_3}/3)"
        if h_over_3 < 3:
            is_perfect = False
    else:
        failed.append("Home over 2.5 (last 3)"); details["Home over 2.5 (last 3)"] = f"FAIL ({h_over_3}/3)"; is_perfect = False

    # Away 3-game
    a_total_3 = sum(gf + ga for gf, ga in away_3)
    if a_total_3 >= 7:
        passed.append("Away total goals (last 3)"); details["Away total goals (last 3)"] = f"PASS ({a_total_3})"
    else:
        failed.append("Away total goals (last 3)"); details["Away total goals (last 3)"] = f"FAIL ({a_total_3})"; is_perfect = False

    prev_a_total = away_3[0][0] + away_3[0][1]
    if prev_a_total >= 2:
        passed.append("Away last match goals"); details["Away last match goals"] = f"PASS ({prev_a_total})"
    else:
        failed.append("Away last match goals"); details["Away last match goals"] = f"FAIL ({prev_a_total})"; is_perfect = False

    a_scored = sum(1 for gf, _ in away_3 if gf > 0)
    if a_scored >= 2:
        passed.append("Away scored (last 3)"); details["Away scored (last 3)"] = f"PASS ({a_scored}/3)"
        if a_scored < 3:
            is_perfect = False
    else:
        failed.append("Away scored (last 3)"); details["Away scored (last 3)"] = f"FAIL ({a_scored}/3)"; is_perfect = False

    a_over_3 = sum(1 for gf, ga in away_3 if gf + ga > 2.5)
    if a_over_3 >= 2:
        passed.append("Away over 2.5 (last 3)"); details["Away over 2.5 (last 3)"] = f"PASS ({a_over_3}/3)"
        if a_over_3 < 3:
            is_perfect = False
    else:
        failed.append("Away over 2.5 (last 3)"); details["Away over 2.5 (last 3)"] = f"FAIL ({a_over_3}/3)"; is_perfect = False

    # 6-game checks
    if len(home_6) >= 6:
        h_over_6 = sum(1 for gf, ga in home_6 if gf + ga > 2.5)
        if h_over_6 >= 4:
            passed.append("Home over 2.5 (last 6)"); details["Home over 2.5 (last 6)"] = f"PASS ({h_over_6}/6)"
        else:
            failed.append("Home over 2.5 (last 6)"); details["Home over 2.5 (last 6)"] = f"FAIL ({h_over_6}/6)"; is_perfect = False

        h_total_6 = sum(gf + ga for gf, ga in home_6)
        if h_total_6 >= 18:
            passed.append("Home total goals (last 6)"); details["Home total goals (last 6)"] = f"PASS ({h_total_6})"
        else:
            failed.append("Home total goals (last 6)"); details["Home total goals (last 6)"] = f"FAIL ({h_total_6})"; is_perfect = False

    if len(away_6) >= 6:
        a_over_6 = sum(1 for gf, ga in away_6 if gf + ga > 2.5)
        if a_over_6 >= 4:
            passed.append("Away over 2.5 (last 6)"); details["Away over 2.5 (last 6)"] = f"PASS ({a_over_6}/6)"
        else:
            failed.append("Away over 2.5 (last 6)"); details["Away over 2.5 (last 6)"] = f"FAIL ({a_over_6}/6)"; is_perfect = False

        a_total_6 = sum(gf + ga for gf, ga in away_6)
        if a_total_6 >= 18:
            passed.append("Away total goals (last 6)"); details["Away total goals (last 6)"] = f"PASS ({a_total_6})"
        else:
            failed.append("Away total goals (last 6)"); details["Away total goals (last 6)"] = f"FAIL ({a_total_6})"; is_perfect = False

    return passed, failed, details, is_perfect


# =============================================================================
# UNDER 2.5 ALGORITHM (Mirror Rules - Defensive Caps)
# =============================================================================
def apply_under_algorithm(home_3, away_3):
    """
    Official Under 2.5 Goals Algorithm from over25tips.com:

    3-GAME HOME CHECKS:
    - Previous three home games must have ended under 2.5 in at least two of three.
    - One or more of the score lines must contain 0 goals (home or away side) in any of the three games.

    3-GAME AWAY CHECKS:
    - Last three away games must be under 2.5 in at least two of three.
    - The AWAY team must not have scored in at least one of the last three away games.
    """
    if len(home_3) < 3 or len(away_3) < 3:
        return None, None, {"error": "Insufficient data"}, False

    passed, failed, details = [], [], {}
    is_perfect = True

    # --- 3-GAME HOME CHECKS ---
    # At least 2 of 3 home games under 2.5
    h_under_3 = sum(1 for gf, ga in home_3 if gf + ga < 2.5)
    if h_under_3 >= 2:
        passed.append("Home under 2.5 (last 3)"); details["Home under 2.5 (last 3)"] = f"PASS ({h_under_3}/3 under 2.5)"
        if h_under_3 < 3:
            is_perfect = False
    else:
        failed.append("Home under 2.5 (last 3)"); details["Home under 2.5 (last 3)"] = f"FAIL ({h_under_3}/3 under 2.5)"; is_perfect = False

    # At least one score line has 0 goals in the 3 home games
    h_has_zero = any(gf == 0 or ga == 0 for gf, ga in home_3)
    if h_has_zero:
        passed.append("Home has 0-goal side (last 3)"); details["Home has 0-goal side (last 3)"] = "PASS (at least one 0-goal side)"
    else:
        failed.append("Home has 0-goal side (last 3)"); details["Home has 0-goal side (last 3)"] = "FAIL (no zero-goal sides)"; is_perfect = False

    # --- 3-GAME AWAY CHECKS ---
    # At least 2 of 3 away games under 2.5
    a_under_3 = sum(1 for gf, ga in away_3 if gf + ga < 2.5)
    if a_under_3 >= 2:
        passed.append("Away under 2.5 (last 3)"); details["Away under 2.5 (last 3)"] = f"PASS ({a_under_3}/3 under 2.5)"
        if a_under_3 < 3:
            is_perfect = False
    else:
        failed.append("Away under 2.5 (last 3)"); details["Away under 2.5 (last 3)"] = f"FAIL ({a_under_3}/3 under 2.5)"; is_perfect = False

    # Away team did NOT score in at least 1 of last 3 away games
    a_blanked = sum(1 for gf, _ in away_3 if gf == 0)
    if a_blanked >= 1:
        passed.append("Away blanked (last 3)"); details["Away blanked (last 3)"] = f"PASS ({a_blanked}/3 away games with 0 scored)"
    else:
        failed.append("Away blanked (last 3)"); details["Away blanked (last 3)"] = f"FAIL ({a_blanked}/3 away games with 0 scored)"; is_perfect = False

    return passed, failed, details, is_perfect


def apply_under_6game_checks(home_6, away_6):
    """
    6-game average checks for Under 2.5 (supplementary to 3-game rules).
    These are NOT part of the official over25tips.com algorithm but add
    statistical rigor for classification tiers.
    """
    passed, failed, details = [], [], {}
    is_perfect = True

    if len(home_6) >= 6 and len(away_6) >= 6:
        # Home scored average <= 1.2
        hs = sum(gf for gf, _ in home_6) / 6
        if hs <= 1.2:
            passed.append("Home scored avg (last 6)"); details["Home scored avg (last 6)"] = f"PASS (HS={hs:.2f} <= 1.2)"
        else:
            failed.append("Home scored avg (last 6)"); details["Home scored avg (last 6)"] = f"FAIL (HS={hs:.2f} > 1.2)"; is_perfect = False

        # Home conceded average <= 1.2
        hc = sum(ga for _, ga in home_6) / 6
        if hc <= 1.2:
            passed.append("Home conceded avg (last 6)"); details["Home conceded avg (last 6)"] = f"PASS (HC={hc:.2f} <= 1.2)"
        else:
            failed.append("Home conceded avg (last 6)"); details["Home conceded avg (last 6)"] = f"FAIL (HC={hc:.2f} > 1.2)"; is_perfect = False

        # Away scored average <= 1.0
        a_s = sum(gf for gf, _ in away_6) / 6
        if a_s <= 1.0:
            passed.append("Away scored avg (last 6)"); details["Away scored avg (last 6)"] = f"PASS (AS={a_s:.2f} <= 1.0)"
        else:
            failed.append("Away scored avg (last 6)"); details["Away scored avg (last 6)"] = f"FAIL (AS={a_s:.2f} > 1.0)"; is_perfect = False

        # Away conceded average <= 1.0
        a_c = sum(ga for _, ga in away_6) / 6
        if a_c <= 1.0:
            passed.append("Away conceded avg (last 6)"); details["Away conceded avg (last 6)"] = f"PASS (AC={a_c:.2f} <= 1.0)"
        else:
            failed.append("Away conceded avg (last 6)"); details["Away conceded avg (last 6)"] = f"FAIL (AC={a_c:.2f} > 1.0)"; is_perfect = False
    else:
        details["UC1-4"] = "SKIPPED (need 6 games each)"

    return passed, failed, details, is_perfect


# =============================================================================
# POISSON MODEL
# =============================================================================
def poisson_pmf(k, lam):
    if lam <= 0:
        return 0.0
    return (math.exp(-lam) * (lam ** k)) / math.factorial(k)


def calculate_poisson_over25(home_lambda, away_lambda, max_goals=10):
    over_prob = 0.0
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            if h + a > 2:
                over_prob += poisson_pmf(h, home_lambda) * poisson_pmf(a, away_lambda)
    return round(over_prob * 100, 1)


def calculate_poisson_under25(home_lambda, away_lambda, max_goals=10):
    """Under 2.5 = 1 - Over 2.5 probability"""
    over_prob = calculate_poisson_over25(home_lambda, away_lambda, max_goals) / 100.0
    return round((1.0 - over_prob) * 100, 1)


# =============================================================================
# EXPECTED GOALS (Shrinkage Estimator)
# =============================================================================
def get_match_lambdas(home_6, away_6):
    """
    Calculate cross-matched Poisson lambdas.
    Home attack is adjusted by away defense weakness, and vice versa.
    """
    home_baseline_attack = 1.45
    away_baseline_attack = 1.20
    home_baseline_defense = 1.35  # avg goals home team concedes
    away_baseline_defense = 1.25  # avg goals away team concedes

    # Raw averages from form data
    h_scored_avg = sum(gf for gf, _ in home_6) / max(len(home_6), 1) if home_6 else home_baseline_attack
    h_conceded_avg = sum(ga for _, ga in home_6) / max(len(home_6), 1) if home_6 else away_baseline_attack

    a_scored_avg = sum(gf for gf, _ in away_6) / max(len(away_6), 1) if away_6 else away_baseline_attack
    a_conceded_avg = sum(ga for _, ga in away_6) / max(len(away_6), 1) if away_6 else home_baseline_attack

    # Shrinkage: blend raw average with league baseline
    h_attack = SHRINKAGE_WEIGHT * h_scored_avg + (1 - SHRINKAGE_WEIGHT) * home_baseline_attack
    h_defense = SHRINKAGE_WEIGHT * h_conceded_avg + (1 - SHRINKAGE_WEIGHT) * away_baseline_defense

    a_attack = SHRINKAGE_WEIGHT * a_scored_avg + (1 - SHRINKAGE_WEIGHT) * away_baseline_attack
    a_defense = SHRINKAGE_WEIGHT * a_conceded_avg + (1 - SHRINKAGE_WEIGHT) * home_baseline_defense

    # Cross-multiply: attack strength × opponent defense weakness
    home_lambda = h_attack * (a_defense / home_baseline_attack)
    away_lambda = a_attack * (h_defense / away_baseline_attack)

    return (
        round(max(0.5, min(3.8, home_lambda)), 2),
        round(max(0.5, min(3.8, away_lambda)), 2)
    )


# =============================================================================
# KELLY CRITERION
# =============================================================================
def calculate_kelly(prob, decimal_odds=2.0, use_half=True):
    if prob <= 0.0 or decimal_odds <= 1.0:
        return 0.0
    kelly = (prob * decimal_odds - 1) / (decimal_odds - 1)
    return max(0.0, kelly * 0.5 if use_half else kelly)


def apply_portfolio_kelly(recommendations, bet_type, bankroll, max_exposure=MAX_TOTAL_EXPOSURE):
    """
    Scale Kelly fractions so total exposure does not exceed max_exposure.
    bet_type: "over" or "under" — targets the correct nested dictionary.
    """
    if not recommendations or bankroll <= 0:
        return recommendations

    total_kelly = sum(r[bet_type]["kelly"] / 100 for r in recommendations)
    if total_kelly <= 0:
        return recommendations

    if total_kelly > max_exposure:
        scale = max_exposure / total_kelly
        for r in recommendations:
            r[bet_type]["kelly"] = round(r[bet_type]["kelly"] * scale, 2)
        logger.info(
            f"Portfolio Kelly ({bet_type}) scaled by {scale:.3f} "
            f"({total_kelly*100:.1f}% -> {max_exposure*100:.1f}% exposure)"
        )

    return recommendations


# =============================================================================
# MATCH PROCESSING
# =============================================================================
def process_single_match(match, target_date, default_odds_over=2.0, default_odds_under=1.85):
    try:
        # Fetch form data
        home_3 = get_team_form(match["home_team_id"], True, 3, target_date)
        away_3 = get_team_form(match["away_team_id"], False, 3, target_date)
        home_6 = get_team_form(match["home_team_id"], True, 6, target_date)
        away_6 = get_team_form(match["away_team_id"], False, 6, target_date)

        # --- OVER 2.5 ANALYSIS ---
        over_passed, over_failed, over_details, over_is_perfect = apply_over_algorithm(
            home_3, away_3, home_6, away_6
        )

        over_score = len(over_passed) if over_passed else 0

        # --- UNDER 2.5 ANALYSIS ---
        under3_result = apply_under_algorithm(home_3, away_3)
        if under3_result[0] is None:
            under3_passed, under3_failed, under3_details, under3_perfect = [], [], {}, False
        else:
            under3_passed, under3_failed, under3_details, under3_perfect = under3_result
            
        under6_result = apply_under_6game_checks(home_6, away_6)
        if under6_result[0] is None:
            under6_passed, under6_failed, under6_details, under6_perfect = [], [], {}, False
        else:
            under6_passed, under6_failed, under6_details, under6_perfect = under6_result

        # Merge 3-game and 6-game Under results
        under_passed = under3_passed + under6_passed
        under_failed = under3_failed + under6_failed
        under_details = {**under3_details, **under6_details}
        under_is_perfect = under3_perfect and under6_perfect

        under_score = len(under_passed) if under_passed else 0

        # --- POISSON PROBABILITIES ---
        home_lambda, away_lambda = get_match_lambdas(home_6, away_6)
        over25_prob_pct = calculate_poisson_over25(home_lambda, away_lambda)
        under25_prob_pct = calculate_poisson_under25(home_lambda, away_lambda)

        over25_prob = over25_prob_pct / 100.0
        under25_prob = under25_prob_pct / 100.0

        over_confidence = (
            "HIGH" if over25_prob >= 0.58
            else "MEDIUM" if over25_prob >= 0.52
            else "LOW"
        )
        under_confidence = (
            "HIGH" if under25_prob >= 0.58
            else "MEDIUM" if under25_prob >= 0.52
            else "LOW"
        )

        # --- KELLY STAKES ---
        over_kelly = calculate_kelly(over25_prob, default_odds_over, use_half=True)
        under_kelly = calculate_kelly(under25_prob, default_odds_under, use_half=True)

        return {
            "status": "success",
            "data": {
                "match": match,
                "over": {
                    "score": over_score,
                    "passed": over_passed,
                    "details": over_details,
                    "is_perfect": over_is_perfect,
                    "prob": over25_prob_pct,
                    "confidence": over_confidence,
                    "kelly": round(over_kelly * 100, 2)
                },
                "under": {
                    "score": under_score,
                    "passed": under_passed,
                    "details": under_details,
                    "is_perfect": under_is_perfect,
                    "prob": under25_prob_pct,
                    "confidence": under_confidence,
                    "kelly": round(under_kelly * 100, 2)
                },
                "poisson": {
                    "home_lambda": home_lambda,
                    "away_lambda": away_lambda,
                    "over25_prob": over25_prob_pct,
                    "under25_prob": under25_prob_pct
                }
            }
        }
    except Exception as e:
        logger.error(
            f"Processing failed for "
            f"{match.get('home', 'N/A')} vs {match.get('away', 'N/A')}: {e}",
            exc_info=True
        )
        return {"status": "error"}


# =============================================================================
# REPORTING
# =============================================================================
def build_report(over_perfect, over_qualified, over_close, over_weak,
               under_perfect, under_qualified, under_close, under_weak,
               scanned_dates, bankroll, odds_over, odds_under, detailed=False):
    """
    Build a clean, mobile-friendly report - both channels show all picks, free is simplified
    Returns: (report, base_date, included_over, included_under)
    """
    all_over_picks = over_perfect + over_qualified + over_close
    all_under_picks = under_perfect + under_qualified + under_close
    
    # Both channels show all picks
    included_over = all_over_picks
    included_under = all_under_picks
    included_dates = scanned_dates
    
    base_date = scanned_dates[0] if scanned_dates else datetime.now().strftime("%Y-%m-%d")

    lines = []
    lines.append("OVER/UNDER 2.5 PICKS")
    lines.append("")
    
    if len(included_dates) > 1:
        lines.append(f"Dates: {included_dates[0]} to {included_dates[-1]}")
    else:
        lines.append(f"Date: {base_date}")
    
    lines.append("")
    
    # Yesterday's results section
    yesterday_date, yesterday_results, yesterday_summary = get_yesterday_results("over_under")
    if yesterday_results:
        lines.append(f"YESTERDAY'S RESULTS ({yesterday_date})")
        header = format_yesterday_header(yesterday_summary)
        if header:
            lines.append(header)
        for res in yesterday_results:
            lines.append(res)
        lines.append("")
    
    # Over 2.5 section
    if included_over:
        lines.append("OVER 2.5 GOALS")
        lines.append("")
        
        # Group over picks
        included_over_perfect = [p for p in included_over if p in over_perfect]
        included_over_qualified = [p for p in included_over if p in over_qualified]
        included_over_close = [p for p in included_over if p in over_close]
        
        if included_over_perfect:
            lines.append("  PREMIUM PICKS")
            for i, item in enumerate(included_over_perfect, 1):
                m = item["match"]
                p = item["poisson"]
                tgt = item["over"]
                if detailed:
                    lines.append(f"  {i}. {m['home']} vs {m['away']} ({m['date']})")
                    lines.append(f"     {tgt['confidence']} ({p['over25_prob']}%)")
                    lines.append(f"     Stake: {tgt['kelly']:.2f}% bankroll at odds {odds_over}")
                    lines.append(f"     Model xG: {p['home_lambda']} - {p['away_lambda']}")
                    lines.append(f"     Rule score: {tgt['score']}/10 checks passed")
                    if tgt.get("passed"):
                        lines.append(f"     Passed: {', '.join(tgt['passed'])}")
                    for rule, detail in sorted(tgt.get("details", {}).items()):
                        lines.append(f"     {rule}: {detail}")
                    lines.append("")
                else:
                    lines.append(f"  {i}. {m['home']} vs {m['away']}")
        
        if included_over_qualified:
            if not detailed and not included_over_perfect:
                lines.append("  PREMIUM PICKS")
            if detailed:
                lines.append("  STRONG PICKS")
            start_idx = len(included_over_perfect) + 1
            for i, item in enumerate(included_over_qualified, start_idx):
                m = item["match"]
                p = item["poisson"]
                tgt = item["over"]
                if detailed:
                    lines.append(f"  {i}. {m['home']} vs {m['away']} ({m['date']})")
                    lines.append(f"     {tgt['confidence']} ({p['over25_prob']}%)")
                    lines.append(f"     Stake: {tgt['kelly']:.2f}% bankroll at odds {odds_over}")
                    lines.append(f"     Model xG: {p['home_lambda']} - {p['away_lambda']}")
                    lines.append(f"     Rule score: {tgt['score']}/10 checks passed")
                    if tgt.get("passed"):
                        lines.append(f"     Passed: {', '.join(tgt['passed'])}")
                    for rule, detail in sorted(tgt.get("details", {}).items()):
                        lines.append(f"     {rule}: {detail}")
                    lines.append("")
                else:
                    lines.append(f"  {i}. {m['home']} vs {m['away']}")
        
        if included_over_close:
            if detailed:
                lines.append("  VALUE PICKS")
            start_idx = len(included_over_perfect) + len(included_over_qualified) + 1
            for i, item in enumerate(included_over_close, start_idx):
                m = item["match"]
                p = item["poisson"]
                tgt = item["over"]
                if detailed:
                    lines.append(f"  {i}. {m['home']} vs {m['away']} ({m['date']})")
                    lines.append(f"     {tgt['confidence']} ({p['over25_prob']}%)")
                    lines.append(f"     Stake: {tgt['kelly']:.2f}% bankroll at odds {odds_over}")
                    lines.append(f"     Model xG: {p['home_lambda']} - {p['away_lambda']}")
                    lines.append(f"     Rule score: {tgt['score']}/10 checks passed")
                    if tgt.get("passed"):
                        lines.append(f"     Passed: {', '.join(tgt['passed'])}")
                    for rule, detail in sorted(tgt.get("details", {}).items()):
                        lines.append(f"     {rule}: {detail}")
                    lines.append("")
                else:
                    lines.append(f"  {i}. {m['home']} vs {m['away']}")
        
        if not detailed and (included_over_perfect or included_over_qualified or included_over_close):
            lines.append("")
    
    # Under 2.5 section
    if included_under:
        lines.append("UNDER 2.5 GOALS")
        lines.append("")
        
        # Group under picks
        included_under_perfect = [p for p in included_under if p in under_perfect]
        included_under_qualified = [p for p in included_under if p in under_qualified]
        included_under_close = [p for p in included_under if p in under_close]
        
        if included_under_perfect:
            lines.append("  PREMIUM PICKS")
            for i, item in enumerate(included_under_perfect, 1):
                m = item["match"]
                p = item["poisson"]
                tgt = item["under"]
                if detailed:
                    lines.append(f"  {i}. {m['home']} vs {m['away']} ({m['date']})")
                    lines.append(f"     {tgt['confidence']} ({p['under25_prob']}%)")
                    lines.append(f"     Stake: {tgt['kelly']:.2f}% bankroll at odds {odds_under}")
                    lines.append(f"     Model xG: {p['home_lambda']} - {p['away_lambda']}")
                    lines.append(f"     Rule score: {tgt['score']}/8 checks passed")
                    if tgt.get("passed"):
                        lines.append(f"     Passed: {', '.join(tgt['passed'])}")
                    for rule, detail in sorted(tgt.get("details", {}).items()):
                        lines.append(f"     {rule}: {detail}")
                    lines.append("")
                else:
                    lines.append(f"  {i}. {m['home']} vs {m['away']}")
        
        if included_under_qualified:
            if not detailed and not included_under_perfect:
                lines.append("  PREMIUM PICKS")
            if detailed:
                lines.append("  STRONG PICKS")
            start_idx = len(included_under_perfect) + 1
            for i, item in enumerate(included_under_qualified, start_idx):
                m = item["match"]
                p = item["poisson"]
                tgt = item["under"]
                if detailed:
                    lines.append(f"  {i}. {m['home']} vs {m['away']} ({m['date']})")
                    lines.append(f"     {tgt['confidence']} ({p['under25_prob']}%)")
                    lines.append(f"     Stake: {tgt['kelly']:.2f}% bankroll at odds {odds_under}")
                    lines.append(f"     Model xG: {p['home_lambda']} - {p['away_lambda']}")
                    lines.append(f"     Rule score: {tgt['score']}/8 checks passed")
                    if tgt.get("passed"):
                        lines.append(f"     Passed: {', '.join(tgt['passed'])}")
                    for rule, detail in sorted(tgt.get("details", {}).items()):
                        lines.append(f"     {rule}: {detail}")
                    lines.append("")
                else:
                    lines.append(f"  {i}. {m['home']} vs {m['away']}")
        
        if included_under_close:
            if detailed:
                lines.append("  VALUE PICKS")
            start_idx = len(included_under_perfect) + len(included_under_qualified) + 1
            for i, item in enumerate(included_under_close, start_idx):
                m = item["match"]
                p = item["poisson"]
                tgt = item["under"]
                if detailed:
                    lines.append(f"  {i}. {m['home']} vs {m['away']} ({m['date']})")
                    lines.append(f"     {tgt['confidence']} ({p['under25_prob']}%)")
                    lines.append(f"     Stake: {tgt['kelly']:.2f}% bankroll at odds {odds_under}")
                    lines.append(f"     Model xG: {p['home_lambda']} - {p['away_lambda']}")
                    lines.append(f"     Rule score: {tgt['score']}/8 checks passed")
                    if tgt.get("passed"):
                        lines.append(f"     Passed: {', '.join(tgt['passed'])}")
                    for rule, detail in sorted(tgt.get("details", {}).items()):
                        lines.append(f"     {rule}: {detail}")
                    lines.append("")
                else:
                    lines.append(f"  {i}. {m['home']} vs {m['away']}")
        
        if not detailed and (included_under_perfect or included_under_qualified or included_under_close):
            lines.append("")

    # Disclaimer
    lines.append("---")
    lines.append("For informational purposes only")
    lines.append("Gamble responsibly")
    lines.append("")

    report = "\n".join(lines)
    return report, base_date, included_over, included_under

# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Over/Under 2.5 Goals Predictor with Unified Analysis"
    )
    parser.add_argument(
        "date",
        nargs="?",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Only include scheduled (upcoming) matches"
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        default=1000.0,
        help="Total bankroll in currency units"
    )
    parser.add_argument(
        "--odds-over",
        type=float,
        default=2.0,
        help="Average decimal odds for Over 2.5"
    )
    parser.add_argument(
        "--odds-under",
        type=float,
        default=1.85,
        help="Average decimal odds for Under 2.5"
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear the SQLite cache before running"
    )
    args = parser.parse_args()

    if args.clear_cache:
        cache.clear()

    start_date = datetime.strptime(args.date, "%Y-%m-%d")

    # Over 2.5 buckets
    over_perfect, over_qualified, over_close, over_weak = [], [], [], []
    # Under 2.5 buckets
    under_perfect, under_qualified, under_close, under_weak = [], [], [], []

    scanned_dates = []

    print(f"Starting Over/Under 2.5 analysis from {args.date}...")

    for day_offset in range(4):
        current_date = start_date + timedelta(days=day_offset)
        date_str = current_date.strftime("%Y-%m-%d")
        scanned_dates.append(date_str)

        fixtures = fetch_soccerbase_fixtures(date_str)
        seen = set()
        unique_fixtures = []
        for f in fixtures:
            key = (f["home_team_id"], f["away_team_id"], f["league"])
            if key not in seen and f["home_team_id"] and f["away_team_id"]:
                if not args.scheduled or f["status"] == "Scheduled":
                    seen.add(key)
                    unique_fixtures.append(f)

        if not unique_fixtures:
            logger.info(f"No fixtures to process on {date_str}")
            continue

        print(f"   Processing {len(unique_fixtures)} matches on {date_str}...")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    process_single_match, match, date_str, args.odds_over, args.odds_under
                ): match
                for match in unique_fixtures
            }
            for future in as_completed(futures, timeout=600):
                try:
                    res = future.result(timeout=60)
                except Exception as e:
                    logger.error(f"Future timeout/error: {e}")
                    continue

                if res["status"] == "insufficient":
                    pass
                elif res["status"] == "success":
                    data = res["data"]

                    # Over 2.5 categorization
                    over_score = data["over"]["score"]
                    if over_score == 10:
                        if data["over"]["is_perfect"]:
                            over_perfect.append(data)
                        else:
                            over_qualified.append(data)
                    elif over_score >= 8:
                        over_close.append(data)
                    elif over_score >= 6:
                        over_weak.append(data)

                    # Under 2.5 categorization
                    under_score = data["under"]["score"]
                    if under_score == 8:
                        if data["under"]["is_perfect"]:
                            under_perfect.append(data)
                        else:
                            under_qualified.append(data)
                    elif under_score >= 7:
                        under_close.append(data)
                    elif under_score >= 6:
                        under_weak.append(data)

        # Early exit only if BOTH markets have sufficient matches
        over_total = len(over_perfect) + len(over_qualified)
        under_total = len(under_perfect) + len(under_qualified)

        if over_total >= 12 and under_total >= 8:
            logger.info(f"Reached targets: Over={over_total}, Under={under_total}. Stopping scan.")
            break
        elif over_total >= 15:
            logger.info(f"Over market saturated ({over_total}). Continuing scan for Under matches.")
            # Don't break - keep scanning for Under value

    # Apply portfolio Kelly cap separately for Over and Under
    apply_portfolio_kelly(over_perfect + over_qualified + over_close, "over", args.bankroll, MAX_TOTAL_EXPOSURE / 2)
    apply_portfolio_kelly(under_perfect + under_qualified + under_close, "under", args.bankroll, MAX_TOTAL_EXPOSURE / 2)

    # Build and output reports (both free and detailed)
    free_report, base_date, included_over, included_under = build_report(
        over_perfect, over_qualified, over_close, over_weak,
        under_perfect, under_qualified, under_close, under_weak,
        scanned_dates, args.bankroll, args.odds_over, args.odds_under, detailed=False
    )
    detailed_report, _, _, _ = build_report(
        over_perfect, over_qualified, over_close, over_weak,
        under_perfect, under_qualified, under_close, under_weak,
        scanned_dates, args.bankroll, args.odds_over, args.odds_under, detailed=True
    )

    # Output free report (default)
    print("\n===EMAIL_START===")
    print(free_report)
    print("===EMAIL_END===")

    # Save detailed report to file
    detailed_report_path = f"over_under_vip_report_{base_date}.txt"
    with open(detailed_report_path, "w", encoding="utf-8") as f:
        f.write(detailed_report)

    # Save JSON
    output_path = f"over_under_25_report_{base_date}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "metadata": {
                "scanned_window": scanned_dates,
                "bankroll": args.bankroll,
                "odds_over": args.odds_over,
                "odds_under": args.odds_under,
                "max_exposure": MAX_TOTAL_EXPOSURE,
                "under_caps": {
                    "home_total_6": UNDER_HOME_TOTAL_6_CAP,
                    "away_total_6": UNDER_AWAY_TOTAL_6_CAP,
                    "home_over25_max": UNDER_HOME_OVER25_MAX,
                    "away_over25_max": UNDER_AWAY_OVER25_MAX
                },
                "generated_at": datetime.now().isoformat()
            },
            "over": {
                "perfect": over_perfect,
                "qualified": over_qualified,
                "close": over_close,
                "weak": over_weak
            },
            "under": {
                "perfect": under_perfect,
                "qualified": under_qualified,
                "close": under_close,
                "weak": under_weak
            }
        }, f, indent=2, default=str)

    # Record predictions for tracking
    try:
        ou_picks = []
        # Record over picks
        for pick in included_over:
            ou_picks.append({
                "league": pick["match"]["league"],
                "home": pick["match"]["home"],
                "away": pick["match"]["away"],
                "date": pick["match"]["date"],
                "prediction": "over",
                "confidence": "perfect" if pick in over_perfect else ("qualified" if pick in over_qualified else "close")
            })
        # Record under picks
        for pick in included_under:
            ou_picks.append({
                "league": pick["match"]["league"],
                "home": pick["match"]["home"],
                "away": pick["match"]["away"],
                "date": pick["match"]["date"],
                "prediction": "under",
                "confidence": "perfect" if pick in under_perfect else ("qualified" if pick in under_qualified else "close")
            })
        stats = record_predictions(base_date, [], ou_picks)
        if stats["added"]:
            print(f"Predictions recorded for performance tracking ({stats['added']} new)")
        elif stats["skipped"]:
            print(f"Predictions already recorded ({stats['skipped']} skipped)")
    except Exception as e:
        print(f"Could not record predictions: {e}")

    print(f"\nReport saved: {output_path}")
    print(f"VIP report saved: {detailed_report_path}")


if __name__ == "__main__":
    main()
