#!/usr/bin/env python3
"""
OVER 2.5 GOALS PREDICTION SYSTEM - SOCCERBASE VERSION
=======================================================
Uses Soccerbase.com for fixtures, results, and team form data.

FIXES APPLIED:
1. Added date extraction and proper sorting of matches (newest first)
2. Improved home/away detection reliability
3. Added retries for requests to handle temporary blocks/errors
4. Added option to filter only scheduled matches (enabled by default)
5. EXTRACTED HIDDEN ISO DATES for accurate date filtering
6. ADDED TARGET DATE FILTER: only use matches BEFORE fixture date
7. STATS ON SEPARATE LINES
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

# Configure retries for requests
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
    """Return headers with a random User-Agent"""
    headers = HEADERS.copy()
    headers["User-Agent"] = ua.random
    return headers


def random_delay():
    """Wait for a random amount of time (1.5 to 3.5 seconds)"""
    delay = random.uniform(1.5, 3.5)
    time.sleep(delay)


def fetch_soccerbase_fixtures(date_str):
    """
    Fetch fixtures/results from Soccerbase for a specific date.
    date_str should be in YYYY-MM-DD format.
    """
    url = f"https://www.soccerbase.com/matches/results.sd?date={date_str}"
    try:
        headers = get_random_headers()
        response = session.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        matches = []
        current_league = None
        
        tables = soup.find_all("table", class_="listWithCards")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all("td")
                league_link = row.find("a", href=lambda href: href and "comp_id=" in href)
                if league_link:
                    current_league = league_link.get_text(strip=True)
                elif len(cells) >= 6:
                    home_team = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                    score_or_v = cells[4].get_text(strip=True) if len(cells) > 4 else ""
                    away_team = cells[5].get_text(strip=True) if len(cells) > 5 else ""
                    
                    if home_team and away_team and current_league:
                        team_links = row.find_all("a", href=True)
                        home_team_id = None
                        away_team_id = None
                        for link in team_links:
                            href = link["href"]
                            if "team_id=" in href:
                                team_id = href.split("team_id=")[1].split("&")[0]
                                if not home_team_id:
                                    home_team_id = team_id
                                else:
                                    away_team_id = team_id
                        
                        matches.append({
                            "league": current_league,
                            "home": home_team,
                            "away": away_team,
                            "home_team_id": home_team_id,
                            "away_team_id": away_team_id,
                            "date": date_str,
                            "status": "Scheduled" if score_or_v == "v" else "Completed",
                            "score": score_or_v if score_or_v != "v" else None
                        })
        return matches
    except Exception as e:
        print(f"[ERROR] Failed to fetch fixtures from Soccerbase: {e}")
        return []


def fetch_soccerbase_team_results(team_id):
    """
    Fetch recent results for a team from Soccerbase using team_id.
    Returns list of matches sorted NEWEST FIRST with (home_team, away_team, gf, ga, is_home, date)
    Uses HIDDEN ISO DATES from Soccerbase's HTML for accuracy!
    """
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
            team_name = team_header.get_text(strip=True).split("Results")[0].strip()
        
        tables = soup.find_all("table", class_="soccerGrid")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows[2:]:
                cells = row.find_all("td")
                if len(cells) >= 8:
                    date_cell = cells[1]
                    home_team = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                    score = cells[4].get_text(strip=True) if len(cells) > 4 else ""
                    away_team = cells[5].get_text(strip=True) if len(cells) > 5 else ""
                    
                    if "-" in score:
                        try:
                            # EXTRACT HIDDEN ISO DATE from the cell HTML
                            iso_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(date_cell))
                            match_date_str = iso_match.group(1) if iso_match else None
                            
                            gf_h, gf_a = map(int, score.split("-"))
                            home_team_clean = re.sub(r'\s+\d+\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}$', '', home_team)
                            away_team_clean = re.sub(r'\s+\d+\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}$', '', away_team)
                            
                            is_home = team_name and (team_name in home_team_clean or home_team_clean in team_name)
                            if is_home:
                                gf = gf_h
                                ga = gf_a
                            else:
                                gf = gf_a
                                ga = gf_h
                            
                            matches.append({
                                "home_team": home_team_clean,
                                "away_team": away_team_clean,
                                "gf": gf,
                                "ga": ga,
                                "is_home": is_home,
                                "date_str": match_date_str
                            })
                        except Exception as e:
                            continue
        
        # Sort matches NEWEST FIRST using the ISO date string
        matches.sort(key=lambda x: x["date_str"] or "", reverse=True)
        return matches
    except Exception as e:
        print(f"[ERROR] Failed to fetch team results for team_id {team_id}: {e}")
        return []


def get_team_form(team_id, is_home=True, num_matches=3, target_date_str=None):
    """
    Get last N home or away matches for a team that occurred BEFORE target_date_str.
    Returns list of (gf, ga) tuples, NEWEST FIRST.
    """
    all_matches = fetch_soccerbase_team_results(team_id)
    form = []
    for match in all_matches:
        # Skip if match date is >= target date
        if target_date_str and match["date_str"] and match["date_str"] >= target_date_str:
            continue
        if (is_home and match["is_home"]) or (not is_home and not match["is_home"]):
            form.append((match["gf"], match["ga"]))
            if len(form) >= num_matches:
                break
    return form


def apply_algorithm(home_data_3, away_data_3, home_data_6=None, away_data_6=None):
    """
    Apply the Over 2.5 goals prediction algorithm
    home_data_3/away_data_3: list of (gf, ga) tuples (last 3 matches, NEWEST FIRST)
    home_data_6/away_data_6: list of (gf, ga) tuples (last 6 matches, NEWEST FIRST, optional)
    Returns: (passed_list, failed_list, details_dict, is_perfect)
    """
    passed = []
    failed = []
    details = {}
    is_perfect = True

    if len(home_data_3) < 3 or len(away_data_3) < 3:
        return None, None, {"error": "Insufficient data (need 3 matches minimum)"}, False

    # Original 6 checks (H1/H2, A1-A4)
    home_goals_total = sum(gf + ga for gf, ga in home_data_3)
    if home_goals_total >= 7:
        passed.append("H1")
        details['H1'] = f"PASS ({home_goals_total} goals)"
    else:
        failed.append("H1")
        details['H1'] = f"FAIL ({home_goals_total}, need 7+)"
        is_perfect = False

    home_over25_3 = sum(1 for gf, ga in home_data_3 if gf + ga > 2.5)
    if home_over25_3 >= 2:
        passed.append("H2")
        if home_over25_3 == 3:
            details['H2'] = f"PERFECT PASS (3/3 Over 2.5)"
        else:
            details['H2'] = f"PASS ({home_over25_3}/3)"
            is_perfect = False
    else:
        failed.append("H2")
        details['H2'] = f"FAIL ({home_over25_3}/3, need 2+)"
        is_perfect = False

    away_goals_total = sum(gf + ga for gf, ga in away_data_3)
    if away_goals_total >= 7:
        passed.append("A1")
        details['A1'] = f"PASS ({away_goals_total} goals)"
    else:
        failed.append("A1")
        details['A1'] = f"FAIL ({away_goals_total}, need 7+)"
        is_perfect = False

    prev_away_total = away_data_3[0][0] + away_data_3[0][1]
    if prev_away_total >= 2:
        passed.append("A2")
        details['A2'] = f"PASS ({prev_away_total} goals)"
    else:
        failed.append("A2")
        details['A2'] = f"FAIL ({prev_away_total}, need 2+)"
        is_perfect = False

    away_scored = sum(1 for gf, _ in away_data_3 if gf > 0)
    if away_scored >= 2:
        passed.append("A3")
        if away_scored == 3:
            details['A3'] = f"PERFECT PASS (scored in 3/3)"
        else:
            details['A3'] = f"PASS (scored in {away_scored}/3)"
            is_perfect = False
    else:
        failed.append("A3")
        details['A3'] = f"FAIL (scored in {away_scored}/3, need 2+)"
        is_perfect = False

    away_over25_3 = sum(1 for gf, ga in away_data_3 if gf + ga > 2.5)
    if away_over25_3 >= 2:
        passed.append("A4")
        if away_over25_3 == 3:
            details['A4'] = f"PERFECT PASS (3/3 Over 2.5)"
        else:
            details['A4'] = f"PASS ({away_over25_3}/3)"
            is_perfect = False
    else:
        failed.append("A4")
        details['A4'] = f"FAIL ({away_over25_3}/3, need 2+)"
        is_perfect = False

    # New clauses: 4/6 home and away Over 2.5 (optional)
    if home_data_6 and len(home_data_6) >= 6:
        home_over25_6 = sum(1 for gf, ga in home_data_6 if gf + ga > 2.5)
        if home_over25_6 >= 4:
            passed.append("H3")
            details['H3'] = f"PASS ({home_over25_6}/6 Over 2.5)"
        else:
            failed.append("H3")
            details['H3'] = f"FAIL ({home_over25_6}/6, need 4+)"
            is_perfect = False
    
    if away_data_6 and len(away_data_6) >= 6:
        away_over25_6 = sum(1 for gf, ga in away_data_6 if gf + ga > 2.5)
        if away_over25_6 >= 4:
            passed.append("A5")
            details['A5'] = f"PASS ({away_over25_6}/6 Over 2.5)"
        else:
            failed.append("A5")
            details['A5'] = f"FAIL ({away_over25_6}/6, need 4+)"
            is_perfect = False

    # New clauses: Total goals in last 6 home/away matches ≥20 (optional)
    if home_data_6 and len(home_data_6) >= 6:
        home_total_goals_6 = sum(gf + ga for gf, ga in home_data_6)
        if home_total_goals_6 >= 20:
            passed.append("H4")
            details['H4'] = f"PASS ({home_total_goals_6} total goals)"
        else:
            failed.append("H4")
            details['H4'] = f"FAIL ({home_total_goals_6}, need 20+)"
            is_perfect = False
    
    if away_data_6 and len(away_data_6) >= 6:
        away_total_goals_6 = sum(gf + ga for gf, ga in away_data_6)
        if away_total_goals_6 >= 20:
            passed.append("A6")
            details['A6'] = f"PASS ({away_total_goals_6} total goals)"
        else:
            failed.append("A6")
            details['A6'] = f"FAIL ({away_total_goals_6}, need 20+)"
            is_perfect = False

    return passed, failed, details, is_perfect


def main(date_str=None, only_scheduled=False):
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    
    print("=" * 70)
    print("OVER 2.5 GOALS PREDICTION SYSTEM - SOCCERBASE")
    print(f"Date: {date_str}")
    if only_scheduled:
        print("Only analyzing scheduled matches")
    else:
        print("Analyzing all matches (scheduled + completed)")
    print("=" * 70)

    print("\n[+] Fetching fixtures from Soccerbase...")
    all_matches = fetch_soccerbase_fixtures(date_str)
    print(f"[*] Total matches found: {len(all_matches)}")

    # Remove duplicates (using a set of (home, away, league) as key)
    seen = set()
    unique_matches = []
    for m in all_matches:
        key = (m["home"], m["away"], m["league"])
        if key not in seen:
            seen.add(key)
            unique_matches.append(m)
    if len(unique_matches) < len(all_matches):
        print(f"[*] Removed {len(all_matches) - len(unique_matches)} duplicate(s)")
    all_matches = unique_matches

    if only_scheduled:
        all_matches = [m for m in all_matches if m["status"] == "Scheduled"]
        print(f"[*] Scheduled matches to analyze: {len(all_matches)}")
    else:
        print(f"[*] Matches to analyze: {len(all_matches)}")

    print("\n[+] Analyzing team form (matches before {})...".format(date_str))
    perfect = []
    qualified = []
    close_calls = []
    under25 = []
    all_results = []

    for match in all_matches:
        home = match["home"]
        away = match["away"]
        home_id = match["home_team_id"]
        away_id = match["away_team_id"]

        print(f"\n   Checking {home} vs {away}...", end=" ")

        if not home_id or not away_id:
            print("[WARN] No team IDs found")
            continue

        # Pass target_date_str to filter team form
        home_form_3 = get_team_form(home_id, is_home=True, num_matches=3, target_date_str=date_str)
        away_form_3 = get_team_form(away_id, is_home=False, num_matches=3, target_date_str=date_str)
        home_form_6 = get_team_form(home_id, is_home=True, num_matches=6, target_date_str=date_str)
        away_form_6 = get_team_form(away_id, is_home=False, num_matches=6, target_date_str=date_str)

        if len(home_form_3) < 3 or len(away_form_3) < 3:
            print(f"[WARN] Insufficient data (home: {len(home_form_3)}, away: {len(away_form_3)})")
            continue

        passed, failed, details, is_perfect = apply_algorithm(home_form_3, away_form_3, home_form_6, away_form_6)

        if passed is None:
            print("[ERROR] Error in algorithm")
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
            if is_perfect:
                perfect.append(result)
                print("[OK] PERFECT QUALIFIED (10/10)")
            else:
                qualified.append(result)
                print("[OK] QUALIFIED (10/10)")
        elif len(passed) == 9:
            close_calls.append(result)
            print("[WARN] Close (9/10)")
        elif len(passed) <= 1:
            under25.append(result)
            print("[!] UNDER 2.5 CANDIDATE ({} checks)".format(len(passed)))
        else:
            print(f"[X] ({len(passed)}/10)")

        # Add random delay between matches
        if match != all_matches[-1]:  # Don't delay after last match
            random_delay()

    print("\n" + "=" * 70)
    print("[+] PERFECT MATCHES (10/10 checks)")
    print("=" * 70)

    if perfect:
        for i, p in enumerate(perfect, 1):
            m = p["match"]
            print(f"\n{i}. {m['league']}: {m['home']} vs {m['away']}")
            print(f"   Status: {m['status']}")
            if m['score']:
                print(f"   Score: {m['score']}")
            print(f"   Home form (last 3):")
            for idx, gf_ga in enumerate(p['home_form'], 1):
                print(f"     {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"   Home form (last 6):")
            for idx, gf_ga in enumerate(p['home_form_6'], 1):
                print(f"     {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"   Away form (last 3):")
            for idx, gf_ga in enumerate(p['away_form'], 1):
                print(f"     {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"   Away form (last 6):")
            for idx, gf_ga in enumerate(p['away_form_6'], 1):
                print(f"     {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"   Checks:")
            for check, status in p['details'].items():
                print(f"     {check}: {status}")
    else:
        print("\n[X] No perfect matches today.")

    print("\n" + "=" * 70)
    print("[+] QUALIFIED MATCHES (10/10 checks)")
    print("=" * 70)

    if qualified:
        for i, q in enumerate(qualified, 1):
            m = q["match"]
            print(f"\n{i}. {m['league']}: {m['home']} vs {m['away']}")
            print(f"   Status: {m['status']}")
            if m['score']:
                print(f"   Score: {m['score']}")
            print(f"   Home form (last 3):")
            for idx, gf_ga in enumerate(q['home_form'], 1):
                print(f"     {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"   Home form (last 6):")
            for idx, gf_ga in enumerate(q['home_form_6'], 1):
                print(f"     {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"   Away form (last 3):")
            for idx, gf_ga in enumerate(q['away_form'], 1):
                print(f"     {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"   Away form (last 6):")
            for idx, gf_ga in enumerate(q['away_form_6'], 1):
                print(f"     {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"   Checks:")
            for check, status in q['details'].items():
                print(f"     {check}: {status}")
    else:
        print("\n[X] No matches fully qualified (non-perfect).")

    print("\n" + "=" * 70)
    print("[WARN] CLOSE CALLS (9/10)")
    print("=" * 70)

    if close_calls:
        for c in close_calls:
            m = c["match"]
            print(f"\n- {m['league']}: {m['home']} vs {m['away']}")
            print(f"  Status: {m['status']}")
            if m['score']:
                print(f"  Score: {m['score']}")
            print(f"  Failed: {', '.join(c['failed'])}")
            print(f"  Home form (last 3):")
            for idx, gf_ga in enumerate(c['home_form'], 1):
                print(f"    {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"  Home form (last 6):")
            for idx, gf_ga in enumerate(c['home_form_6'], 1):
                print(f"    {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"  Away form (last 3):")
            for idx, gf_ga in enumerate(c['away_form'], 1):
                print(f"    {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"  Away form (last 6):")
            for idx, gf_ga in enumerate(c['away_form_6'], 1):
                print(f"    {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"  Checks:")
            for check, status in c['details'].items():
                print(f"    {check}: {status}")
    else:
        print("\nNone")

    print("\n" + "=" * 70)
    print("[!] UNDER 2.5 CANDIDATES (0/10 or 1/10 checks)")
    print("=" * 70)

    if under25:
        for i, u in enumerate(under25, 1):
            m = u["match"]
            print(f"\n{i}. {m['league']}: {m['home']} vs {m['away']}")
            print(f"   Status: {m['status']}")
            if m['score']:
                print(f"   Score: {m['score']}")
            print(f"   Passed checks: {len(u['passed'])}/10")
            print(f"   Home form (last 3):")
            for idx, gf_ga in enumerate(u['home_form'], 1):
                print(f"     {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"   Home form (last 6):")
            for idx, gf_ga in enumerate(u['home_form_6'], 1):
                print(f"     {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"   Away form (last 3):")
            for idx, gf_ga in enumerate(u['away_form'], 1):
                print(f"     {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"   Away form (last 6):")
            for idx, gf_ga in enumerate(u['away_form_6'], 1):
                print(f"     {idx}. GF: {gf_ga[0]}, GA: {gf_ga[1]} (Total: {gf_ga[0]+gf_ga[1]})")
            print(f"   Checks:")
            for check, status in u['details'].items():
                print(f"     {check}: {status}")
    else:
        print("\nNone")

    output = {
        "date": date_str,
        "only_scheduled": only_scheduled,
        "perfect": perfect,
        "qualified": qualified,
        "close_calls": close_calls,
        "under25": under25,
        "total_matches": len(all_matches)
    }

    filename = f"predictions_soccerbase_{date_str}.json"
    with open(filename, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n[*] Results saved to: {filename}")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Soccerbase Over 2.5 goals algorithm.")
    parser.add_argument("date", nargs="?", default=datetime.now().strftime("%Y-%m-%d"), help="Date in YYYY-MM-DD format")
    parser.add_argument("--scheduled", action="store_true", help="Only analyze scheduled matches (default: all matches)")
    parser.add_argument("--all", action="store_true", help="Backwards compatibility (now default)")
    parser.add_argument("--json-out", help="Optional path to save JSON output")
    args = parser.parse_args()
    # --all is now default, so only use --scheduled if provided
    main(args.date, only_scheduled=args.scheduled)
