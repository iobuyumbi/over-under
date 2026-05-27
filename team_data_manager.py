#!/usr/bin/env python3
"""
TEAM DATA MANAGER
=================
When APIs fail or data is missing, use this to manually input
recent match results and run the algorithm.

USAGE:
------
1. Open team_data.json
2. Add team data in this format:
   {
     "Team Name": {
       "home": [[gf,ga], [gf,ga], [gf,ga]],
       "away": [[gf,ga], [gf,ga], [gf,ga]]
     }
   }
3. Run: python3 team_data_manager.py

EXAMPLE team_data.json:
-----------------------
{
  "Manchester City": {
    "home": [[3,1], [2,1], [4,0]],
    "away": [[2,0], [1,1], [3,2]]
  },
  "Liverpool": {
    "home": [[2,2], [3,1], [1,0]],
    "away": [[1,3], [2,1], [0,2]]
  }
}
"""

import json
import os
from datetime import datetime

DATA_FILE = 'team_data.json'

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def add_team():
    data = load_data()
    team = input("Team name: ").strip()

    print("\nEnter last 3 HOME matches (goals for, goals against):")
    home = []
    for i in range(3):
        gf = int(input(f"  Match {i+1} - Goals scored: "))
        ga = int(input(f"  Match {i+1} - Goals conceded: "))
        home.append([gf, ga])

    print("\nEnter last 3 AWAY matches (goals for, goals against):")
    away = []
    for i in range(3):
        gf = int(input(f"  Match {i+1} - Goals scored: "))
        ga = int(input(f"  Match {i+1} - Goals conceded: "))
        away.append([gf, ga])

    data[team] = {"home": home, "away": away}
    save_data(data)
    print(f"\n[OK] {team} data saved!")

def run_manual_analysis():
    data = load_data()
    if not data:
        print("[WARN] No data in team_data.json")
        return

    print("\nEnter today's match:")
    home = input("Home team: ").strip()
    away = input("Away team: ").strip()

    if home not in data or away not in data:
        print(f"[WARN] Missing data for {home if home not in data else away}")
        return

    # Import algorithm from main script
    from over25_predictor import apply_algorithm

    home_form = [tuple(m) for m in data[home]["home"]]
    away_form = [tuple(m) for m in data[away]["away"]]

    passed, failed, details = apply_algorithm(home_form, away_form)

    print(f"\n{'='*50}")
    print(f"RESULTS: {home} vs {away}")
    print(f"{'='*50}")
    for check, status in details.items():
        icon = "[OK]" if "PASS" in status else "[X]"
        print(f"{icon} {check}: {status}")
    print(f"\nScore: {len(passed)}/6")

    if len(passed) == 6:
        print("[+] FULLY QUALIFIED!")
    elif len(passed) == 5:
        print("[WARN] CLOSE CALL")
    else:
        print("[X] Not qualified")

def menu():
    while True:
        print("\n" + "="*40)
        print("TEAM DATA MANAGER")
        print("="*40)
        print("1. Add team data")
        print("2. View all teams")
        print("3. Run analysis with manual data")
        print("4. Exit")

        choice = input("\nChoice: ").strip()

        if choice == '1':
            add_team()
        elif choice == '2':
            data = load_data()
            print(f"\nTeams in database: {len(data)}")
            for team in sorted(data.keys()):
                print(f"  • {team}")
        elif choice == '3':
            run_manual_analysis()
        elif choice == '4':
            break

if __name__ == "__main__":
    menu()
