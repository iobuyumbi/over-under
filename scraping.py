#!/usr/bin/env python3
"""
Shared Soccerbase scraping and thin-data helpers.

This is the second phase of deduplication (utils.py handled the generic
HTTP/cache/date/staking layer). This module holds the Soccerbase-specific
HTML parsing that all three predictors used to carry their own copy of:
fetch_soccerbase_fixtures, fetch_soccerbase_team_results,
get_team_overall_form, and the _thin_count/_thin_total proportional
threshold helpers.

Each engine still owns its own `fetch()` (bound to its own session/cache/
delay config from utils.py) and passes it in here — this module has no
opinion on caching, only on parsing. That keeps each predictor's cache
fully independent (soccerbase_cache.db / _home.db / _btts.db), which is
deliberate: see the note in IMPROVEMENT_REPORT.md about NOT merging the
three caches in this pass, since the daily workflow may run them with
overlapping schedules and merging cache write access needs its own
concurrency review first.
"""

import re
import logging

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _thin_count(needed, of_window, available):
    """Scale a 'need N of window' count threshold to available samples.

    Exact thresholds when available >= of_window; same pass-rate otherwise
    (integer floor, minimum 1). Used by early-season thin-data rules.
    """
    if available >= of_window:
        return needed
    return max(1, int(needed * available / of_window))


def _thin_total(goal_sum, of_window, available):
    """Scale a cumulative goal-total threshold to available samples."""
    if available >= of_window:
        return goal_sum
    return max(1, int(goal_sum * available / of_window))


def fetch_soccerbase_fixtures(date_str, fetch_fn):
    """Scrape the fixtures/results list for a given date.

    fetch_fn: the calling engine's own fetch(url) wrapper (bound to its
    session/cache), so each engine keeps its own caching behavior.
    """
    url = f"https://www.soccerbase.com/matches/results.sd?date={date_str}"
    html = fetch_fn(url)
    if html is None:
        return []

    soup = BeautifulSoup(html, "html.parser")
    matches = []

    tables = soup.find_all("table", class_="listWithCards")
    if not tables:
        logger.warning("No fixture tables found for %s", date_str)
        return matches

    for table in tables:
        current_league = "Unknown League"
        for row in table.find_all("tr"):
            league_link = row.find("a", href=lambda h: h and "comp_id=" in h)
            if league_link:
                current_league = league_link.get_text(strip=True)
                continue

            cells = row.find_all("td")
            if len(cells) < 6:
                continue

            home_raw = cells[3].get_text(strip=True)
            score_or_v = cells[4].get_text(strip=True)
            away_raw = cells[5].get_text(strip=True)

            home = re.sub(r"\s*\d+.*$", "", home_raw).strip()
            away = re.sub(r"\s*\d+.*$", "", away_raw).strip()

            if not home or not away:
                continue

            team_links = row.find_all("a", href=lambda h: h and "team_id=" in h)
            if len(team_links) < 2:
                continue

            try:
                home_id = team_links[0]["href"].split("team_id=")[1].split("&")[0]
                away_id = team_links[1]["href"].split("team_id=")[1].split("&")[0]
            except (KeyError, IndexError):
                continue

            matches.append({
                "league": current_league,
                "home": home,
                "away": away,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "date": date_str,
                "status": "Scheduled" if score_or_v.lower() == "v" else "Completed",
            })

    return matches


def fetch_soccerbase_team_results(team_id, fetch_fn):
    """Scrape a team's recent results.

    Returns dicts with gf/ga/total/is_home/result/date_str/opponent_team_id.
    "total" and "result" are included for every engine (a harmless extra
    key for over25/btts, which only read gf/ga/is_home/date_str/
    opponent_team_id) so this single implementation can serve all three
    predictors without behavior changes to any of them.
    """
    url = f"https://www.soccerbase.com/teams/team.sd?team_id={team_id}&teamTabs=results"
    html = fetch_fn(url)
    if html is None:
        return []

    soup = BeautifulSoup(html, "html.parser")
    matches = []

    for table in soup.find_all("table", class_="soccerGrid"):
        for row in table.find_all("tr")[2:]:
            cells = row.find_all("td")
            if len(cells) < 8:
                continue

            score = cells[4].get_text(strip=True)
            if "-" not in score:
                continue

            try:
                gf_h, gf_a = map(int, score.split("-"))

                home_link = cells[3].find("a", href=lambda h: h and "team_id=" in h)
                away_link = cells[5].find("a", href=lambda h: h and "team_id=" in h)
                if not home_link:
                    continue

                home_id_in_row = home_link["href"].split("team_id=")[1].split("&")[0]
                away_id_in_row = None
                if away_link:
                    away_id_in_row = away_link["href"].split("team_id=")[1].split("&")[0]

                is_home = str(home_id_in_row) == str(team_id)
                opponent_team_id = away_id_in_row if is_home else home_id_in_row
                gf = gf_h if is_home else gf_a
                ga = gf_a if is_home else gf_h
                result = "W" if gf > ga else "D" if gf == ga else "L"

                date_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(cells[1]))
                date_str = date_match.group(1) if date_match else None

                matches.append({
                    "gf": gf,
                    "ga": ga,
                    "total": gf + ga,
                    "is_home": is_home,
                    "result": result,
                    "date_str": date_str,
                    "opponent_team_id": opponent_team_id,
                })
            except (ValueError, KeyError, IndexError):
                continue

    matches.sort(key=lambda x: x.get("date_str") or "0000-00-00", reverse=True)
    return matches


def get_team_form(team_id, results_fetcher, is_home=True, num_matches=6, target_date_str=None, parse_date_fn=None):
    """Last N venue-specific (home or away) matches as (gf, ga) tuples."""
    all_matches = results_fetcher(team_id)
    target_dt = parse_date_fn(target_date_str) if (parse_date_fn and target_date_str) else None
    form = []

    for match in all_matches:
        match_dt = parse_date_fn(match.get("date_str")) if parse_date_fn else None
        if target_dt and match_dt and match_dt >= target_dt:
            continue
        if match["is_home"] == is_home:
            form.append((match["gf"], match["ga"]))
            if len(form) >= num_matches:
                break

    return form


def get_team_overall_form(team_id, results_fetcher, num_matches=6, target_date_str=None, parse_date_fn=None):
    """Last N matches home or away combined, as (gf, ga) tuples."""
    all_matches = results_fetcher(team_id)
    target_dt = parse_date_fn(target_date_str) if (parse_date_fn and target_date_str) else None
    form = []

    for match in all_matches:
        match_dt = parse_date_fn(match.get("date_str")) if parse_date_fn else None
        if target_dt and match_dt and match_dt >= target_dt:
            continue
        form.append((match["gf"], match["ga"]))
        if len(form) >= num_matches:
            break

    return form
