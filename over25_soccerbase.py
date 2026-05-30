#!/usr/bin/env python3
"""
OVER 2.5 GOALS PREDICTION SYSTEM - SOCCERBASE VERSION (PATCHED)
===============================================================
Fixed: League header carryover bug, strict row tracking, and home/away string safety.
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
    "Upgrade-Insecure-Requests": "1",
}

retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session = requests.Session()
session.mount("https://", adapter)
session.mount("http://", adapter)

def get_random_headers():
    headers = HEADERS.copy()
    headers["User-Agent"] = ua.random
    return headers

def random_delay():
    time.sleep(random.uniform(1.5, 3.5))

def fetch_soccerbase_fixtures(date_str):
    url = f"https://www.soccerbase.com/matches/results.sd?date={date_str}"
    try:
        headers = get_random_headers()
        response = session.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        matches = []
        
        # Target the main match container tables explicitly
        tables = soup.find_all("table", class_="listWithCards")
        for table in tables:
            current_league = "Unknown League" # Reset per table to prevent crossing continents
            rows = table.find_all("tr")
            
            for row in rows:
                # Check for explicit league header block inside the table row
                league_link = row.find("a", href=lambda href: href and "comp_id=" in href)
                if league_link:
                    current_league = league_link.get_text(strip=True)
                    continue # Move to match rows under this header
                
                cells = row.find_all("td")
                if len(cells) >= 6:
                    home_team = cells[3].get_text(strip=True)
                    score_or_v = cells[4].get_text(strip=True)
                    away_team = cells[5].get_text(strip=True)
                    
                    # Strip away extraneous date metadata strings from team fields
                    home_team_clean = re.sub(r'\s*\d+.*$', '', home_team).strip()
                    away_team_clean = re.sub(r'\s*\d+.*$', '', away_team).strip()
                    
                    if home_team_clean and away_team_clean:
                        team_links = row.find_all("a", href=lambda href: href and "team_id=" in href)
                        home_team_id = None
                        away_team_id = None
                        
                        if len(team_links) >= 2:
                            home_team_id = team_links[0]["href"].split("team_id=")[1].split("&")[0]
                            away_team_id = team_links[1]["href"].split("team_id=")[1].split("&")[0]
                        
                        matches.append({
                            "league": current_league,
                            "home": home_team_clean,
                            "away": away_team_clean,
                            "home_team_id": home_team_id,
                            "away_team_id": away_team_id,
                            "date": date_str,
                            "status": "Scheduled" if score_or_v.lower() == "v" else "Completed",
                            "score": score_or_v if score_or_v.lower() != "v" else None
                        })
        return matches
    except Exception as e:
        print(f"[ERROR] Failed to fetch fixtures from Soccerbase: {e}")
        return []

def fetch_soccerbase_team_results(team_id):
    url = f"https://www.soccerbase.com/teams/team.sd?team_id={team_id}&teamTabs=results"
    try:
        headers = get_random_headers()
        response = session.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        matches = []
        team_name = None
        team_header = soup.find("table", class_="imageHead")
        if team_header:
            team_name = team_header.get_text(strip=True).split("Results")[0].strip().lower()
        
        tables = soup.find_all("table", class_="soccerGrid")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows[2:]:
                cells = row.find_all("td")
                if len(cells) >= 8:
                    date_cell = cells[1]
                    home_team = cells[3].get_text(strip=True).lower()
                    score = cells[4].get_text(strip=True)
                    away_team = cells[5].get_text(strip=True).lower()
                    
                    if "-" in score:
                        try:
                            iso_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(date_cell))
                            match_date_str = iso_match.group(1) if iso_match else None
                            
                            gf_h, gf_a = map(int, score.split("-"))
                            home_team_clean = re.sub(r'\s*\d+.*$', '', home_team).strip()
                            away_team_clean = re.sub(r'\s*\d+.*$', '', away_team).strip()
                            
                            # Robust normalized containment check
                            if team_name:
                                is_home = (team_name in home_team_clean or home_team_clean in team_name)
                            else:
                                is_home = True # Fallback baseline
                                
                            gf = gf_h if is_home else gf_a
                            ga = gf_a if is_home else gf_h
                            
                            matches.append({
                                "home_team": home_team_clean,
                                "away_team": away_team_clean,
                                "gf": gf,
                                "ga": ga,
                                "is_home": is_home,
                                "date_str": match_date_str
                            })
                        except Exception:
                            continue
        
        # Ensure fallback filtering works seamlessly even on missing date items
        matches.sort(key=lambda x: x["date_str"] if x["date_str"] else "0000-00-00", reverse=True)
        return matches
    except Exception as e:
        print(f"[ERROR] Failed to fetch team results for team_id {team_id}: {e}")
        return []

def get_team_form(team_id, is_home=True, num_matches=3, target_date_str=None):
    all_matches = fetch_soccerbase_team_results(team_id)
    form = []
    for match in all_matches:
        if target_date_str and match["date_str"] and match["date_str"] >= target_date_str:
            continue
        if (is_home and match["is_home"]) or (not is_home and not match["is_home"]):
            form.append((match["gf"], match["ga"]))
            if len(form) >= num_matches:
                break
    return form

def apply_algorithm(home_data_3, away_data_3, home_data_6=None, away_data_6=None):
    passed = []
    failed = []
    details = {}
    is_perfect = True

    if len(home_data_3) < 3 or len(away_data_3) < 3:
        return None, None, {"error": "Insufficient data (need 3 matches minimum)"}, False

    # H1: Home total goals in 3 matches >= 7
    home_goals_total = sum(gf + ga for gf, ga in home_data_3)
    if home_goals_total >= 7:
        passed.append("H1")
        details['H1'] = f"PASS ({home_goals_total} goals)"
    else:
        failed.append("H1")
        details['H1'] = f"FAIL ({home_goals_total}, need 7+)"
        is_perfect = False

    # H2: Home Over 2.5 count >= 2
    home_over25_3 = sum(1 for gf, ga in home_data_3 if gf + ga > 2.5)
    if home_over25_3 >= 2:
        passed.append("H2")
        details['H2'] = "PERFECT PASS (3/3 Over 2.5)" if home_over25_3 == 3 else f"PASS ({home_over25_3}/3)"
        if home_over25_3 != 3: is_perfect = False
    else:
        failed.append("H2")
        details['H2'] = f"FAIL ({home_over25_3}/3, need 2+)"
        is_perfect = False

    # A1: Away total goals in 3 matches >= 7
    away_goals_total = sum(gf + ga for gf, ga in away_data_3)
    if away_goals_total >= 7:
        passed.append("A1")
        details['A1'] = f"PASS ({away_goals_total} goals)"
    else:
        failed.append("A1")
        details['A1'] = f"FAIL ({away_goals_total}, need 7+)"
        is_perfect = False

    # A2: Most recent away match total goals >= 2
    prev_away_total = away_data_3[0][0] + away_data_3[0][1]
    if prev_away_total >= 2:
        passed.append("A2")
        details['A2'] = f"PASS ({prev_away_total} goals)"
    else:
        failed.append("A2")
        details['A2'] = f"FAIL ({prev_away_total}, need 2+)"
        is_perfect = False

    # A3: Away team scored in >= 2 of last 3 matches
    away_scored = sum(1 for gf, _ in away_data_3 if gf > 0)
    if away_scored >= 2:
        passed.append("A3")
        details['A3'] = "PERFECT PASS (scored in 3/3)" if away_scored == 3 else f"PASS (scored in {away_scored}/3)"
        if away_scored != 3: is_perfect = False
    else:
        failed.append("A3")
        details['A3'] = f"FAIL (scored in {away_scored}/3, need 2+)"
        is_perfect = False

    # A4: Away Over 2.5 count >= 2
    away_over25_3 = sum(1 for gf, ga in away_data_3 if gf + ga > 2.5)
    if away_over25_3 >= 2:
        passed.append("A4")
        details['A4'] = "PERFECT PASS (3/3 Over 2.5)" if away_over25_3 == 3 else f"PASS ({away_over25_3}/3)"
        if away_over25_3 != 3: is_perfect = False
    else:
        failed.append("A4")
        details['A4'] = f"FAIL ({away_over25_3}/3, need 2+)"
        is_perfect = False

    # H3: Home Over 2.5 in 6 matches >= 4
    if home_data_6 and len(home_data_6) >= 6:
        home_over25_6 = sum(1 for gf, ga in home_data_6 if gf + ga > 2.5)
        if home_over25_6 >= 4:
            passed.append("H3")
            details['H3'] = f"PASS ({home_over25_6}/6 Over 2.5)"
        else:
            failed.append("H3")
            details['H3'] = f"FAIL ({home_over25_6}/6, need 4+)"
            is_perfect = False
    
    # A5: Away Over 2.5 in 6 matches >= 4
    if away_data_6 and len(away_data_6) >= 6:
        away_over25_6 = sum(1 for gf, ga in away_data_6 if gf + ga > 2.5)
        if away_over25_6 >= 4:
            passed.append("A5")
            details['A5'] = f"PASS ({away_over25_6}/6 Over 2.5)"
        else:
            failed.append("A5")
            details['A5'] = f"FAIL ({away_over25_6}/6, need 4+)"
            is_perfect = False

    # H4: Total goals in last 6 home games >= 18
    if home_data_6 and len(home_data_6) >= 6:
        home_total_goals_6 = sum(gf + ga for gf, ga in home_data_6)
        if home_total_goals_6 >= 18:
            passed.append("H4")
            details['H4'] = f"PASS ({home_total_goals_6} total goals)"
        else:
            failed.append("H4")
            details['H4'] = f"FAIL ({home_total_goals_6}, need 18+)"
            is_perfect = False
    
    # A6: Total goals in last 6 away games >= 18
    if away_data_6 and len(away_data_6) >= 6:
        away_total_goals_6 = sum(gf + ga for gf, ga in away_data_6)
        if away_total_goals_6 >= 18:
            passed.append("A6")
            details['A6'] = f"PASS ({away_total_goals_6} total goals)"
        else:
            failed.append("A6")
            details['A6'] = f"FAIL ({away_total_goals_6}, need 18+)"
            is_perfect = False

    return passed, failed, details, is_perfect

def format_match_block(idx, match_dict):
    m = match_dict["match"]
    lines = [
        f"\n{idx}. {m['league']}: {m['home']} vs {m['away']}",
        f"   Score Metrics Passed: {match_dict['score']}/10 (Perfect: {match_dict['is_perfect']})"
    ]
    lines.append("   Checks:")
    for check, status in match_dict['details'].items():
        lines.append(f"     • {check}: {status}")
    return "\n".join(lines)

def main(date_str=None, only_scheduled=False):
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    
    print(f"[+] Starting analysis for Date: {date_str}...")
    all_matches = fetch_soccerbase_fixtures(date_str)
    
    seen = set()
    unique_matches = []
    for m in all_matches:
        key = (m["home"], m["away"], m["league"])
        if key not in seen:
            seen.add(key)
            unique_matches.append(m)
    all_matches = unique_matches

    if only_scheduled:
        all_matches = [m for m in all_matches if m["status"] == "Scheduled"]

    perfect = []
    qualified = []
    close_calls = []
    under25_0 = []
    under25_1 = []
    insufficient_data_count = 0

    for match in all_matches:
        home_id = match["home_team_id"]
        away_id = match["away_team_id"]

        if not home_id or not away_id:
            continue

        home_form_3 = get_team_form(home_id, is_home=True, num_matches=3, target_date_str=date_str)
        away_form_3 = get_team_form(away_id, is_home=False, num_matches=3, target_date_str=date_str)
        home_form_6 = get_team_form(home_id, is_home=True, num_matches=6, target_date_str=date_str)
        away_form_6 = get_team_form(away_id, is_home=False, num_matches=6, target_date_str=date_str)

        if len(home_form_3) < 3 or len(away_form_3) < 3:
            insufficient_data_count += 1
            continue

        passed, failed, details, is_perfect = apply_algorithm(home_form_3, away_form_3, home_form_6, away_form_6)
        if passed is None:
            continue

        result = {
            "match": match,
            "passed": passed,
            "failed": failed,
            "details": details,
            "home_form": home_form_3,
            "home_form_6": home_form_6,
            "away_form": away_form_3,
            "away_form_6": away_form_6,
            "score": len(passed),
            "is_perfect": is_perfect
        }

        if len(passed) == 10:
            if is_perfect: perfect.append(result)
            else: qualified.append(result)
        elif len(passed) == 9:
            close_calls.append(result)
        elif len(passed) == 0:
            under25_0.append(result)
        elif len(passed) == 1:
            under25_1.append(result)

        if match != all_matches[-1]:
            random_delay()

    email = []
    email.append("⚽ DAILY OVER 2.5 GOALS PREDICTIONS RECAP")
    email.append(f"📅 Date: {date_str}")
    email.append(f"📊 Scope: {'Only Scheduled' if only_scheduled else 'All Matches (Scheduled + Completed)'}")
    email.append("-" * 50)
    email.append(f"• Total fixtures parsed: {len(all_matches)}")
    email.append(f"• Skipped (insufficient data): {insufficient_data_count}")
    email.append(f"• High Value Targets Identified: {len(perfect) + len(qualified)}")
    email.append("-" * 50 + "\n")

    email.append("⭐ PERFECT MATCHES (10/10 Checks & Strict Form)")
    if perfect:
        for idx, p in enumerate(perfect, 1): email.append(format_match_block(idx, p))
    else:
        email.append("  None found today.")

    email.append("\n✅ QUALIFIED MATCHES (10/10 Checks)")
    if qualified:
        for idx, q in enumerate(qualified, 1): email.append(format_match_block(idx, q))
    else:
        email.append("  None found today.")

    email.append("\n⚠️ CLOSE CALLS (9/10 Checks)")
    if close_calls:
        for c in close_calls:
            m = c["match"]
            email.append(f"• {m['league']}: {m['home']} vs {m['away']} | Failed: {', '.join(c['failed'])}")
    else:
        email.append("  None.")

    email.append("\n📉 UNDER 2.5 GOALS CANDIDATES (0/10 or 1/10 Checks)")
    under25_all = under25_0 + under25_1
    if under25_all:
        for u in under25_all:
            m = u["match"]
            email.append(f"• {m['league']}: {m['home']} vs {m['away']} ({u['score']}/10 checks)")
    else:
        email.append("  None.")

    print("\n===EMAIL_START===")
    print("\n".join(email))
    print("===EMAIL_END===")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Soccerbase Over 2.5 goals algorithm.")
    parser.add_argument("date", nargs="?", default=datetime.now().strftime("%Y-%m-%d"), help="Date in YYYY-MM-DD format")
    parser.add_argument("--scheduled", action="store_true", help="Only analyze scheduled matches")
    args = parser.parse_args()
    main(args.date, only_scheduled=args.scheduled)
