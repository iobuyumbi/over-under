#!/usr/bin/env python3
"""
Weekly Report Generator
Generates performance reports for the last 7 days.
"""

import json
from datetime import datetime

from prediction_tracker import generate_weekly_report


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"Generating weekly report ending {today}...")

    report_text, report_data = generate_weekly_report()

    json_filename = f"weekly_report_{today}.json"
    with open(json_filename, "w") as f:
        json.dump(report_data, f, indent=2, default=str)
    print(f"JSON report saved to {json_filename}")

    text_filename = f"weekly_report_{today}.txt"
    with open(text_filename, "w") as f:
        f.write(report_text)
    print(f"Text report saved to {text_filename}")

    print("\nWeekly Performance Report:")
    print(report_text)


if __name__ == "__main__":
    main()
