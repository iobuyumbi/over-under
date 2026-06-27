#!/usr/bin/env python3
"""
OVER 2.5 GOALS PREDICTION SYSTEM - FINAL VERSION
=================================================
Windows-compatible, no emojis, with API-Football key ready.
"""

import requests
import json
import os
import time
from datetime import datetime, timedelta
from urllib.parse import quote

# ============================================================
# CONFIGURATION
# ============================================================

# Better headers to avoid 403 blocks
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Cache-Control': 'max-age=0',
}

# API KEYS
FOOTBALL_DATA_KEY = "a17ca455c2eb4ac79408f48dd8cca2bb"
API_FOOTBALL_KEY = "7fefa847c763ebbbbda8d5ccc41a73e4"

# ============================================================
# SOURCE 1: OpenFootball JSON (GitHub) - FREE, NO BLOCKING
# ============================================================

OPENFOOTBALL_LEAGUES = {
    "Premier League": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/en.1.json",
    "Championship": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/en.2.json",
    "Bundesliga": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/de.1.json",
    "2. Bundesliga": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/de.2.json",
    "La Liga": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/es.1.json",
    "Segunda Division": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/es.2.json",
    "Serie A": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/it.1.json",
    "Serie B": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/it.2.json",
    "Ligue 1": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/fr.1.json",
    "Ligue 2": "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/fr.2.json",
}

def fetch_openfootball_data(league_name, url):
    """Fetch fixtures and results from OpenFootball JSON"""
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        data = response.json()

        today = datetime.now().strftime('%Y-%m-%d')
        matches = []

        for match in data.get('matches', []):
            match_date = match.get('date', '')
            if match_date == today:
                team1 = match.get('team1', '')
                team2 = match.get('team2', '')
                score = match.get('score', {}).get('ft', [None, None])

                matches.append({
                    'league': league_name,
                    'home': team1,
                    'away': team2,
                    'date': match_date,
                    'score': score,
                    'status': 'Scheduled'
                })

        return matches
    except Exception as e:
        print(f"   [OpenFootball] {league_name}: {e}")
        return []

# ============================================================
# SOURCE 2: API-Football (api-sports.io) - 100 FREE/DAY
# ============================================================

API_FOOTBALL_LEAGUES = {
    "Premier League": 39,
    "La Liga": 140,
    "Serie A": 135,
    "Bundesliga": 78,
    "Ligue 1": 61,
    "Championship": 40,
    "Eredivisie": 88,
    "Primeira Liga": 94,
    "Belgian Pro League": 144,
    "Scottish Premiership": 179,
    "Eliteserien": 103,
    "Allsvenskan": 113,
    "Danish Superliga": 119,
    "Veikkausliiga": 244,
    "MLS": 253,
    "Brasileirao": 71,
    "Argentine Primera": 128,
    "Chile Primera": 265,
    "Liga MX": 262,
    "J1 League": 98,
    "K League 1": 292,
    "A-League": 188,
}

def fetch_api_football_fixtures(league_id, season=None):
    """Fetch fixtures from API-Football"""
    if not API_FOOTBALL_KEY:
        return []

    if season is None:
        season = datetime.now().year

    today = datetime.now().strftime('%Y-%m-%d')
    url = f"https://v3.football.api-sports.io/fixtures?date={today}&league={league_id}&season={season}"
    headers = {'x-apisports-key': API_FOOTBALL_KEY}

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        matches = []
        for fixture in data.get('response', []):
            matches.append({
                'league': str(league_id),
                'home': fixture['teams']['home']['name'],
                'away': fixture['teams']['away']['name'],
                'date': today,
                'fixture_id': fixture['fixture']['id'],
                'status': 'Scheduled'
            })
        return matches
    except Exception as e:
        print(f"   [API-Football] League {league_id}: {e}")
        return []

def fetch_api_football_team_form(team_id, last_n=3, venue='home'):
    """Get last N matches for a team from API-Football"""
    if not API_FOOTBALL_KEY:
        return []

    season = datetime.now().year
    url = f"https://v3.football.api-sports.io/fixtures?team={team_id}&last={last_n*2}&season={season}"
    headers = {'x-apisports-key': API_FOOTBALL_KEY}

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        matches = []
        for fixture in data.get('response', []):
            is_home = fixture['teams']['home']['id'] == team_id
            if venue == 'home' and not is_home:
                continue
            if venue == 'away' and is_home:
                continue

            if is_home:
                gf = fixture['goals']['home']
                ga = fixture['goals']['away']
            else:
                gf = fixture['goals']['away']
                ga = fixture['goals']['home']

            if gf is not None and ga is not None:
                matches.append((gf, ga))

            if len(matches) >= last_n:
                break

        return matches
    except Exception as e:
        print(f"   [API-Football] Team {team_id}: {e}")
        return []

# ============================================================
# SOURCE 3: MANUAL DATA (team_data.json)
# ============================================================

def load_manual_data(filepath='team_data.json'):
    """Load manually entered team data"""
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

# ============================================================
# FORM ANALYSIS FROM OPENFOOTBALL HISTORICAL DATA
# ============================================================

def build_form_database():
    """Build a database of recent team form from OpenFootball JSON"""
    form_db = {}

    for league_name, url in OPENFOOTBALL_LEAGUES.items():
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            if response.status_code != 200:
                continue

            data = response.json()
            today = datetime.now()

            for match in data.get('matches', []):
                match_date_str = match.get('date', '')
                if not match_date_str:
                    continue

                try:
                    match_date = datetime.strptime(match_date_str, '%Y-%m-%d')
                except:
                    continue

                if (today - match_date).days > 60:
                    continue

                team1 = match.get('team1', '')
                team2 = match.get('team2', '')
                score = match.get('score', {}).get('ft', [None, None])

                if None in score:
                    continue

                if team1 not in form_db:
                    form_db[team1] = {'home': [], 'away': []}
                form_db[team1]['home'].append((score[0], score[1]))

                if team2 not in form_db:
                    form_db[team2] = {'home': [], 'away': []}
                form_db[team2]['away'].append((score[1], score[0]))

        except Exception as e:
            print(f"   [Form DB] {league_name}: {e}")

        time.sleep(0.3)

    return form_db

# ============================================================
# CORE ALGORITHM
# ============================================================

def apply_algorithm(home_data, away_data):
    """Apply the 6-check Over 2.5 algorithm"""

    if len(home_data) < 3 or len(away_data) < 3:
        return None, None, {"error": "Insufficient data (need 3 matches minimum)"}

    passed = []
    failed = []
    details = {}

    home_goals_total = sum(gf + ga for gf, ga in home_data)
    if home_goals_total >= 7:
        passed.append("H1")
        details['H1'] = f"PASS ({home_goals_total} goals)"
    else:
        failed.append("H1")
        details['H1'] = f"FAIL ({home_goals_total}, need 7+)"

    home_over25 = sum(1 for gf, ga in home_data if gf + ga > 2.5)
    if home_over25 >= 2:
        passed.append("H2")
        details['H2'] = f"PASS ({home_over25}/3 Over 2.5)"
    else:
        failed.append("H2")
        details['H2'] = f"FAIL ({home_over25}/3, need 2+)"

    away_goals_total = sum(gf + ga for gf, ga in away_data)
    if away_goals_total >= 7:
        passed.append("A1")
        details['A1'] = f"PASS ({away_goals_total} goals)"
    else:
        failed.append("A1")
        details['A1'] = f"FAIL ({away_goals_total}, need 7+)"

    prev_away_total = away_data[0][0] + away_data[0][1]
    if prev_away_total >= 2:
        passed.append("A2")
        details['A2'] = f"PASS ({prev_away_total} goals in prev away)"
    else:
        failed.append("A2")
        details['A2'] = f"FAIL ({prev_away_total}, need 2+)"

    away_scored = sum(1 for gf, _ in away_data if gf > 0)
    if away_scored >= 2:
        passed.append("A3")
        details['A3'] = f"PASS (scored in {away_scored}/3 away)"
    else:
        failed.append("A3")
        details['A3'] = f"FAIL (scored in {away_scored}/3, need 2+)"

    away_over25 = sum(1 for gf, ga in away_data if gf + ga > 2.5)
    if away_over25 >= 2:
        passed.append("A4")
        details['A4'] = f"PASS ({away_over25}/3 Over 2.5)"
    else:
        failed.append("A4")
        details['A4'] = f"FAIL ({away_over25}/3, need 2+)"

    return passed, failed, details

# ============================================================
# MAIN EXECUTION
# ============================================================

def main():
    print("=" * 70)
    print("OVER 2.5 GOALS PREDICTION SYSTEM - FINAL VERSION")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 70)

    print("\n[+] Building form database from OpenFootball...")
    form_db = build_form_database()
    print(f"   [OK] Loaded data for {len(form_db)} teams")

    print("\n[+] Fetching today's fixtures...")
    all_matches = []

    for league, url in OPENFOOTBALL_LEAGUES.items():
        matches = fetch_openfootball_data(league, url)
        if matches:
            all_matches.extend(matches)
            print(f"   [OK] OpenFootball: {league} - {len(matches)} matches")
        time.sleep(0.5)

    if API_FOOTBALL_KEY:
        for league, league_id in API_FOOTBALL_LEAGUES.items():
            matches = fetch_api_football_fixtures(league_id)
            if matches:
                all_matches.extend(matches)
                print(f"   [OK] API-Football: {league} - {len(matches)} matches")

    manual_data = load_manual_data()
    print(f"\n[*] Total matches found: {len(all_matches)}")

    print("\n[+] Analyzing matches...")
    qualified = []
    close_calls = []

    for match in all_matches:
        home = match['home']
        away = match['away']
        league = match['league']

        home_form = []
        away_form = []

        if home in form_db and len(form_db[home]['home']) >= 3:
            home_form = form_db[home]['home'][:3]
        if away in form_db and len(form_db[away]['away']) >= 3:
            away_form = form_db[away]['away'][:3]

        if not home_form and home in manual_data:
            home_form = [tuple(m) for m in manual_data[home].get('home', [])[:3]]
        if not away_form and away in manual_data:
            away_form = [tuple(m) for m in manual_data[away].get('away', [])[:3]]

        if not home_form or not away_form:
            print(f"\n   [WARN] {home} vs {away} - Insufficient form data")
            continue

        passed, failed, details = apply_algorithm(home_form, away_form)

        if passed is None:
            continue

        score = len(passed)
        status = "[+] QUALIFIED" if score == 6 else ("[WARN] CLOSE CALL" if score == 5 else "[X]")

        print(f"\n{'─' * 70}")
        print(f"[*] {league} | {home} vs {away}")
        print(f"[*] Score: {score}/6 | {status}")
        for check, result in details.items():
            icon = "[OK]" if "PASS" in result else "[X]"
            print(f"   {icon} {check}: {result}")

        if score == 6:
            qualified.append({'match': match, 'details': details, 'home_form': home_form, 'away_form': away_form})
        elif score == 5:
            close_calls.append({'match': match, 'failed': failed, 'details': details})

    print("\n" + "=" * 70)
    print("[+] QUALIFIED MATCHES")
    print("=" * 70)
    if qualified:
        for i, q in enumerate(qualified, 1):
            m = q['match']
            print(f"\n{i}. {m['league']}: {m['home']} vs {m['away']}")
            print(f"   Home form: {q['home_form']} | Away form: {q['away_form']}")
    else:
        print("\n[X] No matches fully qualified.")

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

    output = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'qualified': qualified,
        'close_calls': close_calls,
        'total': len(all_matches)
    }

    filename = f"predictions_{datetime.now().strftime('%Y-%m-%d')}.json"
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n[*] Results saved: {filename}")
    print("=" * 70)

    if not all_matches:
        print("\n[WARN] No matches found via automated sources.")
        print("   Options:")
        print("   1. Your API-Football key is already added!")
        print("   2. Use manual data entry: python team_data_manager.py")
        print("   3. Check if major leagues are in offseason")

if __name__ == "__main__":
    main()
