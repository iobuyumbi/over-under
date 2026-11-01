#!/usr/bin/env python3
"""
Weekly Performance Report Generator
Generates performance reports and auto-updates README.md
"""

import json
from datetime import datetime
from collections import defaultdict

from prediction_tracker import generate_weekly_report as generate_7day_report
from prediction_tracker import load_history


HISTORY_FILE = "prediction_history.json"
README_FILE = "README.md"

# Tracked markets, in report order. Each entry is
# (key, display name, history section, prediction token or None for "all").
# The negative sides (Under 2.5, BTTS No) are tracked on their own so their
# win rate is not hidden inside the Over 2.5 / BTTS totals.
MARKET_GROUPS = (
    ("over_25", "Over 2.5 Goals", "over_under", "over"),
    ("under_25", "Under 2.5 Goals", "over_under", "under"),
    ("home_win", "Home Win", "home_win", None),
    ("btts_yes", "BTTS Yes", "btts", "yes"),
    ("btts_no", "BTTS No", "btts", "no"),
    ("oo05", "Over 0.5 Team Goal", "oo05", None),
)


def _normalized_prediction(pick, ptype):
    """Collapse the various stored prediction spellings to 'over'/'under'/'yes'/'no'."""
    value = str(pick.get("prediction", "")).strip().lower()
    if ptype == "over_under":
        if value.startswith("under"):
            return "under"
        if value.startswith("over") or not value:
            return "over"
    if ptype == "btts":
        if value in ("no", "btts_no", "btts no"):
            return "no"
        if value in ("yes", "btts_yes", "btts yes"):
            return "yes"
    return value


def calculate_weekly_stats(history, weeks=4, groups=MARKET_GROUPS):
    """Calculate performance for last N weeks, per tracked market."""
    stats = {}

    for key, _name, ptype, token in groups:
        picks = history.get(ptype, [])
        if token is not None:
            picks = [p for p in picks if _normalized_prediction(p, ptype) == token]
        weekly = defaultdict(lambda: {"wins": 0, "losses": 0, "pushes": 0, "total": 0})

        for pick in picks:
            if pick.get("result") in ["pending", None]:
                continue
            try:
                pick_date = datetime.strptime(pick["date"], "%Y-%m-%d")
                week_key = pick_date.strftime("%Y-W%W")
                res = pick["result"]

                weekly[week_key]["total"] += 1
                if res == "win":
                    weekly[week_key]["wins"] += 1
                elif res == "loss":
                    weekly[week_key]["losses"] += 1
                elif res == "push":
                    weekly[week_key]["pushes"] += 1
            except:
                continue

        # Convert to list and sort
        stats[key] = sorted(
            [{"week": k, **v, "win_rate": round(v["wins"]/v["total"]*100, 1) if v["total"] > 0 else 0}
             for k, v in weekly.items()],
            key=lambda x: x["week"],
            reverse=True
        )[:weeks]

    return stats


def generate_readme_section(stats, groups=MARKET_GROUPS):
    lines = ["\n## Weekly Performance Report\n"]
    lines.append(f"**Last Updated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    for key, name, _ptype, _token in groups:
        lines.append(f"### {name}\n")
        lines.append("| Week | Matches | Wins | Losses | Win Rate |")
        lines.append("|------|---------|------|--------|----------|")

        for entry in stats.get(key, []):
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
