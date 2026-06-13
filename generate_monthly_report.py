#!/usr/bin/env python3
"""
Monthly Report Generator
Generates performance reports for the past month with win/loss tracking.
"""

import os
import json
import glob
from datetime import datetime, timedelta
from prediction_tracker import generate_monthly_report, load_history

def main():
    """Main function - generate report for last month."""
    now = datetime.now()
    if now.month == 1:
        report_year = now.year - 1
        report_month = 12
    else:
        report_year = now.year
        report_month = now.month - 1
    
    print(f"Generating monthly report for {report_year}-{report_month:02d}...")
    
    # Generate report using prediction tracker
    report_text, report_data = generate_monthly_report(report_year, report_month)
    
    # Save JSON report
    json_filename = f"monthly_report_{report_year}-{report_month:02d}.json"
    with open(json_filename, "w") as f:
        json.dump(report_data, f, indent=2, default=str)
    print(f"JSON report saved to {json_filename}")
    
    # Save text report
    text_filename = f"monthly_report_{report_year}-{report_month:02d}.txt"
    with open(text_filename, "w") as f:
        f.write(report_text)
    print(f"Text report saved to {text_filename}")
    
    print("\nMonthly Performance Report:")
    print(report_text)

if __name__ == "__main__":
    main()
