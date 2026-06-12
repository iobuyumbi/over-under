#!/usr/bin/env python3
"""
Monthly Report Generator
Generates performance reports for the past month
"""

import os
import json
import glob
from datetime import datetime, timedelta
from collections import defaultdict


def load_predictions(date_str, prediction_type='home_win'):
    """Load prediction files for a given date"""
    if prediction_type == 'home_win':
        pattern = f"home_win_predictions_{date_str}.json"
    else:
        pattern = f"predictions_soccerbase_{date_str}.json"
    
    files = glob.glob(pattern)
    if files:
        try:
            with open(files[0], 'r') as f:
                return json.load(f)
        except:
            return None
    return None


def generate_monthly_report(year, month):
    """Generate a monthly performance report"""
    report = {
        'month': f"{year}-{month:02d}",
        'generated_at': datetime.now().isoformat(),
        'home_win': {
            'total_picks': 0,
            'perfect_picks': 0,
            'good_picks': 0,
            'decent_picks': 0
        },
        'over_under': {
            'total_picks': 0,
            'perfect_picks': 0,
            'good_picks': 0,
            'decent_picks': 0
        }
    }
    
    # Get all dates in the month
    first_day = datetime(year, month, 1)
    if month == 12:
        last_day = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = datetime(year, month + 1, 1) - timedelta(days=1)
    
    current_day = first_day
    while current_day <= last_day:
        date_str = current_day.strftime("%Y-%m-%d")
        
        # Load home win predictions
        hw_preds = load_predictions(date_str, 'home_win')
        if hw_preds:
            report['home_win']['total_picks'] += 1
            if hw_preds.get('perfect'):
                report['home_win']['perfect_picks'] += 1
            if hw_preds.get('qualified'):
                report['home_win']['good_picks'] += 1
            if hw_preds.get('close_calls'):
                report['home_win']['decent_picks'] += 1
        
        # Load over/under predictions
        ou_preds = load_predictions(date_str, 'over_under')
        if ou_preds:
            report['over_under']['total_picks'] += 1
            if ou_preds.get('over', {}).get('perfect'):
                report['over_under']['perfect_picks'] += 1
            if ou_preds.get('over', {}).get('qualified'):
                report['over_under']['good_picks'] += 1
            if ou_preds.get('over', {}).get('close'):
                report['over_under']['decent_picks'] += 1
        
        current_day += timedelta(days=1)
    
    return report


def format_report_text(report):
    """Format the report as readable text for Telegram"""
    month_name = datetime.strptime(report['month'], "%Y-%m").strftime("%B %Y")
    
    lines = [
        "=" * 40,
        f"📊 MONTHLY PERFORMANCE REPORT - {month_name}",
        "=" * 40,
        "",
        "🏠 HOME WIN PREDICTIONS",
        "-" * 40,
        f"  Total days with picks: {report['home_win']['total_picks']}",
        f"  Perfect picks days: {report['home_win']['perfect_picks']}",
        f"  Good picks days: {report['home_win']['good_picks']}",
        f"  Decent picks days: {report['home_win']['decent_picks']}",
        "",
        "🔥 OVER/UNDER 2.5 GOALS",
        "-" * 40,
        f"  Total days with picks: {report['over_under']['total_picks']}",
        f"  Perfect picks days: {report['over_under']['perfect_picks']}",
        f"  Good picks days: {report['over_under']['good_picks']}",
        f"  Decent picks days: {report['over_under']['decent_picks']}",
        "",
        "=" * 40,
        "⚠️ DISCLAIMER: Past performance doesn't guarantee future results.",
        "   Gamble responsibly and within your means.",
        "=" * 40,
        "",
        "💡 Support our free service by registering using our affiliate link below!",
        "🔗 Your Bookmaker Link Here"
    ]
    
    return "\n".join(lines)


def main():
    """Main function - generate report for last month"""
    now = datetime.now()
    if now.month == 1:
        report_year = now.year - 1
        report_month = 12
    else:
        report_year = now.year
        report_month = now.month - 1
    
    print(f"Generating monthly report for {report_year}-{report_month:02d}...")
    
    report = generate_monthly_report(report_year, report_month)
    
    # Save JSON report
    json_filename = f"monthly_report_{report_year}-{report_month:02d}.json"
    with open(json_filename, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(f"  JSON report saved to {json_filename}")
    
    # Save text report
    text_report = format_report_text(report)
    text_filename = f"monthly_report_{report_year}-{report_month:02d}.txt"
    with open(text_filename, 'w') as f:
        f.write(text_report)
    print(f"  Text report saved to {text_filename}")
    
    print("\nMonthly report generated successfully!")


if __name__ == "__main__":
    main()
