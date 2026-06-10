#!/usr/bin/env python3
"""
OVER 2.5 GOALS PREDICTOR - MULTI-DAY CONCURRENT ENGINE
======================================================
Processes matches across up to 4 sequential calendar days.
Halts early when 10 or more apex high-probability selections are saved.
Clean layout generation tailored for cross-platform reading.
"""

import requests
import json
import re
import argparse
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
                            total_goals = gf + ga

                            matches.append({
                                "gf": gf, "ga": ga, "total": total_goals,
                                "is_home": is_home, "date_str": match_date
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

def evaluate_over25_algorithm(home_form, away_form):
    if len(home_form) < 6 or len(away_form) < 6:
        return None

    home_over_count = sum(1 for m in home_form if m["total"] > 2)
    away_over_count = sum(1 for m in away_form if m["total"] > 2)
    
    total_home_goals = sum(m["total"] for m in home_form)
    total_away_goals = sum(m["total"] for m in away_form)
    
    avg_home = total_home_goals / 6.0
    avg_away = total_away_goals / 6.0
    combined_avg = (total_home_goals + total_away_goals) / 12.0

    score = 0
    checks = {}

    if home_over_count >= 4: score += 2; checks["H_O25"] = f"PASS ({home_over_count}/6)"
    else: checks["H_O25"] = f"FAIL ({home_over_count}/6)"

    if away_over_count >= 4: score += 2; checks["A_O25"] = f"PASS ({away_over_count}/6)"
    else: checks["A_O25"] = f"FAIL ({away_over_count}/6)"

    if avg_home >= 2.5: score += 2; checks["H_AVG"] = f"PASS ({avg_home:.2f})"
    else: checks["H_AVG"] = f"FAIL ({avg_home:.2f})"

    if avg_away >= 2.5: score += 2; checks["A_AVG"] = f"PASS ({avg_away:.2f})"
    else: checks["A_AVG"] = f"FAIL ({avg_away:.2f})"

    if combined_avg >= 2.7: score += 2; checks["COMBINED_AVG"] = f"PASS ({combined_avg:.2f})"
    else: checks["COMBINED_AVG"] = f"FAIL ({combined_avg:.2f})"

    return {
        "score": score, "checks": checks,
        "metrics": {
            "home_o25": home_over_count, "away_o25": away_over_count,
            "avg_home": avg_home, "avg_away": avg_away, "combined_avg": combined_avg
        }
    }

def process_single_match(match, date_str):
    try:
        home_form = get_team_form(match["home_team_id"], is_home=True, num_matches=6, target_date_str=date_str)
        away_form = get_team_form(match["away_team_id"], is_home=False, num_matches=6, target_date_str=date_str)

        analysis = evaluate_over25_algorithm(home_form, away_form)
        if not analysis: return {"status": "insufficient"}

        return {
            "status": "success",
            "data": {
                "match": match, "score": analysis["score"],
                "checks": analysis["checks"], "metrics": analysis["metrics"]
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
    
    all_tier_10, all_tier_8, all_tier_6 = [], [], []
    total_analyzed, total_insufficient = 0, 0
    scanned_dates = []

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

        day_tier_10 = []

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(process_single_match, match, date_str): match for match in unique_fixtures}
            for future in as_completed(futures):
                res = future.result()
                if res["status"] == "insufficient":
                    total_insufficient += 1
                elif res["status"] == "success":
                    total_analyzed += 1
                    payload = res["data"]
                    score = payload["score"]
                    
                    if score == 10: day_tier_10.append(payload)
                    elif score == 8: all_tier_8.append(payload)
                    elif score == 6: all_tier_6.append(payload)

        all_tier_10.extend(day_tier_10)

        # Threshold check: stop scanning forward if 10 or more high-probability targets accumulate
        if len(all_tier_10) >= 10:
            break

    # Format output for clean email / Telegram consumption
    report = [
        "🔥 OVER 2.5 GOALS PREDICTIONS REPORT",
        f"📅 Scanned Window: {scanned_dates[0]} to {scanned_dates[-1]} ({len(scanned_dates)} Days)",
        "▪" * 25,
        f"• Matches Analyzed: {total_analyzed} | Skipped Data Profiles: {total_insufficient}",
        f"• Apex Targets (10/10 Score): {len(all_tier_10)}",
        f"• High Candidates (8/10 Score): {len(all_tier_8)}",
        "▪" * 25 + "\n"
    ]

    report.append("⭐ APEX GOAL TARGETS (10/10)")
    if all_tier_10:
        for idx, item in enumerate(all_tier_10, 1):
            m = item["match"]
            met = item["metrics"]
            report.append(f"{idx}. {m['date']} | {m['league']}\n   {m['home']} vs {m['away']} ⚽ (Comb: {met['combined_avg']:.2f}/gm)")
    else:
        report.append("  No direct 10/10 matches qualified across scanned dates.")

    report.append("\n✅ PROBABLE HIGH-YIELD CANDIDATES (8/10)")
    if all_tier_8:
        for item in all_tier_8[:15]:
            m = item["match"]
            met = item["metrics"]
            report.append(f"• {m['date']} | {m['home']} vs {m['away']} (Comb: {met['combined_avg']:.2f}/gm)")
    else:
        report.append("  None tracked.")

    report.append("\n📊 CONTEXTUAL WATCHLIST (6/10)")
    if all_tier_6:
        for item in all_tier_6[:15]:
            m = item["match"]
            report.append(f"• {m['date']} | {m['home']} vs {m['away']}")
    else:
        report.append("  None tracked.")

    print("\n===EMAIL_START===\n" + "\n".join(report) + "\n===EMAIL_END===")

    with open(f"predictions_soccerbase_{scanned_dates[0]}.json", "w") as out:
        json.dump({"scanned_window": scanned_dates, "apex_10_10": all_tier_10, "strong_8_10": all_tier_8}, out, indent=2, default=str)

if __name__ == "__main__":
    main()