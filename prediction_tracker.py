#!/usr/bin/env python3
"""
Historical result tracker for soccer predictions.
Tracks wins/losses of predictions over time.
"""

import os
import json
import glob
from datetime import datetime, timedelta
from collections import defaultdict

HISTORY_FILE = "prediction_history.json"

def load_history():
    """Load prediction history from file."""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return {
        "home_win": [],
        "over_under": []
    }

def save_history(history):
    """Save prediction history to file."""
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, default=str)

def record_predictions(date, home_win_picks, over_under_picks):
    """Record predictions for a given date."""
    history = load_history()
    
    # Record home win picks
    for pick in home_win_picks:
        entry = {
            "date": date,
            "league": pick["league"],
            "home_team": pick["home"],
            "away_team": pick["away"],
            "confidence": pick["confidence"],
            "result": "pending",
            "recorded_at": datetime.now().isoformat()
        }
        history["home_win"].append(entry)
    
    # Record over/under picks
    for pick in over_under_picks:
        entry = {
            "date": date,
            "league": pick["league"],
            "home_team": pick["home"],
            "away_team": pick["away"],
            "prediction": pick["prediction"],  # "over" or "under"
            "confidence": pick["confidence"],
            "result": "pending",
            "recorded_at": datetime.now().isoformat()
        }
        history["over_under"].append(entry)
    
    save_history(history)

def update_result(prediction_type, index, result):
    """Update the result of a specific prediction.
    result should be "win", "loss", or "push"
    """
    history = load_history()
    if prediction_type in history and 0 <= index < len(history[prediction_type]):
        history[prediction_type][index]["result"] = result
        history[prediction_type][index]["updated_at"] = datetime.now().isoformat()
        save_history(history)
        return True
    return False

def get_pending_predictions(days_old=None):
    """Get pending predictions (no result recorded yet)."""
    history = load_history()
    pending = []
    
    for p_type in ["home_win", "over_under"]:
        for idx, pick in enumerate(history[p_type]):
            if pick["result"] == "pending":
                pick["type"] = p_type
                pick["index"] = idx
                if days_old:
                    pick_date = datetime.fromisoformat(pick["date"])
                    if (datetime.now() - pick_date).days <= days_old:
                        pending.append(pick)
                else:
                    pending.append(pick)
    
    return pending

def calculate_performance(history, days=30):
    """Calculate performance metrics for last N days."""
    cutoff = datetime.now() - timedelta(days=days)
    stats = {
        "wins": 0,
        "losses": 0,
        "pushes": 0,
        "pending": 0,
        "win_rate": 0.0,
        "total_decisions": 0
    }
    
    for pick in history:
        pick_date = datetime.fromisoformat(pick["date"])
        if pick_date >= cutoff:
            if pick["result"] == "win":
                stats["wins"] += 1
                stats["total_decisions"] += 1
            elif pick["result"] == "loss":
                stats["losses"] += 1
                stats["total_decisions"] += 1
            elif pick["result"] == "push":
                stats["pushes"] += 1
            else:
                stats["pending"] += 1
    
    if stats["total_decisions"] > 0:
        stats["win_rate"] = (stats["wins"] / stats["total_decisions"]) * 100
    
    return stats

def generate_monthly_report(year, month):
    """Generate detailed monthly performance report with win/loss tracking."""
    history = load_history()
    month_start = datetime(year, month, 1)
    
    if month == 12:
        month_end = datetime(year + 1, 1, 1)
    else:
        month_end = datetime(year, month + 1, 1)
    
    # Calculate home win stats
    hw_stats = calculate_performance_for_month(history["home_win"], month_start, month_end)
    
    # Calculate over/under stats
    ou_stats = calculate_performance_for_month(history["over_under"], month_start, month_end)
    
    # Generate report text
    month_name = month_start.strftime("%B %Y")
    report = []
    report.append("=" * 40)
    report.append(f"📊 MONTHLY PERFORMANCE REPORT - {month_name}")
    report.append("=" * 40)
    report.append("")
    report.append("🏠 HOME WIN PREDICTIONS")
    report.append("-" * 40)
    report.append(f"  Total picks: {hw_stats['total']}")
    report.append(f"  Wins: {hw_stats['wins']}")
    report.append(f"  Losses: {hw_stats['losses']}")
    report.append(f"  Pushes: {hw_stats['pushes']}")
    report.append(f"  Pending: {hw_stats['pending']}")
    if hw_stats['decisions'] > 0:
        report.append(f"  Win Rate: {hw_stats['win_rate']:.1f}%")
    report.append("")
    report.append("🔥 OVER/UNDER 2.5 GOALS")
    report.append("-" * 40)
    report.append(f"  Total picks: {ou_stats['total']}")
    report.append(f"  Wins: {ou_stats['wins']}")
    report.append(f"  Losses: {ou_stats['losses']}")
    report.append(f"  Pushes: {ou_stats['pushes']}")
    report.append(f"  Pending: {ou_stats['pending']}")
    if ou_stats['decisions'] > 0:
        report.append(f"  Win Rate: {ou_stats['win_rate']:.1f}%")
    report.append("")
    report.append("=" * 40)
    report.append("⚠️ DISCLAIMER: Past performance doesn't guarantee future results.")
    report.append("   Gamble responsibly and within your means.")
    report.append("=" * 40)
    report.append("")
    report.append("💡 Support our free service by registering using our affiliate link!")
    report.append("🔗 Place Your Bookmaker Affiliate Link Here")
    
    return "\n".join(report), {
        "month": f"{year}-{month:02d}",
        "home_win": hw_stats,
        "over_under": ou_stats
    }

def calculate_performance_for_month(picks, month_start, month_end):
    """Calculate performance for a specific month."""
    stats = {
        "total": 0,
        "wins": 0,
        "losses": 0,
        "pushes": 0,
        "pending": 0,
        "decisions": 0,
        "win_rate": 0.0
    }
    
    for pick in picks:
        pick_date = datetime.fromisoformat(pick["date"])
        if month_start <= pick_date < month_end:
            stats["total"] += 1
            if pick["result"] == "win":
                stats["wins"] += 1
                stats["decisions"] += 1
            elif pick["result"] == "loss":
                stats["losses"] += 1
                stats["decisions"] += 1
            elif pick["result"] == "push":
                stats["pushes"] += 1
            else:
                stats["pending"] += 1
    
    if stats["decisions"] > 0:
        stats["win_rate"] = (stats["wins"] / stats["decisions"]) * 100
    
    return stats

def main():
    """Main function - show history overview."""
    history = load_history()
    
    print(f"📊 Prediction History Overview")
    print("=" * 40)
    print(f"Home Win Predictions: {len(history['home_win'])} total")
    print(f"Over/Under Predictions: {len(history['over_under'])} total")
    
    pending = get_pending_predictions()
    if pending:
        print(f"\nPending Results: {len(pending)}")
        print("Last 5 pending:")
        for p in pending[-5:]:
            print(f"  - {p['date']}: {p['home_team']} vs {p['away_team']} ({p['type']})")
    
    # Generate last month's report
    now = datetime.now()
    if now.month == 1:
        last_month_year = now.year - 1
        last_month = 12
    else:
        last_month_year = now.year
        last_month = now.month - 1
    
    print(f"\n📈 Last Month's Performance:")
    report_text, report_data = generate_monthly_report(last_month_year, last_month)
    print(report_text)

if __name__ == "__main__":
    main()
