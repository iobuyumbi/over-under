#!/usr/bin/env python3
"""
Weekly Performance Report Generator
Generates performance reports and auto-updates README.md
"""

import json
import os
from datetime import datetime, timedelta
from collections import defaultdict

from prediction_tracker import generate_weekly_report as generate_7day_report
from prediction_tracker import load_history, save_history


HISTORY_FILE = "prediction_history.json"
README_FILE = "README.md"


def calculate_weekly_stats(history, weeks=4):
    """Calculate performance for last N weeks"""
    stats = {"over": {}, "under": {}, "home_win": {}}
    now = datetime.now()

    # Process over_under picks separately by prediction type
    ou_picks = history.get("over_under", [])
    weekly_over = defaultdict(lambda: {"wins": 0, "losses": 0, "pushes": 0, "total": 0})
    weekly_under = defaultdict(lambda: {"wins": 0, "losses": 0, "pushes": 0, "total": 0})

    for pick in ou_picks:
        if pick.get("result") in ["pending", None]:
            continue
        try:
            pick_date = datetime.strptime(pick["date"], "%Y-%m-%d")
            week_key = pick_date.strftime("%Y-W%W")
            res = pick["result"]
            prediction = pick.get("prediction", "over").lower()

            weekly_dict = weekly_over if prediction == "over" else weekly_under
            weekly_dict[week_key]["total"] += 1
            if res == "win":
                weekly_dict[week_key]["wins"] += 1
            elif res == "loss":
                weekly_dict[week_key]["losses"] += 1
            elif res == "push":
                weekly_dict[week_key]["pushes"] += 1
        except:
            continue

    stats["over"] = sorted(
        [{"week": k, **v, "win_rate": round(v["wins"]/v["total"]*100, 1) if v["total"] > 0 else 0}
         for k, v in weekly_over.items()],
        key=lambda x: x["week"],
        reverse=True
    )[:weeks]

    stats["under"] = sorted(
        [{"week": k, **v, "win_rate": round(v["wins"]/v["total"]*100, 1) if v["total"] > 0 else 0}
         for k, v in weekly_under.items()],
        key=lambda x: x["week"],
        reverse=True
    )[:weeks]

    # Process home_win picks
    hw_picks = history.get("home_win", [])
    weekly_hw = defaultdict(lambda: {"wins": 0, "losses": 0, "pushes": 0, "total": 0})

    for pick in hw_picks:
        if pick.get("result") in ["pending", None]:
            continue
        try:
            pick_date = datetime.strptime(pick["date"], "%Y-%m-%d")
            week_key = pick_date.strftime("%Y-W%W")
            res = pick["result"]

            weekly_hw[week_key]["total"] += 1
            if res == "win":
                weekly_hw[week_key]["wins"] += 1
            elif res == "loss":
                weekly_hw[week_key]["losses"] += 1
            elif res == "push":
                weekly_hw[week_key]["pushes"] += 1
        except:
            continue

    stats["home_win"] = sorted(
        [{"week": k, **v, "win_rate": round(v["wins"]/v["total"]*100, 1) if v["total"] > 0 else 0}
         for k, v in weekly_hw.items()],
        key=lambda x: x["week"],
        reverse=True
    )[:weeks]

    return stats


def generate_readme_section(stats):
    lines = ["\n## Weekly Performance Report\n"]
    lines.append(f"**Last Updated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    for ptype, data in stats.items():
        if ptype == "over":
            name = "Over 2.5 Goals"
        elif ptype == "under":
            name = "Under 2.5 Goals"
        else:
            name = "Home Win"
        
        lines.append(f"### {name}\n")
        lines.append("| Week | Matches | Wins | Losses | Win Rate |")
        lines.append("|------|---------|------|--------|----------|")

        for entry in data:
            lines.append(f"| {entry['week']} | {entry['total']} | {entry['wins']} | {entry['losses']} | {entry['win_rate']}% |")

        lines.append("")

    return "\n".join(lines)


def update_readme():
    history = load_history()
    stats = calculate_weekly_stats(history, weeks=6)

    try:
        with open(README_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        content = "# Soccer Predictions\n\n"

    # Replace or append performance section
    if "## Weekly Performance Report" in content:
        before = content.split("## Weekly Performance Report")[0]
        after_part = content.split("## Weekly Performance Report")[1]
        after = after_part.split("\n## ")[0] if "## " in after_part else ""
        new_content = before + generate_readme_section(stats) + after
    else:
        new_content = content.rstrip() + "\n" + generate_readme_section(stats)

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.write(new_content)

    print("[OK] README.md updated with latest weekly performance report!")


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"Generating weekly report ending {today}...")

    # Generate and save the 7-day report
    report_text, report_data = generate_7day_report()

    json_filename = f"weekly_report_{today}.json"
    with open(json_filename, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2, default=str)
    print(f"JSON report saved to {json_filename}")

    text_filename = f"weekly_report_{today}.txt"
    with open(text_filename, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"Text report saved to {text_filename}")

    print("\nWeekly Performance Report (7 days):")
    print(report_text)

    # Update README with weekly history
    print("\nUpdating README...")
    update_readme()


if __name__ == "__main__":
    main()
