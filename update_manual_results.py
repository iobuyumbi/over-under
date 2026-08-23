#!/usr/bin/env python3
"""
Smart manual result updater.
Only checks RECENT pending predictions (last 7 days).
Merges new scores into existing CSV without deleting old entries.
Also ensures every predicted match has a row in the CSV so
fetch_results.py can short-circuit lookups against the local file.
"""

import csv
import json
import os
from datetime import datetime, timedelta

API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")
API_FOOTBALL_HOST = "v3.football.api-sports.io"
MANUAL_CSV = "manual_results.csv"
HISTORY = "prediction_history.json"


def load_prediction_history():
    if os.path.exists(HISTORY):
        try:
            with open(HISTORY, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading {HISTORY}: {e}")
    return {"home_win": [], "over_under": [], "btts": []}


def get_all_predicted_keys(history):
    """All matches we've ever predicted — used to ensure CSV completeness."""
    keys = {}
    for ptype in ("home_win", "over_under", "btts"):
        for pick in history.get(ptype, []):
            date = pick.get("date", "")
            home = pick.get("home_team", pick.get("home", "")).strip()
            away = pick.get("away_team", pick.get("away", "")).strip()
            if not (date and home and away):
                continue
            key = (date, home, away)
            final_score = (pick.get("final_score") or "").strip()
            if key not in keys:
                keys[key] = final_score
            elif final_score and not keys[key]:
                keys[key] = final_score
    return keys


def get_pending_recent(history, days_back=7):
    """Only get PENDING predictions from last N days — these are the only ones we'll fetch online."""
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    matches = set()
    for ptype in ("home_win", "over_under", "btts"):
        for pick in history.get(ptype, []):
            if pick.get("result") != "pending":
                continue
            date = pick.get("date", "")
            if not date or date < cutoff:
                continue
            home = pick.get("home_team", pick.get("home", "")).strip()
            away = pick.get("away_team", pick.get("away", "")).strip()
            if home and away:
                matches.add((date, home, away))
    return sorted(list(matches), key=lambda x: x[0])


def load_csv_dict():
    """Load CSV into dict keyed by (date, home, away)."""
    data = {}
    if not os.path.exists(MANUAL_CSV):
        return data
    with open(MANUAL_CSV, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row.get("date", ""), row.get("home_team", ""), row.get("away_team", ""))
            if all(key):
                score = (row.get("score") or "").strip()
                if key not in data or (score and not data[key]):
                    data[key] = score
    return data


POSTPONED_MARKER = "POSTPONED"
_POSTPONED_API_STATUSES = frozenset({"PST", "CANC", "ABD", "SUSP", "WO", "AWD", "BT"})


def fetch_online(date, home, away):
    """Fast API-Football lookup for a single match. Returns score string or POSTPONED_MARKER."""
    if not API_FOOTBALL_KEY:
        return None
    try:
        import requests
        url = f"https://{API_FOOTBALL_HOST}/fixtures"
        headers = {
            "x-rapidapi-key": API_FOOTBALL_KEY,
            "x-rapidapi-host": API_FOOTBALL_HOST,
        }
        # FT first (most matches), then any voided/postponed statuses
        for status_param in ("FT", "PST-CANC-ABD-SUSP-WO-AWD-BT"):
            params = {"date": date, "status": status_param}
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
                resp.raise_for_status()
            except Exception:
                continue
            data = resp.json()
            for fixture in data.get("response", []):
                status_short = (fixture.get("fixture", {}).get("status", {}) or {}).get("short", "")
                api_home = fixture["teams"]["home"]["name"]
                api_away = fixture["teams"]["away"]["name"]
                if (home.lower() in api_home.lower() or api_home.lower() in home.lower()) and \
                   (away.lower() in api_away.lower() or api_away.lower() in away.lower()):
                    if status_short in _POSTPONED_API_STATUSES:
                        return POSTPONED_MARKER
                    gh = fixture["goals"]["home"]
                    ga = fixture["goals"]["away"]
                    if gh is not None and ga is not None:
                        return f"{gh}-{ga}"
    except Exception:
        pass
    return None


def write_csv(csv_data):
    """Write dict back to CSV — sorted, no duplicates."""
    with open(MANUAL_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "home_team", "away_team", "score"])
        for (date, home, away), score in sorted(csv_data.items()):
            writer.writerow([date, home, away, score or ""])


def main():
    print("Smart manual updater — only recent pending predictions...")
    history = load_prediction_history()

    all_predicted = get_all_predicted_keys(history)
    pending = get_pending_recent(history, days_back=7)
    csv_data = load_csv_dict()

    # 1) Make sure every predicted match has a CSV row (with a blank score if missing)
    added_rows = 0
    for key, history_score in all_predicted.items():
        if key not in csv_data:
            csv_data[key] = history_score or ""
            added_rows += 1
        elif history_score and not csv_data[key]:
            csv_data[key] = history_score
            added_rows += 1

    # 2) Now check only the pending-recent set online
    found_new = 0
    skipped_has_score = 0
    failed = 0

    for date, home, away in pending:
        key = (date, home, away)
        if key in csv_data and csv_data[key]:
            skipped_has_score += 1
            continue

        score = fetch_online(date, home, away)
        if score:
            csv_data[key] = score
            found_new += 1
            print(f"  [NEW] {date} {home} vs {away} = {score}")
        else:
            failed += 1

    write_csv(csv_data)

    with_score = sum(1 for s in csv_data.values() if s)
    print(f"\nDone.")
    print(f"  Predicted matches:         {len(all_predicted)}")
    if added_rows:
        print(f"  CSV rows added (backfill): {added_rows}")
    print(f"  Pending checked (7d):      {len(pending)}")
    print(f"  Already scored in CSV:     {skipped_has_score}")
    print(f"  New scores (from API):     {found_new}")
    print(f"  Still missing:             {failed}")
    print(f"  Total CSV rows:            {len(csv_data)}")
    print(f"  CSV rows with score:       {with_score}")
    print(f"  CSV rows pending score:    {len(csv_data) - with_score}")


if __name__ == "__main__":
    main()
