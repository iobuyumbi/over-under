#!/usr/bin/env python3
"""
Fetch results for all predicted matches and update manual_results.csv
"""

import requests
import csv
import os
import time
import json
from datetime import datetime

API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "7fefa847c763ebbbbda8d5ccc41a73e4")
API_FOOTBALL_HOST = "v3.football.api-sports.io"

# Football-Data.org (free, no rate limit)
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")

def load_prediction_history():
    """Load prediction history from prediction_history.json"""
    history_path = "prediction_history.json"
    if os.path.exists(history_path):
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading prediction_history.json: {e}")
            return {"home_win": [], "over_under": []}
    return {"home_win": [], "over_under": []}

def get_unique_matches(history):
    """Extract unique matches from prediction history"""
    matches = set()
    # Add home win matches
    for pick in history.get("home_win", []):
        date = pick.get("date")
        home = pick.get("home_team", pick.get("home"))
        away = pick.get("away_team", pick.get("away"))
        if date and home and away:
            matches.add((date, home, away))
    # Add over/under matches
    for pick in history.get("over_under", []):
        date = pick.get("date")
        home = pick.get("home_team", pick.get("home"))
        away = pick.get("away_team", pick.get("away"))
        if date and home and away:
            matches.add((date, home, away))
    return sorted(list(matches), key=lambda x: x[0])

def load_manual_results():
    """Load existing manual results from CSV"""
    csv_path = "manual_results.csv"
    existing = {}
    if os.path.exists(csv_path):
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = (row["date"], row["home_team"], row["away_team"])
                    existing[key] = row.get("score", "")
        except Exception as e:
            print(f"Error loading manual_results.csv: {e}")
    return existing

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
    
    # Load prediction history
    history = load_prediction_history()
    matches = get_unique_matches(history)
    # Load existing manual results
    existing_scores = load_manual_results()
    
    results = []
    updated_count = 0
    
    for date, home, away in matches:
        print(f"Processing: {date} {home} vs {away}")
        key = (date, home, away)
        # Check if we already have a score
        existing_score = existing_scores.get(key, "")
        if existing_score:
            print(f"  [OK] Already has score: {existing_score}")
            results.append((date, home, away, existing_score))
            continue
        
        # Try to fetch score
        score = fetch_match_result(date, home, away)
        if score:
            print(f"  [OK] New result: {score}")
            updated_count += 1
        else:
            print(f"  [FAIL] No result found")
        results.append((date, home, away, score or ""))
    
    # Write to CSV
    with open("manual_results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "home_team", "away_team", "score"])
        writer.writerows(results)
    
    print(f"\nUpdated manual_results.csv with {len(results)} matches")
    print(f"Added/updated scores for {updated_count} matches")
    print(f"Total matches with scores: {sum(1 for _, _, _, s in results if s)}")

if __name__ == "__main__":
    main()
