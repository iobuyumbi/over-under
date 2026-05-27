#!/usr/bin/env python3
"""
OVER 2.5 GOALS PREDICTION SYSTEM - WORKING VERSION
===================================================
Uses API-Football and Football-Data.org with your keys!
"""

import requests
import json
import os
import time
from datetime import datetime

# ============================================================
# YOUR API KEYS
# ============================================================
FOOTBALL_DATA_KEY = "a17ca455c2eb4ac79408f48dd8cca2bb"
API_FOOTBALL_KEY = "168c8e43e9ff8e09752249976dc7115d"

# ============================================================
# LEAGUE CONFIGURATION
# ============================================================
LEAGUES_API_FOOTBALL = [
    {"name": "Eliteserien", "id": 103, "season": 2026},
    {"name": "Allsvenskan", "id": 113, "season": 2026},
    {"name": "MLS", "id": 253, "season": 2026},
    {"name": "Brasileirao", "id": 71, "season": 2026},
    {"name": "Premier League", "id": 39, "season": 2025},
    {"name": "La Liga", "id": 140, "season": 2025},
    {"name": "Serie A", "id": 135, "season": 2025},
    {"name": "Bundesliga", "id": 78, "season": 2025},
    {"name": "Ligue 1", "id": 61, "season": 2025},
]

LEAGUES_FOOTBALL_DATA = [
    {"name": "Premier League", "code": "PL"},
    {"name": "La Liga", "code": "PD"},
    {"name": "Serie A", "code": "SA"},
    {"name": "Bundesliga", "code": "BL1"},
    {"name": "Ligue 1", "code": "FL1"},
]

# ============================================================
# API-FOOTBALL FUNCTIONS
# ============================================================
def fetch_fixtures_api_football(league_id, season):
    """Fetch fixtures from API-Football"""
    today = datetime.now().strftime('%Y-%m-%d')
    url = f"https://v3.football.api-sports.io/fixtures?date={today}&league={league_id}&season={season}"
    headers = {'x-apisports-key': API_FOOTBALL_KEY}
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            return []
        data = response.json()
        return data.get('response', [])
    except Exception as e:
        print(f"   [ERROR] API-Football: {e}")
        return []

def fetch_team_form_api_football(team_id, venue, last_n=3):
    """Get team form from API-Football"""
    season = 2025 if venue == 'home' else 2025
    url = f"https://v3.football.api-sports.io/fixtures?team={team_id}&last=10&season={season}"
    headers = {'x-apisports-key': API_FOOTBALL_KEY}
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            return []
        data = response.json()
        fixtures = data.get('response', [])
        
        matches = []
        for fixture in fixtures:
            if fixture['fixture']['status']['short'] not in ['FT', 'AET', 'PEN']:
                continue
                
            is_home = fixture['teams']['home']['id'] == team_id
            if (venue == 'home' and not is_home) or (venue == 'away' and is_home):
                continue
                
            gf = fixture['goals']['home'] if is_home else fixture['goals']['away']
            ga = fixture['goals']['away'] if is_home else fixture['goals']['home']
            
            if gf is not None and ga is not None:
                matches.append((gf, ga))
                
            if len(matches) >= last_n:
                break
                
        return matches
    except Exception as e:
        print(f"   [ERROR] Form: {e}")
        return []

# ============================================================
# FOOTBALL-DATA.ORG FUNCTIONS
# ============================================================
def fetch_fixtures_football_data(competition_code):
    """Fetch fixtures from Football-Data.org"""
    today = datetime.now().strftime('%Y-%m-%d')
    url = f"https://api.football-data.org/v4/competitions/{competition_code}/matches"
    headers = {'X-Auth-Token': FOOTBALL_DATA_KEY}
    params = {'dateFrom': today, 'dateTo': today}
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.status_code != 200:
            return []
        data = response.json()
        return data.get('matches', [])
    except Exception as e:
        print(f"   [ERROR] Football-Data: {e}")
        return []

# ============================================================
# MANUAL DATA FUNCTIONS
# ============================================================
def load_manual_data():
    """Load manual team data from team_data.json"""
    if os.path.exists('team_data.json'):
        with open('team_data.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

# ============================================================
# CORE ALGORITHM
# ============================================================
def apply_algorithm(home_data, away_data):
    """Apply the 6-check algorithm"""
    if len(home_data) < 3 or len(away_data) < 3:
        return None, None, {"error": "Insufficient data"}
    
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
    print("OVER 2.5 GOALS PREDICTION SYSTEM - WORKING")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 70)
    
    all_matches = []
    
    # Try API-Football first
    print("\n[+] Trying API-Football...")
    for league in LEAGUES_API_FOOTBALL:
        print(f"   {league['name']}...")
        fixtures = fetch_fixtures_api_football(league['id'], league['season'])
        if fixtures:
            for f in fixtures:
                all_matches.append({
                    'league': league['name'],
                    'home': f['teams']['home']['name'],
                    'away': f['teams']['away']['name'],
                    'home_id': f['teams']['home']['id'],
                    'away_id': f['teams']['away']['id'],
                    'source': 'api-football'
                })
            print(f"   [OK] {len(fixtures)} matches")
        time.sleep(0.6)
    
    # Try Football-Data.org as backup
    if not all_matches:
        print("\n[+] Trying Football-Data.org...")
        for league in LEAGUES_FOOTBALL_DATA:
            print(f"   {league['name']}...")
            fixtures = fetch_fixtures_football_data(league['code'])
            if fixtures:
                for f in fixtures:
                    all_matches.append({
                        'league': league['name'],
                        'home': f['homeTeam']['name'],
                        'away': f['awayTeam']['name'],
                        'home_id': None,
                        'away_id': None,
                        'source': 'football-data'
                    })
                print(f"   [OK] {len(fixtures)} matches")
    
    print(f"\n[*] Total matches found: {len(all_matches)}")
    
    # Load manual data
    manual_data = load_manual_data()
    
    # If no matches, use test mode with sample data
    if not all_matches:
        print("\n[!] No matches found via APIs (offseason?)")
        print("\n[+] Using sample data from team_data.json...")
        
        test_matches = [
            {'league': 'Test League', 'home': 'Manchester City', 'away': 'Liverpool', 'source': 'manual'},
            {'league': 'Test League', 'home': 'Bayern Munich', 'away': 'Borussia Dortmund', 'source': 'manual'},
            {'league': 'Test League', 'home': 'Real Madrid', 'away': 'Barcelona', 'source': 'manual'},
        ]
        all_matches = test_matches
    
    # Analyze matches
    print("\n[+] Analyzing matches...")
    qualified = []
    close_calls = []
    
    for match in all_matches:
        home = match['home']
        away = match['away']
        league = match['league']
        
        print(f"\n{'-' * 70}")
        print(f"[*] {league}: {home} vs {away}")
        
        # Get form data
        home_form = []
        away_form = []
        
        # Try API-Football first
        if match.get('home_id'):
            home_form = fetch_team_form_api_football(match['home_id'], 'home', 3)
        if match.get('away_id'):
            away_form = fetch_team_form_api_football(match['away_id'], 'away', 3)
        
        # Try manual data
        if not home_form and home in manual_data:
            home_form = [tuple(m) for m in manual_data[home].get('home', [])[:3]]
        if not away_form and away in manual_data:
            away_form = [tuple(m) for m in manual_data[away].get('away', [])[:3]]
        
        if not home_form or not away_form:
            print(f"   [WARN] Insufficient form data")
            continue
        
        # Apply algorithm
        passed, failed, details = apply_algorithm(home_form, away_form)
        
        if passed is None:
            continue
        
        score = len(passed)
        status = "[+] QUALIFIED" if score == 6 else ("[WARN] CLOSE CALL" if score == 5 else "[X]")
        
        print(f"[*] Score: {score}/6 | {status}")
        for check, result in details.items():
            icon = "[OK]" if "PASS" in result else "[X]"
            print(f"   {icon} {check}: {result}")
        
        if score == 6:
            qualified.append({'match': match, 'details': details, 'home_form': home_form, 'away_form': away_form})
        elif score == 5:
            close_calls.append({'match': match, 'failed': failed, 'details': details})
    
    # Print summary
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
    
    # Save results
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

if __name__ == "__main__":
    main()
