#!/usr/bin/env python3
"""
Fetch results for all predicted matches and update manual_results.csv
"""

import requests
import csv
import os
import time
from datetime import datetime

API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "7fefa847c763ebbbbda8d5ccc41a73e4")
API_FOOTBALL_HOST = "v3.football.api-sports.io"

# Football-Data.org (free, no rate limit)
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")

# All unique matches from prediction_history.json
MATCHES = [
    ("2026-06-13", "Coquimbo Unido", "O'Higgins"),
    ("2026-06-13", "Colo-Colo", "Cobresal"),
    ("2026-06-13", "Quilmes", "Gimnasia y Tiro"),
    ("2026-06-13", "Ferrocarril Midland", "Atlanta"),
    ("2026-06-13", "Nueva Chicago", "Chacarita"),
    ("2026-06-13", "Almagro", "Agropecuario"),
    ("2026-06-13", "Audax Italiano", "La Serena"),
    ("2026-06-13", "USA", "Paraguay"),
    ("2026-06-14", "Gimnasia de Jujuy", "San Martin de San Juan"),
    ("2026-06-14", "Temperley", "Guemes"),
    ("2026-06-14", "Almirante Brown", "Godoy Cruz"),
    ("2026-06-14", "Ferro Carril Oeste", "Acassuso"),
    ("2026-06-14", "Central Norte", "San Telmo"),
    ("2026-06-14", "Patronato", "Atletico Rafaela"),
    ("2026-06-15", "Ivory Coast", "Ecuador"),
    ("2026-06-15", "Sundsvall", "Osters"),
]

def find_team_id(team_name):
    """Find team ID from API-Football by searching."""
    try:
        url = f"https://{API_FOOTBALL_HOST}/teams"
        headers = {
            "x-rapidapi-key": API_FOOTBALL_KEY,
            "x-rapidapi-host": API_FOOTBALL_HOST,
        }
        params = {"search": team_name}
        
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        for team in data.get("response", []):
            if team_name.lower() in team["name"].lower() or team["name"].lower() in team_name.lower():
                print(f"  Found team ID: {team['id']} for {team_name} -> {team['name']}")
                return team["id"]
        return None
    except Exception as e:
        print(f"Error finding team ID for {team_name}: {e}")
        return None

def fetch_match_result_football_data(date, home_team, away_team):
    """Fetch result using Football-Data.org API (free, no rate limit)."""
    if not FOOTBALL_DATA_KEY:
        return None
    
    try:
        # Football-Data.org covers major leagues, try common league codes
        leagues = ["CL", "PL", "BL1", "SA", "LL", "FL1", "PD", "ELC", "DED", "PPL"]
        
        for league in leagues:
            url = f"https://api.football-data.org/v4/matches"
            headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
            params = {"dateFrom": date, "dateTo": date, "competitions": league}
            
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 403:
                continue
            resp.raise_for_status()
            data = resp.json()
            
            for match in data.get("matches", []):
                api_home = match["homeTeam"]["name"]
                api_away = match["awayTeam"]["name"]
                
                home_match = (home_team.lower() in api_home.lower() or api_home.lower() in home_team.lower())
                away_match = (away_team.lower() in api_away.lower() or api_away.lower() in away_team.lower())
                
                if home_match and away_match and match["status"] == "FINISHED":
                    score = match["score"]
                    return f"{score['fullTime']['home']}-{score['fullTime']['away']}"
    except Exception as e:
        print(f"  Football-Data.org error: {e}")
    
    return None

def fetch_match_result(date, home_team, away_team):
    """Fetch result for a specific match using multiple APIs."""
    
    # Try Football-Data.org first (no rate limit)
    result = fetch_match_result_football_data(date, home_team, away_team)
    if result:
        return result
    
    # Fall back to API-Football
    if not API_FOOTBALL_KEY:
        print(f"No API key, skipping {home_team} vs {away_team}")
        return None
    
    try:
        # First try: Search by date and fuzzy match team names
        url = f"https://{API_FOOTBALL_HOST}/fixtures"
        headers = {
            "x-rapidapi-key": API_FOOTBALL_KEY,
            "x-rapidapi-host": API_FOOTBALL_HOST,
        }
        params = {"date": date, "status": "FT"}
        
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        for fixture in data.get("response", []):
            api_home = fixture["teams"]["home"]["name"]
            api_away = fixture["teams"]["away"]["name"]
            
            # More flexible matching
            home_match = (home_team.lower() in api_home.lower() or 
                         api_home.lower() in home_team.lower() or
                         home_team.replace(" ", "").lower() in api_home.replace(" ", "").lower())
            away_match = (away_team.lower() in api_away.lower() or 
                         api_away.lower() in away_team.lower() or
                         away_team.replace(" ", "").lower() in api_away.replace(" ", "").lower())
            
            if home_match and away_match:
                goals_home = fixture["goals"]["home"]
                goals_away = fixture["goals"]["away"]
                if goals_home is not None and goals_away is not None:
                    print(f"  Matched: {api_home} vs {api_away}")
                    return f"{goals_home}-{goals_away}"
        
        # Second try: Get team IDs and search H2H
        home_id = find_team_id(home_team)
        away_id = find_team_id(away_team)
        
        if home_id and away_id:
            url = f"https://{API_FOOTBALL_HOST}/fixtures/headtohead"
            params = {"h2h": f"{home_id}-{away_id}", "to": date.replace("-", "-")}
            
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            for fixture in data.get("response", []):
                fixture_date = fixture["fixture"]["date"][:10]
                if fixture_date == date:
                    goals_home = fixture["goals"]["home"]
                    goals_away = fixture["goals"]["away"]
                    if goals_home is not None and goals_away is not None:
                        print(f"  Found via H2H: {fixture['teams']['home']['name']} vs {fixture['teams']['away']['name']}")
                        return f"{goals_home}-{goals_away}"
        
        print(f"No match found for {home_team} vs {away_team} on {date}")
        return None
    except Exception as e:
        print(f"Error fetching {home_team} vs {away_team}: {e}")
        return None

def main():
    print("Fetching results for all predicted matches...")
    
    results = []
    for date, home, away in MATCHES:
        print(f"Fetching: {date} {home} vs {away}")
        score = fetch_match_result(date, home, away)
        if score:
            print(f"  [OK] Result: {score}")
        else:
            print(f"  [FAIL] No result found")
        results.append((date, home, away, score or ""))
    
    # Write to CSV
    with open("manual_results.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "home_team", "away_team", "score"])
        writer.writerows(results)
    
    print(f"\nUpdated manual_results.csv with {len(results)} matches")
    print(f"Found scores for {sum(1 for _, _, _, s in results if s)} matches")

if __name__ == "__main__":
    main()
