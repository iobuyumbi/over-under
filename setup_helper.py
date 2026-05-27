#!/usr/bin/env python3
"""
SETUP HELPER - Run this first!
===============================
Helps you configure data sources and test connectivity.
"""

import requests
import json
import os

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

def test_openfootball():
    """Test OpenFootball JSON (should always work)"""
    print("\n[1] Testing OpenFootball JSON (GitHub)...")
    url = "https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/en.1.json"
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code == 200:
            data = response.json()
            matches = len(data.get('matches', []))
            print(f"   ✅ WORKING - Premier League 2025-26 has {matches} matches loaded")
            return True
        else:
            print(f"   ❌ Failed with status {response.status_code}")
            return False
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False

def test_football_data():
    """Test football-data.org"""
    print("\n[2] Testing football-data.org...")
    url = "https://api.football-data.org/v4/competitions/PL/matches"
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            print("   ✅ WORKING without API key")
            return True
        elif response.status_code == 403:
            print("   ⚠️  Requires API key for this endpoint")
            print("   → Get free key at: https://www.football-data.org/")
            return False
        else:
            print(f"   ❌ Status: {response.status_code}")
            return False
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False

def test_api_football():
    """Test API-Football"""
    print("\n[3] Testing API-Football...")

    # Check if key exists
    key_file = 'api_key.txt'
    if os.path.exists(key_file):
        with open(key_file, 'r') as f:
            api_key = f.read().strip()
    else:
        api_key = input("   Enter API-Football key (or press Enter to skip): ").strip()
        if api_key:
            with open(key_file, 'w') as f:
                f.write(api_key)

    if not api_key:
        print("   ⚠️  No key provided. Skipping.")
        print("   → Get free key at: https://dashboard.api-football.com/register")
        return False

    url = "https://v3.football.api-sports.io/status"
    headers = {'x-apisports-key': api_key}

    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data.get('response', {}).get('account_status') == 'active':
                print("   ✅ WORKING - Account active")
                remaining = data.get('response', {}).get('requests', {}).get('current_day', {}).get('remaining', 'N/A')
                print(f"   📊 Requests remaining today: {remaining}")
                return True
            else:
                print("   ❌ Account not active")
                return False
        else:
            print(f"   ❌ Status: {response.status_code}")
            return False
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False

def create_sample_team_data():
    """Create sample team_data.json"""
    print("\n[4] Creating sample team_data.json...")

    sample = {
        "_instructions": "Add teams here when automated sources fail",
        "_format": "home/away: list of [goals_for, goals_against]",
        "Example Team": {
            "home": [[3, 1], [2, 1], [4, 0]],
            "away": [[1, 2], [3, 2], [0, 1]]
        }
    }

    if not os.path.exists('team_data.json'):
        with open('team_data.json', 'w') as f:
            json.dump(sample, f, indent=2)
        print("   ✅ Created team_data.json (template)")
    else:
        print("   ℹ️  team_data.json already exists")

def main():
    print("=" * 60)
    print("OVER 2.5 PREDICTOR - SETUP HELPER")
    print("=" * 60)

    results = {
        'openfootball': test_openfootball(),
        'football_data': test_football_data(),
        'api_football': test_api_football(),
    }

    create_sample_team_data()

    print("\n" + "=" * 60)
    print("SETUP SUMMARY")
    print("=" * 60)

    working_sources = [k for k, v in results.items() if v]

    if working_sources:
        print(f"\n✅ Working sources: {', '.join(working_sources)}")
        print("\nYou can now run: python3 over25_predictor_v2.py")
    else:
        print("\n⚠️  No automated sources working.")
        print("   You can still use manual data entry:")
        print("   → Edit team_data.json with your teams")
        print("   → Run: python3 team_data_manager.py")

    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()