#!/usr/bin/env python3
"""
DAILY AUTOMATED RUNNER
======================
Run this every morning to get predictions.
Can be scheduled via cron (Linux/Mac) or Task Scheduler (Windows).

LINUX/MAC CRON SETUP:
---------------------
1. Open terminal: crontab -e
2. Add line for 8:00 AM daily:
   0 8 * * * cd /path/to/script && python3 daily_runner.py >> output.log 2>&1
3. Save and exit

WINDOWS TASK SCHEDULER:
-----------------------
1. Open Task Scheduler
2. Create Basic Task → Daily → Time: 8:00 AM
3. Action: Start Program
4. Program: python3 (or full path)
5. Arguments: daily_runner.py
6. Start in: /path/to/script/folder

DOCKER OPTION:
--------------
See Dockerfile below for containerized daily execution.
"""

import subprocess
import sys
import os
from datetime import datetime
import json

def run_predictions():
    """Execute the main predictor"""
    print(f"\n{'='*60}")
    print(f"DAILY OVER 2.5 PREDICTIONS - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    # Run the main predictor
    result = subprocess.run(
        [sys.executable, 'over25_predictor.py'],
        capture_output=True,
        text=True
    )

    print(result.stdout)
    if result.stderr:
        print("ERRORS:", result.stderr)

    # Check if we got any qualified matches
    today = datetime.now().strftime('%Y-%m-%d')
    filename = f"predictions_{today}.json"

    try:
        with open(filename, 'r') as f:
            data = json.load(f)

        qualified_count = len(data.get('qualified', []))

        # Optional: Send notification if matches found
        if qualified_count > 0:
            send_notification(qualified_count, data['qualified'])

    except FileNotFoundError:
        print("⚠️  No output file generated")

def send_notification(count, matches):
    """Send desktop notification (Linux/Mac)"""
    try:
        import platform
        system = platform.system()

        if system == "Linux":
            # Requires: sudo apt install libnotify-bin
            import subprocess
            msg = f"{count} Over 2.5 matches found today!"
            subprocess.run(['notify-send', 'Football Predictions', msg])

        elif system == "Darwin":  # macOS
            import subprocess
            msg = f"{count} Over 2.5 matches found today!"
            script = f'display notification "{msg}" with title "Football Predictions"'
            subprocess.run(['osascript', '-e', script])

        elif system == "Windows":
            # Requires: pip install win10toast
            try:
                from win10toast import ToastNotifier
                toaster = ToastNotifier()
                toaster.show_toast(
                    "Football Predictions",
                    f"{count} Over 2.5 matches found!",
                    duration=10
                )
            except ImportError:
                print("Install win10toast for Windows notifications: pip install win10toast")

    except Exception as e:
        print(f"Notification failed: {e}")

if __name__ == "__main__":
    run_predictions()
