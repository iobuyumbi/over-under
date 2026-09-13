#!/usr/bin/env python3
"""
Test API Football Key - Quick Test

IMPORTANT: API keys must be set via environment variables or .env file.
The previously hardcoded keys below are treated as COMPROMISED —
rotate them at the provider immediately (they were committed to git).
"""

import os
import requests
from datetime import datetime, timedelta

# Load from environment (never hardcode secrets in source)
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")

print("="*70)
print("Testing API Keys...")
print("="*70)

# Test API Football
print("\n1. Testing API-Football...")
try:
    url = "https://v3.football.api-sports.io/status"
    headers = {'x-apisports-key': API_FOOTBALL_KEY}
    response = requests.get(url, headers=headers, timeout=10)
    if response.status_code == 200:
        data = response.json()
        print("   [OK] API-Football key is valid!")
        print(f"   - Requests: {data['response']['requests']['current']} / {data['response']['requests']['limit_day']} today")
    else:
        print(f"   [FAIL] API-Football failed: Status {response.status_code}")
except Exception as e:
    print(f"   [FAIL] API-Football error: {e}")

# Test Football-Data.org
print("\n2. Testing Football-Data.org...")
try:
    url = "https://api.football-data.org/v4/competitions/PL/matches"
    headers = {'X-Auth-Token': FOOTBALL_DATA_KEY}
    today = datetime.now().strftime('%Y-%m-%d')
    params = {'dateFrom': today, 'dateTo': today}
    response = requests.get(url, headers=headers, params=params, timeout=10)
    if response.status_code == 200:
        data = response.json()
        matches = data.get('matches', [])
        print("   [OK] Football-Data.org key is valid!")
        print(f"   - Found {len(matches)} Premier League matches today")
    else:
        print(f"   [FAIL] Football-Data.org failed: Status {response.status_code}")
except Exception as e:
    print(f"   [FAIL] Football-Data.org error: {e}")

# Check if we should use manual mode (offseason)
print("\n" + "="*70)
print("Checking if it's offseason...")
print("="*70)
print("\nNote: Most European leagues are in offseason from May to August.")
print("During this time, you should use manual data from team_data.json.")
print("="*70)