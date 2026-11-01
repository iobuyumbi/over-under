#!/usr/bin/env python3
"""
Smart manual result updater.

Keeps manual_results.csv complete and unambiguous:
  * backfills a blank row for every match we have ever predicted;
  * auto-fills the score for any past-dated row that is still blank -- not just
    the last 7 days, so a result that appears late still lands;
  * marks rows that stay unresolved past a grace window as NOT PLAYED, so a
    fixture that never produced a result is explicit instead of silently empty.

fetch_results.py reads this file as its highest-priority result override.
"""

import csv
import json
import os
from datetime import datetime, timedelta

import requests

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
    for ptype in ("home_win", "over_under", "btts", "oo05"):
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
NOT_PLAYED_MARKER = "NOT PLAYED"
_STALE_DAYS = 14  # a blank row older than this is treated as never played
_POSTPONED_API_STATUSES = frozenset({"PST", "CANC", "ABD", "SUSP", "WO", "AWD", "BT"})

_fixture_cache = {}


def _fixtures_for_date(date):
    """Fixtures for a date (finished + voided), memoised for this run.

    Returns (fixtures, ok). ok is False when every API request for the date
    failed, which lets callers tell "no result yet" apart from "API unreachable".
    Fetching per date rather than per match keeps a backlog of unresolved rows
    down to two API calls per distinct date instead of two per fixture.
    """
    if date in _fixture_cache:
        return _fixture_cache[date]
    fixtures = []
    ok = False
    if API_FOOTBALL_KEY:
        url = f"https://{API_FOOTBALL_HOST}/fixtures"
        headers = {
            "x-rapidapi-key": API_FOOTBALL_KEY,
            "x-rapidapi-host": API_FOOTBALL_HOST,
        }
        for status_param in ("FT", "PST-CANC-ABD-SUSP-WO-AWD-BT"):
            params = {"date": date, "status": status_param}
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
                resp.raise_for_status()
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError, requests.RequestException) as e:
                print(f"  [warn] API fetch failed status={status_param} date={date}: {type(e).__name__}: {e}")
                continue
            try:
                fixtures.extend(resp.json().get("response", []))
                ok = True
            except (ValueError, TypeError) as e:
                print(f"  [warn] API parse error date={date}: {type(e).__name__}: {e}")
    _fixture_cache[date] = (fixtures, ok)
    return _fixture_cache[date]


def fetch_online(date, home, away):
    """Look up one match. Returns a score string, POSTPONED_MARKER, or None."""
    fixtures, _ = _fixtures_for_date(date)
    try:
        for fixture in fixtures:
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
    except (ValueError, KeyError, TypeError) as e:
        print(f"  [warn] fetch_online parse error for {date} {home} vs {away}: {type(e).__name__}: {e}")
    return None


def date_lookup_ok(date):
    """True when the API answered for this date, so a missing score is meaningful."""
    return _fixtures_for_date(date)[1]


def write_csv(csv_data):
    """Write dict back to CSV — sorted, no duplicates."""
    with open(MANUAL_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "home_team", "away_team", "score"])
        for (date, home, away), score in sorted(csv_data.items()):
            writer.writerow([date, home, away, score or ""])


def main():
    print("Smart manual updater — backfill, auto-fill, resolve never-played rows...")
    history = load_prediction_history()

    all_predicted = get_all_predicted_keys(history)
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

    # 2) Every past-dated row still blank is eligible for auto-fill -- not just
    #    the last 7 days, so a result published late still lands in the CSV.
    today = datetime.now().strftime("%Y-%m-%d")
    unresolved = sorted(
        (k for k, v in csv_data.items() if not v and k[0] <= today),
        key=lambda k: k[0],
    )

    found_new = 0
    marked = 0
    if not API_FOOTBALL_KEY:
        print("  [warn] API_FOOTBALL_KEY not set — skipping auto-fill and NOT PLAYED marking.")
    else:
        for date, home, away in unresolved:
            score = fetch_online(date, home, away)
            if score:
                csv_data[(date, home, away)] = score
                found_new += 1
                print(f"  [NEW] {date} {home} vs {away} = {score}")

        # 3) Anything still blank past the grace window never produced a result.
        #    Only rows whose date the API actually answered for are marked, so a
        #    transient outage can't relabel real fixtures as never played.
        stale_cutoff = (datetime.now() - timedelta(days=_STALE_DAYS)).strftime("%Y-%m-%d")
        for key, score in csv_data.items():
            if score or key[0] >= stale_cutoff or not date_lookup_ok(key[0]):
                continue
            csv_data[key] = NOT_PLAYED_MARKER
            marked += 1

    write_csv(csv_data)

    blank = sum(1 for s in csv_data.values() if not s)
    void = sum(1 for s in csv_data.values() if s in (NOT_PLAYED_MARKER, POSTPONED_MARKER))
    print("\nDone.")
    print(f"  Predicted matches:         {len(all_predicted)}")
    if added_rows:
        print(f"  CSV rows added (backfill): {added_rows}")
    print(f"  Blank past rows checked:   {len(unresolved)}")
    print(f"  New scores (from API):     {found_new}")
    print(f"  Marked NOT PLAYED:         {marked}")
    print(f"  Total CSV rows:            {len(csv_data)}")
    print(f"  CSV rows with score:       {len(csv_data) - blank - void}")
    print(f"  CSV rows void:             {void}")
    print(f"  CSV rows still blank:      {blank}")


if __name__ == "__main__":
    main()
