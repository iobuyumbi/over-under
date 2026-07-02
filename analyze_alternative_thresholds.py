#!/usr/bin/env python3
"""
Analyze how Over 1.5, Under 3.5, and Double Chance (home or draw) picks would have performed using existing prediction history
"""
import json
from datetime import datetime, timedelta

def load_history():
    with open("prediction_history.json", "r") as f:
        return json.load(f)

def calculate_result(home_score, away_score, prediction):
    total_goals = home_score + away_score
    if prediction.lower() == "over":
        # Over 1.5: goals > 1.5 (2 or more)
        return "win" if total_goals >= 2 else "loss"
    elif prediction.lower() == "under":
        # Under 3.5: goals < 3.5 (3 or less)
        return "win" if total_goals <= 3 else "loss"
    return "pending"

def calculate_double_chance_result(home_score, away_score):
    # Double chance home or draw: home wins OR draw
    if home_score >= away_score:
        return "win"
    else:
        return "loss"

def analyze_period(history, days_back=30):
    now = datetime.now()
    cutoff = now - timedelta(days=days_back)
    
    stats = {
        "home_win": {
            "total": 0, "wins": 0, "losses": 0, "pushes": 0, "pending": 0
        },
        "double_chance": {
            "total": 0, "wins": 0, "losses": 0, "pushes": 0, "pending": 0
        },
        "over_15": {
            "total": 0, "wins": 0, "losses": 0, "pushes": 0, "pending": 0
        },
        "under_35": {
            "total": 0, "wins": 0, "losses": 0, "pushes": 0, "pending": 0
        }
    }
    
    # Process home win picks (still using original criteria)
    for pick in history["home_win"]:
        try:
            if len(pick["date"]) == 10:
                pick_date = datetime.strptime(pick["date"], "%Y-%m-%d")
            else:
                pick_date = datetime.fromisoformat(pick["date"])
                
            if pick_date < cutoff:
                continue
                
            stats["home_win"]["total"] += 1
            if pick["result"] != "pending":
                if pick["result"] == "win":
                    stats["home_win"]["wins"] += 1
                elif pick["result"] == "loss":
                    stats["home_win"]["losses"] += 1
                elif pick["result"] == "push":
                    stats["home_win"]["pushes"] += 1
            else:
                stats["home_win"]["pending"] += 1
                
            # Also analyze double chance for these home win picks
            stats["double_chance"]["total"] +=1
            if pick["result"] == "pending" or "final_score" not in pick:
                stats["double_chance"]["pending"] +=1
            else:
                home_score, away_score = map(int, pick["final_score"].split("-"))
                dc_result = calculate_double_chance_result(home_score, away_score)
                if dc_result == "win":
                    stats["double_chance"]["wins"] +=1
                else:
                    stats["double_chance"]["losses"] +=1
        except:
            pass
    
    # Process over/under picks with new thresholds
    for pick in history["over_under"]:
        try:
            if len(pick["date"]) == 10:
                pick_date = datetime.strptime(pick["date"], "%Y-%m-%d")
            else:
                pick_date = datetime.fromisoformat(pick["date"])
                
            if pick_date < cutoff:
                continue
                
            prediction = pick.get("prediction", "over").lower()
            if pick["result"] == "pending" or "final_score" not in pick:
                stats[f"{prediction}_15" if prediction == "over" else "under_35"]["pending"] +=1
                continue
                
            # Parse final score
            home_score, away_score = map(int, pick["final_score"].split("-"))
            
            # Analyze both thresholds
            if prediction == "over":
                stats["over_15"]["total"] +=1
                result = calculate_result(home_score, away_score, "over")
                if result == "win":
                    stats["over_15"]["wins"] +=1
                elif result == "loss":
                    stats["over_15"]["losses"] +=1
            
            if prediction == "under":
                stats["under_35"]["total"] +=1
                result = calculate_result(home_score, away_score, "under")
                if result == "win":
                    stats["under_35"]["wins"] +=1
                elif result == "loss":
                    stats["under_35"]["losses"] +=1
                
        except Exception as e:
            print(f"Error processing pick: {e}")
            pass
            
    # Calculate win rates
    for key in ["home_win", "double_chance", "over_15", "under_35"]:
        total_decisions = stats[key]["wins"] + stats[key]["losses"] + stats[key]["pushes"]
        if total_decisions > 0:
            stats[key]["win_rate"] = round(stats[key]["wins"] / total_decisions * 100, 1)
        else:
            stats[key]["win_rate"] = 0.0
            
    return stats

def print_report(period_name, stats):
    print("="*60)
    print(f"{period_name} PERFORMANCE ANALYSIS")
    print("="*60)
    print()
    
    print("HOME WIN PREDICTIONS (original criteria)")
    print("-"*40)
    print(f"Total picks: {stats['home_win']['total']}")
    print(f"Wins: {stats['home_win']['wins']}")
    print(f"Losses: {stats['home_win']['losses']}")
    print(f"Pushes: {stats['home_win']['pushes']}")
    print(f"Pending: {stats['home_win']['pending']}")
    if stats['home_win']['win_rate'] >0:
        print(f"Win rate: {stats['home_win']['win_rate']}%")
    print()
    
    print("DOUBLE CHANCE (Home or Draw) - safer alternative")
    print("-"*40)
    print(f"Total picks: {stats['double_chance']['total']}")
    print(f"Wins: {stats['double_chance']['wins']}")
    print(f"Losses: {stats['double_chance']['losses']}")
    print(f"Pending: {stats['double_chance']['pending']}")
    if stats['double_chance']['win_rate'] >0:
        print(f"Win rate: {stats['double_chance']['win_rate']}%")
    print()
    
    print("OVER 1.5 GOALS (instead of Over 2.5)")
    print("-"*40)
    print(f"Total picks: {stats['over_15']['total']}")
    print(f"Wins: {stats['over_15']['wins']}")
    print(f"Losses: {stats['over_15']['losses']}")
    print(f"Pending: {stats['over_15']['pending']}")
    if stats['over_15']['win_rate'] >0:
        print(f"Win rate: {stats['over_15']['win_rate']}%")
    print()
    
    print("UNDER 3.5 GOALS (instead of Under 2.5)")
    print("-"*40)
    print(f"Total picks: {stats['under_35']['total']}")
    print(f"Wins: {stats['under_35']['wins']}")
    print(f"Losses: {stats['under_35']['losses']}")
    print(f"Pending: {stats['under_35']['pending']}")
    if stats['under_35']['win_rate'] >0:
        print(f"Win rate: {stats['under_35']['win_rate']}%")
    print()

def main():
    history = load_history()
    
    # Weekly analysis (last 7 days)
    weekly_stats = analyze_period(history, 7)
    print_report("WEEKLY", weekly_stats)
    
    # Monthly analysis (last 30 days)
    monthly_stats = analyze_period(history, 30)
    print_report("MONTHLY", monthly_stats)

if __name__ == "__main__":
    main()
