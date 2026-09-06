#!/usr/bin/env python3
"""
OVER 0.5 TEAM GOAL PREDICTOR - PRODUCTION v1
================================================
Bet: "At least one team will score 1+ goal" — i.e. NOT a 0-0 draw.

Core thesis (from totalcorner.com featured):
  1. SCORING STREAK — a side has scored in >= 10 CONSECUTIVE matches
     (venue-specific first, overall as fallback). If a team hasn't
     blanked in 10+ games straight, a 0-0 is extremely unlikely
     unless the opponent is an elite defence.
  2. OPPONENT LEAKY DEFENCE — the OTHER side has kept a clean sheet
     in <= 1 of their last 6 matches (venue-specific). They concede
     goals easily, so the streaking team will almost certainly score.
  3. FAVOURABLE CONFIRMERS (boost score / tier premium):
     - Both teams scored in recent overall form
     - Combined goals-per-game floor (both sides total >= 1.5 gpg venue)
     - No H2H 0-0 bogey pattern in last 4 meetings
     - Away side has scored on their travels consistently
     - Team on scoring streak is NOT on a recent 0-goal cold shock
"""

import json
import argparse
import sys
import math
import logging
import os
from datetime import datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils import (
    Cache,
    build_session,
    fetch as _shared_fetch,
    parse_date,
    calculate_kelly,
    apply_portfolio_kelly,
    is_weak_roi_league as _shared_is_weak_roi_league,
    poisson_pmf as _shared_poisson_pmf,
)

from scraping import (
    fetch_soccerbase_fixtures as _shared_fetch_fixtures,
    fetch_soccerbase_team_results as _shared_fetch_team_results,
    get_team_form as _shared_get_team_form,
    get_team_overall_form as _shared_get_team_overall_form,
    get_h2h_meetings as _shared_get_h2h_meetings,
    _thin_count,
    _thin_total,
)

from prediction_tracker import (
    record_predictions,
    format_vip_extra_lines,
    format_pick_block,
    format_compact_pick_line,
    format_confidence_label,
    describe_pick_categories,
    filter_pick_items_by_date,
    is_static_blocked_fixture,
    write_telegram_section,
    append_yesterday_section,
    format_vip_banner,
    format_vip_summary,
    PICK_TIER_PREMIUM,
    PICK_TIER_STRONG,
    PICK_TIER_VALUE,
    COMPACT_TIER_HEADER_PREMIUM,
    COMPACT_TIER_HEADER_STRONG,
    COMPACT_TIER_HEADER_WATCH,
    MARKET_SECTION_DIVIDER,
)

MARKET_OVER05 = "over05_tg"
MARKET_LABEL_OVER05 = "Over 0.5 Team Goal"
SHORT_MARKET_OVER05 = "O0.5TG"

CACHE_DB = "soccerbase_cache_oo05.db"
CACHE_TTL_HOURS = 24
MAX_WORKERS = 4
REQUEST_DELAY_MIN = 2.5
REQUEST_DELAY_MAX = 5.0
MAX_TOTAL_EXPOSURE = 0.15
DEFAULT_ODDS = 1.18

if os.getenv("CI"):
    MAX_WORKERS = 2
    REQUEST_DELAY_MIN = 4.0
    REQUEST_DELAY_MAX = 8.0
    print("CI environment detected: throttling to 2 workers")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

session = build_session()
cache = Cache(db_path=CACHE_DB, ttl_hours=CACHE_TTL_HOURS)


MIN_SCORING_STREAK = 10
MIN_DATA_GAMES = 6
MAX_SCORE = 12

_WEAK_ROI_OVER05_KEYWORDS = [
    "Youth", "U17", "U19", "U20", "U21", "Amateur", "Friendly",
    "Pre-season", "Copa do Brasil Sub", "Qualification preliminary",
    "Women Reserve",
]

_WEAK_ROI_MULTIPLIER = 0.90

_WEIGHT_RULES = 0.55
_WEIGHT_MODEL = 0.30
_WEIGHT_EDGE = 0.15

_TIER_PREMIUM_CUTOFF = 0.80
_TIER_SOLID_CUTOFF = 0.66

_PREMIUM_STREAK_FLOOR = 14
_PREMIUM_COMBINED_GPG = 2.2


def fetch(url, use_cache=True):
    delay = REQUEST_DELAY_MIN + (REQUEST_DELAY_MAX - REQUEST_DELAY_MIN) * (0.5 if use_cache else 1.0) * 0.1
    return _shared_fetch(
        url, session, cache,
        use_cache=use_cache, min_delay_seconds=delay,
    )


def fetch_soccerbase_team_results(team_id):
    return _shared_fetch_team_results(team_id, fetch)


def fetch_soccerbase_fixtures(date_str):
    return _shared_fetch_fixtures(date_str, fetch)


def get_team_form(team_id, is_home=True, num_matches=20, target_date_str=None):
    return _shared_get_team_form(
        team_id, fetch_soccerbase_team_results,
        is_home, num_matches, target_date_str, parse_date,
    )


def get_team_overall_form(team_id, num_matches=20, target_date_str=None):
    return _shared_get_team_overall_form(
        team_id, fetch_soccerbase_team_results,
        num_matches, target_date_str, parse_date,
    )


def get_h2h_meetings(home_team_id, away_team_id, target_date_str=None, limit=8):
    return _shared_get_h2h_meetings(
        home_team_id, away_team_id,
        fetch_soccerbase_team_results, target_date_str, limit=limit,
    )


def _is_weak_roi_league(league_name):
    return _shared_is_weak_roi_league(league_name, _WEAK_ROI_OVER05_KEYWORDS)


# =============================================================================
# STREAK + OPPONENT DETECTION (the 2 pillars)
# =============================================================================

def _longest_scoring_streak(form):
    """Return the length of the LONGEST consecutive run of matches
    where the team scored >= 1 goal. Walks BACKWARDS through form
    (most recent first). Returns 0 if empty."""
    streak = 0
    current = 0
    for gf, _ in (form or []):
        if gf >= 1:
            current += 1
            if current > streak:
                streak = current
        else:
            current = 0
    return streak


def _current_recent_scored_run(form):
    """Scored in every one of the MOST RECENT N matches (no recent blanks).
    This is a STRONGER signal than longest-ever streak — a team currently
    on fire is less likely to blank today than one whose streak was mid-season.
    Returns (run_length, sample_size)."""
    if not form:
        return 0, 0
    run = 0
    for gf, _ in form:
        if gf >= 1:
            run += 1
        else:
            break
    return run, len(form)


def _clean_sheets_in_last_n(form, n=6):
    """Clean sheets (GA == 0) in last n matches of the given form list."""
    sample = (form or [])[:n]
    if not sample:
        return None, None
    cs = sum(1 for _, ga in sample if ga == 0)
    return cs, len(sample)


def _consecutive_games_opponent_conceded(form):
    """MOST RECENT consecutive matches where OPPONENT side (team represented
    by this form) conceded >= 1 goal. i.e. no CS in their recent matches."""
    run = 0
    for _, ga in (form or []):
        if ga >= 1:
            run += 1
        else:
            break
    return run


# =============================================================================
# RULE ENGINE — 12-check scoring system
# =============================================================================

def apply_algorithm(streak_team_form_20, opponent_form_6,
                    streak_team_overall_20, opponent_overall_10,
                    streak_team_venue_6, opponent_venue_6,
                    streak_is_home=True):
    """
    Apply Over 0.5 Team Goal 12-check rule system.

    Returns (passed_rules, failed_rules, rule_details_dict, is_perfect).
    Rule checks are written from the POV of the "streak team" — the side
    with the long scoring run we're riding.
    """
    passed = []
    failed = []
    details = {}
    is_perfect = True

    if len(streak_team_form_20 or []) < MIN_DATA_GAMES:
        return None, None, {"error": "Insufficient streak-team data"}, False
    if len(opponent_form_6 or []) < 3:
        return None, None, {"error": "Insufficient opponent data"}, False

    streak_len = _longest_scoring_streak(streak_team_form_20)
    recent_run, recent_n = _current_recent_scored_run(streak_team_form_20)
    streak_team_scored_6 = sum(1 for gf, _ in (streak_team_venue_6 or [])[:6] if gf >= 1)

    opp_cs_6, opp_cs_n = _clean_sheets_in_last_n(opponent_venue_6, 6)
    opp_cs_6 = opp_cs_6 if opp_cs_6 is not None else 2
    opp_cs_n = opp_cs_n if opp_cs_n is not None else 0

    opp_recent_concede_run = _consecutive_games_opponent_conceded(opponent_venue_6)

    # 1 — LONG SCORING STREAK (core). Team has scored in >= MIN_SCORING_STREAK
    #     consecutive matches at any point in recent history.
    if streak_len >= MIN_SCORING_STREAK:
        passed.append("Core scoring streak (>=10)")
        details["Core scoring streak (>=10)"] = f"PASS ({streak_len})"
    else:
        failed.append("Core scoring streak (>=10)")
        details["Core scoring streak (>=10)"] = f"FAIL ({streak_len})"
        is_perfect = False

    # 2 — RECENT RUN (no recent blanks). Current consecutive scored streak
    #     >= 6 confirms the streak is NOW, not faded mid-season.
    if recent_run >= 6 and recent_n >= 6:
        passed.append("Recent 6-match scoring run")
        details["Recent 6-match scoring run"] = f"PASS ({recent_run})"
    else:
        failed.append("Recent 6-match scoring run")
        details["Recent 6-match scoring run"] = f"FAIL ({recent_run}/{recent_n})"
        is_perfect = False

    # 3 — OPPONENT NOT AIR-TIGHT at venue. Opponent has CS <= 1 of last 6
    #     games at their venue (i.e. they concede goals). This is the
    #     user's explicit requirement.
    if opp_cs_n >= 4 and opp_cs_6 <= 1:
        passed.append("Opponent leaky venue defence (CS<=1/6)")
        details["Opponent leaky venue defence (CS<=1/6)"] = (
            f"PASS (CS {opp_cs_6}/{opp_cs_n}, concede run {opp_recent_concede_run})"
        )
    else:
        failed.append("Opponent leaky venue defence (CS<=1/6)")
        details["Opponent leaky venue defence (CS<=1/6)"] = (
            f"FAIL (CS {opp_cs_6}/{opp_cs_n})"
        )
        is_perfect = False

    # 4 — OPPONENT RECENTLY CONCEDED. Opponent conceded in each of their
    #     most recent 3+ venue matches (confirming the CS<=1 isn't ancient).
    if opp_recent_concede_run >= 3:
        passed.append("Opponent recent concede run (>=3)")
        details["Opponent recent concede run (>=3)"] = f"PASS ({opp_recent_concede_run})"
    else:
        failed.append("Opponent recent concede run (>=3)")
        details["Opponent recent concede run (>=3)"] = f"FAIL ({opp_recent_concede_run})"
        is_perfect = False

    # 5 — STREAK TEAM ALSO SCORED at VENUE in >=5 of 6. Confirm venue form
    #     mirrors overall — no "they score everywhere EXCEPT home/away".
    s6_n = min(len(streak_team_venue_6 or []), 6)
    s6_thresh = _thin_count(5, 6, s6_n)
    if s6_n >= 3 and streak_team_scored_6 >= s6_thresh:
        passed.append("Streak team venue scored (>=5/6)")
        details["Streak team venue scored (>=5/6)"] = f"PASS ({streak_team_scored_6}/{s6_n})"
    else:
        failed.append("Streak team venue scored (>=5/6)")
        details["Streak team venue scored (>=5/6)"] = f"FAIL ({streak_team_scored_6}/{s6_n})"
        is_perfect = False

    # 6 — OPPONENT OVERALL NOT DEFENSIVE TITAN. Opponent overall last 10:
    #     >= 5 of matches conceded (overall, not venue) — they leak across
    #     the board, so streak-team has avenues home OR away.
    opp_ov_cs10, opp_ov_n10 = _clean_sheets_in_last_n(opponent_overall_10, 10)
    if opp_ov_cs10 is not None and opp_ov_n10 >= 6:
        opp_ov_conceded_10 = opp_ov_n10 - opp_ov_cs10
        if opp_ov_conceded_10 >= 5:
            passed.append("Opponent overall conceded (>=5/10)")
            details["Opponent overall conceded (>=5/10)"] = f"PASS ({opp_ov_conceded_10}/{opp_ov_n10})"
        else:
            failed.append("Opponent overall conceded (>=5/10)")
            details["Opponent overall conceded (>=5/10)"] = f"FAIL ({opp_ov_conceded_10}/{opp_ov_n10})"
            is_perfect = False
    else:
        failed.append("Opponent overall conceded (>=5/10)")
        details["Opponent overall conceded (>=5/10)"] = "SKIP (thin data)"
        is_perfect = False

    # 7 — COMBINED GOAL VOLUME FLOOR. Last 6 venue each: combined average
    #     total goals per game >= 1.5 — we're in active-goal territory,
    #     not a 0-0 stalemate division.
    sf = (streak_team_venue_6 or [])[:6]
    of = (opponent_venue_6 or [])[:6]
    combined_total = sum(gf + ga for gf, ga in sf) + sum(gf + ga for gf, ga in of)
    games = max(1, len(sf) + len(of))
    avg = combined_total / games
    if avg >= 1.5:
        passed.append("Combined venue GPG floor (>=1.5)")
        details["Combined venue GPG floor (>=1.5)"] = f"PASS ({avg:.2f})"
    else:
        failed.append("Combined venue GPG floor (>=1.5)")
        details["Combined venue GPG floor (>=1.5)"] = f"FAIL ({avg:.2f})"
        is_perfect = False

    # 8 — BOTH TEAMS SCORED OVERALL FREQUENCY. Last 6 overall each:
    #     if both sides scored in >= 4 of 6 overall, a 0-0 is near-impossible.
    so = (streak_team_overall_20 or [])[:6]
    oo = (opponent_overall_10 or [])[:6]
    bs1 = sum(1 for gf, _ in so if gf >= 1) if len(so) >= 4 else 0
    bs2 = sum(1 for gf, _ in oo if gf >= 1) if len(oo) >= 4 else 0
    if len(so) >= 4 and len(oo) >= 4 and bs1 >= 4 and bs2 >= 4:
        passed.append("Both teams scored overall (>=4/6 each)")
        details["Both teams scored overall (>=4/6 each)"] = f"PASS ({bs1} vs {bs2})"
    else:
        failed.append("Both teams scored overall (>=4/6 each)")
        details["Both teams scored overall (>=4/6 each)"] = f"FAIL ({bs1} vs {bs2})"
        is_perfect = False

    # 9 — AWAY-TEAM SCORING CONFIRMATION. If AWAY is either the streak-team
    #     OR opponent: confirm away scored in >=3 of 6 away (teams don't
    #     contribute to a "non 0-0" if the away side never scores on road).
    if not streak_is_home:
        away_form = streak_team_venue_6
    else:
        away_form = opponent_venue_6
    away_scored_6 = sum(1 for gf, _ in (away_form or [])[:6] if gf >= 1)
    away_n = min(len(away_form or []), 6)
    away_thr = _thin_count(3, 6, away_n)
    if away_n >= 3 and away_scored_6 >= away_thr:
        passed.append("Away side scored road (>=3/6)")
        details["Away side scored road (>=3/6)"] = f"PASS ({away_scored_6}/{away_n})"
    else:
        failed.append("Away side scored road (>=3/6)")
        details["Away side scored road (>=3/6)"] = f"FAIL ({away_scored_6}/{away_n})"
        is_perfect = False

    # 10 — HOME-TEAM DEFENCE NOT PERFECT. If HOME is streak-team opponent:
    #      home conceded >=1 in >=3 of 6 home (so streak-team scores when home-opp leaks).
    #      (Equivalently: home CS <= 3 of last 6.)
    if streak_is_home:
        home_form = opponent_venue_6
    else:
        home_form = streak_team_venue_6
    home_cs_6, home_n = _clean_sheets_in_last_n(home_form, 6)
    if home_cs_6 is not None and home_n >= 4 and home_cs_6 <= 3:
        passed.append("Home defence not elite (CS<=3/6)")
        details["Home defence not elite (CS<=3/6)"] = f"PASS (CS {home_cs_6}/{home_n})"
    elif home_cs_6 is not None:
        failed.append("Home defence not elite (CS<=3/6)")
        details["Home defence not elite (CS<=3/6)"] = f"FAIL (CS {home_cs_6}/{home_n})"
        is_perfect = False
    else:
        failed.append("Home defence not elite (CS<=3/6)")
        details["Home defence not elite (CS<=3/6)"] = "SKIP (thin data)"
        is_perfect = False

    # 11 — RECENT OFFENSIVE SHOCK ABSENT. Neither side BLANKED in both
    #      of their two most recent overall matches. Such a double-shock
    #      is a 0-0 preview.
    def _double_blank_last_2(form):
        sample = (form or [])[:2]
        return len(sample) == 2 and all(gf == 0 for gf, _ in sample)
    a = _double_blank_last_2(streak_team_overall_20)
    b = _double_blank_last_2(opponent_overall_10)
    if not a and not b:
        passed.append("No recent double-blank shock")
        details["No recent double-blank shock"] = "PASS"
    else:
        failed.append("No recent double-blank shock")
        details["No recent double-blank shock"] = f"FAIL (streak={a}, opp={b})"
        is_perfect = False

    # 12 — STREAK LENGTH BONUS (>=14 = best-in-class premium feeder).
    if streak_len >= _PREMIUM_STREAK_FLOOR:
        passed.append("Elite streak length (>=14)")
        details["Elite streak length (>=14)"] = f"PASS ({streak_len})"
    else:
        failed.append("Elite streak length (>=14)")
        details["Elite streak length (>=14)"] = f"FAIL ({streak_len})"

    return passed, failed, details, is_perfect


# =============================================================================
# MODEL PROBABILITY (Poisson 0-0 avoidance)
# =============================================================================

def calculate_poisson_prob(home_lambda, away_lambda):
    """Probability of AT LEAST 1 goal in the match (= 1 - P(0-0))."""
    p_0_home = _shared_poisson_pmf(0, home_lambda)
    p_0_away = _shared_poisson_pmf(0, away_lambda)
    p_0_0 = p_0_home * p_0_away
    return round(max(50.0, (1.0 - p_0_0) * 100), 1)


def get_match_lambdas(home_6, away_6, league_name=None):
    home_scores = sum(gf for gf, _ in (home_6 or [])[:6])
    home_concedes = sum(ga for _, ga in (home_6 or [])[:6])
    away_scores = sum(gf for gf, _ in (away_6 or [])[:6])
    away_concedes = sum(ga for _, ga in (away_6 or [])[:6])
    h_n = min(len(home_6 or []), 6) or 1
    a_n = min(len(away_6 or []), 6) or 1
    league_boost = 0.0 if not _is_weak_roi_league(league_name or "") else -0.10
    home_lambda = (home_scores / h_n + away_concedes / a_n) / 2.0 + 0.15 + league_boost
    away_lambda = (away_scores / a_n + home_concedes / h_n) / 2.0 + 0.15 + league_boost
    return (
        round(max(0.3, min(3.2, home_lambda)), 2),
        round(max(0.3, min(3.2, away_lambda)), 2),
    )


def data_volume_penalty(streak_20, opp_6, streak_ov_20, opp_ov_10):
    n = min(
        len(streak_20 or []),
        len(opp_6 or []),
        len(streak_ov_20 or []),
        len(opp_ov_10 or []),
    )
    if n >= MIN_DATA_GAMES:
        return 1.0
    if n >= 4:
        return 0.94
    if n >= 3:
        return 0.86
    if n >= 2:
        return 0.78
    return 0.65


def compute_confidence_score(rule_score, max_score, model_prob_pct, decimal_odds, data_mult=1.0):
    rule_component = max(0.0, min(1.0, rule_score / max(max_score, 1)))
    model_component = max(0.0, min(1.0, model_prob_pct / 100.0))
    implied = 1.0 / max(1.02, decimal_odds)
    edge_component = max(0.0, min(1.0, (model_prob_pct / 100.0 - implied) + 0.5))
    raw = _WEIGHT_RULES * rule_component + _WEIGHT_MODEL * model_component + _WEIGHT_EDGE * edge_component
    return max(0.0, min(1.0, raw * data_mult))


def tier_from_confidence(score, is_perfect, streak_len, combined_gpg):
    premium_ok = (
        is_perfect and
        streak_len >= _PREMIUM_STREAK_FLOOR and
        combined_gpg >= _PREMIUM_COMBINED_GPG
    )
    if score >= _TIER_PREMIUM_CUTOFF and premium_ok:
        return "perfect"
    if score >= _TIER_SOLID_CUTOFF:
        return "qualified"
    return "close"


# =============================================================================
# HARD VETOES (absolute blocks — override rule score)
# =============================================================================

def _h2h_zero_zero_bogey_veto(home_tid, away_tid, target_date):
    """Block Over 0.5 if last 4+ H2H meetings include >=3 pure 0-0 results."""
    meetings = get_h2h_meetings(home_tid, away_tid, target_date, limit=6)
    if len(meetings) < 3:
        return False, [], None
    zeros = 0
    for m in meetings:
        if m.get("gf", -1) == 0 and m.get("ga", -1) == 0:
            zeros += 1
    if len(meetings) >= 4 and zeros >= 3:
        return True, meetings, f"{zeros}_of_{len(meetings)}_h2h_were_0_0"
    return False, meetings, None


def _combined_mutual_cold_start_veto(streak_ov_6, opp_ov_6):
    """Block if BOTH teams blanked in BOTH of their last 2 overall matches.
    Classic 0-0 preview — a sudden cold spell hitting both sides."""
    so = (streak_ov_6 or [])[:2]
    oo = (opp_ov_6 or [])[:2]
    if len(so) < 2 or len(oo) < 2:
        return False, None
    s_cold = all(gf == 0 for gf, _ in so)
    o_cold = all(gf == 0 for gf, _ in oo)
    if s_cold and o_cold:
        return True, "both_teams_double_blank_last_2"
    return False, None


# =============================================================================
# PICK SIDE: which team is the "streak team"?
# =============================================================================

def _choose_best_side(home_form_20, away_form_20,
                     home_overall_20, away_overall_20):
    """Return dict {best_side_is_home, streak_len, recent_run, opp_is_away}.
    Pick whichever side (home or away) has the longer scoring streak.
    Ties go to home (home advantage for scoring)."""
    h_streak = _longest_scoring_streak(home_form_20)
    a_streak = _longest_scoring_streak(away_form_20)
    h_run = _current_recent_scored_run(home_form_20)[0]
    a_run = _current_recent_scored_run(away_form_20)[0]

    h_score = h_streak * 2 + h_run
    a_score = a_streak * 2 + a_run
    if h_score >= a_score:
        return {
            "best_side_is_home": True,
            "streak_len": h_streak,
            "recent_run": h_run,
            "streak_team": "home",
            "streak_form": home_form_20,
            "streak_venue_6": home_form_20[:6],
            "streak_overall": home_overall_20,
            "opp_venue_6": away_form_20[:6],
            "opp_overall": away_overall_20,
        }
    return {
        "best_side_is_home": False,
        "streak_len": a_streak,
        "recent_run": a_run,
        "streak_team": "away",
        "streak_form": away_form_20,
        "streak_venue_6": away_form_20[:6],
        "streak_overall": away_overall_20,
        "opp_venue_6": home_form_20[:6],
        "opp_overall": home_overall_20,
    }


# =============================================================================
# MATCH PROCESSING
# =============================================================================

def process_single_match(match, target_date, default_odds=DEFAULT_ODDS):
    try:
        league_name = match.get("league", "")

        home_form_20 = get_team_form(match["home_team_id"], True, 20, target_date)
        away_form_20 = get_team_form(match["away_team_id"], False, 20, target_date)
        home_overall_20 = get_team_overall_form(match["home_team_id"], 20, target_date)
        away_overall_20 = get_team_overall_form(match["away_team_id"], 20, target_date)

        if len(home_form_20 or []) < MIN_DATA_GAMES or len(away_form_20 or []) < MIN_DATA_GAMES:
            return {"status": "insufficient"}

        side = _choose_best_side(
            home_form_20, away_form_20, home_overall_20, away_overall_20,
        )

        data_mult = data_volume_penalty(
            side["streak_form"],
            side["opp_venue_6"],
            side["streak_overall"],
            side["opp_overall"],
        )

        passed, failed, details, is_perfect = apply_algorithm(
            side["streak_form"],
            side["opp_venue_6"],
            side["streak_overall"],
            side["opp_overall"][:10] if side["opp_overall"] else [],
            side["streak_venue_6"],
            side["opp_venue_6"],
            streak_is_home=side["best_side_is_home"],
        )
        if passed is None:
            return {"status": "insufficient"}

        score = len(passed)
        weak_league = _is_weak_roi_league(league_name)

        home_6 = home_form_20[:6]
        away_6 = away_form_20[:6]
        home_lambda, away_lambda = get_match_lambdas(home_6, away_6, league_name=league_name)
        prob_pct = calculate_poisson_prob(home_lambda, away_lambda)

        h2h_veto, h2h_meetings, h2h_reason = _h2h_zero_zero_bogey_veto(
            match["home_team_id"], match["away_team_id"], target_date,
        )
        cold_veto, cold_reason = _combined_mutual_cold_start_veto(
            home_overall_20[:6], away_overall_20[:6],
        )

        min_score = MAX_SCORE - 3 if weak_league else MAX_SCORE - 4
        qualifies = (
            score >= min_score
            and not h2h_veto
            and not cold_veto
            and side["streak_len"] >= MIN_SCORING_STREAK
        )

        league_mult = _WEAK_ROI_MULTIPLIER if weak_league else 1.0
        final_mult = data_mult * league_mult

        sf = (side["streak_venue_6"] or [])[:6]
        of = (side["opp_venue_6"] or [])[:6]
        combined_total = sum(gf + ga for gf, ga in sf) + sum(gf + ga for gf, ga in of)
        combined_gpg = combined_total / max(1, len(sf) + len(of))

        conf_score = compute_confidence_score(
            score, MAX_SCORE, prob_pct, default_odds, final_mult,
        )
        tier = (
            tier_from_confidence(conf_score, is_perfect, side["streak_len"], combined_gpg)
            if qualifies else None
        )
        if not qualifies:
            tier = None
        kelly_half = calculate_kelly(prob_pct / 100, default_odds) if qualifies else 0.0

        regressions = []
        if final_mult < 1.0:
            regressions.append(f"data volume / weak league multiplier (x{final_mult:.2f})")
        if h2h_veto:
            regressions.append(f"h2h 0-0 bogey ({h2h_reason})")
        if cold_veto:
            regressions.append(f"mutual double-blank cold start ({cold_reason})")
        if side["streak_len"] < MIN_SCORING_STREAK:
            regressions.append(f"streak too short ({side['streak_len']}<10)")

        return {
            "status": "success",
            "data": {
                "match": match,
                "score": score,
                "passed": passed,
                "failed": failed,
                "details": details,
                "is_perfect": is_perfect,
                "tier": tier,
                "confidence_score": round(conf_score * 100, 1),
                "model": {
                    "streak_side": side["streak_team"],
                    "streak_len": side["streak_len"],
                    "recent_run": side["recent_run"],
                    "home_lambda": home_lambda,
                    "away_lambda": away_lambda,
                    "combined_lambda": round(home_lambda + away_lambda, 2),
                    "prob_pct": prob_pct,
                    "combined_gpg": round(combined_gpg, 2),
                },
                "prob": prob_pct,
                "confidence": "HIGH" if prob_pct >= 95 else (
                    "MEDIUM" if prob_pct >= 88 else "LOW"
                ),
                "kelly": round(kelly_half * 100, 2),
                "gate_passed": True,
                "h2h_zero_bogey_veto": h2h_veto,
                "h2h_zero_bogey_reason": h2h_reason,
                "h2h_zero_bogey_meetings_count": len(h2h_meetings),
                "cold_start_veto": cold_veto,
                "cold_start_reason": cold_reason,
                "data_mult": round(data_mult, 2),
                "weak_league_mult": round(league_mult, 2),
                "min_score_threshold": min_score,
                "weak_roi_league": weak_league,
                "regression_penalty_applied": regressions,
            }
        }
    except Exception as e:
        logger.error(
            f"Processing failed for "
            f"{match.get('home', 'N/A')} vs {match.get('away', 'N/A')}: {e}",
            exc_info=True,
        )
        return {"status": "error"}


# =============================================================================
# REPORTING
# =============================================================================

def _append_o05_pick(lines, idx, item, odds, detailed, compact=False):
    m = item["match"]
    tgt = item
    md = tgt["model"]
    side_label = "Home streak" if md["streak_side"] == "home" else "Away streak"
    if compact:
        lines.append(format_compact_pick_line(
            m["home"], m["away"], SHORT_MARKET_OVER05,
            tgt.get("tier"), tgt.get("prob"), m.get("date"),
        ))
        return
    extra = None
    if detailed:
        extra = format_vip_extra_lines(
            tgt["kelly"], odds, tgt["score"], MAX_SCORE,
            home_lambda=md["home_lambda"], away_lambda=md["away_lambda"],
            model_prob=tgt["prob"],
            market=MARKET_OVER05,
            h2h_note=(
                f"streak={md['streak_len']} ({side_label}, recent {md['recent_run']}) "
                f"· combined GPG {md['combined_gpg']}"
            ),
            rule_details=tgt.get("details"),
        )
    categories = describe_pick_categories(
        m["home"], m["away"], m.get("league", ""),
        market=MARKET_OVER05,
        tier=tgt.get("tier"),
        weak_roi_league=bool(tgt.get("weak_roi_league")),
    )
    lines.extend(format_pick_block(
        idx, m["home"], m["away"], m["date"],
        (
            f"{MARKET_LABEL_OVER05} · "
            f"{format_confidence_label(tgt['confidence'])} ({tgt['prob']}%)"
        ),
        extra,
        league=m.get("league"),
        categories=categories,
    ))


def build_report(perfect, qualified, close, weak,
                 scanned_dates, bankroll, odds, detailed=False, compact=False,
                 include_yesterday=True, include_header=True, include_footer=True,
                 report_date=None):
    included = [
        item for item in (perfect + qualified + close)
        if not is_static_blocked_fixture(item.get("match", {}))
    ]
    if report_date:
        included = filter_pick_items_by_date(included, report_date)
    included_dates = scanned_dates
    base_date = scanned_dates[0] if scanned_dates else datetime.now().strftime("%Y-%m-%d")

    lines = []
    if report_date and not include_header and not compact:
        lines.append(f"📅 Picks for {report_date}")
        lines.append("")
    if not compact:
        if detailed and include_header:
            lines.extend(format_vip_banner(
                "Over 0.5 Team Goal (Non 0-0)", base_date, included_dates,
            ))
        if include_header:
            lines.append("🎯 Over 0.5 Team Goal (no 0-0)")
            lines.append("")
            if len(included_dates) > 1:
                lines.append(f"Dates: {included_dates[0]} to {included_dates[-1]}")
            else:
                lines.append(f"Date: {base_date}")
            lines.append("")
            if include_yesterday:
                append_yesterday_section(lines, "over05_tg", detailed=detailed)
    elif compact:
        perf_tier = [p for p in included if p in perfect]
        qual_tier = [p for p in included if p in qualified]
        clos_tier = [p for p in included if p in close]
        has_any = False
        for tier_header, items in [
            (COMPACT_TIER_HEADER_PREMIUM, perf_tier),
            (COMPACT_TIER_HEADER_STRONG, qual_tier),
            (COMPACT_TIER_HEADER_WATCH, clos_tier),
        ]:
            if not items:
                continue
            if not has_any:
                lines.append(f"▸ {MARKET_LABEL_OVER05.upper()}")
                has_any = True
            lines.append(f"  {tier_header}")
            for item in items:
                lines.append(f"  {format_compact_pick_line(
                    item['match']['home'], item['match']['away'], SHORT_MARKET_OVER05,
                    item.get('tier'), item.get('prob'), item['match'].get('date'),
                )}")

    if not compact and included:
        lines.append("")
        lines.append("🏁 Over 0.5 Team Goal picks")
        lines.append("")
        inc_perf = [p for p in included if p in perfect]
        inc_qual = [p for p in included if p in qualified]
        inc_close = [p for p in included if p in close]
        if inc_perf:
            lines.append(f"  {PICK_TIER_PREMIUM}")
            lines.append("")
            for i, item in enumerate(inc_perf, 1):
                _append_o05_pick(lines, i, item, odds, detailed, compact)
        if inc_qual:
            lines.append(f"  {PICK_TIER_STRONG}")
            lines.append("")
            start = len(inc_perf) + 1
            for i, item in enumerate(inc_qual, start):
                _append_o05_pick(lines, i, item, odds, detailed, compact)
        if inc_close:
            if detailed:
                lines.append(f"  {PICK_TIER_VALUE}")
                lines.append("")
            start = len(inc_perf) + len(inc_qual) + 1
            for i, item in enumerate(inc_close, start):
                _append_o05_pick(lines, i, item, odds, detailed, compact)

    if include_footer:
        if not compact:
            lines.append("")
            if detailed:
                lines.extend(format_vip_summary(
                    "OVER 0.5 TEAM GOAL · Pick summary",
                    perfect, qualified, close,
                ))
            lines.append("---")
            lines.append("For informational purposes only")
            lines.append("Gamble responsibly")
            lines.append("")

    report = "\n".join(lines).strip()
    if not report:
        report = "— none"
    return report, base_date, included


# =============================================================================
# MAIN
# =============================================================================

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(
        description="Over 0.5 Team Goal (no 0-0) Predictor — >=10 scoring streak + leaky opponent"
    )
    parser.add_argument("date", nargs="?",
                        default=datetime.now().strftime("%Y-%m-%d"),
                        help="Start date (YYYY-MM-DD)")
    parser.add_argument("--scheduled", action="store_true",
                        help="Only include scheduled (upcoming) matches")
    parser.add_argument("--bankroll", type=float, default=1000.0)
    parser.add_argument("--odds", type=float, default=DEFAULT_ODDS,
                        help=f"Average decimal odds for Over 0.5 TG (default {DEFAULT_ODDS})")
    parser.add_argument("--clear-cache", action="store_true")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--publish-date", default=None)
    args = parser.parse_args()

    if args.clear_cache:
        cache.clear()

    start_date = datetime.strptime(args.date, "%Y-%m-%d")
    scan_days = args.days
    if scan_days is None:
        scan_days = 6 if start_date.weekday() >= 4 else 4

    perfect, qualified, close, weak = [], [], [], []
    seen_fixtures = set()
    unique_fixtures = []
    scanned_dates = []

    for day_offset in range(scan_days):
        d = start_date + timedelta(days=day_offset)
        date_str = d.strftime("%Y-%m-%d")
        scanned_dates.append(date_str)
        fixtures = fetch_soccerbase_fixtures(date_str)
        if args.scheduled:
            fixtures = [f for f in fixtures if f.get("status") == "scheduled"]
        for m in fixtures:
            key = (m["home_team_id"], m["away_team_id"], date_str)
            if key in seen_fixtures:
                continue
            seen_fixtures.add(key)
            unique_fixtures.append(m)

    logger.info(
        "Scanning %d unique fixtures across %d days",
        len(unique_fixtures), scan_days,
    )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_single_match, m, date_str, args.odds): m
            for m in unique_fixtures
        }
        for future in as_completed(futures):
            try:
                res = future.result(timeout=60)
            except Exception as e:
                logger.error(f"Future timeout/error: {e}")
                continue
            if res["status"] == "insufficient":
                continue
            if res["status"] == "success":
                t = res["data"]["tier"]
                if t == "perfect":
                    perfect.append(res["data"])
                elif t == "qualified":
                    qualified.append(res["data"])
                elif t == "close":
                    close.append(res["data"])
                elif res["data"]["score"] >= max(1, MAX_SCORE - 3):
                    weak.append(res["data"])

    apply_portfolio_kelly(
        perfect + qualified + close, "over05_tg", args.bankroll, MAX_TOTAL_EXPOSURE,
    )

    free_report, base_date, included = build_report(
        perfect, qualified, close, weak, scanned_dates,
        args.bankroll, args.odds, detailed=False,
    )
    publish_date = args.publish_date or datetime.now().strftime("%Y-%m-%d")
    telegram_report, _, _ = build_report(
        perfect, qualified, close, weak, scanned_dates,
        args.bankroll, args.odds, detailed=False, compact=False,
        include_yesterday=False, include_header=False, include_footer=False,
        report_date=publish_date,
    )
    detailed_report, _, _ = build_report(
        perfect, qualified, close, weak, scanned_dates,
        args.bankroll, args.odds, detailed=True,
    )

    print("\n===EMAIL_START===")
    print(free_report)
    print("===EMAIL_END===")
    write_telegram_section(telegram_report, "oo05_telegram.txt")

    detailed_report_path = f"over05_team_goal_vip_report_{base_date}.txt"
    with open(detailed_report_path, "w", encoding="utf-8") as f:
        f.write(detailed_report)

    output_path = f"over05_team_goal_report_{base_date}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "metadata": {
                "scanned_window": scanned_dates,
                "bankroll": args.bankroll,
                "odds": args.odds,
                "min_scoring_streak": MIN_SCORING_STREAK,
                "max_score": MAX_SCORE,
                "generated_at": datetime.now().isoformat(),
            },
            "perfect": perfect,
            "qualified": qualified,
            "close": close,
            "weak": weak,
        }, f, indent=2, ensure_ascii=False, default=str)

    try:
        oo05_picks = []
        for pick in perfect + qualified + close:
            tier = pick.get("tier") or (
                "perfect" if pick in perfect else
                "qualified" if pick in qualified else "close"
            )
            oo05_picks.append({
                "league": pick["match"]["league"],
                "home": pick["match"]["home"],
                "away": pick["match"]["away"],
                "date": pick["match"]["date"],
                "prediction": "over05_tg",
                "confidence": tier,
            })
        stats = record_predictions(base_date, oo05_picks=oo05_picks)
        if stats["added"]:
            print(f"Predictions recorded ({stats['added']} new)")
        elif stats["skipped"]:
            print(f"Predictions already recorded ({stats['skipped']} skipped)")
    except Exception as e:
        print(f"Could not record predictions: {e}")

    print(f"\nReport saved: {output_path}")
    print(f"VIP report saved: {detailed_report_path}")


if __name__ == "__main__":
    main()
