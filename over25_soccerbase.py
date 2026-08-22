#!/usr/bin/env python3
"""
OVER/UNDER 2.5 GOALS PREDICTOR - UNIFIED v5
==============================================
Over 2.5: High-scoring rules + overall goal-activity filter (last 6)
Under 2.5: Low-scoring mirror rules + overall under 2.5 in 4/6
Shrinkage xG | Portfolio Kelly | SQLite Cache
"""

import requests
import json
import re
import argparse
import sys
import time
import random
import math
import logging
import os
import sqlite3
import hashlib
from datetime import datetime, timedelta
from collections import defaultdict
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from fake_useragent import UserAgent
from concurrent.futures import ThreadPoolExecutor, as_completed

# Import prediction tracker
from prediction_tracker import (
    record_predictions,
    format_vip_extra_lines,
    format_pick_block,
    format_compact_pick_line,
    format_confidence_label,
    describe_pick_categories,
    append_yesterday_section,
    PICK_TIER_PREMIUM,
    PICK_TIER_STRONG,
    PICK_TIER_VALUE,
    COMPACT_TIER_HEADER_PREMIUM,
    COMPACT_TIER_HEADER_STRONG,
    COMPACT_TIER_HEADER_WATCH,
    MARKET_SECTION_DIVIDER,
    MARKET_OVER25,
    MARKET_UNDER25,
)

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
MAX_OVER_SCORE = 13
MAX_UNDER_SCORE = 12

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
            away_link = cells[5].find("a", href=lambda h: h and "team_id=" in h)
            if not home_link:
                continue

            try:
                home_id_in_row = home_link["href"].split("team_id=")[1].split("&")[0]
                away_id_in_row = None
                if away_link:
                    away_id_in_row = away_link["href"].split("team_id=")[1].split("&")[0]
            except (KeyError, IndexError):
                continue

            is_home = str(home_id_in_row) == str(team_id)
            opponent_team_id = away_id_in_row if is_home else home_id_in_row
            gf = gf_h if is_home else gf_a
            ga = gf_a if is_home else gf_h

            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(cells[1]))
            date_str = date_match.group(1) if date_match else None

            matches.append({
                "gf": gf,
                "ga": ga,
                "total": gf + ga,
                "is_home": is_home,
                "date_str": date_str,
                "opponent_team_id": opponent_team_id,
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


def get_team_overall_form(team_id, num_matches=6, target_date_str=None):
    """Last N matches home or away combined as (gf, ga) tuples."""
    all_matches = fetch_soccerbase_team_results(team_id)
    target_dt = parse_date(target_date_str) if target_date_str else None
    form = []

    for match in all_matches:
        match_dt = parse_date(match.get("date_str"))
        if target_dt and match_dt and match_dt >= target_dt:
            continue
        form.append((match["gf"], match["ga"]))
        if len(form) >= num_matches:
            break

    return form


def _team_scored_every_match(form):
    sample = form[:6]
    if len(sample) < 4:
        return False
    min_required = len(sample)
    return all(gf >= 1 for gf, _ in sample[:min_required])


def _team_conceded_every_match(form):
    sample = form[:6]
    if len(sample) < 4:
        return False
    min_required = len(sample)
    return all(ga >= 1 for _, ga in sample[:min_required])


def _team_active_goal_profile(form):
    """Scored or conceded in every one of the last N matches (min 4 required)."""
    return _team_scored_every_match(form) or _team_conceded_every_match(form)


def _count_under_25_overall(form):
    return sum(1 for gf, ga in form[:6] if gf + ga < 2.5)


BTTS_MIN_6 = 3
NON_BTTS_MIN_6 = 3


def _count_btts(form):
    """Count matches in form where both teams scored (gf>0 AND ga>0 from team's view)."""
    return sum(1 for gf, ga in form[:6] if gf >= 1 and ga >= 1)


def _count_non_btts(form):
    """Count matches in form where at least one team blanked (gf=0 OR ga=0) — opposite of BTTS."""
    return sum(1 for gf, ga in form[:6] if gf == 0 or ga == 0)


def _count_over25(form):
    return sum(1 for gf, ga in form[:6] if gf + ga > 2.5)


def _count_under25(form):
    return sum(1 for gf, ga in form[:6] if gf + ga < 2.5)


def _thin_count(needed, of_window, available):
    if available >= of_window:
        return needed
    return max(1, int(needed * available / of_window))


def _thin_total(goal_sum, of_window, available):
    if available >= of_window:
        return goal_sum
    return max(1, int(goal_sum * available / of_window))


def _venue_over_gate_passes(home_6, away_6):
    """Hard gate: at least one side has ≥3/6 over 2.5 at venue (OR logic)."""
    h_over = _count_over25(home_6)
    a_over = _count_over25(away_6)
    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)
    if h_len >= 6 and a_len >= 6:
        return h_over >= 3 or a_over >= 3
    h_min = max(1, round(h_len * 0.5))
    a_min = max(1, round(a_len * 0.5))
    h_ok = h_len >= 2 and h_over >= h_min
    a_ok = a_len >= 2 and a_over >= a_min
    return h_ok or a_ok


def _high_scoring_blocks_under(home_6, away_6):
    """Hard veto on Under when either side's venue form is consistently high-scoring."""
    h_over = _count_over25(home_6)
    a_over = _count_over25(away_6)
    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)
    if h_len >= 6 and h_over >= 4:
        return True
    if a_len >= 6 and a_over >= 4:
        return True
    if h_len >= 2 and h_over >= max(2, round(h_len * 0.67)):
        return True
    if a_len >= 2 and a_over >= max(2, round(a_len * 0.67)):
        return True
    return False


def _recent_cold_blocks_over(home_3, away_3):
    """Veto Over when either team's last 2 venue games were low-scoring (≤2 goals)."""
    if len(home_3 or []) >= 2:
        if all(gf + ga <= 2 for gf, ga in home_3[:2]):
            return True
    if len(away_3 or []) >= 2:
        if all(gf + ga <= 2 for gf, ga in away_3[:2]):
            return True
    return False


def _btts_gate_passes(home_6, away_6):
    """BTTS gate with thin-data fallback: pass if home OR away meets venue BTTS threshold."""
    h_btts = _count_btts(home_6)
    a_btts = _count_btts(away_6)
    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)
    if h_len >= 6 and a_len >= 6:
        return h_btts >= BTTS_MIN_6 or a_btts >= BTTS_MIN_6
    h_min = max(1, round(h_len * 0.5))
    a_min = max(1, round(a_len * 0.5))
    h_ok = (h_len >= 6 and h_btts >= BTTS_MIN_6) or (h_len < 6 and h_len >= 2 and h_btts >= h_min)
    a_ok = (a_len >= 6 and a_btts >= BTTS_MIN_6) or (a_len < 6 and a_len >= 2 and a_btts >= a_min)
    return h_ok or a_ok


def _non_btts_gate_passes(home_6, away_6):
    """Mirror of BTTS gate: pass if home OR away has ≥3 non-BTTS in venue 6."""
    h_nb = _count_non_btts(home_6)
    a_nb = _count_non_btts(away_6)
    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)
    if h_len >= 6 and a_len >= 6:
        return h_nb >= NON_BTTS_MIN_6 or a_nb >= NON_BTTS_MIN_6
    h_min = max(1, round(h_len * 0.5))
    a_min = max(1, round(a_len * 0.5))
    h_ok = (h_len >= 6 and h_nb >= NON_BTTS_MIN_6) or (h_len < 6 and h_len >= 2 and h_nb >= h_min)
    a_ok = (a_len >= 6 and a_nb >= NON_BTTS_MIN_6) or (a_len < 6 and a_len >= 2 and a_nb >= a_min)
    return h_ok or a_ok


_OU_H2H_MAX_LOOKBACK = 6
_OU_H2H_MIN_MEETINGS = 3
_OU_H2H_OVER_BLOCK_RATE = 0.33
_OU_H2H_UNDER_BLOCK_RATE = 0.67

def get_h2h_meetings(home_team_id, away_team_id, target_date_str=None, limit=_OU_H2H_MAX_LOOKBACK):
    """Recent meetings between these sides, merged from both teams' result pages."""
    collected = {}
    for team_id, opponent_id in (
        (home_team_id, away_team_id),
        (away_team_id, home_team_id),
    ):
        for match in fetch_soccerbase_team_results(team_id):
            if str(match.get("opponent_team_id") or "") != str(opponent_id):
                continue
            match_date = match.get("date_str")
            if target_date_str and match_date and match_date >= target_date_str:
                continue
            key = (match_date, match.get("gf"), match.get("ga"), bool(match.get("is_home")))
            if key in collected:
                continue
            if str(team_id) == str(home_team_id):
                perspective = dict(match)
            else:
                perspective = {
                    **match,
                    "gf": match.get("ga"),
                    "ga": match.get("gf"),
                    "is_home": not match.get("is_home"),
                }
            collected[key] = perspective
    meetings = sorted(collected.values(), key=lambda m: m.get("date_str") or "", reverse=True)
    return meetings[:limit]


def _h2h_over_blocked(home_team_id, away_team_id, target_date_str=None):
    """Block Over 2.5 when recent H2H games are consistently low-scoring.

    Blocks if >=3 H2H meetings AND <=33% went Over 2.5 (bogey low-scoring matchup).
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str)
    if len(meetings) < _OU_H2H_MIN_MEETINGS:
        return False, meetings
    over_count = sum(1 for m in meetings if m.get("total", m.get("gf", 0) + m.get("ga", 0)) > 2.5)
    rate = over_count / len(meetings)
    return rate <= _OU_H2H_OVER_BLOCK_RATE, meetings


def _h2h_under_blocked(home_team_id, away_team_id, target_date_str=None):
    """Block Under 2.5 when recent H2H games are consistently high-scoring.

    Blocks if >=3 H2H meetings AND >=67% went Over 2.5 (bogey high-scoring matchup).
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str)
    if len(meetings) < _OU_H2H_MIN_MEETINGS:
        return False, meetings
    over_count = sum(1 for m in meetings if m.get("total", m.get("gf", 0) + m.get("ga", 0)) > 2.5)
    rate = over_count / len(meetings)
    return rate >= _OU_H2H_UNDER_BLOCK_RATE, meetings


# =============================================================================
# OVER 2.5 ALGORITHM (10-Check Rules + Overall Form)
# =============================================================================
def apply_over_algorithm(home_3, away_3, home_6, away_6, home_overall_6=None, away_overall_6=None):
    if len(home_3) < 2 or len(away_3) < 2:
        return None, None, {"error": "Insufficient data"}, False

    passed, failed, details = [], [], {}
    is_perfect = True

    hn3, an3 = len(home_3), len(away_3)

    # Home 3-game
    h_total_3 = sum(gf + ga for gf, ga in home_3)
    if h_total_3 >= _thin_total(7, 3, hn3):
        passed.append("Home total goals (last 3)"); details["Home total goals (last 3)"] = f"PASS ({h_total_3})"
    else:
        failed.append("Home total goals (last 3)"); details["Home total goals (last 3)"] = f"FAIL ({h_total_3})"; is_perfect = False

    h_over_3 = sum(1 for gf, ga in home_3 if gf + ga > 2.5)
    if h_over_3 >= _thin_count(2, 3, hn3):
        passed.append("Home over 2.5 (last 3)"); details["Home over 2.5 (last 3)"] = f"PASS ({h_over_3}/{hn3})"
        if h_over_3 < hn3:
            is_perfect = False
    else:
        failed.append("Home over 2.5 (last 3)"); details["Home over 2.5 (last 3)"] = f"FAIL ({h_over_3}/{hn3})"; is_perfect = False

    # Away 3-game
    a_total_3 = sum(gf + ga for gf, ga in away_3)
    if a_total_3 >= _thin_total(7, 3, an3):
        passed.append("Away total goals (last 3)"); details["Away total goals (last 3)"] = f"PASS ({a_total_3})"
    else:
        failed.append("Away total goals (last 3)"); details["Away total goals (last 3)"] = f"FAIL ({a_total_3})"; is_perfect = False

    prev_a_total = away_3[0][0] + away_3[0][1]
    if prev_a_total >= 2:
        passed.append("Away last match goals"); details["Away last match goals"] = f"PASS ({prev_a_total})"
    else:
        failed.append("Away last match goals"); details["Away last match goals"] = f"FAIL ({prev_a_total})"; is_perfect = False

    a_scored = sum(1 for gf, _ in away_3 if gf > 0)
    if a_scored >= _thin_count(2, 3, an3):
        passed.append("Away scored (last 3)"); details["Away scored (last 3)"] = f"PASS ({a_scored}/{an3})"
        if a_scored < an3:
            is_perfect = False
    else:
        failed.append("Away scored (last 3)"); details["Away scored (last 3)"] = f"FAIL ({a_scored}/{an3})"; is_perfect = False

    a_over_3 = sum(1 for gf, ga in away_3 if gf + ga > 2.5)
    if a_over_3 >= _thin_count(2, 3, an3):
        passed.append("Away over 2.5 (last 3)"); details["Away over 2.5 (last 3)"] = f"PASS ({a_over_3}/{an3})"
        if a_over_3 < an3:
            is_perfect = False
    else:
        failed.append("Away over 2.5 (last 3)"); details["Away over 2.5 (last 3)"] = f"FAIL ({a_over_3}/{an3})"; is_perfect = False

    # 6-game checks (proportional fallback when <6 venue games available)
    h_len = min(len(home_6), 6)
    if h_len >= 6:
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
    elif h_len >= 2:
        h_over_6 = sum(1 for gf, ga in home_6 if gf + ga > 2.5)
        h_min = max(1, round(h_len * 0.67))
        if h_over_6 >= h_min:
            passed.append("Home over 2.5 (last 6)"); details["Home over 2.5 (last 6)"] = f"PASS-THIN ({h_over_6}/{h_len} >= {h_min})"
            is_perfect = False
        else:
            failed.append("Home over 2.5 (last 6)"); details["Home over 2.5 (last 6)"] = f"FAIL-THIN ({h_over_6}/{h_len} < {h_min})"; is_perfect = False

        h_total_6 = sum(gf + ga for gf, ga in home_6)
        h_total_min = max(6, round(h_len * 3))
        if h_total_6 >= h_total_min:
            passed.append("Home total goals (last 6)"); details["Home total goals (last 6)"] = f"PASS-THIN ({h_total_6} >= {h_total_min})"
            is_perfect = False
        else:
            failed.append("Home total goals (last 6)"); details["Home total goals (last 6)"] = f"FAIL-THIN ({h_total_6} < {h_total_min})"; is_perfect = False

    a_len = min(len(away_6), 6)
    if a_len >= 6:
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
    elif a_len >= 2:
        a_over_6 = sum(1 for gf, ga in away_6 if gf + ga > 2.5)
        a_min = max(1, round(a_len * 0.67))
        if a_over_6 >= a_min:
            passed.append("Away over 2.5 (last 6)"); details["Away over 2.5 (last 6)"] = f"PASS-THIN ({a_over_6}/{a_len} >= {a_min})"
            is_perfect = False
        else:
            failed.append("Away over 2.5 (last 6)"); details["Away over 2.5 (last 6)"] = f"FAIL-THIN ({a_over_6}/{a_len} < {a_min})"; is_perfect = False

        a_total_6 = sum(gf + ga for gf, ga in away_6)
        a_total_min = max(6, round(a_len * 3))
        if a_total_6 >= a_total_min:
            passed.append("Away total goals (last 6)"); details["Away total goals (last 6)"] = f"PASS-THIN ({a_total_6} >= {a_total_min})"
            is_perfect = False
        else:
            failed.append("Away total goals (last 6)"); details["Away total goals (last 6)"] = f"FAIL-THIN ({a_total_6} < {a_total_min})"; is_perfect = False

    home_overall_6 = home_overall_6 or []
    away_overall_6 = away_overall_6 or []
    home_active = _team_active_goal_profile(home_overall_6)
    away_active = _team_active_goal_profile(away_overall_6)
    if home_active or away_active:
        teams = []
        if home_active:
            if _team_scored_every_match(home_overall_6):
                teams.append("home scored each")
            else:
                teams.append("home conceded each")
        if away_active:
            if _team_scored_every_match(away_overall_6):
                teams.append("away scored each")
            else:
                teams.append("away conceded each")
        passed.append("Overall goal activity (6)")
        details["Overall goal activity (6)"] = f"PASS ({', '.join(teams)})"
    else:
        h_len = min(len(home_overall_6), 6)
        a_len = min(len(away_overall_6), 6)
        if h_len < 2 and a_len < 2:
            details["Overall goal activity (6)"] = f"SKIPPED (only {h_len}/{a_len} overall matches)"
        else:
            failed.append("Overall goal activity (6)")
            details["Overall goal activity (6)"] = f"FAIL (neither team had activity in all available: {h_len}/{a_len})"
            is_perfect = False

    # BTTS rules: both teams scored (home 6 home, away 6 away). Thin-data: use >=50% of available (min 2 games).
    h_btts = _count_btts(home_6)
    h_len = min(len(home_6), 6)
    if h_len >= 6:
        if h_btts >= BTTS_MIN_6:
            passed.append("Home BTTS (last 6 home)")
            details["Home BTTS (last 6 home)"] = f"PASS ({h_btts}/6)"
            if h_btts < 6:
                is_perfect = False
        else:
            failed.append("Home BTTS (last 6 home)")
            details["Home BTTS (last 6 home)"] = f"FAIL ({h_btts}/6)"
            is_perfect = False
    elif h_len >= 2:
        h_min = max(1, round(h_len * 0.5))
        if h_btts >= h_min:
            passed.append("Home BTTS (last 6 home)")
            details["Home BTTS (last 6 home)"] = f"PASS-THIN ({h_btts}/{h_len} >= {h_min})"
        else:
            failed.append("Home BTTS (last 6 home)")
            details["Home BTTS (last 6 home)"] = f"FAIL-THIN ({h_btts}/{h_len} < {h_min})"
            is_perfect = False

    a_btts = _count_btts(away_6)
    a_len = min(len(away_6), 6)
    if a_len >= 6:
        if a_btts >= BTTS_MIN_6:
            passed.append("Away BTTS (last 6 away)")
            details["Away BTTS (last 6 away)"] = f"PASS ({a_btts}/6)"
            if a_btts < 6:
                is_perfect = False
        else:
            failed.append("Away BTTS (last 6 away)")
            details["Away BTTS (last 6 away)"] = f"FAIL ({a_btts}/6)"
            is_perfect = False
    elif a_len >= 2:
        a_min = max(1, round(a_len * 0.5))
        if a_btts >= a_min:
            passed.append("Away BTTS (last 6 away)")
            details["Away BTTS (last 6 away)"] = f"PASS-THIN ({a_btts}/{a_len} >= {a_min})"
        else:
            failed.append("Away BTTS (last 6 away)")
            details["Away BTTS (last 6 away)"] = f"FAIL-THIN ({a_btts}/{a_len} < {a_min})"
            is_perfect = False

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
    if len(home_3) < 2 or len(away_3) < 2:
        return None, None, {"error": "Insufficient data"}, False

    passed, failed, details = [], [], {}
    is_perfect = True

    hn3, an3 = len(home_3), len(away_3)

    # --- 3-GAME HOME CHECKS ---
    # At least 2 of 3 home games under 2.5
    h_under_3 = sum(1 for gf, ga in home_3 if gf + ga < 2.5)
    if h_under_3 >= _thin_count(2, 3, hn3):
        passed.append("Home under 2.5 (last 3)"); details["Home under 2.5 (last 3)"] = f"PASS ({h_under_3}/{hn3} under 2.5)"
        if h_under_3 < hn3:
            is_perfect = False
    else:
        failed.append("Home under 2.5 (last 3)"); details["Home under 2.5 (last 3)"] = f"FAIL ({h_under_3}/{hn3} under 2.5)"; is_perfect = False

    # At least one score line has 0 goals in the 3 home games
    h_has_zero = any(gf == 0 or ga == 0 for gf, ga in home_3)
    if h_has_zero:
        passed.append("Home has 0-goal side (last 3)"); details["Home has 0-goal side (last 3)"] = "PASS (at least one 0-goal side)"
    else:
        failed.append("Home has 0-goal side (last 3)"); details["Home has 0-goal side (last 3)"] = "FAIL (no zero-goal sides)"; is_perfect = False

    # --- 3-GAME AWAY CHECKS ---
    # At least 2 of 3 away games under 2.5
    a_under_3 = sum(1 for gf, ga in away_3 if gf + ga < 2.5)
    if a_under_3 >= _thin_count(2, 3, an3):
        passed.append("Away under 2.5 (last 3)"); details["Away under 2.5 (last 3)"] = f"PASS ({a_under_3}/{an3} under 2.5)"
        if a_under_3 < an3:
            is_perfect = False
    else:
        failed.append("Away under 2.5 (last 3)"); details["Away under 2.5 (last 3)"] = f"FAIL ({a_under_3}/{an3} under 2.5)"; is_perfect = False

    # Away team did NOT score in at least 1 of last 3 away games
    a_blanked = sum(1 for gf, _ in away_3 if gf == 0)
    if a_blanked >= _thin_count(1, 3, an3):
        passed.append("Away blanked (last 3)"); details["Away blanked (last 3)"] = f"PASS ({a_blanked}/{an3} away games with 0 scored)"
    else:
        failed.append("Away blanked (last 3)"); details["Away blanked (last 3)"] = f"FAIL ({a_blanked}/{an3} away games with 0 scored)"; is_perfect = False

    return passed, failed, details, is_perfect


def apply_under_6game_checks(home_6, away_6):
    """
    6-game average checks for Under 2.5 (supplementary to 3-game rules).
    These are NOT part of the official over25tips.com algorithm but add
    statistical rigor for classification tiers.

    Added UC5/UC6 (non-BTTS / clean-sheet games): direct opposite of Over's BTTS checks.
    A match where at least one team blanked (gf=0 OR ga=0) is the statistical
    complement of a BTTS match and strongly correlates with Under 2.5 outcomes.
    """
    passed, failed, details = [], [], {}
    is_perfect = True

    home_6 = home_6 or []
    away_6 = away_6 or []

    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)

    if h_len >= 6 and a_len >= 6:
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

        # Non-BTTS 6-game (home): at least NON_BTTS_MIN_6 matches where one team blanked (opposite of Over BTTS)
        h_non_btts = _count_non_btts(home_6)
        if h_non_btts >= NON_BTTS_MIN_6:
            passed.append("Home non-BTTS (last 6 home)")
            details["Home non-BTTS (last 6 home)"] = f"PASS ({h_non_btts}/6 blanked matches)"
            if h_non_btts < 6:
                is_perfect = False
        else:
            failed.append("Home non-BTTS (last 6 home)")
            details["Home non-BTTS (last 6 home)"] = f"FAIL ({h_non_btts}/6 blanked matches < {NON_BTTS_MIN_6})"
            is_perfect = False

        # Non-BTTS 6-game (away): at least NON_BTTS_MIN_6 matches where one team blanked
        a_non_btts = _count_non_btts(away_6)
        if a_non_btts >= NON_BTTS_MIN_6:
            passed.append("Away non-BTTS (last 6 away)")
            details["Away non-BTTS (last 6 away)"] = f"PASS ({a_non_btts}/6 blanked matches)"
            if a_non_btts < 6:
                is_perfect = False
        else:
            failed.append("Away non-BTTS (last 6 away)")
            details["Away non-BTTS (last 6 away)"] = f"FAIL ({a_non_btts}/6 blanked matches < {NON_BTTS_MIN_6})"
            is_perfect = False
    else:
        # Thin-data pass: proportional non-BTTS check on what's available (min 2 games, >=50% non-BTTS)
        details["UC1-4"] = f"SKIPPED-THIN ({h_len}/{a_len} 6-game averages)"
        if h_len >= 2:
            h_non_btts = _count_non_btts(home_6)
            h_min = max(1, round(h_len * 0.5))
            if h_non_btts >= h_min:
                passed.append("Home non-BTTS (last 6 home)")
                details["Home non-BTTS (last 6 home)"] = f"PASS-THIN ({h_non_btts}/{h_len} >= {h_min})"
            else:
                failed.append("Home non-BTTS (last 6 home)")
                details["Home non-BTTS (last 6 home)"] = f"FAIL-THIN ({h_non_btts}/{h_len} < {h_min})"
                is_perfect = False

        if a_len >= 2:
            a_non_btts = _count_non_btts(away_6)
            a_min = max(1, round(a_len * 0.5))
            if a_non_btts >= a_min:
                passed.append("Away non-BTTS (last 6 away)")
                details["Away non-BTTS (last 6 away)"] = f"PASS-THIN ({a_non_btts}/{a_len} >= {a_min})"
            else:
                failed.append("Away non-BTTS (last 6 away)")
                details["Away non-BTTS (last 6 away)"] = f"FAIL-THIN ({a_non_btts}/{a_len} < {a_min})"
                is_perfect = False

    return passed, failed, details, is_perfect


def apply_under_overall_checks(home_overall_6, away_overall_6):
    """Each team must have under 2.5 in at least 4 of last 6 overall matches. Thin-data fallback included."""
    passed, failed, details = [], [], {}
    is_perfect = True

    home_overall_6 = home_overall_6 or []
    away_overall_6 = away_overall_6 or []

    h_len = min(len(home_overall_6), 6)
    if h_len >= 6:
        h_under = _count_under_25_overall(home_overall_6)
        if h_under >= 4:
            passed.append("Home under 2.5 overall (6)")
            details["Home under 2.5 overall (6)"] = f"PASS ({h_under}/6 under 2.5)"
            if h_under < 6:
                is_perfect = False
        else:
            failed.append("Home under 2.5 overall (6)")
            details["Home under 2.5 overall (6)"] = f"FAIL ({h_under}/6 under 2.5)"
            is_perfect = False
    elif h_len >= 2:
        h_under = _count_under_25_overall(home_overall_6)
        h_min = max(1, round(h_len * 0.6))
        if h_under >= h_min:
            passed.append("Home under 2.5 overall (6)")
            details["Home under 2.5 overall (6)"] = f"PASS-THIN ({h_under}/{h_len} >= {h_min})"
        else:
            failed.append("Home under 2.5 overall (6)")
            details["Home under 2.5 overall (6)"] = f"FAIL-THIN ({h_under}/{h_len} < {h_min})"
            is_perfect = False

    a_len = min(len(away_overall_6), 6)
    if a_len >= 6:
        a_under = _count_under_25_overall(away_overall_6)
        if a_under >= 4:
            passed.append("Away under 2.5 overall (6)")
            details["Away under 2.5 overall (6)"] = f"PASS ({a_under}/6 under 2.5)"
            if a_under < 6:
                is_perfect = False
        else:
            failed.append("Away under 2.5 overall (6)")
            details["Away under 2.5 overall (6)"] = f"FAIL ({a_under}/6 under 2.5)"
            is_perfect = False
    elif a_len >= 2:
        a_under = _count_under_25_overall(away_overall_6)
        a_min = max(1, round(a_len * 0.6))
        if a_under >= a_min:
            passed.append("Away under 2.5 overall (6)")
            details["Away under 2.5 overall (6)"] = f"PASS-THIN ({a_under}/{a_len} >= {a_min})"
        else:
            failed.append("Away under 2.5 overall (6)")
            details["Away under 2.5 overall (6)"] = f"FAIL-THIN ({a_under}/{a_len} < {a_min})"
            is_perfect = False

    return passed, failed, details, is_perfect


# =============================================================================
# POISSON MODEL (with Dixon-Coles correction)
# =============================================================================
DIXON_COLES_RHO = -0.13

_MIN_DATA_GAMES = 5
_MIN_COMBINED_LAMBDA_OVER = 2.75
_MAX_COMBINED_LAMBDA_UNDER = 2.15
_PREMIUM_COMBINED_LAMBDA_OVER = 3.30
_PREMIUM_COMBINED_LAMBDA_UNDER = 2.00

IMPLIED_ODDS_OVER = 1.95
IMPLIED_ODDS_UNDER = 1.85

_WEIGHT_RULES = 0.40
_WEIGHT_MODEL = 0.40
_WEIGHT_EDGE = 0.20

_TIER_PREMIUM_CUTOFF = 0.62
_TIER_SOLID_CUTOFF = 0.54

_MIN_FORM_HALFLIFE = 3.0

_WEAK_ROI_LEAGUE_KEYWORDS = (
    "swedish allsvenskan",
    "allsvenskan",
    "superettan",
    "belarus",
    "k-league",
    "k league",
    "league of ireland",
    "fai cup",
    "mexican primera",
    "brazilian serie a",
    "mls",
    "ecuador",
    "argentina primera",
    "chile primera",
)
_WEAK_ROI_MULTIPLIER = 0.82
_WEAK_ROI_OVER_LAMBDA_BOOST = 0.30
_WEAK_ROI_UNDER_LAMBDA_REDUCTION = 0.20

_REGRESSION_OVER_STREAK = 5
_REGRESSION_UNDER_STREAK = 5
_REGRESSION_PENALTY = 0.08

_LEAGUE_BASELINE_CACHE = {}


def _exponential_form_averages(form_tuples, halflife=_MIN_FORM_HALFLIFE):
    """Weighted average of (gf, ga) with exponential decay.

    form_tuples[0] is the most recent match (weight=1.0); each older match
    is multiplied by 0.5 ** (n / halflife) for match index n going back.
    Returns (weighted_gf_per_game, weighted_ga_per_game, effective_sample_weight).
    """
    if not form_tuples:
        return 0.0, 0.0, 0.0
    w_sum = 0.0
    gf_sum = 0.0
    ga_sum = 0.0
    for idx, (gf, ga) in enumerate(form_tuples):
        w = 0.5 ** (idx / halflife)
        w_sum += w
        gf_sum += w * gf
        ga_sum += w * ga
    if w_sum <= 0:
        return 0.0, 0.0, 0.0
    return gf_sum / w_sum, ga_sum / w_sum, w_sum


def _is_weak_roi_league(league_name):
    name = str(league_name or "").strip().lower()
    return any(k in name for k in _WEAK_ROI_LEAGUE_KEYWORDS)


def _over_streak_count(form_tuples):
    """How many of the most recent form games were consecutively Over 2.5?"""
    n = 0
    for gf, ga in form_tuples:
        if gf + ga > 2.5:
            n += 1
        else:
            break
    return n


def _under_streak_count(form_tuples):
    n = 0
    for gf, ga in form_tuples:
        if gf + ga < 2.5:
            n += 1
        else:
            break
    return n


def regression_penalty(home_6, away_6, side):
    """0..1 multiplicative penalty applied to confidence score; 1.0 = no penalty."""
    if side == "over":
        h_streak = _over_streak_count(home_6 or [])
        a_streak = _over_streak_count(away_6 or [])
        if max(h_streak, a_streak) >= _REGRESSION_OVER_STREAK:
            return 1.0 - _REGRESSION_PENALTY
    elif side == "under":
        h_streak = _under_streak_count(home_6 or [])
        a_streak = _under_streak_count(away_6 or [])
        if max(h_streak, a_streak) >= _REGRESSION_UNDER_STREAK:
            return 1.0 - _REGRESSION_PENALTY
    return 1.0


def _chaos_rule_over(home_6, away_6):
    """Both sides concede heavily: 85% of their matches involve goals against.

    Returns True if BOTH teams conceded on average >= 1.2 goals/game in 6-game
    form. This is the 'chaotic open game' signal independent of who scores.
    """
    if len(home_6 or []) < 2 or len(away_6 or []) < 2:
        return False
    hc = sum(ga for _, ga in home_6) / max(len(home_6), 1)
    ac = sum(ga for _, ga in away_6) / max(len(away_6), 1)
    return hc >= 1.2 and ac >= 1.2


def _compact_rule_under(home_6, away_6):
    """Both sides attack AND defense are stingy.

    Returns True if BOTH teams scored <= 1.0/game AND conceded <= 0.9/game
    average over their form window.
    """
    if len(home_6 or []) < 2 or len(away_6 or []) < 2:
        return False
    hs = sum(gf for gf, _ in home_6) / max(len(home_6), 1)
    hc = sum(ga for _, ga in home_6) / max(len(home_6), 1)
    as_ = sum(gf for gf, _ in away_6) / max(len(away_6), 1)
    ac = sum(ga for _, ga in away_6) / max(len(away_6), 1)
    return hs <= 1.0 and hc <= 0.9 and as_ <= 1.0 and ac <= 0.9



def poisson_pmf(k, lam):
    if lam <= 0:
        return 0.0
    return (math.exp(-lam) * (lam ** k)) / math.factorial(k)


def _dixon_coles_tau(h, a, lh, la, rho):
    if (h, a) == (0, 0):
        return max(0.01, 1.0 - rho * lh * la)
    if (h, a) == (1, 0):
        return 1.0 + rho * la
    if (h, a) == (0, 1):
        return 1.0 + rho * lh
    if (h, a) == (1, 1):
        return 1.0 - rho
    return 1.0


def calculate_poisson_over25(home_lambda, away_lambda, max_goals=10):
    over_prob = 0.0
    total = 0.0
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            joint = poisson_pmf(h, home_lambda) * poisson_pmf(a, away_lambda)
            if joint <= 0:
                continue
            tau = _dixon_coles_tau(h, a, home_lambda, away_lambda, DIXON_COLES_RHO)
            p = joint * tau
            total += p
            if h + a > 2:
                over_prob += p
    if total > 0:
        over_prob /= total
    return round(over_prob * 100, 1)


def calculate_poisson_under25(home_lambda, away_lambda, max_goals=10):
    over_prob = calculate_poisson_over25(home_lambda, away_lambda, max_goals) / 100.0
    return round((1.0 - over_prob) * 100, 1)


def _load_league_baselines():
    """Compute per-league home/away goals baselines from prediction_history settled picks.

    Falls back to global defaults if a league has < 5 settled matches or the file
    is missing. Returns a dict: league_name -> (h_att, a_att, h_def, a_def)
    """
    if _LEAGUE_BASELINE_CACHE:
        return _LEAGUE_BASELINE_CACHE
    default = (1.45, 1.20, 1.35, 1.25)
    history_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), HISTORY_FILE_FALLBACK)
    if not os.path.exists(history_path):
        _LEAGUE_BASELINE_CACHE["_default"] = default
        return _LEAGUE_BASELINE_CACHE
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        _LEAGUE_BASELINE_CACHE["_default"] = default
        return _LEAGUE_BASELINE_CACHE
    league_stats = defaultdict(lambda: {"h_gf": 0.0, "h_ga": 0.0, "a_gf": 0.0, "a_ga": 0.0, "n": 0})
    for market in ("home_win", "over_under"):
        for row in data.get(market, []) or []:
            score = row.get("final_score") or row.get("result_source")
            final_score = row.get("final_score")
            if not final_score or "-" not in str(final_score):
                continue
            try:
                hg, ag = str(final_score).split("-", 1)
                hg = int(hg.strip())
                ag = int(ag.strip())
            except ValueError:
                continue
            lg = row.get("league", "")
            if not lg:
                continue
            s = league_stats[lg]
            s["h_gf"] += hg
            s["h_ga"] += ag
            s["a_gf"] += ag
            s["a_ga"] += hg
            s["n"] += 1
    global_h_gf = sum(s["h_gf"] for s in league_stats.values())
    global_h_ga = sum(s["h_ga"] for s in league_stats.values())
    global_a_gf = sum(s["a_gf"] for s in league_stats.values())
    global_a_ga = sum(s["a_ga"] for s in league_stats.values())
    global_n = max(1, sum(s["n"] for s in league_stats.values()))
    fallback = (
        global_h_gf / global_n,
        global_a_gf / global_n,
        global_h_ga / global_n,
        global_a_ga / global_n,
    )
    if not all(fallback) or fallback[0] < 0.6 or fallback[0] > 2.5:
        fallback = default
    _LEAGUE_BASELINE_CACHE["_default"] = fallback
    for lg, s in league_stats.items():
        n = s["n"]
        if n < 5:
            _LEAGUE_BASELINE_CACHE[lg] = fallback
            continue
        ha = s["h_gf"] / n
        aa = s["a_gf"] / n
        hd = s["h_ga"] / n
        ad = s["a_ga"] / n
        if ha < 0.5 or aa < 0.4 or hd < 0.4 or ad < 0.4:
            _LEAGUE_BASELINE_CACHE[lg] = fallback
            continue
        _LEAGUE_BASELINE_CACHE[lg] = (ha, aa, hd, ad)
    return _LEAGUE_BASELINE_CACHE


HISTORY_FILE_FALLBACK = "prediction_history.json"


def _league_baselines(league_name):
    cache = _load_league_baselines()
    return cache.get(league_name, cache.get("_default", (1.45, 1.20, 1.35, 1.25)))


# =============================================================================
# EXPECTED GOALS (Shrinkage Estimator, per-league baselines)
# =============================================================================
def get_match_lambdas(home_6, away_6, league_name=None):
    """
    Calculate cross-matched Poisson lambdas.

    Uses exponential-decay weighted form averages (recent games count ~3x more
    than games from 6 weeks back) plus per-league baselines from history.
    Weak-ROI leagues have baselines nudged to make the combined-lambda gate
    harder to satisfy on the historically unprofitable side.
    """
    bl = _league_baselines(league_name or "")
    home_baseline_attack, away_baseline_attack, home_baseline_defense, away_baseline_defense = bl

    weak_league = _is_weak_roi_league(league_name)

    n_home = max(len(home_6 or []), 1)
    n_away = max(len(away_6 or []), 1)

    h_gf_avg, h_ga_avg, _ = _exponential_form_averages(home_6 or [])
    if not (home_6 or []):
        h_gf_avg, h_ga_avg = home_baseline_attack, away_baseline_defense
    a_gf_avg, a_ga_avg, _ = _exponential_form_averages(away_6 or [])
    if not (away_6 or []):
        a_gf_avg, a_ga_avg = away_baseline_attack, home_baseline_defense

    adaptive_shrinkage = SHRINKAGE_WEIGHT
    if len(home_6 or []) < _MIN_DATA_GAMES or len(away_6 or []) < _MIN_DATA_GAMES:
        adaptive_shrinkage = max(0.45, SHRINKAGE_WEIGHT - 0.15)

    h_attack = adaptive_shrinkage * h_gf_avg + (1 - adaptive_shrinkage) * home_baseline_attack
    h_defense = adaptive_shrinkage * h_ga_avg + (1 - adaptive_shrinkage) * away_baseline_defense

    a_attack = adaptive_shrinkage * a_gf_avg + (1 - adaptive_shrinkage) * away_baseline_attack
    a_defense = adaptive_shrinkage * a_ga_avg + (1 - adaptive_shrinkage) * home_baseline_defense

    home_lambda = h_attack * (a_defense / max(0.5, home_baseline_attack))
    away_lambda = a_attack * (h_defense / max(0.5, away_baseline_attack))

    if weak_league:
        home_lambda -= _WEAK_ROI_OVER_LAMBDA_BOOST / 2.0
        away_lambda -= _WEAK_ROI_OVER_LAMBDA_BOOST / 2.0

    return (
        round(max(0.5, min(3.8, home_lambda)), 2),
        round(max(0.5, min(3.8, away_lambda)), 2),
    )


def data_volume_penalty(home_6, away_6):
    """Return a 0..1 multiplier. 1.0 = full data, <1.0 = thin-data penalty."""
    n = min(len(home_6 or []), len(away_6 or []))
    if n >= _MIN_DATA_GAMES:
        return 1.0
    if n >= 4:
        return 0.97
    if n >= 3:
        return 0.90
    if n >= 2:
        return 0.82
    return 0.70


def lambda_gate_passes(home_lambda, away_lambda, side):
    combined = home_lambda + away_lambda
    if side == "over":
        return combined >= _MIN_COMBINED_LAMBDA_OVER
    if side == "under":
        return combined <= _MAX_COMBINED_LAMBDA_UNDER
    return True


def compute_confidence_score(rule_score, max_score, model_prob_pct, decimal_odds, data_mult=1.0):
    """Weighted 0..1 confidence score combining rules, Poisson probability and value edge."""
    rule_component = max(0.0, min(1.0, (rule_score / max(max_score, 1))))
    model_component = max(0.0, min(1.0, (model_prob_pct / 100.0)))
    implied = 1.0 / max(1.05, decimal_odds)
    edge_component = max(0.0, min(1.0, ((model_prob_pct / 100.0) - implied) + 0.5))
    raw = _WEIGHT_RULES * rule_component + _WEIGHT_MODEL * model_component + _WEIGHT_EDGE * edge_component
    return max(0.0, min(1.0, raw * data_mult))


def tier_from_confidence(score, side, home_lambda, away_lambda, is_perfect=True):
    combined = home_lambda + away_lambda
    premium_ok = False
    if side == "over" and combined >= _PREMIUM_COMBINED_LAMBDA_OVER:
        premium_ok = True
    elif side == "under" and combined <= _PREMIUM_COMBINED_LAMBDA_UNDER:
        premium_ok = True
    if score >= _TIER_PREMIUM_CUTOFF and premium_ok and is_perfect:
        return "perfect"
    if score >= _TIER_SOLID_CUTOFF:
        return "qualified"
    return "close"


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
        league_name = match.get("league", "")

        home_3 = get_team_form(match["home_team_id"], True, 3, target_date)
        away_3 = get_team_form(match["away_team_id"], False, 3, target_date)
        home_6 = get_team_form(match["home_team_id"], True, 6, target_date)
        away_6 = get_team_form(match["away_team_id"], False, 6, target_date)
        home_overall_6 = get_team_overall_form(match["home_team_id"], 6, target_date)
        away_overall_6 = get_team_overall_form(match["away_team_id"], 6, target_date)

        data_mult = data_volume_penalty(home_6, away_6)

        over_passed, over_failed, over_details, over_is_perfect = apply_over_algorithm(
            home_3, away_3, home_6, away_6, home_overall_6, away_overall_6
        )

        over_score = len(over_passed) if over_passed else 0
        if _chaos_rule_over(home_6, away_6):
            over_score += 1

        home_btts_6 = _count_btts(home_6)
        away_btts_6 = _count_btts(away_6)
        home_non_btts_6 = _count_non_btts(home_6)
        away_non_btts_6 = _count_non_btts(away_6)
        btts_gate = _btts_gate_passes(home_6, away_6)
        non_btts_gate = _non_btts_gate_passes(home_6, away_6)
        venue_over_gate = _venue_over_gate_passes(home_6, away_6)
        high_scoring_under_block = _high_scoring_blocks_under(home_6, away_6)
        recent_cold_over_block = _recent_cold_blocks_over(home_3, away_3)
        h2h_over_blocked, h2h_over_meetings = _h2h_over_blocked(
            match["home_team_id"], match["away_team_id"], target_date
        )
        h2h_under_blocked, h2h_under_meetings = _h2h_under_blocked(
            match["home_team_id"], match["away_team_id"], target_date
        )

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

        under_overall_passed, under_overall_failed, under_overall_details, under_overall_perfect = (
            apply_under_overall_checks(home_overall_6, away_overall_6)
        )

        under_passed = under3_passed + under6_passed + under_overall_passed
        under_failed = under3_failed + under6_failed + under_overall_failed
        under_details = {**under3_details, **under6_details, **under_overall_details}
        under_is_perfect = under3_perfect and under6_perfect and under_overall_perfect

        under_score = len(under_passed) if under_passed else 0
        if _compact_rule_under(home_6, away_6):
            under_score += 1

        home_lambda, away_lambda = get_match_lambdas(home_6, away_6, league_name=league_name)

        over_gate = lambda_gate_passes(home_lambda, away_lambda, "over")
        under_gate = lambda_gate_passes(home_lambda, away_lambda, "under")

        data_quality_min = min(
            len(home_6), len(away_6),
            len(home_overall_6), len(away_overall_6)
        )
        thin_data_gap = max(0, 6 - data_quality_min)
        base_over_min = MAX_OVER_SCORE - 3 if _is_weak_roi_league(league_name) else MAX_OVER_SCORE - 4
        base_under_min = MAX_UNDER_SCORE - 2 if _is_weak_roi_league(league_name) else MAX_UNDER_SCORE - 3
        over_min_score = max(6, base_over_min - thin_data_gap)
        under_min_score = max(5, base_under_min - thin_data_gap)
        over_qualifies = (
            bool(over_passed) and over_score >= over_min_score and over_gate
            and btts_gate and venue_over_gate and not recent_cold_over_block
            and not h2h_over_blocked
        )
        under_qualifies = (
            bool(under_passed) and under_score >= under_min_score and under_gate
            and non_btts_gate and not high_scoring_under_block
            and not h2h_under_blocked
        )

        if over_qualifies or under_qualifies:
            over25_prob_pct = calculate_poisson_over25(home_lambda, away_lambda)
            under25_prob_pct = calculate_poisson_under25(home_lambda, away_lambda)
        else:
            combined = home_lambda + away_lambda
            if combined > 2.5:
                over25_prob_pct = round(max(50.0, 50.0 + (combined - 2.5) * 15), 1)
                under25_prob_pct = round(100.0 - over25_prob_pct, 1)
            else:
                under25_prob_pct = round(max(50.0, 50.0 + (2.5 - combined) * 15), 1)
                over25_prob_pct = round(100.0 - under25_prob_pct, 1)

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

        league_mult = _WEAK_ROI_MULTIPLIER if _is_weak_roi_league(league_name) else 1.0
        over_regression_penalty = regression_penalty(home_6, away_6, "over")
        under_regression_penalty = regression_penalty(home_6, away_6, "under")

        over_final_mult = data_mult * league_mult * over_regression_penalty
        under_final_mult = data_mult * league_mult * under_regression_penalty

        over_conf_score = compute_confidence_score(
            over_score, MAX_OVER_SCORE, over25_prob_pct, default_odds_over, over_final_mult
        )
        under_conf_score = compute_confidence_score(
            under_score, MAX_UNDER_SCORE, under25_prob_pct, default_odds_under, under_final_mult
        )

        over_tier = tier_from_confidence(over_conf_score, "over", home_lambda, away_lambda, over_is_perfect)
        under_tier = tier_from_confidence(under_conf_score, "under", home_lambda, away_lambda, under_is_perfect)

        over_kelly = calculate_kelly(over25_prob, default_odds_over, use_half=True)
        under_kelly = calculate_kelly(under25_prob, default_odds_under, use_half=True)

        if not over_qualifies:
            over_tier = None
            over_kelly = 0.0
        if not under_qualifies:
            under_tier = None
            under_kelly = 0.0

        regressions = []
        if over_regression_penalty < 1.0:
            regressions.append("over streak")
        if under_regression_penalty < 1.0:
            regressions.append("under streak")
        if recent_cold_over_block:
            regressions.append("recent cold scoring form")
        if high_scoring_under_block:
            regressions.append("high-scoring venue form")
        if h2h_over_blocked:
            regressions.append(f"h2h low-scoring bogey ({len(h2h_over_meetings)} meetings)")
        if h2h_under_blocked:
            regressions.append(f"h2h high-scoring bogey ({len(h2h_under_meetings)} meetings)")

        return {
            "status": "success",
            "data": {
                "match": match,
                "over": {
                    "score": over_score,
                    "passed": over_passed,
                    "details": over_details,
                    "is_perfect": over_is_perfect,
                    "tier": over_tier,
                    "confidence_score": round(over_conf_score * 100, 1),
                    "prob": over25_prob_pct,
                    "confidence": over_confidence,
                    "kelly": round(over_kelly * 100, 2),
                    "gate_passed": over_gate,
                    "btts_gate_passed": btts_gate,
                    "venue_over_gate_passed": venue_over_gate,
                    "recent_cold_blocked": recent_cold_over_block,
                    "h2h_blocked": h2h_over_blocked,
                    "h2h_meetings": len(h2h_over_meetings),
                    "home_btts_6": home_btts_6,
                    "away_btts_6": away_btts_6,
                    "data_mult": round(data_mult, 2),
                    "weak_league_mult": round(league_mult, 2),
                    "regression_mult": round(over_regression_penalty, 2),
                    "min_score_threshold": over_min_score,
                },
                "under": {
                    "score": under_score,
                    "passed": under_passed,
                    "details": under_details,
                    "is_perfect": under_is_perfect,
                    "tier": under_tier,
                    "confidence_score": round(under_conf_score * 100, 1),
                    "prob": under25_prob_pct,
                    "confidence": under_confidence,
                    "kelly": round(under_kelly * 100, 2),
                    "gate_passed": under_gate,
                    "non_btts_gate_passed": non_btts_gate,
                    "high_scoring_blocked": high_scoring_under_block,
                    "h2h_blocked": h2h_under_blocked,
                    "h2h_meetings": len(h2h_under_meetings),
                    "home_non_btts_6": home_non_btts_6,
                    "away_non_btts_6": away_non_btts_6,
                    "data_mult": round(data_mult, 2),
                    "weak_league_mult": round(league_mult, 2),
                    "regression_mult": round(under_regression_penalty, 2),
                    "min_score_threshold": under_min_score,
                },
                "poisson": {
                    "home_lambda": home_lambda,
                    "away_lambda": away_lambda,
                    "combined_lambda": round(home_lambda + away_lambda, 2),
                    "over25_prob": over25_prob_pct,
                    "under25_prob": under25_prob_pct,
                },
                "guards": {
                    "weak_roi_league": _is_weak_roi_league(league_name),
                    "chaos_rule_over": _chaos_rule_over(home_6, away_6),
                    "compact_rule_under": _compact_rule_under(home_6, away_6),
                    "btts_gate_passed": btts_gate,
                    "non_btts_gate_passed": non_btts_gate,
                    "high_scoring_blocked": high_scoring_under_block,
                    "h2h_over_blocked": h2h_over_blocked,
                    "h2h_under_blocked": h2h_under_blocked,
                    "h2h_over_meetings": len(h2h_over_meetings),
                    "h2h_under_meetings": len(h2h_under_meetings),
                    "home_btts_6": home_btts_6,
                    "away_btts_6": away_btts_6,
                    "home_non_btts_6": home_non_btts_6,
                    "away_non_btts_6": away_non_btts_6,
                    "regression_penalty_applied": regressions,
                },
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
def _append_ou_pick(lines, idx, item, side, odds, detailed, compact=False):
    """Append one over/under pick with consistent spacing."""
    m = item["match"]
    p = item["poisson"]
    tgt = item[side]
    prob_key = "over25_prob" if side == "over" else "under25_prob"
    max_score = MAX_OVER_SCORE if side == "over" else MAX_UNDER_SCORE
    market_label = "Over 2.5" if side == "over" else "Under 2.5"
    market = MARKET_OVER25 if side == "over" else MARKET_UNDER25
    if compact:
        lines.append(format_compact_pick_line(
            m["home"], m["away"], "O2.5" if side == "over" else "U2.5",
            tgt.get("tier"), p[prob_key], m.get("date"),
        ))
        return
    extra = None
    if detailed:
        extra = format_vip_extra_lines(
            tgt["kelly"], odds, tgt["score"], max_score,
            home_lambda=p["home_lambda"], away_lambda=p["away_lambda"],
        )
    categories = describe_pick_categories(
        m["home"], m["away"], m.get("league", ""),
        market=market,
        tier=tgt.get("tier"),
        weak_roi_league=bool((item.get("guards") or {}).get("weak_roi_league")),
    )
    lines.extend(format_pick_block(
        idx, m["home"], m["away"], m["date"],
        f"{market_label} · {format_confidence_label(tgt['confidence'])} ({p[prob_key]}%)",
        extra,
        league=m.get("league"),
        categories=categories,
    ))


def build_report(over_perfect, over_qualified, over_close, over_weak,
               under_perfect, under_qualified, under_close, under_weak,
               scanned_dates, bankroll, odds_over, odds_under, detailed=False, compact=False,
               include_yesterday=True, include_header=True, include_footer=True):
    """
    Build a clean, mobile-friendly report - both channels show all picks, free is simplified
    Returns: (report, base_date, included_over, included_under)
    """
    included_over = list(over_perfect + over_qualified + over_close)
    included_under = list(under_perfect + under_qualified + under_close)
    included_dates = scanned_dates
    
    base_date = scanned_dates[0] if scanned_dates else datetime.now().strftime("%Y-%m-%d")

    lines = []
    if not compact:
        if include_header:
            lines.append("⚽️ Over/Under 2.5 picks")
            lines.append("")
            if len(included_dates) > 1:
                lines.append(f"Dates: {included_dates[0]} to {included_dates[-1]}")
            else:
                lines.append(f"Date: {base_date}")
            lines.append("")
            if include_yesterday:
                append_yesterday_section(lines, "over_under", detailed=detailed)
    elif compact:
        def _append_compact_tier_group(tiers_items, side_label):
            has_group = False
            for tier_header, items in tiers_items:
                if not items:
                    continue
                if not has_group:
                    lines.append(f"▸ {side_label}")
                    has_group = True
                lines.append(f"  {tier_header}")
                for item in items:
                    lines.append(f"  {format_compact_pick_line(
                        item['match']['home'], item['match']['away'],
                        'over' if side_label == 'OVER 2.5' else 'under',
                        (item['over'] if side_label == 'OVER 2.5' else item['under']).get('tier'),
                        (item['poisson']['over25_prob'] if side_label == 'OVER 2.5' else item['poisson']['under25_prob']),
                        item['match'].get('date'),
                    )}")
            return has_group

        over_perfect_tier = [p for p in included_over if p in over_perfect]
        over_strong_tier = [p for p in included_over if p in over_qualified]
        over_watch_tier = [p for p in included_over if p in over_close]
        under_perfect_tier = [p for p in included_under if p in under_perfect]
        under_strong_tier = [p for p in included_under if p in under_qualified]
        under_watch_tier = [p for p in included_under if p in under_close]

        any_over = _append_compact_tier_group([
            (COMPACT_TIER_HEADER_PREMIUM, over_perfect_tier),
            (COMPACT_TIER_HEADER_STRONG, over_strong_tier),
            (COMPACT_TIER_HEADER_WATCH, over_watch_tier),
        ], "OVER 2.5")
        if any_over and (under_perfect_tier or under_strong_tier or under_watch_tier):
            lines.append("")
        _append_compact_tier_group([
            (COMPACT_TIER_HEADER_PREMIUM, under_perfect_tier),
            (COMPACT_TIER_HEADER_STRONG, under_strong_tier),
            (COMPACT_TIER_HEADER_WATCH, under_watch_tier),
        ], "UNDER 2.5")

    if not compact:
        # Over 2.5 section (full detail)
        if included_over:
            lines.append("")
            lines.append("🟢 Over 2.5 goals")
            lines.append("")
            included_over_perfect = [p for p in included_over if p in over_perfect]
            included_over_qualified = [p for p in included_over if p in over_qualified]
            included_over_close = [p for p in included_over if p in over_close]
            if included_over_perfect:
                lines.append(f"  {PICK_TIER_PREMIUM}")
                lines.append("")
                for i, item in enumerate(included_over_perfect, 1):
                    _append_ou_pick(lines, i, item, "over", odds_over, detailed, compact)
            if included_over_qualified:
                lines.append(f"  {PICK_TIER_STRONG}")
                lines.append("")
                start_idx = len(included_over_perfect) + 1
                for i, item in enumerate(included_over_qualified, start_idx):
                    _append_ou_pick(lines, i, item, "over", odds_over, detailed, compact)
            if included_over_close:
                if detailed:
                    lines.append(f"  {PICK_TIER_VALUE}")
                    lines.append("")
                start_idx = len(included_over_perfect) + len(included_over_qualified) + 1
                for i, item in enumerate(included_over_close, start_idx):
                    _append_ou_pick(lines, i, item, "over", odds_over, detailed, compact)

        # Under 2.5 section (full detail)
        if included_under:
            lines.append("")
            lines.append("🔵 Under 2.5 goals")
            lines.append("")
            included_under_perfect = [p for p in included_under if p in under_perfect]
            included_under_qualified = [p for p in included_under if p in under_qualified]
            included_under_close = [p for p in included_under if p in under_close]
            if included_under_perfect:
                lines.append(f"  {PICK_TIER_PREMIUM}")
                lines.append("")
                for i, item in enumerate(included_under_perfect, 1):
                    _append_ou_pick(lines, i, item, "under", odds_under, detailed, compact)
            if included_under_qualified:
                lines.append(f"  {PICK_TIER_STRONG}")
                lines.append("")
                start_idx = len(included_under_perfect) + 1
                for i, item in enumerate(included_under_qualified, start_idx):
                    _append_ou_pick(lines, i, item, "under", odds_under, detailed, compact)
            if included_under_close:
                if detailed:
                    lines.append(f"  {PICK_TIER_VALUE}")
                    lines.append("")
                start_idx = len(included_under_perfect) + len(included_under_qualified) + 1
                for i, item in enumerate(included_under_close, start_idx):
                    _append_ou_pick(lines, i, item, "under", odds_under, detailed, compact)

        if include_footer:
            lines.append("---")
            lines.append("For informational purposes only")
            lines.append("Gamble responsibly")
            lines.append("")

    report = "\n".join(lines).strip()
    if not report:
        report = "— none"
    return report, base_date, included_over, included_under

# =============================================================================
# MAIN
# =============================================================================
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
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
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of days to scan starting from the date (default: 4, weekends: 6)"
    )
    args = parser.parse_args()

    if args.clear_cache:
        cache.clear()

    start_date = datetime.strptime(args.date, "%Y-%m-%d")
    scan_days = args.days
    if scan_days is None:
        scan_days = 6 if start_date.weekday() >= 4 else 4

    # Over 2.5 buckets
    over_perfect, over_qualified, over_close, over_weak = [], [], [], []
    # Under 2.5 buckets
    under_perfect, under_qualified, under_close, under_weak = [], [], [], []

    scanned_dates = []

    print(f"Starting Over/Under 2.5 analysis from {args.date}...")

    for day_offset in range(scan_days):
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

                    over_tier = data["over"]["tier"]
                    if over_tier == "perfect":
                        over_perfect.append(data)
                    elif over_tier == "qualified":
                        over_qualified.append(data)
                    elif over_tier == "close":
                        over_close.append(data)
                    elif data["over"]["score"] >= max(1, MAX_OVER_SCORE - 4):
                        over_weak.append(data)

                    under_tier = data["under"]["tier"]
                    if under_tier == "perfect":
                        under_perfect.append(data)
                    elif under_tier == "qualified":
                        under_qualified.append(data)
                    elif under_tier == "close":
                        under_close.append(data)
                    elif data["under"]["score"] >= max(1, MAX_UNDER_SCORE - 3):
                        under_weak.append(data)

    # Apply portfolio Kelly cap separately for Over and Under
    apply_portfolio_kelly(over_perfect + over_qualified + over_close, "over", args.bankroll, MAX_TOTAL_EXPOSURE / 2)
    apply_portfolio_kelly(under_perfect + under_qualified + under_close, "under", args.bankroll, MAX_TOTAL_EXPOSURE / 2)

    # Build and output reports (both free and detailed)
    free_report, base_date, included_over, included_under = build_report(
        over_perfect, over_qualified, over_close, over_weak,
        under_perfect, under_qualified, under_close, under_weak,
        scanned_dates, args.bankroll, args.odds_over, args.odds_under, detailed=False
    )
    telegram_report, _, _, _ = build_report(
        over_perfect, over_qualified, over_close, over_weak,
        under_perfect, under_qualified, under_close, under_weak,
        scanned_dates, args.bankroll, args.odds_over, args.odds_under,
        detailed=False, compact=False,
        include_yesterday=False, include_header=False, include_footer=False,
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
    print("\n===TELEGRAM_START===")
    print(telegram_report.strip() or "— none")
    print("===TELEGRAM_END===")

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
    # IMPORTANT: Use original tier buckets (not build_report's included_*) so
    # statistically-blocked leagues are still recorded with published=false.
    # record_predictions() handles the published flag internally via
    # is_statistical_block_only() and fully skips only static/integrity blocks.
    try:
        ou_picks = []
        all_over = over_perfect + over_qualified + over_close
        for pick in all_over:
            tier = pick["over"].get("tier") or (
                "perfect" if pick in over_perfect else
                "qualified" if pick in over_qualified else "close"
            )
            ou_picks.append({
                "league": pick["match"]["league"],
                "home": pick["match"]["home"],
                "away": pick["match"]["away"],
                "date": pick["match"]["date"],
                "prediction": "over",
                "confidence": tier,
            })
        all_under = under_perfect + under_qualified + under_close
        for pick in all_under:
            tier = pick["under"].get("tier") or (
                "perfect" if pick in under_perfect else
                "qualified" if pick in under_qualified else "close"
            )
            ou_picks.append({
                "league": pick["match"]["league"],
                "home": pick["match"]["home"],
                "away": pick["match"]["away"],
                "date": pick["match"]["date"],
                "prediction": "under",
                "confidence": tier,
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
