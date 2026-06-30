#!/usr/bin/env python3 
""" 
PREDICTION TRACKER - PRODUCTION VERSION 
======================================= 
Tracks performance of Over 2.5 and Home Win predictions with auto-updating stats. 
""" 
 
import json
import os
import csv
import logging
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO) 
 
HISTORY_FILE = "prediction_history.json" 
SETTLED_RESULTS = frozenset({"win", "loss", "push"})
MEDIUM = "MEDIUM"
 
 
def home_win_key(pick):
    return (pick["date"], pick.get("home_team", pick.get("home")), 
            pick.get("away_team", pick.get("away")), pick["confidence"])


def over_under_key(pick):
    return (
        pick["date"],
        pick.get("home_team", pick.get("home")),
        pick.get("away_team", pick.get("away")),
        pick["prediction"],
        pick["confidence"],
    )


def _pick_better(existing, new):
    """Prefer settled results; otherwise keep the most recent entry."""
    existing_settled = existing.get("result") in SETTLED_RESULTS
    new_settled = new.get("result") in SETTLED_RESULTS
    if existing_settled and not new_settled:
        return existing
    if new_settled and not existing_settled:
        return new
    if existing.get("recorded_at", "") >= new.get("recorded_at", ""):
        return existing
    return new


def dedupe_predictions(picks, key_fn):
    best = {}
    for pick in picks:
        key = key_fn(pick)
        best[key] = _pick_better(best[key], pick) if key in best else pick
    return list(best.values())


def dedupe_history(history=None, save=True):
    """Remove duplicate prediction rows from history."""
    history = history or load_history()
    before_hw = len(history["home_win"])
    before_ou = len(history["over_under"])

    history["home_win"] = dedupe_predictions(history["home_win"], home_win_key)
    history["over_under"] = dedupe_predictions(history["over_under"], over_under_key)

    stats = {
        "home_win_removed": before_hw - len(history["home_win"]),
        "over_under_removed": before_ou - len(history["over_under"]),
        "home_win_remaining": len(history["home_win"]),
        "over_under_remaining": len(history["over_under"]),
    }

    if save:
        save_history(history)
    return history, stats

 
def add_to_manual_results_csv(date_str, home_team, away_team):
    """Add a match to manual_results.csv if it doesn't already exist."""
    csv_path = "manual_results.csv"
    existing_rows = []
    
    # Read existing CSV
    if os.path.exists(csv_path):
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                existing_rows = list(reader)
        except Exception as e:
            logger.warning(f"Could not read existing manual_results.csv: {e}")
    
    # Check if match already exists
    match_exists = False
    for row in existing_rows:
        if (row.get("date") == date_str and
            row.get("home_team") == home_team and
            row.get("away_team") == away_team):
            match_exists = True
            break
    
    # Add if not exists
    if not match_exists:
        new_row = {
            "date": date_str,
            "home_team": home_team,
            "away_team": away_team,
            "score": ""
        }
        existing_rows.append(new_row)
        
        # Write back to CSV
        try:
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["date", "home_team", "away_team", "score"])
                writer.writeheader()
                for row in existing_rows:
                    # Ensure all fields exist
                    for field in ["date", "home_team", "away_team", "score"]:
                        if field not in row:
                            row[field] = ""
                    writer.writerow(row)
            logger.info(f"Added to manual_results.csv: {date_str} - {home_team} vs {away_team}")
        except Exception as e:
            logger.error(f"Could not write to manual_results.csv: {e}")


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"home_win": [], "over_under": [], "stats": {}} 
 
 
def save_history(history): 
    with open(HISTORY_FILE, "w") as f: 
        json.dump(history, f, indent=2, default=str) 
 
 
def record_predictions(date_str, home_win_picks=None, over_under_picks=None): 
    """Record new predictions (called after running predictors)""" 
    history = load_history() 
    existing_hw = {home_win_key(p) for p in history["home_win"]}
    existing_ou = {over_under_key(p) for p in history["over_under"]}
    added = 0 
    skipped = 0

    if home_win_picks: 
        for pick in home_win_picks: 
            entry = { 
                "date": pick.get("date", date_str), 
                "type": "home_win", 
                "league": pick.get("league"), 
                "home_team": pick.get("home"), 
                "away_team": pick.get("away"), 
                "confidence": pick.get("confidence", MEDIUM), 
                "result": "pending", 
                "recorded_at": datetime.now().isoformat() 
            } 
            key = home_win_key(entry)
            if key in existing_hw:
                skipped += 1
                continue
            history["home_win"].append(entry) 
            existing_hw.add(key)
            added += 1 
            # Add to manual_results.csv
            add_to_manual_results_csv(entry["date"], entry["home_team"], entry["away_team"])

    if over_under_picks: 
        for pick in over_under_picks: 
            entry = { 
                "date": pick.get("date", date_str), 
                "type": "over_under", 
                "league": pick.get("league"), 
                "home_team": pick.get("home"), 
                "away_team": pick.get("away"), 
                "prediction": pick.get("prediction", "over"), 
                "prob": pick.get("prob", pick.get("over25_prob")), 
                "confidence": pick.get("confidence", MEDIUM), 
                "result": "pending", 
                "recorded_at": datetime.now().isoformat() 
            } 
            key = over_under_key(entry)
            if key in existing_ou:
                skipped += 1
                continue
            history["over_under"].append(entry) 
            existing_ou.add(key)
            added += 1 
            # Add to manual_results.csv
            add_to_manual_results_csv(entry["date"], entry["home_team"], entry["away_team"])

    if added > 0: 
        save_history(history) 
        print(f"[OK] Recorded {added} new predictions for {date_str}") 

    return {"added": added, "skipped": skipped}
 
 
def update_result(date_str, home_team, away_team, result, prediction_type="over_under"): 
    """Manually update result after match ends: 'win', 'loss', 'push'""" 
    history = load_history() 
    updated = 0 
 
    picks = history["over_under"] if prediction_type == "over_under" else history["home_win"] 
 
    for pick in picks: 
        pick_home = pick.get("home_team", pick.get("home"))
        pick_away = pick.get("away_team", pick.get("away"))
        if (pick.get("date") == date_str and 
            pick_home == home_team and 
            pick_away == away_team): 
            pick["result"] = result 
            pick["updated_at"] = datetime.now().isoformat() 
            updated += 1 
 
    if updated > 0: 
        save_history(history) 
        print(f"[OK] Updated {updated} match result(s) to '{result}'") 
    else: 
        print("[WARN] No matching prediction found to update.") 
 
    return updated 
 
 
def get_performance_summary(days=30): 
    """Return performance stats""" 
    history = load_history() 
    cutoff = datetime.now() - timedelta(days=days) 
    
    stats = {"home_win": {}, "over_under": {}} 
 
    for ptype in ["home_win", "over_under"]: 
        picks = history.get(ptype, []) 
        wins = losses = pushes = pending = 0 
        recent = [p for p in picks if datetime.fromisoformat(p["date"]) >= cutoff] 
 
        for p in recent: 
            res = p.get("result", "pending") 
            if res == "win": 
                wins += 1 
            elif res == "loss": 
                losses += 1 
            elif res == "push": 
                pushes += 1 
            else: 
                pending += 1 
 
        total_decided = wins + losses + pushes 
        win_rate = (wins / total_decided * 100) if total_decided > 0 else 0 
 
        stats[ptype] = { 
            "total": len(recent), 
            "wins": wins, 
            "losses": losses, 
            "pushes": pushes, 
            "pending": pending, 
            "win_rate": round(win_rate, 1), 
            "roi_estimate": round((wins * 0.9 - losses) / max(1, (wins+losses)), 2) 
        } 
 
    return stats 
 
 
def print_summary(): 
    stats = get_performance_summary(days=30) 
    print("\n📊 PERFORMANCE SUMMARY (Last 30 Days)") 
    print("=" * 50) 
    for ptype, s in stats.items(): 
        print(f"\n{ptype.upper().replace('_', ' ')}:") 
        print(f"  Total: {s['total']} | Win Rate: {s['win_rate']}%") 
        print(f"  Wins: {s['wins']} | Losses: {s['losses']} | Pending: {s['pending']}") 
 
 
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
                home = pick.get("home_team", pick.get("home"))
                away = pick.get("away_team", pick.get("away"))
                status = format_status(pick["result"]) 
                results.append(f"HOME WIN: {home} vs {away} - {status}") 
 
    if prediction_type in (None, "over_under"): 
        for pick in history["over_under"]: 
            if pick["date"] == yesterday: 
                home = pick.get("home_team", pick.get("home"))
                away = pick.get("away_team", pick.get("away"))
                status = format_status(pick["result"]) 
                direction = "OVER 2.5" if pick["prediction"] in ("over", "Over 2.5") else "UNDER 2.5"
                results.append(f"{direction}: {home} vs {away} - {status}") 
 
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


def calculate_performance(history_list, days=30): 
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
    
    for pick in history_list: 
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
        # Handle both date-only (YYYY-MM-DD) and full ISO strings
        date_str = pick["date"]
        if len(date_str) == 10:
            pick_date = datetime.strptime(date_str, "%Y-%m-%d")
        else:
            pick_date = datetime.fromisoformat(date_str)
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
    
    # Separate over and under picks
    over_picks = [p for p in history["over_under"] if p.get("prediction", "over").lower() == "over"]
    under_picks = [p for p in history["over_under"] if p.get("prediction", "over").lower() == "under"]
    
    # Calculate separate stats
    over_stats = calculate_performance_for_month(over_picks, month_start, month_end)
    under_stats = calculate_performance_for_month(under_picks, month_start, month_end)
    
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
    report.append("OVER 2.5 GOALS") 
    report.append(f"Total picks: {over_stats['total']}") 
    report.append(f"Wins: {over_stats['wins']}") 
    report.append(f"Losses: {over_stats['losses']}") 
    report.append(f"Pushes: {over_stats['pushes']}") 
    report.append(f"Pending: {over_stats['pending']}") 
    if over_stats['decisions'] > 0: 
        report.append(f"Win Rate: {over_stats['win_rate']:.1f}%") 
    report.append("") 
    report.append("UNDER 2.5 GOALS") 
    report.append(f"Total picks: {under_stats['total']}") 
    report.append(f"Wins: {under_stats['wins']}") 
    report.append(f"Losses: {under_stats['losses']}") 
    report.append(f"Pushes: {under_stats['pushes']}") 
    report.append(f"Pending: {under_stats['pending']}") 
    if under_stats['decisions'] > 0: 
        report.append(f"Win Rate: {under_stats['win_rate']:.1f}%") 
    report.append("") 
    report.append("---") 
    report.append("For informational purposes only") 
    report.append("Gamble responsibly") 
    report.append("") 
    
    return "\n".join(report), { 
        "month": f"{year}-{month:02d}", 
        "home_win": hw_stats, 
        "over": over_stats,
        "under": under_stats 
    }


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
            # Handle both date-only (YYYY-MM-DD) and full ISO strings
            date_str = pick["date"]
            if len(date_str) == 10:
                pick_date = datetime.strptime(date_str, "%Y-%m-%d")
            else:
                pick_date = datetime.fromisoformat(date_str)
            # Normalize to date-only comparison (ignore time)
            if week_ago.date() <= pick_date.date() <= today.date(): 
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
    
    # Separate over and under picks
    over_picks = [p for p in history["over_under"] if p.get("prediction", "over").lower() == "over"]
    under_picks = [p for p in history["over_under"] if p.get("prediction", "over").lower() == "under"]
    
    over_stats = calc_stats(over_picks)
    under_stats = calc_stats(under_picks)
    
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
    report.append("OVER 2.5 GOALS") 
    report.append(f"Total picks: {over_stats['total']}") 
    report.append(f"Wins: {over_stats['wins']}") 
    report.append(f"Losses: {over_stats['losses']}") 
    report.append(f"Pushes: {over_stats['pushes']}") 
    report.append(f"Pending: {over_stats['pending']}") 
    if over_stats['decisions']>0: 
        report.append(f"Win rate: {over_stats['win_rate']:.1f}%") 
    
    settled_over = [ 
        (conf, cs) for conf, cs in sorted(over_stats['by_confidence'].items()) 
        if cs['decisions'] > 0 
    ] 
    if settled_over: 
        report.append("") 
        report.append("Performance by confidence:") 
        for conf, cs in settled_over: 
            report.append(f"  {conf}: {cs['wins']}/{cs['decisions']} wins ({cs['win_rate']:.1f}%)") 
    
    report.append("") 
    report.append("UNDER 2.5 GOALS") 
    report.append(f"Total picks: {under_stats['total']}") 
    report.append(f"Wins: {under_stats['wins']}") 
    report.append(f"Losses: {under_stats['losses']}") 
    report.append(f"Pushes: {under_stats['pushes']}") 
    report.append(f"Pending: {under_stats['pending']}") 
    if under_stats['decisions']>0: 
        report.append(f"Win rate: {under_stats['win_rate']:.1f}%") 
    
    settled_under = [ 
        (conf, cs) for conf, cs in sorted(under_stats['by_confidence'].items()) 
        if cs['decisions'] > 0 
    ] 
    if settled_under:
        report.append("")
        report.append("Performance by confidence:")
        for conf, cs in settled_under:
            report.append(f"  {conf}: {cs['wins']}/{cs['decisions']} wins ({cs['win_rate']:.1f}%)")

    report.append("")
    report.append("---")
    report.append("For informational purposes only")
    report.append("Gamble responsibly") 
    
    return "\n".join(report), {"home_win": hw_stats, "over": over_stats, "under": under_stats}

 
def main(): 
    print_summary()

if __name__ == "__main__": 
    main()
