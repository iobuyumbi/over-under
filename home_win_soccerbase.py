#!/usr/bin/env python3
"""
HOME WIN PREDICTOR - PRODUCTION HARDENED v5
=============================================
11-check rule system | Shrinkage strength | Logistic prob | Portfolio Kelly | SQLite Cache
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
from prediction_tracker import (
    record_predictions,
    format_vip_extra_lines,
    format_pick_block,
    is_blocked_fixture,
    append_yesterday_section,
)

# =============================================================================
# CONFIGURATION
# =============================================================================
CACHE_DB = "soccerbase_cache_home.db"
CACHE_TTL_HOURS = 24
MAX_WORKERS = 4
REQUEST_DELAY_MIN = 2.5
REQUEST_DELAY_MAX = 5.0
MAX_TOTAL_EXPOSURE = 0.25
SHRINKAGE_WEIGHT = 0.65

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

    for table in soup.find_all("table", class_="listWithCards"):
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
                home_link = cells[3].find("a", href=lambda h: h and "team_id=" in h)
                if not home_link:
                    continue
                home_id_in_row = home_link["href"].split("team_id=")[1].split("&")[0]

                is_home = str(home_id_in_row) == str(team_id)
                gf = gf_h if is_home else gf_a
                ga = gf_a if is_home else gf_h
                result = "W" if gf > ga else "D" if gf == ga else "L"

                date_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(cells[1]))
                date_str = date_match.group(1) if date_match else None

                matches.append({
                    "gf": gf, "ga": ga, "is_home": is_home,
                    "result": result, "date_str": date_str
                })
            except Exception:
                continue

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
    match = re.search(r'(\d{4})[-/](\d{2})[-/](\d{2})', date_str)
    if match:
        try:
            return datetime.strptime(match.group(0), "%Y-%m-%d")
        except ValueError:
            pass

    logger.warning(f"Could not parse date: {date_str}")
    return None


MAX_HOME_WIN_SCORE = 11


def get_team_form(team_id, is_home=True, num_matches=6, target_date_str=None):
    all_matches = fetch_soccerbase_team_results(team_id)
    form = []
    target_dt = parse_date(target_date_str) if target_date_str else None

    for match in all_matches:
        match_dt = parse_date(match.get("date_str"))
        if target_dt and match_dt and match_dt >= target_dt:
            continue
        if match["is_home"] == is_home:
            form.append(match)
            if len(form) >= num_matches:
                break
    return form


def get_team_overall_form(team_id, num_matches=5, target_date_str=None):
    """Last N matches home or away combined."""
    all_matches = fetch_soccerbase_team_results(team_id)
    form = []
    target_dt = parse_date(target_date_str) if target_date_str else None

    for match in all_matches:
        match_dt = parse_date(match.get("date_str"))
        if target_dt and match_dt and match_dt >= target_dt:
            continue
        form.append(match)
        if len(form) >= num_matches:
            break
    return form


def _form_record_summary(form):
    wins = sum(1 for m in form if m["result"] == "W")
    losses = sum(1 for m in form if m["result"] == "L")
    draws = len(form) - wins - losses
    return wins, losses, draws


# =============================================================================
# HOME WIN ALGORITHM (11 Checks - Official Rules + Overall Form)
# =============================================================================
def apply_home_win_algorithm(home_data_6, away_data_6, home_overall_5=None, away_overall_5=None):
    if len(home_data_6) < 6 or len(away_data_6) < 6:
        return None, None, {"error": "Insufficient data"}, False

    passed, failed, details = [], [], {}
    is_perfect = True

    # Home Checks
    home_not_lost = sum(1 for m in home_data_6 if m["result"] != "L")
    if home_not_lost >= 5:
        passed.append("Home form (no losses)"); details["Home form (no losses)"] = f"PASS ({home_not_lost}/6 No Losses)"
        if home_not_lost < 6:
            is_perfect = False
    else:
        failed.append("Home form (no losses)"); details["Home form (no losses)"] = f"FAIL ({home_not_lost}/6)"; is_perfect = False

    home_gf = sum(m["gf"] for m in home_data_6)
    if home_gf >= 10:
        passed.append("Home goals scored"); details["Home goals scored"] = f"PASS ({home_gf} GF)"
    else:
        failed.append("Home goals scored"); details["Home goals scored"] = f"FAIL ({home_gf})"; is_perfect = False

    home_ga = sum(m["ga"] for m in home_data_6)
    if home_ga <= 5:
        passed.append("Home goals conceded"); details["Home goals conceded"] = f"PASS ({home_ga} GA)"
    else:
        failed.append("Home goals conceded"); details["Home goals conceded"] = f"FAIL ({home_ga})"; is_perfect = False

    home_wins = sum(1 for m in home_data_6 if m["result"] == "W")
    if home_wins >= 3:
        passed.append("Home wins"); details["Home wins"] = f"PASS ({home_wins}/6 Wins)"
    else:
        failed.append("Home wins"); details["Home wins"] = f"FAIL ({home_wins})"; is_perfect = False

    last_2_wins = sum(1 for m in home_data_6[:2] if m["result"] == "W")
    if last_2_wins == 2:
        passed.append("Home recent form"); details["Home recent form"] = "PASS (Won Last 2)"
    else:
        failed.append("Home recent form"); details["Home recent form"] = f"FAIL ({last_2_wins}/2)"; is_perfect = False

    # Away Checks
    away_losses = sum(1 for m in away_data_6 if m["result"] == "L")
    if away_losses >= 2:
        passed.append("Away losses"); details["Away losses"] = f"PASS ({away_losses}/6 Losses)"
    else:
        failed.append("Away losses"); details["Away losses"] = f"FAIL ({away_losses})"; is_perfect = False

    away_ga = sum(m["ga"] for m in away_data_6)
    if away_ga >= 10:
        passed.append("Away goals conceded"); details["Away goals conceded"] = f"PASS ({away_ga} GA)"
    else:
        failed.append("Away goals conceded"); details["Away goals conceded"] = f"FAIL ({away_ga})"; is_perfect = False

    away_gf = sum(m["gf"] for m in away_data_6)
    if away_gf <= 5:
        passed.append("Away goals scored"); details["Away goals scored"] = f"PASS ({away_gf} GF)"
    else:
        failed.append("Away goals scored"); details["Away goals scored"] = f"FAIL ({away_gf})"; is_perfect = False

    away_wins = sum(1 for m in away_data_6 if m["result"] == "W")
    if away_wins <= 2:
        passed.append("Away wins"); details["Away wins"] = f"PASS ({away_wins}/6 Wins)"
    else:
        failed.append("Away wins"); details["Away wins"] = f"FAIL ({away_wins})"; is_perfect = False

    # Overall form (last 5, home or away combined)
    home_overall_5 = home_overall_5 or []
    if len(home_overall_5) >= 5:
        hw, hl, _ = _form_record_summary(home_overall_5[:5])
        home_overall_ok = hl <= 1 or (hl == 2 and hw == 3)
        if home_overall_ok:
            passed.append("Home overall form (5)")
            details["Home overall form (5)"] = f"PASS ({hw}W-{hl}L in 5)"
            if hl == 2:
                is_perfect = False
        else:
            failed.append("Home overall form (5)")
            details["Home overall form (5)"] = f"FAIL ({hw}W-{hl}L in 5)"
            is_perfect = False
    else:
        failed.append("Home overall form (5)")
        details["Home overall form (5)"] = f"FAIL (only {len(home_overall_5)}/5 matches)"
        is_perfect = False

    away_overall_5 = away_overall_5 or []
    if len(away_overall_5) >= 5:
        aw, al, _ = _form_record_summary(away_overall_5[:5])
        away_overall_ok = al >= 2 and aw <= 2
        if away_overall_ok:
            passed.append("Away overall form (5)")
            details["Away overall form (5)"] = f"PASS ({aw}W-{al}L in 5)"
        else:
            failed.append("Away overall form (5)")
            details["Away overall form (5)"] = f"FAIL ({aw}W-{al}L in 5)"
            is_perfect = False
    else:
        failed.append("Away overall form (5)")
        details["Away overall form (5)"] = f"FAIL (only {len(away_overall_5)}/5 matches)"
        is_perfect = False

    return passed, failed, details, is_perfect


# =============================================================================
# STRENGTH MODEL (Shrinkage Estimator)
# =============================================================================
def get_team_strength(form_data, is_home=True):
    """
    Shrinkage estimator for team strength based on win rate.
    Blends observed win rate with league baseline to prevent overfitting.
    """
    baseline = 0.50  # League average win rate

    if not form_data:
        return baseline

    sample = form_data[:6]
    n = len(sample)
    wins = sum(1 for m in sample if m["result"] == "W")
    win_rate = wins / n

    strength = SHRINKAGE_WEIGHT * win_rate + (1 - SHRINKAGE_WEIGHT) * baseline
    return round(max(0.1, min(0.95, strength)), 3)


def calculate_home_win_prob(home_strength, away_strength):
    """
    Logistic probability model.
    diff > 0 favors home, diff < 0 favors away.
    """
    diff = home_strength - away_strength
    prob = 1.0 / (1.0 + math.exp(-4.0 * diff))
    return round(prob * 100, 1)


def get_confidence(prob):
    if prob >= 70:
        return "HIGH"
    elif prob >= 58:
        return "MEDIUM"
    else:
        return "LOW"


# =============================================================================
# KELLY CRITERION
# =============================================================================
def calculate_kelly(prob, decimal_odds=2.8, use_half=True):
    if prob <= 0.0 or decimal_odds <= 1.0:
        return 0.0
    kelly = (prob * decimal_odds - 1) / (decimal_odds - 1)
    return max(0.0, kelly * 0.5 if use_half else kelly)


def apply_portfolio_kelly(recommendations, bankroll, max_exposure=MAX_TOTAL_EXPOSURE):
    """
    Scale Kelly fractions so total exposure does not exceed max_exposure.
    """
    if not recommendations or bankroll <= 0:
        return recommendations

    total_kelly = sum(r["kelly"] / 100 for r in recommendations)
    if total_kelly <= 0:
        return recommendations

    if total_kelly > max_exposure:
        scale = max_exposure / total_kelly
        for r in recommendations:
            r["kelly"] = round(r["kelly"] * scale, 2)
        logger.info(
            f"Portfolio Kelly scaled by {scale:.3f} "
            f"({total_kelly*100:.1f}% -> {max_exposure*100:.1f}% exposure)"
        )

    return recommendations


# =============================================================================
# MATCH PROCESSING
# =============================================================================
def process_single_match(match, target_date, default_odds=2.8):
    try:
        home_form = get_team_form(match["home_team_id"], True, 6, target_date)
        away_form = get_team_form(match["away_team_id"], False, 6, target_date)
        home_overall_5 = get_team_overall_form(match["home_team_id"], 5, target_date)
        away_overall_5 = get_team_overall_form(match["away_team_id"], 5, target_date)

        if len(home_form) < 6 or len(away_form) < 6:
            return {"status": "insufficient"}

        passed, failed, details, is_perfect = apply_home_win_algorithm(
            home_form, away_form, home_overall_5, away_overall_5
        )
        if passed is None:
            return {"status": "insufficient"}

        home_strength = get_team_strength(home_form, True)
        away_strength = get_team_strength(away_form, False)
        home_win_prob = calculate_home_win_prob(home_strength, away_strength)
        confidence = get_confidence(home_win_prob)

        score = len(passed)
        kelly_half = calculate_kelly(home_win_prob / 100, default_odds)

        return {
            "status": "success",
            "data": {
                "match": match,
                "score": score,
                "passed": passed,
                "details": details,
                "is_perfect": is_perfect,
                "model": {
                    "home_strength": home_strength,
                    "away_strength": away_strength,
                    "home_win_prob": home_win_prob,
                    "confidence": confidence
                },
                "kelly": round(kelly_half * 100, 2)
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
def build_report(perfect, qualified, close_calls, scanned_dates, bankroll, odds, detailed=False):
    """
    Build a clean, mobile-friendly report with all qualifying picks across scanned days.
    Returns: (report, base_date, included_perfect, included_qualified, included_close)
    """
    included_perfect = [p for p in perfect if not is_blocked_fixture(p["match"])]
    included_qualified = [p for p in qualified if not is_blocked_fixture(p["match"])]
    included_close = [p for p in close_calls if not is_blocked_fixture(p["match"])]
    included_dates = scanned_dates

    base_date = scanned_dates[0] if scanned_dates else datetime.now().strftime("%Y-%m-%d")

    # Clean report (mobile-friendly)
    lines = []
    lines.append("HOME WIN PICKS")
    lines.append("")
    
    if len(included_dates) > 1:
        lines.append(f"Dates: {included_dates[0]} to {included_dates[-1]}")
    else:
        lines.append(f"Date: {base_date}")
    
    lines.append("")
    
    append_yesterday_section(lines, "home_win", detailed=detailed)
    
    if included_perfect:
        lines.append("  PREMIUM PICKS")
        lines.append("")
        for i, item in enumerate(included_perfect, 1):
            m = item["match"]
            p = item["model"]
            extra = None
            if detailed:
                extra = format_vip_extra_lines(
                    item["kelly"], odds, item["score"], MAX_HOME_WIN_SCORE,
                    home_strength=p["home_strength"], away_strength=p["away_strength"],
                )
            lines.extend(format_pick_block(
                i, m["home"], m["away"], m["date"],
                f"{p['confidence']} ({p['home_win_prob']}%)",
                extra,
            ))
        
    if included_qualified:
        lines.append("  STRONG PICKS")
        lines.append("")
        for i, item in enumerate(included_qualified, 1):
            m = item["match"]
            p = item["model"]
            extra = None
            if detailed:
                extra = format_vip_extra_lines(
                    item["kelly"], odds, item["score"], MAX_HOME_WIN_SCORE,
                    home_strength=p["home_strength"], away_strength=p["away_strength"],
                )
            lines.extend(format_pick_block(
                i, m["home"], m["away"], m["date"],
                f"{p['confidence']} ({p['home_win_prob']}%)",
                extra,
            ))
        
    if included_close:
        lines.append("  VALUE PICKS")
        lines.append("")
        for i, item in enumerate(included_close, 1):
            m = item["match"]
            p = item["model"]
            extra = None
            if detailed:
                extra = format_vip_extra_lines(
                    item["kelly"], odds, item["score"], MAX_HOME_WIN_SCORE,
                    home_strength=p["home_strength"], away_strength=p["away_strength"],
                )
            lines.extend(format_pick_block(
                i, m["home"], m["away"], m["date"],
                f"{p['confidence']} ({p['home_win_prob']}%)",
                extra,
            ))

    # Add disclaimer
    lines.append("---")
    lines.append("For informational purposes only")
    lines.append("Gamble responsibly")
    lines.append("")

    report = "\n".join(lines)
    return report, base_date, included_perfect, included_qualified, included_close


# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Home Win Predictor with Strength Model + Kelly"
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
        "--odds",
        type=float,
        default=2.8,
        help="Average decimal odds for Home Win"
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
    perfect, qualified, close_calls = [], [], []
    scanned_dates = []

    print(f"Starting Home Win analysis from {args.date}...")

    for day_offset in range(4):
        current_date = start_date + timedelta(days=day_offset)
        date_str = current_date.strftime("%Y-%m-%d")
        scanned_dates.append(date_str)

        fixtures = fetch_soccerbase_fixtures(date_str)
        seen = set()
        unique_fixtures = []
        blocked = 0
        for f in fixtures:
            key = (f["home_team_id"], f["away_team_id"], f["league"])
            if key not in seen and f["home_team_id"] and f["away_team_id"]:
                if is_blocked_fixture(f):
                    blocked += 1
                    continue
                if not args.scheduled or f["status"] == "Scheduled":
                    seen.add(key)
                    unique_fixtures.append(f)

        if blocked:
            print(f"   Skipped {blocked} flagged-region fixtures on {date_str}")

        if not unique_fixtures:
            logger.info(f"No fixtures to process on {date_str}")
            continue

        print(f"   Processing {len(unique_fixtures)} matches on {date_str}...")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(process_single_match, match, date_str, args.odds): match
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
                    if data["score"] == MAX_HOME_WIN_SCORE:
                        if data["is_perfect"]:
                            perfect.append(data)
                        else:
                            qualified.append(data)
                    elif data["score"] == MAX_HOME_WIN_SCORE - 1:
                        qualified.append(data)
                    elif data["score"] == MAX_HOME_WIN_SCORE - 2:
                        close_calls.append(data)

        if len(perfect) + len(qualified) >= 12:
            logger.info("Reached target of 12+ qualifying matches. Stopping scan.")
            break

    # Apply portfolio Kelly cap
    all_recs = perfect + qualified + close_calls
    apply_portfolio_kelly(all_recs, args.bankroll, MAX_TOTAL_EXPOSURE)

    # Build and output reports (both free and detailed)
    free_report, base_date, included_perfect, included_qualified, included_close = build_report(
        perfect, qualified, close_calls, scanned_dates, args.bankroll, args.odds, detailed=False
    )
    detailed_report, _, _, _, _ = build_report(
        perfect, qualified, close_calls, scanned_dates, args.bankroll, args.odds, detailed=True
    )

    # Output free report (default)
    print("\n===EMAIL_START===")
    print(free_report)
    print("===EMAIL_END===")

    # Save detailed report to file
    detailed_report_path = f"home_win_vip_report_{base_date}.txt"
    with open(detailed_report_path, "w", encoding="utf-8") as f:
        f.write(detailed_report)

    # Save JSON
    output_path = f"home_win_report_{base_date}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "metadata": {
                "scanned_window": scanned_dates,
                "bankroll": args.bankroll,
                "odds": args.odds,
                "max_exposure": MAX_TOTAL_EXPOSURE,
                "generated_at": datetime.now().isoformat()
            },
            "perfect": perfect,
            "qualified": qualified,
            "close_calls": close_calls
        }, f, indent=2, default=str)

    # Record predictions for tracking
    try:
        hw_picks = []
        for pick in included_perfect + included_qualified + included_close:
            hw_picks.append({
                "league": pick["match"]["league"],
                "home": pick["match"]["home"],
                "away": pick["match"]["away"],
                "date": pick["match"]["date"],
                "confidence": "perfect" if pick in included_perfect else ("qualified" if pick in included_qualified else "close")
            })
        stats = record_predictions(base_date, hw_picks, [])
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
