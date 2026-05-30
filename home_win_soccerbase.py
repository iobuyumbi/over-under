#!/usr/bin/env python3
"""
HOME WIN PREDICTION SYSTEM - SOCCERBASE ORIGINAL SPEC
=====================================================
Strictly enforces the original 9-point rule checklist.
Uses ID-driven parsing to completely eliminate team naming bugs.
"""

import requests
import json
import re
import argparse
import time
import random
from datetime import datetime
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from fake_useragent import UserAgent

ua = UserAgent()
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "DNT": "1",
    "Connection": "keep-alive",
}

retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session = requests.Session()
session.mount("https://", adapter)
session.mount("http://", adapter)

def get_random_headers():
    headers = HEADERS.copy()
    headers["User-Agent"] = ua.random
    return headers

def random_delay():
    time.sleep(random.uniform(1.5, 3.0))

def fetch_soccerbase_fixtures(date_str):
    url = f"https://www.soccerbase.com/matches/results.sd?date={date_str}"
    try:
        response = session.get(url, headers=get_random_headers(), timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        matches = []
        current_league = None

        tables = soup.find_all("table", class_="listWithCards")
        for table in tables:
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                league_link = row.find("a", href=lambda h: h and "comp_id=" in h)
                if league_link:
                    current_league = league_link.get_text(strip=True)
                elif len(cells) >= 6 and current_league:
                    home_team = cells[3].get_text(strip=True)
                    score_or_v = cells[4].get_text(strip=True)
                    away_team = cells[5].get_text(strip=True)

                    if home_team and away_team:
                        team_links = row.find_all("a", href=lambda h: h and "team_id=" in h)
                        ids = []
                        for link in team_links:
                            m_id = link["href"].split("team_id=")[1].split("&")[0]
                            if m_id not in ids:
                                ids.append(m_id)
                        
                        if len(ids) >= 2:
                            home_id, away_id = ids[0], ids[1]
                            matches.append({
                                "league": current_league,
                                "home": home_team,
                                "away": away_team,
                                "home_team_id": home_id,
                                "away_team_id": away_id,
                                "date": date_str,
                                "status": "Scheduled" if score_or_v == "v" else "Completed"
                            })
        return matches
    except Exception as e:
        print(f"[ERROR] Fixture fetch failed: {e}")
        return []

def fetch_soccerbase_team_results(team_id):
    url = f"https://www.soccerbase.com/teams/team.sd?team_id={team_id}&teamTabs=results"
    try:
        response = session.get(url, headers=get_random_headers(), timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        matches = []
        tables = soup.find_all("table", class_="soccerGrid")
        for table in tables:
            for row in table.find_all("tr")[2:]:
                cells = row.find_all("td")
                if len(cells) >= 8:
                    date_cell = cells[1]
                    score = cells[4].get_text(strip=True)

                    if "-" in score:
                        try:
                            iso_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(date_cell))
                            match_date = iso_match.group(1) if iso_match else None

                            gf_h, gf_a = map(int, score.split("-"))
                            home_link = cells[3].find("a", href=lambda h: h and "team_id=" in h)
                            if not home_link:
                                continue
                            url_home_id = home_link["href"].split("team_id=")[1].split("&")[0]
                            
                            is_home = (str(url_home_id) == str(team_id))
                            gf = gf_h if is_home else gf_a
                            ga = gf_a if is_home else gf_h
                            result = "W" if gf > ga else "D" if gf == ga else "L"

                            matches.append({
                                "gf": gf, "ga": ga, "is_home": is_home,
                                "result": result, "date_str": match_date
                            })
                        except Exception:
                            continue
        matches.sort(key=lambda x: x["date_str"] or "", reverse=True)
        return matches
    except Exception as e:
        print(f"[ERROR] Team ID data processing error {team_id}: {e}")
        return []

def get_team_form(team_id, is_home=True, num_matches=6, target_date_str=None):
    all_matches = fetch_soccerbase_team_results(team_id)
    form = []
    for match in all_matches:
        if target_date_str and match["date_str"] and match["date_str"] >= target_date_str:
            continue
        if match["is_home"] == is_home:
            form.append(match)
            if len(form) >= num_matches:
                break
    return form

def apply_home_win_algorithm(home_data_6, away_data_6):
    """
    Exact Original 9-Point Rule Logic Engine
    """
    passed, failed, details = [], [], {}

    if len(home_data_6) < 6 or len(away_data_6) < 6:
        return None, None, {"error": "Insufficient matches"}, False

    # --- HOME METRICS (6 Home Matches) ---
    # H1: Home not to lose in 5/6
    home_not_lost = sum(1 for m in home_data_6 if m["result"] != "L")
    if home_not_lost >= 5:
        passed.append("H1")
        details["H1"] = f"PASS ({home_not_lost}/6 No Losses)"
    else:
        failed.append("H1")
        details["H1"] = f"FAIL ({home_not_lost}/6, need 5+)"

    # H2: Home scored 10+ goals
    home_gf = sum(m["gf"] for m in home_data_6)
    if home_gf >= 10:
        passed.append("H2")
        details["H2"] = f"PASS ({home_gf} goals scored)"
    else:
        failed.append("H2")
        details["H2"] = f"FAIL ({home_gf} goals, need 10+)"

    # H3: Home conceded < 5 goals
    home_ga = sum(m["ga"] for m in home_data_6)
    if home_ga < 5:
        passed.append("H3")
        details["H3"] = f"PASS ({home_ga} goals conceded)"
    else:
        failed.append("H3")
        details["H3"] = f"FAIL ({home_ga} goals, need < 5)"

    # H4: Home won 4+ matches (FIXED)
    home_wins = sum(1 for m in home_data_6 if m["result"] == "W")
    if home_wins >= 4:
        passed.append("H4")
        details["H4"] = f"PASS ({home_wins}/6 Wins)"
    else:
        failed.append("H4")
        details["H4"] = f"FAIL ({home_wins}/6, need 4+)"

    # H5: Won last 2 home matches
    last_2_home_wins = sum(1 for m in home_data_6[:2] if m["result"] == "W")
    if last_2_home_wins == 2:
        passed.append("H5")
        details["H5"] = f"PASS (Won last 2)"
    else:
        failed.append("H5")
        details["H5"] = f"FAIL (Won {last_2_home_wins}/2 immediate form)"

    # --- AWAY METRICS (6 Away Matches) ---
    # A1: Away lost 2+ matches
    away_losses = sum(1 for m in away_data_6 if m["result"] == "L")
    if away_losses >= 2:
        passed.append("A1")
        details["A1"] = f"PASS ({away_losses}/6 Losses)"
    else:
        failed.append("A1")
        details["A1"] = f"FAIL ({away_losses}/6, need 2+)"

    # A2: Away conceded 10+ goals
    away_ga = sum(m["ga"] for m in away_data_6)
    if away_ga >= 10:
        passed.append("A2")
        details["A2"] = f"PASS ({away_ga} goals conceded)"
    else:
        failed.append("A2")
        details["A2"] = f"FAIL ({away_ga} goals, need 10+)"

    # A3: Away scored 5 or fewer goals
    away_gf = sum(m["gf"] for m in away_data_6)
    if away_gf <= 5:
        passed.append("A3")
        details["A3"] = f"PASS ({away_gf} goals scored)"
    else:
        failed.append("A3")
        details["A3"] = f"FAIL ({away_gf} goals, need <= 5)"

    # A4: Away won fewer than 4 matches (0-3 wins) (FIXED REDUNDANCY)
    away_wins = sum(1 for m in away_data_6 if m["result"] == "W")
    if away_wins < 4:
        passed.append("A4")
        details["A4"] = f"PASS ({away_wins}/6 Wins)"
    else:
        failed.append("A4")
        details["A4"] = f"FAIL ({away_wins}/6, need < 4)"

    # Check for "Perfect" status: Passed all 9 conditions AND home team has 0 losses at home
    is_perfect = (len(passed) == 9 and home_not_lost == 6)
    
    return passed, failed, details, is_perfect


def format_match_block(idx, r):
    m = r["match"]
    lines = [f"\n{idx}. {m['league']}: {m['home']} vs {m['away']} [Score: {r['score']}/9]"]
    for check, res in r["details"].items():
        lines.append(f"      • {check}: {res}")
    return "\n".join(lines)


def main(date_str=None, only_scheduled=False):
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    print(f"[+] Launching Pure 9-Point Spec Engine for Date: {date_str}...")
    all_matches = fetch_soccerbase_fixtures(date_str)

    seen = set()
    unique_matches = []
    for m in all_matches:
        key = (m["home_team_id"], m["away_team_id"])
        if key not in seen:
            seen.add(key)
            unique_matches.append(m)
    all_matches = unique_matches

    if only_scheduled:
        all_matches = [m for m in all_matches if m["status"] == "Scheduled"]

    perfect, qualified, close_calls, general_pool = [], [], [], []
    insufficient_data = 0

    for idx, match in enumerate(all_matches):
        home_id, away_id = match["home_team_id"], match["away_team_id"]
        
        home_form = get_team_form(home_id, is_home=True, num_matches=6, target_date_str=date_str)
        away_form = get_team_form(away_id, is_home=False, num_matches=6, target_date_str=date_str)

        if len(home_form) < 6 or len(away_form) < 6:
            insufficient_data += 1
            continue

        passed, failed, details, is_perfect = apply_home_win_algorithm(home_form, away_form)
        if passed is None:
            continue

        res = {
            "match": match, "passed": passed, "failed": failed, 
            "details": details, "score": len(passed), "is_perfect": is_perfect
        }

        if len(passed) == 9:
            if is_perfect: perfect.append(res)
            else: qualified.append(res)
        elif len(passed) == 8:
            close_calls.append(res)
        elif len(passed) == 7:
            general_pool.append(res)

        if match != all_matches[-1]:
            random_delay()

    # Consolidated 9-Point Report Formatting
    email = [
        "🏠 CORRECTED 9-POINT HOME WIN PREDICTIONS REPORT",
        f"📅 Date: {date_str}",
        "-" * 50,
        f"• Total Clean Fixtures: {len(all_matches)}",
        f"• Skipped (Insufficient Data): {insufficient_data}",
        f"• Perfect targets (9/9 Form Perfect): {len(perfect)}",
        f"• Qualified targets (9/9 Standard): {len(qualified)}",
        f"• Close Calls (8/9): {len(close_calls)}",
        f"• Watchlist Funnel Pool (7/9): {len(general_pool)}",
        "-" * 50 + "\n"
    ]

    email.append("⭐ PERFECT MATCHES")
    for i, p in enumerate(perfect, 1): email.append(format_match_block(i, p))
    if not perfect: email.append("  None.")

    email.append("\n✅ QUALIFIED MATCHES (9/9)")
    for i, q in enumerate(qualified, 1): email.append(format_match_block(i, q))
    if not qualified: email.append("  None.")

    email.append("\n⚠️ CLOSE CALL CONDITIONALS (8/9)")
    for c in close_calls:
        m = c["match"]
        email.append(f"• {m['league']}: {m['home']} vs {m['away']} | Failed: {', '.join(c['failed'])}")

    email.append("\n📊 WATCHLIST POOL (7/9)")
    for g in general_pool:
        m = g["match"]
        email.append(f"• {m['league']}: {m['home']} vs {m['away']} | Failed: {', '.join(g['failed'])}")

    print("\n===EMAIL_START===")
    print("\n".join(email))
    print("===EMAIL_END===")

    with open(f"home_win_predictions_{date_str}.json", "w") as f:
        json.dump({"date": date_str, "perfect": perfect, "qualified": qualified, "close_calls": close_calls, "pool_7_9": general_pool}, f, indent=2, default=str)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("date", nargs="?", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--scheduled", action="store_true")
    args = parser.parse_args()
    main(args.date, only_scheduled=args.scheduled)
