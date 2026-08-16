#!/usr/bin/env python3
"""
DAILY AUTOMATED RUNNER
======================
Run all three predictors locally (Over/Under, BTTS, Home Win).
Can be scheduled via cron (Linux/Mac) or Task Scheduler (Windows).
"""

import subprocess
import sys
from datetime import datetime


PREDICTORS = (
    ("over25_soccerbase.py", "Over/Under 2.5"),
    ("btts_soccerbase.py", "BTTS Yes/No"),
    ("home_win_soccerbase.py", "Home Win"),
)


def run_predictor(script_name, label):
    print(f"\n{'=' * 60}")
    print(f"{label} - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 60}")
    result = subprocess.run([sys.executable, script_name], capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print("ERRORS:", result.stderr)
    return result.returncode


def main():
    print(f"\nDAILY PREDICTIONS - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    failures = []
    for script, label in PREDICTORS:
        code = run_predictor(script, label)
        if code != 0:
            failures.append(label)
    if failures:
        print(f"\nCompleted with errors: {', '.join(failures)}")
        raise SystemExit(1)
    print("\nAll predictors finished successfully.")


if __name__ == "__main__":
    main()
