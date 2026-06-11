#!/usr/bin/env python3
"""
OVER 2.5 GOALS PREDICTOR - PRODUCTION HARDENED v4
====================================================
Rule-based filtering | Shrinkage xG | Portfolio Kelly | SQLite Cache
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

ua = UserAgent()

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
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
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
# ALGORITHM (Your Original Rules — Fully Preserved)
# =============================================================================
def apply_algorithm(home_3, away_3, home_6, away_6):
    if len(home_3) < 3 or len(away_3) < 3:
        return None, None, {"error": "Insufficient data"}, False

    passed, failed, details = [], [], {}
    is_perfect = True

    # Home 3-game checks
    h_total_3 = sum(gf + ga for gf, ga in home_3)
    if h_total_3 >= 7:
        passed.append("H1"); details["H1"] = f"PASS ({h_total_3})"
    else:
        failed.append("H1"); details["H1"] = f"FAIL ({h_total_3})"; is_perfect = False

    h_over_3 = sum(1 for gf, ga in home_3 if gf + ga > 2.5)
    if h_over_3 >= 2:
        passed.append("H2"); details["H2"] = f"PASS ({h_over_3}/3)"
        if h_over_3 < 3:
            is_perfect = False
    else:
        failed.append("H2"); details["H2"] = f"FAIL ({h_over_3}/3)"; is_perfect = False

    # Away 3-game checks
    a_total_3 = sum(gf + ga for gf, ga in away_3)
    if a_total_3 >= 7:
        passed.append("A1"); details["A1"] = f"PASS ({a_total_3})"
    else:
        failed.append("A1"); details["A1"] = f"FAIL ({a_total_3})"; is_perfect = False

    prev_a_total = away_3[0][0] + away_3[0][1]
    if prev_a_total >= 2:
        passed.append("A2"); details["A2"] = f"PASS ({prev_a_total})"
    else:
        failed.append("A2"); details["A2"] = f"FAIL ({prev_a_total})"; is_perfect = False

    a_scored = sum(1 for gf, _ in away_3 if gf > 0)
    if a_scored >= 2:
        passed.append("A3"); details["A3"] = f"PASS ({a_scored}/3)"
        if a_scored < 3:
            is_perfect = False
    else:
        failed.append("A3"); details["A3"] = f"FAIL ({a_scored}/3)"; is_perfect = False

    a_over_3 = sum(1 for gf, ga in away_3 if gf + ga > 2.5)
    if a_over_3 >= 2:
        passed.append("A4"); details["A4"] = f"PASS ({a_over_3}/3)"
        if a_over_3 < 3:
            is_perfect = False
    else:
        failed.append("A4"); details["A4"] = f"FAIL ({a_over_3}/3)"; is_perfect = False

    # 6-game checks
    if len(home_6) >= 6:
        h_over_6 = sum(1 for gf, ga in home_6 if gf + ga > 2.5)
        if h_over_6 >= 4:
            passed.append("H3"); details["H3"] = f"PASS ({h_over_6}/6)"
        else:
            failed.append("H3"); details["H3"] = f"FAIL ({h_over_6}/6)"; is_perfect = False

        h_total_6 = sum(gf + ga for gf, ga in home_6)
        if h_total_6 >= 18:
            passed.append("H4"); details["H4"] = f"PASS ({h_total_6})"
        else:
            failed.append("H4"); details["H4"] = f"FAIL ({h_total_6})"; is_perfect = False

    if len(away_6) >= 6:
        a_over_6 = sum(1 for gf, ga in away_6 if gf + ga > 2.5)
        if a_over_6 >= 4:
            passed.append("A5"); details["A5"] = f"PASS ({a_over_6}/6)"
        else:
            failed.append("A5"); details["A5"] = f"FAIL ({a_over_6}/6)"; is_perfect = False

        a_total_6 = sum(gf + ga for gf, ga in away_6)
        if a_total_6 >= 18:
            passed.append("A6"); details["A6"] = f"PASS ({a_total_6})"
        else:
            failed.append("A6"); details["A6"] = f"FAIL ({a_total_6})"; is_perfect = False

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


# =============================================================================
# EXPECTED GOALS (Shrinkage Estimator — Fixed)
# =============================================================================
def get_expected_goals(form_data, is_home=True):
    baseline = 1.45 if is_home else 1.20

    if not form_data:
        return round(baseline, 2)

    sample = form_data[:6]
    n = len(sample)
    goals_scored = sum(gf for gf, _ in sample)
    raw_avg = goals_scored / n

    xg = SHRINKAGE_WEIGHT * raw_avg + (1 - SHRINKAGE_WEIGHT) * baseline
    return round(max(0.8, min(3.8, xg)), 2)


# =============================================================================
# KELLY CRITERION
# =============================================================================
def calculate_kelly(prob, decimal_odds=2.0, use_half=True):
    if prob <= 0.0 or decimal_odds <= 1.0:
        return 0.0
    kelly = (prob * decimal_odds - 1) / (decimal_odds - 1)
    return max(0.0, kelly * 0.5 if use_half else kelly)


def apply_portfolio_kelly(recommendations, bankroll, max_exposure=MAX_TOTAL_EXPOSURE):
    if not recommendations or bankroll <= 0:
        return recommendations

    total_kelly = sum(r["kelly"]["half"] / 100 for r in recommendations)
    if total_kelly <= 0:
        return recommendations

    if total_kelly > max_exposure:
        scale = max_exposure / total_kelly
        for r in recommendations:
            r["kelly"]["half"] *= scale
        logger.info(
            f"Portfolio Kelly scaled by {scale:.3f} "
            f"({total_kelly*100:.1f}% -> {max_exposure*100:.1f}% exposure)"
        )

    return recommendations


# =============================================================================
# MATCH PROCESSING
# =============================================================================
def process_single_match(match, target_date, default_odds=2.0):
    try:
        home_3 = get_team_form(match["home_team_id"], True, 3, target_date)
        away_3 = get_team_form(match["away_team_id"], False, 3, target_date)
        home_6 = get_team_form(match["home_team_id"], True, 6, target_date)
        away_6 = get_team_form(match["away_team_id"], False, 6, target_date)

        passed, failed, details, is_perfect = apply_algorithm(home_3, away_3, home_6, away_6)
        if passed is None:
            return {"status": "insufficient"}

        home_xg = get_expected_goals(home_6, True)
        away_xg = get_expected_goals(away_6, False)
        over25_prob_pct = calculate_poisson_over25(home_xg, away_xg)
        over25_prob = over25_prob_pct / 100.0

        score = len(passed)
        confidence = (
            "HIGH" if over25_prob >= 0.58
            else "MEDIUM" if over25_prob >= 0.52
            else "LOW"
        )

        kelly_half = calculate_kelly(over25_prob, default_odds, use_half=True)

        return {
            "status": "success",
            "data": {
                "match": match,
                "score": score,
                "passed": passed,
                "details": details,
                "is_perfect": is_perfect,
                "poisson": {
                    "home_xg": home_xg,
                    "away_xg": away_xg,
                    "over25_prob": over25_prob_pct,
                    "confidence": confidence
                },
                "kelly": {"half": round(kelly_half * 100, 2)}
            }
        }
    except Exception as e:
        logger.error(
            f"Processing failed for "
            f"{match.get("home", "N/A")} vs {match.get("away", "N/A")}: {e}",
            exc_info=True
        )
        return {"status": "error"}


# =============================================================================
# REPORTING
# =============================================================================
def build_report(perfect, qualified, close_calls, scanned_dates, bankroll, odds):
    base_date = scanned_dates[0] if scanned_dates else datetime.now().strftime("%Y-%m-%d")
    end_date = scanned_dates[-1] if scanned_dates else base_date

    total_analyzed = len(perfect) + len(qualified) + len(close_calls)

    report = [
        "🔥 OVER 2.5 GOALS - PROFESSIONAL PREDICTION REPORT",
        f"📅 Period: {base_date} → {end_date}",
        f"📊 Analyzed: {total_analyzed} matches",
        f"⭐ Perfect (10/10): {len(perfect)} | Qualified (10/10): {len(qualified)} | Candidates (8/10): {len(close_calls)}",
        "=" * 70,
        ""
    ]

    report.append("🏆 PERFECT MATCHES (10/10 + All Checks Passed)")
    if perfect:
        for i, item in enumerate(perfect[:10], 1):
            m = item["match"]
            p = item["poisson"]
            k = item["kelly"]
            stake = round(bankroll * k["half"] / 100, 2)
            report.append(
                f"{i:2d}. {m['date']} | {m['league']}\n"
                f"     {m['home']} vs {m['away']}\n"
                f"     Poisson: {p['over25_prob']}% ({p['home_xg']}-{p['away_xg']}) → {p['confidence']}\n"
                f"     Half-Kelly: {k['half']:.2f}% → ${stake:,.2f}"
            )
    else:
        report.append("   No perfect matches found.")

    report.append("\n✅ QUALIFIED MATCHES (10/10)")
    if qualified:
        for item in qualified[:12]:
            m = item["match"]
            p = item["poisson"]
            report.append(f"• {m['date']} | {m['home']} vs {m['away']} ({p['over25_prob']}%)")
    else:
        report.append("   No qualified matches found.")

    report.append("\n📊 PROBABLE CANDIDATES (8/10)")
    if close_calls:
        for item in close_calls[:10]:
            m = item["match"]
            p = item["poisson"]
            report.append(f"• {m['league']}: {m['home']} vs {m['away']} ({p['over25_prob']}%)")
    else:
        report.append("   No candidates found.")

    report.extend([
        "",
        "=" * 70,
        f"💰 Bankroll: ${bankroll:,.2f} | Avg Odds: {odds}",
        f"💡 Total exposure capped at {MAX_TOTAL_EXPOSURE*100:.0f}% of bankroll.",
        "💡 Tip: Prioritize HIGH confidence matches. Never bet more than you can afford to lose."
    ])

    return "\n".join(report), base_date


# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Over 2.5 Goals Predictor with Poisson + Kelly"
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
        default=2.0,
        help="Average decimal odds for Over 2.5"
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

    print(f"🔍 Starting analysis from {args.date}...")

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
                    if data["score"] == 10:
                        if data["is_perfect"]:
                            perfect.append(data)
                        else:
                            qualified.append(data)
                    elif data["score"] == 8:
                        close_calls.append(data)

        if len(perfect) + len(qualified) >= 12:
            logger.info("Reached target of 12+ qualifying matches. Stopping scan.")
            break

    # Apply portfolio Kelly cap
    all_recs = perfect + qualified
    apply_portfolio_kelly(all_recs, args.bankroll, MAX_TOTAL_EXPOSURE)

    # Build and output report
    final_report, base_date = build_report(
        perfect, qualified, close_calls, scanned_dates, args.bankroll, args.odds
    )

    print("\n===EMAIL_START===")
    print(final_report)
    print("===EMAIL_END===")

    # Save JSON
    output_path = f"over25_report_{base_date}.json"
    with open(output_path, "w") as f:
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

    print(f"\n✅ Report saved: {output_path}")


if __name__ == "__main__":
    main()
