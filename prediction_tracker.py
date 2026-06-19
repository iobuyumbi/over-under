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
            "date": pick.get("date", date),
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
            "date": pick.get("date", date),
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
    report.append("MONTHLY PERFORMANCE REPORT")
    report.append(f"{month_name}")
    report.append("")
    report.append("HOME WIN PREDICTIONS")
    report.append(f"Total picks: {hw_stats['total']}")
    report.append(f"Wins: {hw_stats['wins']}")
    report.append(f"Losses: {hw_stats['losses']}")
    report.append(f"Pushes: {hw_stats['pushes']}")
    report.append(f"Pending: {hw_stats['pending']}")
    if hw_stats['decisions'] > 0:
        report.append(f"Win Rate: {hw_stats['win_rate']:.1f}%")
    report.append("")
    report.append("OVER/UNDER 2.5 GOALS")
    report.append(f"Total picks: {ou_stats['total']}")
    report.append(f"Wins: {ou_stats['wins']}")
    report.append(f"Losses: {ou_stats['losses']}")
    report.append(f"Pushes: {ou_stats['pushes']}")
    report.append(f"Pending: {ou_stats['pending']}")
    if ou_stats['decisions'] > 0:
        report.append(f"Win Rate: {ou_stats['win_rate']:.1f}%")
    report.append("")
    report.append("---")
    report.append("For informational purposes only")
    report.append("Gamble responsibly")
    report.append("")
    
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


def get_yesterday_results(prediction_type=None):
    """Get a clean summary of yesterday's results.

    prediction_type: None for all, "home_win", or "over_under"
    Returns: (date_str, result_lines, summary_dict)
    """
    history = load_history()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    results = []
    summary = {"wins": 0, "losses": 0, "pushes": 0, "pending": 0}

    def format_status(result):
        if result == "win":
            summary["wins"] += 1
            return "Win"
        if result == "loss":
            summary["losses"] += 1
            return "Loss"
        if result == "push":
            summary["pushes"] += 1
            return "Push"
        summary["pending"] += 1
        return "Pending"

    if prediction_type in (None, "home_win"):
        for pick in history["home_win"]:
            if pick["date"] == yesterday:
                status = format_status(pick["result"])
                results.append(f"HOME WIN: {pick['home_team']} vs {pick['away_team']} - {status}")

    if prediction_type in (None, "over_under"):
        for pick in history["over_under"]:
            if pick["date"] == yesterday:
                status = format_status(pick["result"])
                direction = "OVER 2.5" if pick["prediction"] == "over" else "UNDER 2.5"
                results.append(f"{direction}: {pick['home_team']} vs {pick['away_team']} - {status}")

    return yesterday, results, summary


def format_yesterday_header(summary):
    """Build a one-line record summary for daily reports."""
    settled = summary["wins"] + summary["losses"] + summary["pushes"]
    if settled:
        line = f"Record: {summary['wins']}W-{summary['losses']}L-{summary['pushes']}P"
        if summary["pending"]:
            line += f" ({summary['pending']} pending)"
        return line
    if summary["pending"]:
        return f"Pending: {summary['pending']}"
    return None


def generate_weekly_report():
    """Generate a weekly performance report for the last 7 days."""
    history = load_history()
    week_ago = datetime.now() - timedelta(days=7)
    today = datetime.now()
    
    # Function to calculate stats for a date range
    def calc_stats(picks):
        stats = {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "pushes": 0,
            "pending": 0,
            "decisions": 0,
            "win_rate": 0.0,
            "by_confidence": {}
        }
        
        for pick in picks:
            pick_date = datetime.fromisoformat(pick["date"])
            if week_ago <= pick_date <= today:
                stats["total"] += 1
                conf = pick.get("confidence", "N/A")
                if conf not in stats["by_confidence"]:
                    stats["by_confidence"][conf] = {"wins":0, "losses":0, "pushes":0, "pending":0, "decisions":0}
                
                if pick["result"] == "win":
                    stats["wins"] += 1
                    stats["decisions"] += 1
                    stats["by_confidence"][conf]["wins"] +=1
                    stats["by_confidence"][conf]["decisions"] +=1
                elif pick["result"] == "loss":
                    stats["losses"] += 1
                    stats["decisions"] +=1
                    stats["by_confidence"][conf]["losses"] +=1
                    stats["by_confidence"][conf]["decisions"] +=1
                elif pick["result"] == "push":
                    stats["pushes"] +=1
                    stats["by_confidence"][conf]["pushes"] +=1
                else:
                    stats["pending"] +=1
                    stats["by_confidence"][conf]["pending"] +=1
        
        if stats["decisions"] >0:
            stats["win_rate"] = (stats["wins"] / stats["decisions"])*100
        
        # Calculate win rate by confidence
        for conf in stats["by_confidence"]:
            if stats["by_confidence"][conf]["decisions"] >0:
                stats["by_confidence"][conf]["win_rate"] = (stats["by_confidence"][conf]["wins"] / stats["by_confidence"][conf]["decisions"])*100
        
        return stats
    
    hw_stats = calc_stats(history["home_win"])
    ou_stats = calc_stats(history["over_under"])
    
    report = []
    report.append("WEEKLY PERFORMANCE REPORT")
    report.append(f"{week_ago.strftime('%Y-%m-%d')} to {today.strftime('%Y-%m-%d')}")
    report.append("")
    report.append("HOME WIN PREDICTIONS")
    report.append(f"Total picks: {hw_stats['total']}")
    report.append(f"Wins: {hw_stats['wins']}")
    report.append(f"Losses: {hw_stats['losses']}")
    report.append(f"Pushes: {hw_stats['pushes']}")
    report.append(f"Pending: {hw_stats['pending']}")
    if hw_stats['decisions']>0:
        report.append(f"Win rate: {hw_stats['win_rate']:.1f}%")
    
    settled_hw = [
        (conf, cs) for conf, cs in sorted(hw_stats['by_confidence'].items())
        if cs['decisions'] > 0
    ]
    if settled_hw:
        report.append("")
        report.append("Performance by confidence:")
        for conf, cs in settled_hw:
            report.append(f"  {conf}: {cs['wins']}/{cs['decisions']} wins ({cs['win_rate']:.1f}%)")
    
    report.append("")
    report.append("OVER/UNDER 2.5 GOALS")
    report.append(f"Total picks: {ou_stats['total']}")
    report.append(f"Wins: {ou_stats['wins']}")
    report.append(f"Losses: {ou_stats['losses']}")
    report.append(f"Pushes: {ou_stats['pushes']}")
    report.append(f"Pending: {ou_stats['pending']}")
    if ou_stats['decisions']>0:
        report.append(f"Win rate: {ou_stats['win_rate']:.1f}%")
    
    settled_ou = [
        (conf, cs) for conf, cs in sorted(ou_stats['by_confidence'].items())
        if cs['decisions'] > 0
    ]
    if settled_ou:
        report.append("")
        report.append("Performance by confidence:")
        for conf, cs in settled_ou:
            report.append(f"  {conf}: {cs['wins']}/{cs['decisions']} wins ({cs['win_rate']:.1f}%)")
    
    report.append("")
    report.append("---")
    report.append("For informational purposes only")
    report.append("Gamble responsibly")
    
    return "\n".join(report), {"home_win": hw_stats, "over_under": ou_stats}

def main():
    """Main function - show history overview."""
    history = load_history()
    
    print(f"Prediction History Overview")
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
    
    print(f"\nLast Month's Performance:")
    report_text, report_data = generate_monthly_report(last_month_year, last_month)
    print(report_text)

if __name__ == "__main__":
    main()
