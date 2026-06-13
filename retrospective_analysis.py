#!/usr/bin/env python3
"""Retrospective prediction analysis: Update results and analyze performance"""

import os
import json
import re
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from prediction_tracker import load_history, save_history


def normalize_team_name(name):
    """Normalize team name for better matching"""
    if not name:
        return ""
    name = name.strip().lower()
    # Remove common suffixes
    name = re.sub(r"\s+fc$", "", name)
    name = re.sub(r"\s+cf$", "", name)
    name = re.sub(r"\s+city$", "", name)
    name = re.sub(r"\s+united$", "", name)
    name = re.sub(r"\s+athletico$", "", name)
    name = re.sub(r"\s+afc$", "", name)
    return name.strip()


def fetch_match_results(date_str):
    """Fetch match results for a specific date (YYYY-MM-DD) from Soccerbase"""
    print(f"\nFetching results for {date_str}")
    
    url = f"https://www.soccerbase.com/matches/results.sd?date={date_str}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
    except Exception as e:
        print(f"  Failed to fetch: {e}")
        return []
    
    soup = BeautifulSoup(response.text, "html.parser")
    
    matches = []
    
    tables = soup.find_all("table", class_="listWithCards")
    
    for table in tables:
        for row in table.find_all("tr", class_="match"):
            try:
                home_elem = row.find("td", class_="homeTeam")
                away_elem = row.find("td", class_="awayTeam")
                score_elem = row.find("td", class_="score")
                
                if not (home_elem and away_elem and score_elem):
                    continue
                
                home_team = home_elem.get_text(strip=True)
                away_team = away_elem.get_text(strip=True)
                score_text = score_elem.get_text(strip=True)
                
                if not score_text or "v" in score_text.lower():
                    continue  # Skip if it's a future match or no score
                
                matches.append({
                    "home_team": home_team,
                    "away_team": away_team,
                    "score": score_text
                })
                
            except Exception as e:
                continue
    
    print(f"  Found {len(matches)} completed matches")
    return matches


def determine_home_win_result(prediction, match_result):
    """Determine if a home win prediction outcome"""
    score = match_result["score"]
    if "-" not in score:
        return None
    try:
        home_goals, away_goals = map(int, score.split("-"))
        if home_goals > away_goals:
            return "win"
        elif home_goals < away_goals:
            return "loss"
        else:
            return "push"
    except ValueError:
        return None


def determine_over_under_result(prediction, match_result):
    """Determine if an over/under prediction outcome"""
    score = match_result["score"]
    if "-" not in score:
        return None
    try:
        home_goals, away_goals = map(int, score.split("-"))
        total_goals = home_goals + away_goals
        
        if prediction["prediction"] == "over":
            if total_goals > 2:
                return "win"
            elif total_goals < 2:
                return "loss"
            else:
                return "push"
        else:  # under
            if total_goals < 2:
                return "win"
            elif total_goals > 2:
                return "loss"
            else:
                return "push"
    except ValueError:
        return None


def update_and_analyze():
    print("="*60)
    print("Retrospective Analysis & Result Updater")
    print("="*60)
    
    history = load_history()
    updated_count = 0
    
    all_dates = set()
    for pick in history["home_win"] + history["over_under"]:
        all_dates.add(pick["date"])
    
    print(f"\nDates to check: {sorted(all_dates)}")
    
    for date_str in sorted(all_dates):
        results = fetch_match_results(date_str)
        
        # Update home win predictions
        for idx, pick in enumerate(history["home_win"]):
            if pick["date"] != date_str or pick["result"] != "pending":
                continue
            
            home_norm = normalize_team_name(pick["home_team"])
            away_norm = normalize_team_name(pick["away_team"])
            
            for match in results:
                if (normalize_team_name(match["home_team"]) == home_norm and
                    normalize_team_name(match["away_team"]) == away_norm):
                    result = determine_home_win_result(pick, match)
                    if result:
                        history["home_win"][idx]["result"] = result
                        updated_count += 1
                        print(f"  Updated home_win: {pick['home_team']} vs {pick['away_team']} - {result}")
                    break
        
        # Update over/under predictions
        for idx, pick in enumerate(history["over_under"]):
            if pick["date"] != date_str or pick["result"] != "pending":
                continue
            
            home_norm = normalize_team_name(pick["home_team"])
            away_norm = normalize_team_name(pick["away_team"])
            
            for match in results:
                if (normalize_team_name(match["home_team"]) == home_norm and
                    normalize_team_name(match["away_team"]) == away_norm):
                    result = determine_over_under_result(pick, match)
                    if result:
                        history["over_under"][idx]["result"] = result
                        updated_count += 1
                        print(f"  Updated over_under: {pick['home_team']} vs {pick['away_team']} - {result}")
                    break
    
    if updated_count > 0:
        save_history(history)
        print(f"\nUpdated {updated_count} predictions!")
    
    # Now do performance analysis
    print("\n" + "="*60)
    print("Performance Analysis")
    print("="*60)
    
    pred_type_names = {"home_win": "Home Win", "over_under": "Over/Under 2.5"}
    for pred_type in ["home_win", "over_under"]:
        print(f"\n{pred_type_names[pred_type]}")
        print("-" * len(pred_type_names[pred_type]))
        
        stats = {"win": 0, "loss": 0, "push": 0, "pending": 0}
        by_confidence = {}
        
        for pick in history[pred_type]:
            result = pick["result"]
            if result in stats:
                stats[result] += 1
                
            conf = pick["confidence"]
            if conf not in by_confidence:
                by_confidence[conf] = {"win": 0, "loss": 0, "push": 0, "pending": 0}
            if result in by_confidence[conf]:
                by_confidence[conf][result] += 1
        
        total_decisions = stats["win"] + stats["loss"]
        win_rate = (stats["win"] / total_decisions * 100) if total_decisions > 0 else 0
        
        print(f"  Total picks: {sum(stats.values())}")
        print(f"  Wins: {stats['win']}, Losses: {stats['loss']}, Pushes: {stats['push']}, Pending: {stats['pending']}")
        if total_decisions > 0:
            print(f"  Win rate: {win_rate:.1f}%")
        
        if by_confidence:
            print("\n  By confidence:")
            for conf in sorted(by_confidence.keys()):
                c_stats = by_confidence[conf]
                c_decisions = c_stats["win"] + c_stats["loss"]
                c_win_rate = (c_stats["win"] / c_decisions * 100) if c_decisions > 0 else 0
                print(f"    {conf}: W:{c_stats['win']} L:{c_stats['loss']} P:{c_stats['push']}")
                if c_decisions > 0:
                    print(f"      Win rate: {c_win_rate:.1f}%")
    
    print("\n" + "="*60)


if __name__ == "__main__":
    update_and_analyze()
