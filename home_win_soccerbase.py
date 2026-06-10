#!/usr/bin/env python3
"""
HOME WIN PREDICTION SYSTEM - MULTI-DAY CONCURRENT ENGINE
=========================================================
Strictly enforces the customized 10-point rule checklist.
Scans up to 4 days ahead if fewer than 10 combined targets are found.
Optimized clean notification UI for Email and Telegram.
"""

import requests
import json
import re
import argparse
import os
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from fake_useragent import UserAgent
from concurrent.futures import ThreadPoolExecutor, as_completed

ua = UserAgent()
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "Connection": "keep-alive",
}

retry_strategy = Retry(total=5, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=20, pool_maxsize=20)
session = requests.Session()
session.mount("https://", adapter)
session.mount("http://", adapter)

def get_random_headers():
    headers = HEADERS.copy()
    headers["User-Agent"] = ua.random
    return headers

def fetch_soccerbase_fixtures(date_str):
    url = f"https://www.soccerbase.com/matches/results.sd?date={date_str}"
    try:
        response = session.get(url, headers=get_random_headers(), timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        matches = []
        current_league = None

        tables = soup.find_all("table", class_="listWithCards")
        for table in tables:
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                league_link = row.find("a", href=lambda h: h and "comp_id=" in h)
                if league_link:
                    current_league = league_link.get_text(strip=True)
                    continue
                elif len(cells) >= 6 and current_league:
                    home_team = cells[3].get_text(strip=True)
                    score_or_v = cells[4].get_text(strip=True)
                    away_team = cells[5].get_text(strip=True)

                    if home_team and away_team:
                        team_links = row.find_all("a", href=lambda h: h and "team_id=" in h)
                        ids = []
                        for link in team_links:
                            m_id = link["href"].split("team_id=")[1].split("&")[0]
                            if m_id not in ids:
                                ids.append(m_id)
                        
                        if len(ids) >= 2:
                            matches.append({
                                "league": current_league,
                                "home": home_team,
                                "away": away_team,
                                "home_team_id": ids[0],
                                "away_team_id": ids[1],
                                "date": date_str,
                                "status": "Scheduled" if score_or_v.lower() == "v" else "Completed"
                            })
        return matches
    except Exception:
        return []

def fetch_soccerbase_team_results(team_id):
    url = f"https://www.soccerbase.com/teams/team.sd?team_id={team_id}&teamTabs=results"
    try:
        response = session.get(url, headers=get_random_headers(), timeout=12)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        matches = []
        tables = soup.find_all("table", class_="soccerGrid")
        for table in tables:
            for row in table.find_all("tr")[2:]:
                cells = row.find_all("td")
                if len(cells) >= 8:
                    date_cell = cells[1]
                    score = cells[4].get_text(strip=True)

                    if "-" in score:
                        try:
                            iso_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(date_cell))
                            match_date = iso_match.group(1) if iso_match else None

                            gf_h, gf_a = map(int, score.split("-"))
                            home_link = cells[3].find("a", href=lambda h: h and "team_id=" in h)
                            if not home_link:
                                continue
                            url_home_id = home_link["href"].split("team_id=")[1].split("&")[0]
                            
                            is_home = (str(url_home_id) == str(team_id))
                            gf = gf_h if is_home else gf_a
                            ga = gf_a if is_home else gf_h
                            result = "W" if gf > ga else "D" if gf == ga else "L"

                            matches.append({
                                "gf": gf, "ga": ga, "is_home": is_home,
                                "result": result, "date_str": match_date
                            })
                        except Exception:
                            continue
                            
        matches.sort(key=lambda x: x["date_str"] if x["date_str"] is not None else "0000-00-00", reverse=True)
        return matches
    except Exception:
        return []

def get_team_form(team_id, is_home=True, num_matches=6, target_date_str=None):
    all_matches = fetch_soccerbase_team_results(team_id)
    form = []
    for match in all_matches:
        if target_date_str and match["date_str"] and match["date_str"] >= target_date_str:
            continue
        if (is_home and match["is_home"]) or (not is_home and not match["is_home"]):
            form.append(match)
            if len(form) >= num_matches:
                break
    return form

def apply_home_win_algorithm(home_form, away_form):
    if len(home_form) < 6 or len(away_form) < 6:
        return None, None, {}, False

    passed, failed, details = [], [], {}

    # Home team checks (H1-H5)
    h_not_lost = sum(1 for m in home_form if m["result"] != "L")
    if h_not_lost >= 5: passed.append("H1"); details["H1"] = f"PASS ({h_not_lost}/6 No Losses)"
    else: failed.append("H1"); details["H1"] = f"FAIL ({h_not_lost}/6)"

    h_gf = sum(m["gf"] for m in home_form)
    if h_gf >= 10: passed.append("H2"); details["H2"] = f"PASS ({h_gf} GF)"
    else: failed.append("H2"); details["H2"] = f"FAIL ({h_gf} GF)"

    h_ga = sum(m["ga"] for m in home_form)
    if h_ga <= 5: passed.append("H3"); details["H3"] = f"PASS ({h_ga} GA)"
    else: failed.append("H3"); details["H3"] = f"FAIL ({h_ga} GA)"

    h_wins = sum(1 for m in home_form if m["result"] == "W")
    if h_wins >= 3: passed.append("H4"); details["H4"] = f"PASS ({h_wins}/6 Wins)"
    else: failed.append("H4"); details["H4"] = f"FAIL ({h_wins}/6)"

    h_last_2 = sum(1 for m in home_form[:2] if m["result"] == "W")
    if h_last_2 == 2: passed.append("H5"); details["H5"] = f"PASS (Won Last 2)"
    else: failed.append("H5"); details["H5"] = f"FAIL (Won {h_last_2}/2)"

    # Away team checks (A1-A5)
    a_losses = sum(1 for m in away_form if m["result"] == "L")
    if a_losses >= 2: passed.append("A1"); details["A1"] = f"PASS ({a_losses}/6 Losses)"
    else: failed.append("A1"); details["A1"] = f"FAIL ({a_losses}/6)"

    a_ga = sum(m["ga"] for m in away_form)
    if a_ga >= 10: passed.append("A2"); details["A2"] = f"PASS ({a_ga} GA)"
    else: failed.append("A2"); details["A2"] = f"FAIL ({a_ga} GA)"

    a_gf = sum(m["gf"] for m in away_form)
    if a_gf <= 5: passed.append("A3"); details["A3"] = f"PASS ({a_gf} GF)"
    else: failed.append("A3"); details["A3"] = f"FAIL ({a_gf} GF)"

    a_wins = sum(1 for m in away_form if m["result"] == "W")
    if a_wins <= 2: passed.append("A4"); details["A4"] = f"PASS ({a_wins}/6 Wins)"
    else: failed.append("A4"); details["A4"] = f"FAIL ({a_wins}/6)"

    a_last_2_no_win = sum(1 for m in away_form[:2] if m["result"] != "W")
    if a_last_2_no_win == 2: passed.append("A5"); details["A5"] = f"PASS (No Win Last 2)"
    else: failed.append("A5"); details["A5"] = f"FAIL (Win tracked)"

    is_perfect = (len(passed) == 10 and h_not_lost == 6)
    return passed, failed, details, is_perfect

def process_single_match(match, date_str):
    try:
        home_form = get_team_form(match["home_team_id"], is_home=True, num_matches=6, target_date_str=date_str)
        away_form = get_team_form(match["away_team_id"], is_home=False, num_matches=6, target_date_str=date_str)

        passed, failed, details, is_perfect = apply_home_win_algorithm(home_form, away_form)
        if passed is None: return {"status": "insufficient"}

        return {
            "status": "success",
            "data": {
                "match": match, "score": len(passed), "failed": failed,
                "details": details, "is_perfect": is_perfect
            }
        }
    except Exception:
        return {"status": "error"}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("date", nargs="?", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--scheduled", action="store_true")
    args = parser.parse_args()

    start_date = datetime.strptime(args.date, "%Y-%m-%d")
    
    all_perfect, all_qualified, all_close_calls, all_pool = [], [], [], []
    total_analyzed, total_insufficient = 0, 0
    scanned_dates = []

    # Multi-day Scanning Evaluation Loop
    for day_offset in range(4):
        current_date_obj = start_date + timedelta(days=day_offset)
        date_str = current_date_obj.strftime("%Y-%m-%d")
        scanned_dates.append(date_str)

        fixtures = fetch_soccerbase_fixtures(date_str)
        seen = set()
        unique_fixtures = []
        for f in fixtures:
            key = (f["home_team_id"], f["away_team_id"], f["league"])
            if key not in seen:
                seen.add(key)
                unique_fixtures.append(f)
        
        if args.scheduled:
            unique_fixtures = [m for m in unique_fixtures if m["status"] == "Scheduled"]

        if not unique_fixtures:
            continue

        day_perfect, day_qualified = [], []
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(process_single_match, m, date_str): m for m in unique_fixtures}
            for future in as_completed(futures):
                res = future.result()
                if res["status"] == "insufficient":
                    total_insufficient += 1
                elif res["status"] == "success":
                    total_analyzed += 1
                    payload = res["data"]
                    score = payload["score"]
                    
                    if score == 10:
                        if payload["is_perfect"]: day_perfect.append(payload)
                        else: day_qualified.append(payload)
                    elif score == 9:
                        all_close_calls.append(payload)
                    elif score == 8:
                        all_pool.append(payload)

        all_perfect.extend(day_perfect)
        all_qualified.extend(day_qualified)

        # Threshold check: break loop if 10 or more secure targets are captured
        if (len(all_perfect) + len(all_qualified)) >= 10:
            break

    # Clean UI Layout Construction
    ui = [
        "🏠 HOME WIN PREDICTIONS REPORT",
        f"📅 Scanned Window: {scanned_dates[0]} to {scanned_dates[-1]} ({len(scanned_dates)} Days)",
        "▪" * 25,
        f"• Matches Analyzed: {total_analyzed} | Skipped Data Profiles: {total_insufficient}",
        f"• Perfect Core (10/10 - Undefeated): {len(all_perfect)}",
        f"• Qualified Core (10/10): {len(all_qualified)}",
        f"• Close Checks (9/10): {len(all_close_calls)}",
        "▪" * 25 + "\n"
    ]

    ui.append("🔥 APEX HOME WIN TARGETS (10/10)")
    combined_top = all_perfect + all_qualified
    if combined_top:
        for idx, item in enumerate(combined_top, 1):
            m = item["match"]
            badge = "⭐ [PERFECT]" if item["is_perfect"] else "✅ [QUALIFIED]"
            ui.append(f"{idx}. {m['date']} | {m['league']}\n   {m['home']} vs {m['away']} {badge}")
    else:
        ui.append("  No direct 10/10 matches qualified across scanned dates.")

    ui.append("\n⚠️ STRONG CONDITIONALS (9/10)")
    if all_close_calls:
        for item in all_close_calls[:15]:  # Caps output sizing comfortably
            m = item["match"]
            ui.append(f"• {m['date']} | {m['home']} vs {m['away']} (Missed: {', '.join(item['failed'])})")
    else:
        ui.append("  None tracked.")

    ui.append("\n📊 PIPELINE FUNNEL WATCHLIST (8/10)")
    if all_pool:
        for item in all_pool[:15]:
            m = item["match"]
            ui.append(f"• {m['date']} | {m['home']} vs {m['away']} [{item['score']}/10]")
    else:
        ui.append("  None tracked.")

    print("\n===EMAIL_START===\n" + "\n".join(ui) + "\n===EMAIL_END===")

    with open(f"home_win_predictions_{scanned_dates[0]}.json", "w") as out:
        json.dump({"scanned_window": scanned_dates, "perfect": all_perfect, "qualified": all_qualified, "close_calls": all_close_calls}, out, indent=2, default=str)

if __name__ == "__main__":
    main()