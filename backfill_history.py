#!/usr/bin/env python3
"""
Clean up prediction_history.json:
- Remove duplicate entries from re-runs
- Backfill match dates from saved reports or Soccerbase
- Optionally refresh results for corrected dates
"""

import argparse
import glob
import json
import re
from datetime import datetime, timedelta

from fetch_results import find_match_date, update_history_with_results
from prediction_tracker import (
    dedupe_history,
    load_history,
    save_history,
    over_under_key,
    home_win_key,
)


def load_dates_from_report_json():
    """Build lookup tables from saved predictor JSON reports."""
    home_win_dates = {}
    over_under_dates = {}

    for path in glob.glob("home_win_report_*.json"):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for section in ("perfect", "qualified", "close_calls"):
            for item in data.get(section, []):
                match = item.get("match", {})
                home = match.get("home")
                away = match.get("away")
                date = match.get("date")
                if not home or not away or not date:
                    continue
                if item in data.get("perfect", []):
                    confidence = "perfect"
                elif item in data.get("qualified", []):
                    confidence = "qualified"
                else:
                    confidence = "close"
                home_win_dates[(home, away, confidence)] = date

    for path in glob.glob("over_under_25_report_*.json"):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for side in ("over", "under"):
            for bucket in ("perfect", "qualified", "close"):
                for item in data.get(side, {}).get(bucket, []):
                    match = item.get("match", {})
                    home = match.get("home")
                    away = match.get("away")
                    date = match.get("date")
                    if not home or not away or not date:
                        continue
                    prediction = "over" if side == "over" else "under"
                    confidence = bucket if bucket != "close" else "close"
                    over_under_dates[(home, away, prediction, confidence)] = date

    return home_win_dates, over_under_dates


def load_dates_from_vip_reports():
    """Parse match dates from VIP text reports."""
    home_win_dates = {}
    over_under_dates = {}
    line_re = re.compile(r"^\d+\.\s+(.+?)\s+vs\s+(.+?)\s+\((\d{4}-\d{2}-\d{2})\)")

    for path in glob.glob("home_win_vip_report_*.txt"):
        with open(path, encoding="utf-8") as f:
            for line in f:
                match = line_re.match(line.strip())
                if match:
                    home, away, date = match.groups()
                    home_win_dates[(home, away)] = date

    for path in glob.glob("over_under_vip_report_*.txt"):
        with open(path, encoding="utf-8") as f:
            for line in f:
                match = line_re.match(line.strip())
                if match:
                    home, away, date = match.groups()
                    over_under_dates[(home, away)] = date

    return home_win_dates, over_under_dates


def _history_date_range(history):
    dates = []
    for pick in history["home_win"] + history["over_under"]:
        dates.append(datetime.fromisoformat(pick["date"]))
        if pick.get("recorded_at"):
            dates.append(datetime.fromisoformat(pick["recorded_at"][:10]))
    if not dates:
        today = datetime.now()
        return today - timedelta(days=7), today + timedelta(days=7)
    return min(dates), max(dates) + timedelta(days=7)


def backfill_dates(history, use_online=True):
    """Correct stored match dates using local reports and Soccerbase lookups."""
    report_hw, report_ou = load_dates_from_report_json()
    vip_hw, vip_ou = load_dates_from_vip_reports()
    start_date, end_date = _history_date_range(history)
    updated = 0

    for pick in history["home_win"]:
        new_date = report_hw.get((pick["home_team"], pick["away_team"], pick["confidence"]))
        if not new_date:
            new_date = vip_hw.get((pick["home_team"], pick["away_team"]))
        if not new_date and use_online:
            new_date = find_match_date(
                pick["home_team"], pick["away_team"], start_date, end_date
            )
        if new_date and new_date != pick["date"]:
            pick["date"] = new_date
            updated += 1

    for pick in history["over_under"]:
        new_date = report_ou.get(
            (pick["home_team"], pick["away_team"], pick["prediction"], pick["confidence"])
        )
        if not new_date:
            new_date = vip_ou.get((pick["home_team"], pick["away_team"]))
        if not new_date and use_online:
            new_date = find_match_date(
                pick["home_team"], pick["away_team"], start_date, end_date
            )
        if new_date and new_date != pick["date"]:
            pick["date"] = new_date
            updated += 1

    if updated:
        history["home_win"] = dedupe_predictions_after_date_fix(history["home_win"], home_win_key)
        history["over_under"] = dedupe_predictions_after_date_fix(
            history["over_under"], over_under_key
        )

    return updated


def dedupe_predictions_after_date_fix(picks, key_fn):
    best = {}
    for pick in picks:
        key = key_fn(pick)
        if key not in best:
            best[key] = pick
    return list(best.values())


def refresh_results(history):
    """Fetch Soccerbase results for every date present in history."""
    dates = sorted({pick["date"] for pick in history["home_win"] + history["over_under"]})
    total = 0
    for date_str in dates:
        total += update_history_with_results(date_str) or 0
    return total


def main():
    parser = argparse.ArgumentParser(description="Clean and backfill prediction history.")
    parser.add_argument("--skip-online", action="store_true", help="Do not query Soccerbase.")
    parser.add_argument("--skip-results", action="store_true", help="Do not refresh match results.")
    args = parser.parse_args()

    history, dedupe_stats = dedupe_history(save=False)
    print(
        "Deduped history: "
        f"-{dedupe_stats['home_win_removed']} home win, "
        f"-{dedupe_stats['over_under_removed']} over/under "
        f"({dedupe_stats['home_win_remaining']} home win, "
        f"{dedupe_stats['over_under_remaining']} over/under remaining)"
    )

    dates_updated = backfill_dates(history, use_online=not args.skip_online)
    print(f"Updated dates on {dates_updated} picks")

    save_history(history)

    if not args.skip_results and not args.skip_online:
        refreshed = refresh_results(load_history())
        print(f"Refreshed results on {refreshed} picks")


if __name__ == "__main__":
    main()
