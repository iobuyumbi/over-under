#!/usr/bin/env python3
"""
TEST SCRIPT FOR OVER 2.5 GOALS PREDICTION SYSTEM
Demonstrates the algorithm using sample data
"""

import json
import os
from datetime import datetime
from over25_predictor import apply_algorithm

def main():
    print("=" * 70)
    print("OVER 2.5 GOALS PREDICTION SYSTEM - TEST MODE")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 70)
    
    # Load sample data
    DATA_FILE = 'team_data.json'
    if not os.path.exists(DATA_FILE):
        print(f"[ERROR] {DATA_FILE} not found!")
        return
    
    with open(DATA_FILE, 'r') as f:
        team_data = json.load(f)
    
    print(f"\n[*] Loaded {len(team_data)} teams from {DATA_FILE}")
    print("    Teams available:", ", ".join(sorted(team_data.keys())))
    
    # Test some matches
    test_matches = [
        ("Manchester City", "Liverpool"),
        ("Bayern Munich", "Borussia Dortmund"),
        ("Real Madrid", "Barcelona")
    ]
    
    print("\n" + "=" * 70)
    print("TESTING MATCHES")
    print("=" * 70)
    
    qualified = []
    close_calls = []
    
    for home_team, away_team in test_matches:
        if home_team not in team_data or away_team not in team_data:
            print(f"\n[WARN] Missing data for {home_team} vs {away_team}")
            continue
        
        home_form = [tuple(m) for m in team_data[home_team]["home"]]
        away_form = [tuple(m) for m in team_data[away_team]["away"]]
        
        passed, failed, details = apply_algorithm(home_form, away_form)
        
        result = {
            "home": home_team,
            "away": away_team,
            "home_form": home_form,
            "away_form": away_form,
            "passed": passed,
            "failed": failed,
            "details": details,
            "score": len(passed)
        }
        
        print(f"\n--- {home_team} vs {away_team} ---")
        print(f"Home form: {home_form}")
        print(f"Away form: {away_form}")
        for check, status in details.items():
            icon = "[OK]" if "PASS" in status else "[X]"
            print(f"  {icon} {check}: {status}")
        print(f"Score: {len(passed)}/6")
        
        if len(passed) == 6:
            qualified.append(result)
            print("[+] FULLY QUALIFIED!")
        elif len(passed) == 5:
            close_calls.append(result)
            print("[WARN] CLOSE CALL (5/6)")
        else:
            print("[X] Not qualified")
    
    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"\nQualified matches: {len(qualified)}")
    for q in qualified:
        print(f"  - {q['home']} vs {q['away']}")
    
    print(f"\nClose calls (5/6): {len(close_calls)}")
    for c in close_calls:
        print(f"  - {c['home']} vs {c['away']} (failed: {', '.join(c['failed'])})")
    
    print("\n" + "=" * 70)

if __name__ == "__main__":
    main()
