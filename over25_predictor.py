#!/usr/bin/env python3
"""
OVER 2.5 GOALS PREDICTION SYSTEM
================================
Auto-fetches today's matches and applies the 6-check algorithm.

ALGORITHM RULES:
H1 - Home team: 7+ goals in last 3 home matches
H2 - Home team: 2 or 3 of last 3 home matches ended Over 2.5
A1 - Away team: 7+ goals in last 3 away matches  
A2 - Away team: previous match had 2+ goals total
A3 - Away team: scored in 2 or 3 of last 3 away matches
A4 - Away team: 2 or 3 of last 3 away matches ended Over 2.5

REQUIREMENTS: pip install requests beautifulsoup4
"""

import requests
import json
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import time

# ============================================================
# CONFIGURATION
# ============================================================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Create persistent session globally
session = requests.Session()
session.headers.update(HEADERS)

# WorldFootball.net league URLs (reliable for fixtures & results)
LEAGUE_SOURCES = {
    # Top European Leagues
    "Premier League": "https://www.worldfootball.net/all_matches/eng-premier-league/",
    "La Liga": "https://www.worldfootball.net/all_matches/esp-primera-division/",
    "Serie A": "https://www.worldfootball.net/all_matches/ita-serie-a/",
    "Bundesliga": "https://www.worldfootball.net/all_matches/bundesliga/",
    "Ligue 1": "https://www.worldfootball.net/all_matches/fra-ligue-1/",

    # Secondary Leagues (high scoring)
    "Eredivisie": "https://www.worldfootball.net/all_matches/ned-eredivisie/",
    "Belgian Pro League": "https://www.worldfootball.net/all_matches/bel-jupiler-pro-league/",
    "Austrian Bundesliga": "https://www.worldfootball.net/all_matches/aut-bundesliga/",
    "Swiss Super League": "https://www.worldfootball.net/all_matches/sui-super-league/",
    "Scottish Premiership": "https://www.worldfootball.net/all_matches/sco-premiership/",

    # Scandinavian Leagues (summer season - high value)
    "Eliteserien": "https://www.worldfootball.net/all_matches/nor-eliteserien/",
    "Allsvenskan": "https://www.worldfootball.net/all_matches/swe-allsvenskan/",
    "Superettan": "https://www.worldfootball.net/all_matches/swe-superettan/",
    "Danish Superliga": "https://www.worldfootball.net/all_matches/den-superligaen/",

    # Other High-Scoring Leagues
    "MLS": "https://www.worldfootball.net/all_matches/usa-major-league-soccer/",
    "J1 League": "https://www.worldfootball.net/all_matches/jpn-j1-league/",
    "K League 1": "https://www.worldfootball.net/all_matches/kor-k-league-1/",
    "A-League": "https://www.worldfootball.net/all_matches/aus-a-league/",
    "Brasileirão": "https://www.worldfootball.net/all_matches/bra-serie-a/",
    "Argentine Primera": "https://www.worldfootball.net/all_matches/arg-primera-division/",
    "Chile Primera": "https://www.worldfootball.net/all_matches/chi-primera-division/",
    "Liga MX": "https://www.worldfootball.net/all_matches/mex-liga-mx/",

    # Lower divisions (often higher scoring)
    "Championship": "https://www.worldfootball.net/all_matches/eng-championship/",
    "League One": "https://www.worldfootball.net/all_matches/eng-league-one/",
    "Segunda Division": "https://www.worldfootball.net/all_matches/esp-segunda-division/",
    "Serie B": "https://www.worldfootball.net/all_matches/ita-serie-b/",
    "2. Bundesliga": "https://www.worldfootball.net/all_matches/2-bundesliga/",
    "Ligue 2": "https://www.worldfootball.net/all_matches/fra-ligue-2/",
}

# ============================================================
# DATA FETCHING FUNCTIONS
# ============================================================

def get_current_season():
    """Determine current season string for URLs"""
    now = datetime.now()
    year = now.year
    month = now.month

    # European leagues: Aug-May season
    if month >= 8:
        return f"{year}-{year+1}"
    else:
        return f"{year-1}-{year}"

def get_scandinavian_season():
    """Scandinavian leagues run April-November"""
    now = datetime.now()
    return str(now.year)

def fetch_fixtures_worldfootball(league_name, url_template):
    """Fetch today's fixtures from WorldFootball.net"""
    today = datetime.now()
    today_str = today.strftime("%d.%m.%Y")

    # Determine season
    if league_name in ["Eliteserien", "Allsvenskan", "Superettan", "Danish Superliga"]:
        season = get_scandinavian_season()
    elif league_name in ["MLS", "Brasileirão", "Argentine Primera", "Chile Primera", "Liga MX"]:
        season = str(today.year)
    else:
        season = get_current_season()

    url = f"{url_template}{season}/"

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        matches = []
        # Find all match rows
        rows = soup.find_all('tr')

        for row in rows:
            tds = row.find_all('td')
            if len(tds) >= 4:
                # Extract date
                date_text = tds[0].get_text(strip=True)
                if today_str in date_text or is_today_date(date_text):
                    time_text = tds[1].get_text(strip=True) if len(tds) > 1 else ""
                    home = tds[2].get_text(strip=True)
                    away = tds[3].get_text(strip=True)

                    if home and away and home != '-':
                        matches.append({
                            'league': league_name,
                            'home': clean_team_name(home),
                            'away': clean_team_name(away),
                            'date': today.strftime('%Y-%m-%d'),
                            'time': time_text,
                            'status': 'Scheduled'
                        })

        return matches
    except Exception as e:
        print(f"[WARN] Error fetching {league_name}: {e}")
        return []

def is_today_date(date_text):
    """Check if date text is today"""
    today = datetime.now()
    patterns = [
        today.strftime('%d.%m.%Y'),
        today.strftime('%d/%m/%Y'),
        today.strftime('%Y-%m-%d'),
    ]
    return any(p in date_text for p in patterns)

def clean_team_name(name):
    """Clean team names for consistency"""
    name = name.replace('&nbsp;', ' ').strip()
    # Remove common suffixes
    suffixes = [' FC', ' CF', ' SC', ' BK', ' IF', ' SK', ' FK']
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name.strip()

def fetch_team_recent_matches(team_name, is_home=True, num_matches=3):
    """
    Fetch last N matches for a team from WorldFootball.net
    Returns list of (goals_for, goals_against) tuples
    """
    # This searches the team's schedule page
    search_url = f"https://www.worldfootball.net/teams/{team_name.lower().replace(' ', '-')}/"

    try:
        response = requests.get(search_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')

        matches = []
        # Find match table
        tables = soup.find_all('table', class_='standard_tabelle')

        for table in tables:
            rows = table.find_all('tr')[1:]  # Skip header
            for row in rows:
                tds = row.find_all('td')
                if len(tds) >= 5:
                    # Determine if home/away
                    venue = 'home' if 'align="right"' in str(tds[2]) else 'away'

                    if is_home and venue == 'home':
                        score_td = tds[4] if len(tds) > 4 else None
                    elif not is_home and venue == 'away':
                        score_td = tds[4] if len(tds) > 4 else None
                    else:
                        continue

                    if score_td:
                        score_text = score_td.get_text(strip=True)
                        if ':' in score_text and not 'vs' in score_text:
                            try:
                                parts = score_text.split(':')
                                if is_home:
                                    gf = int(parts[0].strip())
                                    ga = int(parts[1].strip().split()[0])
                                else:
                                    ga = int(parts[0].strip())
                                    gf = int(parts[1].strip().split()[0])
                                matches.append((gf, ga))
                            except:
                                continue

                    if len(matches) >= num_matches:
                        return matches[:num_matches]

        return matches[:num_matches] if matches else []
    except Exception as e:
        print(f"[WARN] Error fetching data for {team_name}: {e}")
        return []

# ============================================================
# ALTERNATIVE: API-BASED DATA (MORE RELIABLE)
# ============================================================

def fetch_from_api_football(league_id, season, api_key):
    """
    Fetch fixtures and results from API-Football (api-football.com)
    Free tier: 100 requests/day
    Get API key at: https://www.api-football.com/
    """
    base_url = "https://v3.football.api-sports.io/"
    headers = {
        'x-rapidapi-key': api_key,
        'x-rapidapi-host': 'v3.football.api-sports.io'
    }

    today = datetime.now().strftime('%Y-%m-%d')

    # Get fixtures
    fixtures_url = f"{base_url}fixtures?date={today}&league={league_id}&season={season}"
    response = requests.get(fixtures_url, headers=headers)
    fixtures = response.json().get('response', [])

    matches = []
    for fixture in fixtures:
        home = fixture['teams']['home']['name']
        away = fixture['teams']['away']['name']
        matches.append({
            'league': str(league_id),
            'home': home,
            'away': away,
            'fixture_id': fixture['fixture']['id'],
            'date': today
        })

    return matches

def get_team_form_api(team_id, api_key, last_n=3, venue='home'):
    """Get last N matches for a team using API-Football"""
    base_url = "https://v3.football.api-sports.io/"
    headers = {
        'x-rapidapi-key': api_key,
        'x-rapidapi-host': 'v3.football.api-sports.io'
    }

    season = get_current_season().split('-')[0]
    url = f"{base_url}fixtures?team={team_id}&last={last_n*2}&season={season}"
    response = requests.get(url, headers=headers)
    fixtures = response.json().get('response', [])

    matches = []
    for f in fixtures:
        is_home = f['teams']['home']['id'] == team_id
        if venue == 'home' and not is_home:
            continue
        if venue == 'away' and is_home:
            continue

        if is_home:
            gf = f['goals']['home']
            ga = f['goals']['away']
        else:
            gf = f['goals']['away']
            ga = f['goals']['home']

        if gf is not None and ga is not None:
            matches.append((gf, ga))

        if len(matches) >= last_n:
            break

    return matches

# ============================================================
# ALTERNATIVE: FOOTBALL-DATA.ORG (FREE, NO KEY NEEDED)
# ============================================================

def fetch_from_football_data(competition_code):
    """
    Fetch from football-data.org (free tier available)
    Competition codes: PL, BL1, DED, BSA, PD, FL1, ELC, PPL, CLI, etc.
    """
    url = f"https://api.football-data.org/v4/competitions/{competition_code}/matches"
    headers = {'X-Auth-Token': 'YOUR_TOKEN_HERE'}  # Get free token at football-data.org

    today = datetime.now().strftime('%Y-%m-%d')
    params = {'dateFrom': today, 'dateTo': today}

    try:
        response = requests.get(url, headers=headers, params=params)
        data = response.json()

        matches = []
        for match in data.get('matches', []):
            matches.append({
                'league': competition_code,
                'home': match['homeTeam']['shortName'],
                'away': match['awayTeam']['shortName'],
                'date': today,
                'home_id': match['homeTeam']['id'],
                'away_id': match['awayTeam']['id']
            })
        return matches
    except:
        return []

# ============================================================
# ALTERNATIVE: USE PRE-COLLECTED DATA FILES
# ============================================================

def load_manual_data(filepath='team_data.json'):
    """Load team data from manually maintained JSON file"""
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_manual_data(data, filepath='team_data.json'):
    """Save team data to JSON for reuse"""
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

# ============================================================
# CORE ALGORITHM
# ============================================================

def apply_algorithm(home_data, away_data):
    """
    Apply the Over 2.5 goals prediction algorithm
    home_data/away_data: list of (goals_for, goals_against) tuples
    Returns: (passed_list, failed_list, details_dict)
    """
    passed = []
    failed = []
    details = {}

    if len(home_data) < 3 or len(away_data) < 3:
        return None, None, {"error": "Insufficient data (need 3 matches minimum)"}

    # H1: Home team 7+ goals in last 3 home matches
    home_goals_total = sum(gf + ga for gf, ga in home_data)
    if home_goals_total >= 7:
        passed.append("H1")
        details['H1'] = f"PASS ({home_goals_total} goals)"
    else:
        failed.append("H1")
        details['H1'] = f"FAIL ({home_goals_total}, need 7+)"

    # H2: 2 or 3 of last 3 home matches Over 2.5
    home_over25 = sum(1 for gf, ga in home_data if gf + ga > 2.5)
    if home_over25 >= 2:
        passed.append("H2")
        details['H2'] = f"PASS ({home_over25}/3)"
    else:
        failed.append("H2")
        details['H2'] = f"FAIL ({home_over25}/3, need 2+)"

    # A1: Away team 7+ goals in last 3 away matches
    away_goals_total = sum(gf + ga for gf, ga in away_data)
    if away_goals_total >= 7:
        passed.append("A1")
        details['A1'] = f"PASS ({away_goals_total} goals)"
    else:
        failed.append("A1")
        details['A1'] = f"FAIL ({away_goals_total}, need 7+)"

    # A2: Previous away match 2+ goals total
    prev_away_total = away_data[0][0] + away_data[0][1]
    if prev_away_total >= 2:
        passed.append("A2")
        details['A2'] = f"PASS ({prev_away_total} goals)"
    else:
        failed.append("A2")
        details['A2'] = f"FAIL ({prev_away_total}, need 2+)"

    # A3: Away team scored in 2 or 3 of last 3 away
    away_scored = sum(1 for gf, _ in away_data if gf > 0)
    if away_scored >= 2:
        passed.append("A3")
        details['A3'] = f"PASS (scored in {away_scored}/3)"
    else:
        failed.append("A3")
        details['A3'] = f"FAIL (scored in {away_scored}/3, need 2+)"

    # A4: 2 or 3 of last 3 away matches Over 2.5
    away_over25 = sum(1 for gf, ga in away_data if gf + ga > 2.5)
    if away_over25 >= 2:
        passed.append("A4")
        details['A4'] = f"PASS ({away_over25}/3)"
    else:
        failed.append("A4")
        details['A4'] = f"FAIL ({away_over25}/3, need 2+)"

    return passed, failed, details

# ============================================================
# MAIN EXECUTION
# ============================================================

def main():
    print("=" * 70)
    print("OVER 2.5 GOALS PREDICTION SYSTEM")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 70)

    # Step 1: Fetch today's matches
    print("\n[+] Fetching today's fixtures...")
    all_matches = []

    for league, url in LEAGUE_SOURCES.items():
        matches = fetch_fixtures_worldfootball(league, url)
        all_matches.extend(matches)
        if matches:
            print(f"   [OK] {league}: {len(matches)} matches")
        time.sleep(0.5)  # Be nice to servers

    print(f"\n[*] Total matches found: {len(all_matches)}")

    # Step 2: Fetch historical data and apply algorithm
    print("\n[+] Analyzing team form...")
    qualified = []
    close_calls = []

    for match in all_matches:
        home = match['home']
        away = match['away']

        print(f"\n   Checking {home} vs {away}...", end=" ")

        # Fetch recent form
        home_form = fetch_team_recent_matches(home, is_home=True, num_matches=3)
        away_form = fetch_team_recent_matches(away, is_home=False, num_matches=3)

        if not home_form or not away_form:
            print("[WARN] Insufficient data")
            continue

        # Apply algorithm
        passed, failed, details = apply_algorithm(home_form, away_form)

        if passed is None:
            print("[ERROR] Error")
            continue

        result = {
            'match': match,
            'passed': passed,
            'failed': failed,
            'details': details,
            'home_form': home_form,
            'away_form': away_form,
            'score': len(passed)
        }

        if len(passed) == 6:
            qualified.append(result)
            print("[OK] QUALIFIED")
        elif len(passed) == 5:
            close_calls.append(result)
            print("[WARN] Close (5/6)")
        else:
            print(f"[X] ({len(passed)}/6)")

    # Step 3: Output results
    print("\n" + "=" * 70)
    print("[+] QUALIFIED MATCHES")
    print("=" * 70)

    if qualified:
        for i, q in enumerate(qualified, 1):
            m = q['match']
            print(f"\n{i}. {m['league']}: {m['home']} vs {m['away']}")
            print(f"   Time: {m['time']}")
            print(f"   Home form: {q['home_form']} | Away form: {q['away_form']}")
            for check, status in q['details'].items():
                print(f"   {check}: {status}")
    else:
        print("\n[X] No matches fully qualified today.")

    print("\n" + "=" * 70)
    print("[WARN] CLOSE CALLS (5/6)")
    print("=" * 70)

    if close_calls:
        for c in close_calls:
            m = c['match']
            print(f"\n- {m['league']}: {m['home']} vs {m['away']}")
            print(f"  Failed: {', '.join(c['failed'])}")
    else:
        print("\nNone")

    # Save results
    output = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'qualified': qualified,
        'close_calls': close_calls,
        'total_matches': len(all_matches)
    }

    filename = f"predictions_{datetime.now().strftime('%Y-%m-%d')}.json"
    with open(filename, 'w') as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n[*] Results saved to: {filename}")
    print("=" * 70)

if __name__ == "__main__":
    main()
