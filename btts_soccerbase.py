#!/usr/bin/env python3
"""
BTTS (BOTH TEAMS TO SCORE) PREDICTOR - STANDALONE v1
=====================================================
BTTS Yes / BTTS No rule scoring | Poisson model | Portfolio Kelly | SQLite Cache
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

# Shared scraping/caching/date/staking helpers (see utils.py) — do not
# redefine Cache, fetch, parse_date, or calculate_kelly locally; that
# copy-paste pattern is exactly what let this file and its siblings
# (over25_soccerbase.py, home_win_soccerbase.py) drift apart before.
from utils import (
    Cache,
    build_session,
    fetch as _shared_fetch,
    parse_date,
    calculate_kelly as _shared_calculate_kelly,
    apply_portfolio_kelly as _shared_apply_portfolio_kelly,
)

# Shared Soccerbase scraping/parsing (see scraping.py) — do not redefine
# fetch_soccerbase_fixtures, fetch_soccerbase_team_results, get_team_form,
# get_team_overall_form, or _thin_count/_thin_total locally.
from scraping import (
    fetch_soccerbase_fixtures as _shared_fetch_fixtures,
    fetch_soccerbase_team_results as _shared_fetch_team_results,
    get_team_form as _shared_get_team_form,
    get_team_overall_form as _shared_get_team_overall_form,
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
    PICK_TIER_PREMIUM,
    PICK_TIER_STRONG,
    PICK_TIER_VALUE,
    COMPACT_TIER_HEADER_PREMIUM,
    COMPACT_TIER_HEADER_STRONG,
    COMPACT_TIER_HEADER_WATCH,
    MARKET_SECTION_DIVIDER,
    MARKET_BTTS_YES,
    MARKET_BTTS_NO,
)

# =============================================================================
# CONFIGURATION
# =============================================================================
CACHE_DB = "soccerbase_cache_btts.db"
CACHE_TTL_HOURS = 24
MAX_WORKERS = 4
REQUEST_DELAY_MIN = 2.5
REQUEST_DELAY_MAX = 5.0
MAX_TOTAL_EXPOSURE = 0.25
SHRINKAGE_WEIGHT = 0.60

if os.getenv("CI"):
    MAX_WORKERS = 2
    REQUEST_DELAY_MIN = 4.0
    REQUEST_DELAY_MAX = 8.0
    print("CI environment detected: throttling to 2 workers")

MAX_BTTS_YES_SCORE = 14
MAX_BTTS_NO_SCORE = 14
BTTS_MIN_6 = 3
NON_BTTS_MIN_6 = 3

MIN_COMBINED_LAMBDA_BTTS_YES = 2.50
MAX_COMBINED_LAMBDA_BTTS_NO = 2.80
PREMIUM_COMBINED_LAMBDA_BTTS_YES = 2.90
PREMIUM_COMBINED_LAMBDA_BTTS_NO = 2.20

DEFAULT_ODDS_BTTS_YES = 1.90
DEFAULT_ODDS_BTTS_NO = 1.85

# over25tips.com official BTTS point algorithm (BetAndSkill / over25tips)
MIN_O25TIPS_BTTS_YES_POINTS = 7.0
MAX_O25TIPS_BTTS_NO_POINTS = 3.0
_O25TIPS_FORM_WINDOW = 6

_WEIGHT_RULES = 0.40
_WEIGHT_MODEL = 0.40
_WEIGHT_EDGE = 0.20
_TIER_PREMIUM_CUTOFF = 0.62
_TIER_SOLID_CUTOFF = 0.54
_MIN_DATA_GAMES = 5
_MIN_FORM_HALFLIFE = 3.0

_WEAK_ROI_LEAGUE_KEYWORDS = (
    "swedish allsvenskan", "allsvenskan", "superettan",
    "belarus",
    "k-league", "k league", "korean k-league",
    "league of ireland", "irish", "fai cup",
    "mexican primera", "brazilian serie a",
    "mls", "ecuador", "argentina primera", "chile primera",
)
_WEAK_ROI_MULTIPLIER = 0.82

_BTTS_H2H_MAX_LOOKBACK = 6
_BTTS_H2H_MIN_MEETINGS = 3
_BTTS_H2H_YES_BLOCK_RATE = 0.33
_BTTS_H2H_NO_BLOCK_RATE = 0.67

_LEAGUE_BASELINE_CACHE = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# HTTP session + cache: implementation lives in utils.py and is shared
# with the other two predictors. Build the session once per process.
session = build_session()
cache = Cache(db_path=CACHE_DB, ttl_hours=CACHE_TTL_HOURS)


def fetch(url, use_cache=True):
    """Thin wrapper binding the shared fetch() to this module's session/cache/delay config."""
    return _shared_fetch(
        url,
        session=session,
        cache=cache,
        use_cache=use_cache,
        min_delay=REQUEST_DELAY_MIN,
        max_delay=REQUEST_DELAY_MAX,
    )


# =============================================================================
# SCRAPING (shared implementation in scraping.py)
# =============================================================================
def fetch_soccerbase_fixtures(date_str):
    return _shared_fetch_fixtures(date_str, fetch)


def fetch_soccerbase_team_results(team_id):
    return _shared_fetch_team_results(team_id, fetch)


def get_team_form(team_id, is_home=True, num_matches=6, target_date_str=None):
    return _shared_get_team_form(
        team_id, fetch_soccerbase_team_results, is_home, num_matches, target_date_str, parse_date
    )


def get_team_overall_form(team_id, num_matches=6, target_date_str=None):
    return _shared_get_team_overall_form(
        team_id, fetch_soccerbase_team_results, num_matches, target_date_str, parse_date
    )


def _count_btts(form):
    return sum(1 for gf, ga in form[:6] if gf >= 1 and ga >= 1)


def _count_non_btts(form):
    return sum(1 for gf, ga in form[:6] if gf == 0 or ga == 0)


def _count_clean_sheets(form):
    return sum(1 for _, ga in form[:6] if ga == 0)


def _count_failed_to_score(form):
    return sum(1 for gf, _ in form[:6] if gf == 0)


# _thin_count/_thin_total are imported from scraping.py — do not redefine locally.


def _btts_gate_passes(home_6, away_6):
    h_btts = _count_btts(home_6)
    a_btts = _count_btts(away_6)
    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)
    if h_len >= 6 and a_len >= 6:
        return h_btts >= BTTS_MIN_6 and a_btts >= BTTS_MIN_6
    h_min = max(1, round(h_len * 0.5))
    a_min = max(1, round(a_len * 0.5))
    h_ok = (h_len >= 6 and h_btts >= BTTS_MIN_6) or (h_len >= 2 and h_btts >= h_min)
    a_ok = (a_len >= 6 and a_btts >= BTTS_MIN_6) or (a_len >= 2 and a_btts >= a_min)
    return h_ok and a_ok


def _non_btts_gate_passes(home_6, away_6):
    h_nb = _count_non_btts(home_6)
    a_nb = _count_non_btts(away_6)
    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)
    if h_len >= 6 and a_len >= 6:
        return h_nb >= NON_BTTS_MIN_6 and a_nb >= NON_BTTS_MIN_6
    h_min = max(1, round(h_len * 0.5))
    a_min = max(1, round(a_len * 0.5))
    h_ok = (h_len >= 6 and h_nb >= NON_BTTS_MIN_6) or (h_len >= 2 and h_nb >= h_min)
    a_ok = (a_len >= 6 and a_nb >= NON_BTTS_MIN_6) or (a_len >= 2 and a_nb >= a_min)
    return h_ok and a_ok


def _chaos_btts_bonus(home_6, away_6):
    """Both sides concede >= 1.2/game — leaky defences favour BTTS Yes."""
    if len(home_6 or []) < 2 or len(away_6 or []) < 2:
        return False
    hc = sum(ga for _, ga in home_6) / max(len(home_6), 1)
    ac = sum(ga for _, ga in away_6) / max(len(away_6), 1)
    return hc >= 1.2 and ac >= 1.2


def _compact_btts_no_bonus(home_6, away_6):
    """Both sides stingy in attack and defence — favours BTTS No."""
    if len(home_6 or []) < 2 or len(away_6 or []) < 2:
        return False
    hs = sum(gf for gf, _ in home_6) / max(len(home_6), 1)
    hc = sum(ga for _, ga in home_6) / max(len(home_6), 1)
    as_ = sum(gf for gf, _ in away_6) / max(len(away_6), 1)
    ac = sum(ga for _, ga in away_6) / max(len(away_6), 1)
    return hs <= 1.0 and hc <= 0.9 and as_ <= 1.0 and ac <= 0.9


def get_h2h_meetings(home_team_id, away_team_id, target_date_str=None, limit=_BTTS_H2H_MAX_LOOKBACK):
    """Recent meetings between these sides, merged from both teams' result pages."""
    collected = {}
    for team_id, opponent_id in (
        (home_team_id, away_team_id),
        (away_team_id, home_team_id),
    ):
        for match in fetch_soccerbase_team_results(team_id):
            if str(match.get("opponent_team_id") or "") != str(opponent_id):
                continue
            match_date = match.get("date_str")
            if target_date_str and match_date and match_date >= target_date_str:
                continue
            key = (match_date, match.get("gf"), match.get("ga"), bool(match.get("is_home")))
            if key in collected:
                continue
            if str(team_id) == str(home_team_id):
                perspective = dict(match)
            else:
                perspective = {
                    **match,
                    "gf": match.get("ga"),
                    "ga": match.get("gf"),
                    "is_home": not match.get("is_home"),
                }
            collected[key] = perspective
    meetings = sorted(collected.values(), key=lambda m: m.get("date_str") or "", reverse=True)
    return meetings[:limit]


def _h2h_btts_yes_blocked(home_team_id, away_team_id, target_date_str=None):
    """Block BTTS Yes when recent H2H games are consistently non-BTTS.

    Blocks if >=3 H2H meetings AND <=33% had both teams score (bogey non-BTTS matchup).
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str)
    if len(meetings) < _BTTS_H2H_MIN_MEETINGS:
        return False, meetings
    btts_count = sum(1 for m in meetings if m.get("gf", 0) >= 1 and m.get("ga", 0) >= 1)
    rate = btts_count / len(meetings)
    return rate <= _BTTS_H2H_YES_BLOCK_RATE, meetings


def _h2h_btts_no_blocked(home_team_id, away_team_id, target_date_str=None):
    """Block BTTS No when recent H2H games are consistently BTTS.

    Blocks if >=3 H2H meetings AND >=67% had both teams score (bogey BTTS matchup).
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str)
    if len(meetings) < _BTTS_H2H_MIN_MEETINGS:
        return False, meetings
    btts_count = sum(1 for m in meetings if m.get("gf", 0) >= 1 and m.get("ga", 0) >= 1)
    rate = btts_count / len(meetings)
    return rate >= _BTTS_H2H_NO_BLOCK_RATE, meetings


# =============================================================================
# BTTS YES HARDENING GATES
# =============================================================================

def _scoring_drought_veto(home_3, away_3):
    """Block BTTS Yes if EITHER team failed to score in BOTH of their last 2 venue games."""
    if len(home_3 or []) >= 2 and all(gf == 0 for gf, _ in home_3[:2]):
        return True, "home_scoring_drought_2"
    if len(away_3 or []) >= 2 and all(gf == 0 for gf, _ in away_3[:2]):
        return True, "away_scoring_drought_2"
    return False, None


def _defensive_wall_veto(home_3, away_3):
    """Block BTTS Yes if EITHER team kept a clean sheet in BOTH of their last 2 venue games."""
    if len(home_3 or []) >= 2 and all(ga == 0 for _, ga in home_3[:2]):
        return True, "home_defensive_wall_2"
    if len(away_3 or []) >= 2 and all(ga == 0 for _, ga in away_3[:2]):
        return True, "away_defensive_wall_2"
    return False, None


def _lambda_mismatch_veto(home_lambda, away_lambda):
    """Block BTTS Yes when one attack is expected to dominate the other.

    BTTS requires both attacks to contribute. If one lambda is < 0.8 or the
    ratio exceeds 2.0x, the match is likely one-sided.
    """
    if home_lambda <= 0 or away_lambda <= 0:
        return False, None
    lo, hi = sorted((home_lambda, away_lambda))
    if hi / lo >= 2.0:
        return True, f"lambda_ratio_{hi/lo:.1f}"
    if lo < 0.8:
        return True, f"weak_attack_lambda_{lo:.2f}"
    return False, None


def _recent_shutout_shock_veto(home_3, away_3, home_overall_1, away_overall_1):
    """Block BTTS Yes if a team blanked (scored 0) in their most recent match overall,
    OR failed to score in their last venue match. We ignore clean-sheets-kept (ga==0)
    because one good defensive game is not a reliable BTTS No signal."""
    if home_3 and home_3[0][0] == 0:
        return True, "home_last_venue_blank"
    if away_3 and away_3[0][0] == 0:
        return True, "away_last_venue_blank"
    if home_overall_1 and home_overall_1[0][0] == 0:
        return True, "home_last_match_blank"
    if away_overall_1 and away_overall_1[0][0] == 0:
        return True, "away_last_match_blank"
    return False, None


def _extended_scoring_drought_veto(home_3, away_3):
    """Block BTTS Yes if EITHER team failed to score in 2+ of their last 3
    venue games. The existing _scoring_drought_veto only catches a streak
    of 2 consecutive blanks; this catches intermittent blanks too.
    """
    if len(home_3 or []) >= 3:
        blanks = sum(1 for gf, _ in home_3[:3] if gf == 0)
        if blanks >= 2:
            return True, f"home_scoring_blanks_{blanks}_of_3"
    if len(away_3 or []) >= 3:
        blanks = sum(1 for gf, _ in away_3[:3] if gf == 0)
        if blanks >= 2:
            return True, f"away_scoring_blanks_{blanks}_of_3"
    return False, None


def _minimum_attack_rate_veto(home_3, away_3):
    """Block BTTS Yes if either team averages < 0.8 goals scored per venue game."""
    if len(home_3 or []) >= 3:
        avg = sum(gf for gf, _ in home_3) / len(home_3)
        if avg < 0.8:
            return True, f"home_attack_avg_{avg:.2f}"
    if len(away_3 or []) >= 3:
        avg = sum(gf for gf, _ in away_3) / len(away_3)
        if avg < 0.8:
            return True, f"away_attack_avg_{avg:.2f}"
    return False, None


def _cup_mismatch_veto(match, home_lambda, away_lambda):
    """
    Block BTTS Yes in cup matches with a clear tier gap.
    Detected by: 'Cup' in league name AND lambda mismatch >= 1.8.
    """
    league = str(match.get("league", "")).lower()
    is_cup = any(k in league for k in ("cup", "trophy", "shield", "challenge"))
    if not is_cup:
        return False, None
    if home_lambda <= 0 or away_lambda <= 0:
        return False, None
    lo, hi = sorted((home_lambda, away_lambda))
    if hi / lo >= 1.8:
        return True, f"cup_tier_mismatch_{hi/lo:.1f}x"
    return False, None


def _clean_sheet_frequency_veto(home_6, away_6):
    """
    Hard block: if either team has kept a clean sheet in 3+ of their last 6
    venue games (requires full 6-game sample), BTTS Yes is unlikely.
    """
    if len(home_6 or []) >= 6:
        cs = sum(1 for _, ga in home_6[:6] if ga == 0)
        if cs >= 3:
            return True, f"home_clean_sheets_{cs}_of_6"
    if len(away_6 or []) >= 6:
        cs = sum(1 for gf, _ in away_6[:6] if gf == 0)
        if cs >= 3:
            return True, f"away_clean_sheets_{cs}_of_6"
    return False, None


def _non_league_reliability_veto(match, home_6, away_6):
    """Block BTTS and Over in very low-tier English / non-league football.
    Soccerbase coverage for the 7th tier and below is sparse and often
    limited to 2-3 games per team — the thin-data fallbacks kick in but
    the underlying sample is still too unreliable for predictions.

    Added 2026-08-30 after Eastbourne vs Leatherhead (Isthmian League,
    7th tier): both the Over 2.5 and BTTS Yes picks lost on a match
    where the algorithm was running on ~3 games of venue data per team.
    Working on thin-data fallbacks with 2-3 matches is gambling, not
    statistical prediction.
    """
    league = str(match.get("league", "")).lower()
    non_league_keywords = [
        "isthmian", "southern league", "northern premier",
        "national league south", "national league north",
        "evostik", "pitching in", "betvictor", "southern prem",
        "isthmian prem", "npl premier",
    ]
    is_non_league = any(k in league for k in non_league_keywords)
    if not is_non_league:
        return False, None
    if len(home_6 or []) < 5 or len(away_6 or []) < 5:
        return True, "non_league_thin_data"
    return False, None


def _defensive_permeability_check(home_6, away_6):
    """Both teams must concede enough to suggest leaky defenses.

    Returns (passed, failed, detail) tuples for scoring into BTTS Yes.
    """
    passed, failed, details = [], [], {}
    if len(home_6 or []) >= 4:
        hc = sum(ga for _, ga in home_6) / max(len(home_6), 1)
        if hc >= 1.0:
            passed.append("Home defence leaky")
            details["Home defence leaky"] = f"PASS ({hc:.2f} GA/game)"
        else:
            failed.append("Home defence too solid")
            details["Home defence too solid"] = f"FAIL ({hc:.2f} GA/game < 1.0)"
    else:
        details["Home defence leaky"] = f"SKIPPED ({len(home_6 or [])} games)"
    if len(away_6 or []) >= 4:
        ac = sum(ga for _, ga in away_6) / max(len(away_6), 1)
        if ac >= 1.0:
            passed.append("Away defence leaky")
            details["Away defence leaky"] = f"PASS ({ac:.2f} GA/game)"
        else:
            failed.append("Away defence too solid")
            details["Away defence too solid"] = f"FAIL ({ac:.2f} GA/game < 1.0)"
    else:
        details["Away defence leaky"] = f"SKIPPED ({len(away_6 or [])} games)"
    return passed, failed, details


def _h2h_btts_yes_blocked_2game(home_team_id, away_team_id, target_date_str=None):
    """Relaxed H2H veto for BTTS Yes: if only 2 meetings exist and BOTH were non-BTTS, block.

    Catches tight derby matchups where the 3-meeting minimum gate would otherwise miss.
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str, limit=2)
    if len(meetings) == 2:
        non_btts = sum(1 for m in meetings if m.get("gf", 0) == 0 or m.get("ga", 0) == 0)
        if non_btts == 2:
            return True, meetings
    return False, meetings


# =============================================================================
# OVER25TIPS.COM BTTS POINT ALGORITHM (R1-R14)
# https://www.over25tips.com/both-teams-to-score-tips/
# =============================================================================
def _o25tips_venue_points(form_venue):
    """R3-R5 (home) or R8-R10 (away): goals-over-2 and 0-0 penalties on venue form."""
    pts = 0.0
    goals_over_2 = 0.0
    conceded_over_2 = 0.0
    nil_nil = 0
    for gf, ga in form_venue[:_O25TIPS_FORM_WINDOW]:
        if gf > 2:
            goals_over_2 += (gf - 2) * 0.5
        if ga > 2:
            conceded_over_2 += (ga - 2) * 0.5
        if gf == 0 and ga == 0:
            nil_nil += 1
    pts += goals_over_2 + conceded_over_2 - (nil_nil * 2)
    return pts, {
        "goals_over_2_bonus": round(goals_over_2, 1),
        "conceded_over_2_bonus": round(conceded_over_2, 1),
        "nil_nil_penalty": nil_nil * -2,
    }


def _o25tips_team_points(form_overall, form_venue, prefix):
    """R1-R5 home or R6-R10 away team point block."""
    overall_btts = sum(1 for gf, ga in form_overall[:_O25TIPS_FORM_WINDOW] if gf >= 1 and ga >= 1)
    venue_btts = sum(1 for gf, ga in form_venue[:_O25TIPS_FORM_WINDOW] if gf >= 1 and ga >= 1)
    venue_pts, venue_detail = _o25tips_venue_points(form_venue)
    total = overall_btts + venue_btts + venue_pts
    details = {
        f"{prefix}_overall_btts": overall_btts,
        f"{prefix}_venue_btts": venue_btts,
        f"{prefix}_venue_extras": round(venue_pts, 1),
        **{f"{prefix}_{k}": v for k, v in venue_detail.items()},
        f"{prefix}_subtotal": round(total, 1),
    }
    return total, details


def _o25tips_match_points(home_lambda, away_lambda):
    """R11-R14: favourite context. Uses xG lambdas when match-winner odds are unavailable."""
    if home_lambda <= 0 or away_lambda <= 0:
        return 0.0, {"match_favourite_rule": "SKIPPED (no lambda)"}
    ratio = away_lambda / home_lambda
    points = 0.0
    rule = "neutral"
    if ratio >= 1.35:
        points = -1.0
        rule = "R14 away heavy favourite (-1)"
    elif ratio >= 1.08:
        points = 2.0
        rule = "R11 away slight favourite (+2)"
    elif ratio <= 0.74:
        points = -2.0
        rule = "R13 home heavy favourite (-2)"
    elif ratio <= 0.92:
        points = 1.0
        rule = "R12 home slight favourite (+1)"
    return points, {
        "match_favourite_rule": rule,
        "lambda_ratio_away_home": round(ratio, 2),
        "match_points": points,
    }


def calculate_o25tips_btts_score(home_overall_6, home_6, away_overall_6, away_6,
                                 home_lambda, away_lambda):
    """Return (total_points, details_dict) per over25tips.com BTTS algorithm."""
    home_overall_6 = home_overall_6 or []
    home_6 = home_6 or []
    away_overall_6 = away_overall_6 or []
    away_6 = away_6 or []

    hp, hdet = _o25tips_team_points(home_overall_6, home_6, "home")
    ap, adet = _o25tips_team_points(away_overall_6, away_6, "away")
    mp, mdet = _o25tips_match_points(home_lambda, away_lambda)

    total = round(hp + ap + mp, 1)
    details = {**hdet, **adet, **mdet}
    details["o25tips_total"] = total
    details["o25tips_yes_ok"] = total >= MIN_O25TIPS_BTTS_YES_POINTS
    details["o25tips_no_ok"] = total <= MAX_O25TIPS_BTTS_NO_POINTS
    return total, details


# =============================================================================
# BTTS ALGORITHMS
# =============================================================================
def apply_btts_yes_algorithm(home_3, away_3, home_6, away_6, home_overall_6=None, away_overall_6=None):
    if len(home_3) < 2 or len(away_3) < 2:
        return None, None, {"error": "Insufficient data"}, False
    passed, failed, details = [], [], {}
    is_perfect = True

    hn3, an3 = len(home_3), len(away_3)
    h_btts_3 = sum(1 for gf, ga in home_3 if gf >= 1 and ga >= 1)
    if h_btts_3 >= _thin_count(2, 3, hn3):
        passed.append("Home BTTS (last 3)"); details["Home BTTS (last 3)"] = f"PASS ({h_btts_3}/{hn3})"
        if h_btts_3 < hn3:
            is_perfect = False
    else:
        failed.append("Home BTTS (last 3)"); details["Home BTTS (last 3)"] = f"FAIL ({h_btts_3}/{hn3})"; is_perfect = False

    a_btts_3 = sum(1 for gf, ga in away_3 if gf >= 1 and ga >= 1)
    if a_btts_3 >= _thin_count(2, 3, an3):
        passed.append("Away BTTS (last 3)"); details["Away BTTS (last 3)"] = f"PASS ({a_btts_3}/{an3})"
    else:
        failed.append("Away BTTS (last 3)"); details["Away BTTS (last 3)"] = f"FAIL ({a_btts_3}/{an3})"; is_perfect = False

    h_scored_3 = sum(1 for gf, _ in home_3 if gf >= 1)
    if h_scored_3 >= _thin_count(3, 3, hn3):
        passed.append("Home scored (last 3)"); details["Home scored (last 3)"] = f"PASS ({h_scored_3}/{hn3})"
    else:
        failed.append("Home scored (last 3)"); details["Home scored (last 3)"] = f"FAIL ({h_scored_3}/{hn3})"; is_perfect = False

    a_scored_3 = sum(1 for gf, _ in away_3 if gf >= 1)
    if a_scored_3 >= _thin_count(3, 3, an3):
        passed.append("Away scored (last 3)"); details["Away scored (last 3)"] = f"PASS ({a_scored_3}/{an3})"
    else:
        failed.append("Away scored (last 3)"); details["Away scored (last 3)"] = f"FAIL ({a_scored_3}/{an3})"; is_perfect = False

    h_conceded_3 = sum(1 for _, ga in home_3 if ga >= 1)
    if h_conceded_3 >= _thin_count(3, 3, hn3):
        passed.append("Home conceded (last 3)"); details["Home conceded (last 3)"] = f"PASS ({h_conceded_3}/{hn3})"
    else:
        failed.append("Home conceded (last 3)"); details["Home conceded (last 3)"] = f"FAIL ({h_conceded_3}/{hn3})"; is_perfect = False

    a_conceded_3 = sum(1 for _, ga in away_3 if ga >= 1)
    if a_conceded_3 >= _thin_count(3, 3, an3):
        passed.append("Away conceded (last 3)"); details["Away conceded (last 3)"] = f"PASS ({a_conceded_3}/{an3})"
    else:
        failed.append("Away conceded (last 3)"); details["Away conceded (last 3)"] = f"FAIL ({a_conceded_3}/{an3})"; is_perfect = False

    h_total_3 = sum(gf + ga for gf, ga in home_3)
    if h_total_3 >= _thin_total(5, 3, hn3):
        passed.append("Home total goals (last 3)"); details["Home total goals (last 3)"] = f"PASS ({h_total_3})"
    else:
        failed.append("Home total goals (last 3)"); details["Home total goals (last 3)"] = f"FAIL ({h_total_3})"; is_perfect = False

    a_total_3 = sum(gf + ga for gf, ga in away_3)
    if a_total_3 >= _thin_total(5, 3, an3):
        passed.append("Away total goals (last 3)"); details["Away total goals (last 3)"] = f"PASS ({a_total_3})"
    else:
        failed.append("Away total goals (last 3)"); details["Away total goals (last 3)"] = f"FAIL ({a_total_3})"; is_perfect = False

    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)
    h_btts_6 = _count_btts(home_6)
    a_btts_6 = _count_btts(away_6)

    if h_len >= 6 and h_btts_6 >= BTTS_MIN_6:
        passed.append("Home BTTS (last 6)"); details["Home BTTS (last 6)"] = f"PASS ({h_btts_6}/6)"
        if h_btts_6 < 6:
            is_perfect = False
    elif h_len >= 2 and h_btts_6 >= max(1, round(h_len * 0.5)):
        passed.append("Home BTTS (last 6)"); details["Home BTTS (last 6)"] = f"PASS-THIN ({h_btts_6}/{h_len})"
    else:
        failed.append("Home BTTS (last 6)"); details["Home BTTS (last 6)"] = f"FAIL ({h_btts_6}/{h_len})"; is_perfect = False

    if a_len >= 6 and a_btts_6 >= BTTS_MIN_6:
        passed.append("Away BTTS (last 6)"); details["Away BTTS (last 6)"] = f"PASS ({a_btts_6}/6)"
        if a_btts_6 < 6:
            is_perfect = False
    elif a_len >= 2 and a_btts_6 >= max(1, round(a_len * 0.5)):
        passed.append("Away BTTS (last 6)"); details["Away BTTS (last 6)"] = f"PASS-THIN ({a_btts_6}/{a_len})"
    else:
        failed.append("Away BTTS (last 6)"); details["Away BTTS (last 6)"] = f"FAIL ({a_btts_6}/{a_len})"; is_perfect = False

    h_scored_6 = sum(1 for gf, _ in home_6[:6] if gf >= 1)
    if h_len >= 6 and h_scored_6 >= 4:
        passed.append("Home scored (last 6)"); details["Home scored (last 6)"] = f"PASS ({h_scored_6}/6)"
    elif h_len >= 2 and h_scored_6 >= max(1, round(h_len * 0.66)):
        passed.append("Home scored (last 6)"); details["Home scored (last 6)"] = f"PASS-THIN ({h_scored_6}/{h_len})"
    else:
        failed.append("Home scored (last 6)"); details["Home scored (last 6)"] = f"FAIL ({h_scored_6}/{h_len})"; is_perfect = False

    a_scored_6 = sum(1 for gf, _ in away_6[:6] if gf >= 1)
    if a_len >= 6 and a_scored_6 >= 4:
        passed.append("Away scored (last 6)"); details["Away scored (last 6)"] = f"PASS ({a_scored_6}/6)"
    elif a_len >= 2 and a_scored_6 >= max(1, round(a_len * 0.66)):
        passed.append("Away scored (last 6)"); details["Away scored (last 6)"] = f"PASS-THIN ({a_scored_6}/{a_len})"
    else:
        failed.append("Away scored (last 6)"); details["Away scored (last 6)"] = f"FAIL ({a_scored_6}/{a_len})"; is_perfect = False

    h_conceded_6 = sum(1 for _, ga in home_6[:6] if ga >= 1)
    if h_len >= 6 and h_conceded_6 >= 4:
        passed.append("Home conceded (last 6)"); details["Home conceded (last 6)"] = f"PASS ({h_conceded_6}/6)"
    elif h_len >= 2 and h_conceded_6 >= max(1, round(h_len * 0.66)):
        passed.append("Home conceded (last 6)"); details["Home conceded (last 6)"] = f"PASS-THIN ({h_conceded_6}/{h_len})"
    else:
        failed.append("Home conceded (last 6)"); details["Home conceded (last 6)"] = f"FAIL ({h_conceded_6}/{h_len})"; is_perfect = False

    a_conceded_6 = sum(1 for _, ga in away_6[:6] if ga >= 1)
    if a_len >= 6 and a_conceded_6 >= 4:
        passed.append("Away conceded (last 6)"); details["Away conceded (last 6)"] = f"PASS ({a_conceded_6}/6)"
    elif a_len >= 2 and a_conceded_6 >= max(1, round(a_len * 0.66)):
        passed.append("Away conceded (last 6)"); details["Away conceded (last 6)"] = f"PASS-THIN ({a_conceded_6}/{a_len})"
    else:
        failed.append("Away conceded (last 6)"); details["Away conceded (last 6)"] = f"FAIL ({a_conceded_6}/{a_len})"; is_perfect = False

    h_cs = _count_clean_sheets(home_6)
    if h_len >= 6 and h_cs <= 2:
        passed.append("Home clean sheet cap (6)"); details["Home clean sheet cap (6)"] = f"PASS ({h_cs}/6 CS)"
    elif h_len >= 2 and h_cs <= max(0, round(h_len * 0.33)):
        passed.append("Home clean sheet cap (6)"); details["Home clean sheet cap (6)"] = f"PASS-THIN ({h_cs}/{h_len})"
    else:
        failed.append("Home clean sheet cap (6)"); details["Home clean sheet cap (6)"] = f"FAIL ({h_cs}/{h_len})"; is_perfect = False

    a_cs = _count_clean_sheets(away_6)
    if a_len >= 6 and a_cs <= 2:
        passed.append("Away clean sheet cap (6)"); details["Away clean sheet cap (6)"] = f"PASS ({a_cs}/6 CS)"
    elif a_len >= 2 and a_cs <= max(0, round(a_len * 0.33)):
        passed.append("Away clean sheet cap (6)"); details["Away clean sheet cap (6)"] = f"PASS-THIN ({a_cs}/{a_len})"
    else:
        failed.append("Away clean sheet cap (6)"); details["Away clean sheet cap (6)"] = f"FAIL ({a_cs}/{a_len})"; is_perfect = False

    home_overall_6 = home_overall_6 or []
    away_overall_6 = away_overall_6 or []
    if home_overall_6 and away_overall_6:
        overall_btts = _count_btts(home_overall_6) + _count_btts(away_overall_6)
        o_len = min(len(home_overall_6), 6) + min(len(away_overall_6), 6)
        if o_len >= 10 and overall_btts >= 6:
            passed.append("Overall BTTS activity (6)"); details["Overall BTTS activity (6)"] = f"PASS ({overall_btts}/{o_len})"
        elif overall_btts >= round(o_len * 0.55):
            passed.append("Overall BTTS activity (6)"); details["Overall BTTS activity (6)"] = f"PASS-THIN ({overall_btts}/{o_len})"
        else:
            failed.append("Overall BTTS activity (6)"); details["Overall BTTS activity (6)"] = f"FAIL ({overall_btts}/{o_len})"; is_perfect = False

    return passed, failed, details, is_perfect


def apply_btts_no_algorithm(home_3, away_3, home_6, away_6, home_overall_6=None, away_overall_6=None):
    if len(home_3) < 2 or len(away_3) < 2:
        return None, None, {"error": "Insufficient data"}, False
    passed, failed, details = [], [], {}
    is_perfect = True

    hn3, an3 = len(home_3), len(away_3)
    h_nb_3 = sum(1 for gf, ga in home_3 if gf == 0 or ga == 0)
    if h_nb_3 >= _thin_count(2, 3, hn3):
        passed.append("Home non-BTTS (last 3)"); details["Home non-BTTS (last 3)"] = f"PASS ({h_nb_3}/{hn3})"
    else:
        failed.append("Home non-BTTS (last 3)"); details["Home non-BTTS (last 3)"] = f"FAIL ({h_nb_3}/{hn3})"; is_perfect = False

    a_nb_3 = sum(1 for gf, ga in away_3 if gf == 0 or ga == 0)
    if a_nb_3 >= _thin_count(2, 3, an3):
        passed.append("Away non-BTTS (last 3)"); details["Away non-BTTS (last 3)"] = f"PASS ({a_nb_3}/{an3})"
    else:
        failed.append("Away non-BTTS (last 3)"); details["Away non-BTTS (last 3)"] = f"FAIL ({a_nb_3}/{an3})"; is_perfect = False

    h_blanked_3 = sum(1 for gf, _ in home_3 if gf == 0)
    if h_blanked_3 >= _thin_count(1, 3, hn3):
        passed.append("Home blanked (last 3)"); details["Home blanked (last 3)"] = f"PASS ({h_blanked_3}/{hn3})"
    else:
        failed.append("Home blanked (last 3)"); details["Home blanked (last 3)"] = f"FAIL ({h_blanked_3}/{hn3})"; is_perfect = False

    a_blanked_3 = sum(1 for gf, _ in away_3 if gf == 0)
    if a_blanked_3 >= _thin_count(1, 3, an3):
        passed.append("Away blanked (last 3)"); details["Away blanked (last 3)"] = f"PASS ({a_blanked_3}/{an3})"
    else:
        failed.append("Away blanked (last 3)"); details["Away blanked (last 3)"] = f"FAIL ({a_blanked_3}/{an3})"; is_perfect = False

    h_cs_3 = sum(1 for _, ga in home_3 if ga == 0)
    if h_cs_3 >= _thin_count(1, 3, hn3):
        passed.append("Home clean sheet (last 3)"); details["Home clean sheet (last 3)"] = f"PASS ({h_cs_3}/{hn3})"
    else:
        failed.append("Home clean sheet (last 3)"); details["Home clean sheet (last 3)"] = f"FAIL ({h_cs_3}/{hn3})"; is_perfect = False

    a_cs_3 = sum(1 for _, ga in away_3 if ga == 0)
    if a_cs_3 >= _thin_count(1, 3, an3):
        passed.append("Away clean sheet (last 3)"); details["Away clean sheet (last 3)"] = f"PASS ({a_cs_3}/{an3})"
    else:
        failed.append("Away clean sheet (last 3)"); details["Away clean sheet (last 3)"] = f"FAIL ({a_cs_3}/{an3})"; is_perfect = False

    h_total_3 = sum(gf + ga for gf, ga in home_3)
    if h_total_3 <= _thin_total(6, 3, hn3):
        passed.append("Home total goals cap (last 3)"); details["Home total goals cap (last 3)"] = f"PASS ({h_total_3})"
    else:
        failed.append("Home total goals cap (last 3)"); details["Home total goals cap (last 3)"] = f"FAIL ({h_total_3})"; is_perfect = False

    a_total_3 = sum(gf + ga for gf, ga in away_3)
    if a_total_3 <= _thin_total(6, 3, an3):
        passed.append("Away total goals cap (last 3)"); details["Away total goals cap (last 3)"] = f"PASS ({a_total_3})"
    else:
        failed.append("Away total goals cap (last 3)"); details["Away total goals cap (last 3)"] = f"FAIL ({a_total_3})"; is_perfect = False

    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)
    h_nb_6 = _count_non_btts(home_6)
    a_nb_6 = _count_non_btts(away_6)

    if h_len >= 6 and h_nb_6 >= NON_BTTS_MIN_6:
        passed.append("Home non-BTTS (last 6)"); details["Home non-BTTS (last 6)"] = f"PASS ({h_nb_6}/6)"
    elif h_len >= 2 and h_nb_6 >= max(1, round(h_len * 0.5)):
        passed.append("Home non-BTTS (last 6)"); details["Home non-BTTS (last 6)"] = f"PASS-THIN ({h_nb_6}/{h_len})"
    else:
        failed.append("Home non-BTTS (last 6)"); details["Home non-BTTS (last 6)"] = f"FAIL ({h_nb_6}/{h_len})"; is_perfect = False

    if a_len >= 6 and a_nb_6 >= NON_BTTS_MIN_6:
        passed.append("Away non-BTTS (last 6)"); details["Away non-BTTS (last 6)"] = f"PASS ({a_nb_6}/6)"
    elif a_len >= 2 and a_nb_6 >= max(1, round(a_len * 0.5)):
        passed.append("Away non-BTTS (last 6)"); details["Away non-BTTS (last 6)"] = f"PASS-THIN ({a_nb_6}/{a_len})"
    else:
        failed.append("Away non-BTTS (last 6)"); details["Away non-BTTS (last 6)"] = f"FAIL ({a_nb_6}/{a_len})"; is_perfect = False

    h_under_3 = sum(1 for gf, ga in home_3 if gf + ga < 2.5)
    if h_under_3 >= _thin_count(2, 3, hn3):
        passed.append("Home under 2.5 (last 3)"); details["Home under 2.5 (last 3)"] = f"PASS ({h_under_3}/{hn3})"
    else:
        failed.append("Home under 2.5 (last 3)"); details["Home under 2.5 (last 3)"] = f"FAIL ({h_under_3}/{hn3})"; is_perfect = False

    a_under_3 = sum(1 for gf, ga in away_3 if gf + ga < 2.5)
    if a_under_3 >= _thin_count(2, 3, an3):
        passed.append("Away under 2.5 (last 3)"); details["Away under 2.5 (last 3)"] = f"PASS ({a_under_3}/{an3})"
    else:
        failed.append("Away under 2.5 (last 3)"); details["Away under 2.5 (last 3)"] = f"FAIL ({a_under_3}/{an3})"; is_perfect = False

    h_cs_6 = _count_clean_sheets(home_6)
    if h_len >= 6 and h_cs_6 >= 2:
        passed.append("Home clean sheets (last 6)"); details["Home clean sheets (last 6)"] = f"PASS ({h_cs_6}/6)"
    elif h_len >= 2 and h_cs_6 >= max(1, round(h_len * 0.33)):
        passed.append("Home clean sheets (last 6)"); details["Home clean sheets (last 6)"] = f"PASS-THIN ({h_cs_6}/{h_len})"
    else:
        failed.append("Home clean sheets (last 6)"); details["Home clean sheets (last 6)"] = f"FAIL ({h_cs_6}/{h_len})"; is_perfect = False

    a_failed_6 = _count_failed_to_score(away_6)
    if a_len >= 6 and a_failed_6 >= 2:
        passed.append("Away failed to score (last 6)"); details["Away failed to score (last 6)"] = f"PASS ({a_failed_6}/6)"
    elif a_len >= 2 and a_failed_6 >= max(1, round(a_len * 0.33)):
        passed.append("Away failed to score (last 6)"); details["Away failed to score (last 6)"] = f"PASS-THIN ({a_failed_6}/{a_len})"
    else:
        failed.append("Away failed to score (last 6)"); details["Away failed to score (last 6)"] = f"FAIL ({a_failed_6}/{a_len})"; is_perfect = False

    home_overall_6 = home_overall_6 or []
    away_overall_6 = away_overall_6 or []
    if home_overall_6 and away_overall_6:
        overall_nb = _count_non_btts(home_overall_6) + _count_non_btts(away_overall_6)
        o_len = min(len(home_overall_6), 6) + min(len(away_overall_6), 6)
        if o_len >= 10 and overall_nb >= 7:
            passed.append("Overall non-BTTS activity (6)"); details["Overall non-BTTS activity (6)"] = f"PASS ({overall_nb}/{o_len})"
        elif overall_nb >= round(o_len * 0.6):
            passed.append("Overall non-BTTS activity (6)"); details["Overall non-BTTS activity (6)"] = f"PASS-THIN ({overall_nb}/{o_len})"
        else:
            failed.append("Overall non-BTTS activity (6)"); details["Overall non-BTTS activity (6)"] = f"FAIL ({overall_nb}/{o_len})"; is_perfect = False

    return passed, failed, details, is_perfect


# =============================================================================
# POISSON / LAMBDAS
# =============================================================================
def _exponential_form_averages(form_tuples, halflife=_MIN_FORM_HALFLIFE):
    if not form_tuples:
        return 0.0, 0.0, 0.0
    w_sum = gf_sum = ga_sum = 0.0
    for idx, (gf, ga) in enumerate(form_tuples):
        w = 0.5 ** (idx / halflife)
        w_sum += w
        gf_sum += w * gf
        ga_sum += w * ga
    if w_sum <= 0:
        return 0.0, 0.0, 0.0
    return gf_sum / w_sum, ga_sum / w_sum, w_sum


def _is_weak_roi_league(league_name):
    name = str(league_name or "").strip().lower()
    return any(k in name for k in _WEAK_ROI_LEAGUE_KEYWORDS)


def _load_league_baselines():
    if _LEAGUE_BASELINE_CACHE:
        return _LEAGUE_BASELINE_CACHE
    default = (1.45, 1.20, 1.35, 1.25)
    history_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prediction_history.json")
    if not os.path.exists(history_path):
        _LEAGUE_BASELINE_CACHE["_default"] = default
        return _LEAGUE_BASELINE_CACHE
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        _LEAGUE_BASELINE_CACHE["_default"] = default
        return _LEAGUE_BASELINE_CACHE
    league_stats = defaultdict(lambda: {"h_gf": 0.0, "h_ga": 0.0, "a_gf": 0.0, "a_ga": 0.0, "n": 0})
    for market in ("home_win", "over_under", "btts"):
        for row in data.get(market, []) or []:
            final_score = row.get("final_score")
            if not final_score or "-" not in str(final_score):
                continue
            try:
                hg, ag = str(final_score).split("-", 1)
                hg, ag = int(hg.strip()), int(ag.strip())
            except ValueError:
                continue
            lg = row.get("league", "")
            if not lg:
                continue
            s = league_stats[lg]
            s["h_gf"] += hg
            s["h_ga"] += ag
            s["a_gf"] += ag
            s["a_ga"] += hg
            s["n"] += 1
    global_n = max(1, sum(s["n"] for s in league_stats.values()))
    fallback = (
        sum(s["h_gf"] for s in league_stats.values()) / global_n,
        sum(s["a_gf"] for s in league_stats.values()) / global_n,
        sum(s["h_ga"] for s in league_stats.values()) / global_n,
        sum(s["a_ga"] for s in league_stats.values()) / global_n,
    )
    if not all(fallback) or fallback[0] < 0.6:
        fallback = default
    _LEAGUE_BASELINE_CACHE["_default"] = fallback
    for lg, s in league_stats.items():
        n = s["n"]
        if n < 5:
            _LEAGUE_BASELINE_CACHE[lg] = fallback
            continue
        ha, aa, hd, ad = s["h_gf"] / n, s["a_gf"] / n, s["h_ga"] / n, s["a_ga"] / n
        _LEAGUE_BASELINE_CACHE[lg] = (ha, aa, hd, ad) if ha >= 0.5 else fallback
    return _LEAGUE_BASELINE_CACHE


def _league_baselines(league_name):
    cache = _load_league_baselines()
    return cache.get(league_name, cache.get("_default", (1.45, 1.20, 1.35, 1.25)))


def get_match_lambdas(home_6, away_6, league_name=None):
    bl = _league_baselines(league_name or "")
    h_att_b, a_att_b, h_def_b, a_def_b = bl
    h_gf, h_ga, _ = _exponential_form_averages(home_6 or [])
    if not (home_6 or []):
        h_gf, h_ga = h_att_b, a_def_b
    a_gf, a_ga, _ = _exponential_form_averages(away_6 or [])
    if not (away_6 or []):
        a_gf, a_ga = a_att_b, h_def_b
    shrink = SHRINKAGE_WEIGHT
    if len(home_6 or []) < _MIN_DATA_GAMES or len(away_6 or []) < _MIN_DATA_GAMES:
        shrink = max(0.45, SHRINKAGE_WEIGHT - 0.15)
    h_attack = shrink * h_gf + (1 - shrink) * h_att_b
    h_defense = shrink * h_ga + (1 - shrink) * a_def_b
    a_attack = shrink * a_gf + (1 - shrink) * a_att_b
    a_defense = shrink * a_ga + (1 - shrink) * h_def_b
    home_lambda = h_attack * (a_defense / max(0.5, h_att_b))
    away_lambda = a_attack * (h_defense / max(0.5, a_att_b))
    return (
        round(max(0.5, min(3.8, home_lambda)), 2),
        round(max(0.5, min(3.8, away_lambda)), 2),
    )


def poisson_pmf(k, lam):
    if lam <= 0:
        return 0.0
    return (math.exp(-lam) * (lam ** k)) / math.factorial(k)


def calculate_poisson_btts_yes(home_lambda, away_lambda):
    h_zero = poisson_pmf(0, home_lambda)
    a_zero = poisson_pmf(0, away_lambda)
    prob = (1.0 - h_zero) * (1.0 - a_zero)
    return round(min(99.0, max(1.0, prob * 100.0)), 1)


def calculate_poisson_btts_no(home_lambda, away_lambda):
    return round(100.0 - calculate_poisson_btts_yes(home_lambda, away_lambda), 1)


def lambda_gate_passes(home_lambda, away_lambda, side):
    combined = home_lambda + away_lambda
    if side == "yes":
        return combined >= MIN_COMBINED_LAMBDA_BTTS_YES
    if side == "no":
        return combined <= MAX_COMBINED_LAMBDA_BTTS_NO
    return True


def data_volume_penalty(home_6, away_6):
    n = min(len(home_6 or []), len(away_6 or []))
    if n >= _MIN_DATA_GAMES:
        return 1.0
    if n >= 4:
        return 0.95
    if n >= 3:
        return 0.86
    if n >= 2:
        return 0.75
    return 0.60


def compute_confidence_score(rule_score, max_score, model_prob_pct, decimal_odds, data_mult=1.0):
    rule_component = max(0.0, min(1.0, rule_score / max(max_score, 1)))
    model_component = max(0.0, min(1.0, model_prob_pct / 100.0))
    implied = 1.0 / max(1.05, decimal_odds)
    edge_component = max(0.0, min(1.0, (model_prob_pct / 100.0 - implied) + 0.5))
    raw = _WEIGHT_RULES * rule_component + _WEIGHT_MODEL * model_component + _WEIGHT_EDGE * edge_component
    return max(0.0, min(1.0, raw * data_mult))


def tier_from_confidence(score, side, home_lambda, away_lambda, is_perfect=True):
    combined = home_lambda + away_lambda
    premium_ok = (
        (side == "yes" and combined >= PREMIUM_COMBINED_LAMBDA_BTTS_YES)
        or (side == "no" and combined <= PREMIUM_COMBINED_LAMBDA_BTTS_NO)
    )
    if score >= _TIER_PREMIUM_CUTOFF and premium_ok and is_perfect:
        return "perfect"
    if score >= _TIER_SOLID_CUTOFF:
        return "qualified"
    return "close"


# calculate_kelly/apply_portfolio_kelly logic itself is imported from
# utils.py. Thin wrappers below preserve this file's original call
# signatures (market-specific default odds of 1.90; "side_key" here is
# the same thing utils.py calls "bet_type" — a nested dict key).
def calculate_kelly(prob, decimal_odds=1.90, use_half=True):
    return _shared_calculate_kelly(prob, decimal_odds, use_half)


def apply_portfolio_kelly(recommendations, side_key, bankroll, max_exposure=MAX_TOTAL_EXPOSURE):
    return _shared_apply_portfolio_kelly(recommendations, side_key, bankroll, max_exposure)


# =============================================================================
# MATCH PROCESSING
# =============================================================================
def process_single_match(match, target_date, odds_yes=DEFAULT_ODDS_BTTS_YES, odds_no=DEFAULT_ODDS_BTTS_NO):
    try:
        league_name = match.get("league", "")
        home_3 = get_team_form(match["home_team_id"], True, 3, target_date)
        away_3 = get_team_form(match["away_team_id"], False, 3, target_date)
        home_6 = get_team_form(match["home_team_id"], True, 6, target_date)
        away_6 = get_team_form(match["away_team_id"], False, 6, target_date)
        home_overall_6 = get_team_overall_form(match["home_team_id"], 6, target_date)
        away_overall_6 = get_team_overall_form(match["away_team_id"], 6, target_date)

        if len(home_3) < 2 or len(away_3) < 2:
            return {"status": "insufficient"}

        data_mult = data_volume_penalty(home_6, away_6)
        home_lambda, away_lambda = get_match_lambdas(home_6, away_6, league_name=league_name)
        btts_yes_pct = calculate_poisson_btts_yes(home_lambda, away_lambda)
        btts_no_pct = calculate_poisson_btts_no(home_lambda, away_lambda)

        yes_result = apply_btts_yes_algorithm(home_3, away_3, home_6, away_6, home_overall_6, away_overall_6)
        no_result = apply_btts_no_algorithm(home_3, away_3, home_6, away_6, home_overall_6, away_overall_6)
        if yes_result[0] is None or no_result[0] is None:
            return {"status": "insufficient"}

        yes_passed, _, yes_details, yes_perfect = yes_result
        no_passed, _, no_details, no_perfect = no_result
        yes_score = len(yes_passed)
        no_score = len(no_passed)
        if _chaos_btts_bonus(home_6, away_6):
            yes_score += 1
        if _compact_btts_no_bonus(home_6, away_6):
            no_score += 1

        yes_gate = lambda_gate_passes(home_lambda, away_lambda, "yes")
        no_gate = lambda_gate_passes(home_lambda, away_lambda, "no")
        btts_gate = _btts_gate_passes(home_6, away_6)
        non_btts_gate = _non_btts_gate_passes(home_6, away_6)
        h2h_btts_yes_blocked, h2h_btts_yes_meetings = _h2h_btts_yes_blocked(
            match["home_team_id"], match["away_team_id"], target_date
        )
        h2h_btts_yes_blocked_2, h2h_btts_yes_meetings_2 = _h2h_btts_yes_blocked_2game(
            match["home_team_id"], match["away_team_id"], target_date
        )
        h2h_btts_no_blocked, h2h_btts_no_meetings = _h2h_btts_no_blocked(
            match["home_team_id"], match["away_team_id"], target_date
        )
        o25tips_total, o25tips_details = calculate_o25tips_btts_score(
            home_overall_6, home_6, away_overall_6, away_6, home_lambda, away_lambda
        )
        o25tips_yes_ok = o25tips_details["o25tips_yes_ok"]
        o25tips_no_ok = o25tips_details["o25tips_no_ok"]

        drought_veto, drought_reason = _scoring_drought_veto(home_3, away_3)
        wall_veto, wall_reason = _defensive_wall_veto(home_3, away_3)
        mismatch_veto, mismatch_reason = _lambda_mismatch_veto(home_lambda, away_lambda)
        home_overall_1 = get_team_overall_form(match["home_team_id"], 1, target_date)
        away_overall_1 = get_team_overall_form(match["away_team_id"], 1, target_date)
        shutout_shock_veto, shutout_shock_reason = _recent_shutout_shock_veto(
            home_3, away_3, home_overall_1, away_overall_1
        )
        extended_drought_veto, extended_drought_reason = _extended_scoring_drought_veto(home_3, away_3)
        min_attack_veto, min_attack_reason = _minimum_attack_rate_veto(home_3, away_3)
        cup_veto, cup_reason = _cup_mismatch_veto(match, home_lambda, away_lambda)
        cs_freq_veto, cs_freq_reason = _clean_sheet_frequency_veto(home_6, away_6)
        non_league_veto, non_league_reason = _non_league_reliability_veto(match, home_6, away_6)
        perm_passed, perm_failed, perm_details = _defensive_permeability_check(home_6, away_6)

        yes_passed = yes_passed + perm_passed
        yes_details = {**yes_details, **perm_details}
        yes_score = len(yes_passed)
        if _chaos_btts_bonus(home_6, away_6):
            yes_score += 1

        weak = _is_weak_roi_league(league_name)
        thin_gap = max(0, 6 - min(len(home_6), len(away_6)))
        base_min_y = MAX_BTTS_YES_SCORE - 3 if weak else MAX_BTTS_YES_SCORE - 4
        yes_min = max(9, base_min_y + thin_gap)
        base_min_n = MAX_BTTS_NO_SCORE - 3 if weak else MAX_BTTS_NO_SCORE - 4
        no_min = max(8, base_min_n + thin_gap)
        yes_qualifies = (
            bool(yes_passed) and yes_score >= yes_min and yes_gate
            and btts_gate and o25tips_yes_ok
            and not h2h_btts_yes_blocked
            and not h2h_btts_yes_blocked_2
            and not drought_veto
            and not wall_veto
            and not mismatch_veto
            and not shutout_shock_veto
            and not extended_drought_veto
            and not min_attack_veto
            and not cup_veto
            and not cs_freq_veto
            and not non_league_veto
        )
        no_qualifies = (
            bool(no_passed) and no_score >= no_min and no_gate
            and non_btts_gate and o25tips_no_ok
            and not h2h_btts_no_blocked
            and not non_league_veto
        )

        min_venue = min(len(home_6 or []), len(away_6 or []))
        if yes_qualifies and min_venue < 4:
            home_reliable = (
                len(home_3 or []) >= 3
                and all(gf >= 1 for gf, _ in home_3)
                and all(ga >= 1 for _, ga in home_3)
            )
            away_reliable = (
                len(away_3 or []) >= 3
                and all(gf >= 1 for gf, _ in away_3)
                and all(ga >= 1 for _, ga in away_3)
            )
            if not (home_reliable and away_reliable):
                yes_qualifies = False

        league_mult = _WEAK_ROI_MULTIPLIER if weak else 1.0
        final_mult = data_mult * league_mult

        yes_conf = compute_confidence_score(yes_score, MAX_BTTS_YES_SCORE + 1, btts_yes_pct, odds_yes, final_mult)
        no_conf = compute_confidence_score(no_score, MAX_BTTS_NO_SCORE + 1, btts_no_pct, odds_no, final_mult)
        yes_tier = tier_from_confidence(yes_conf, "yes", home_lambda, away_lambda, yes_perfect) if yes_qualifies else None
        no_tier = tier_from_confidence(no_conf, "no", home_lambda, away_lambda, no_perfect) if no_qualifies else None

        home_last_2_ok = True
        away_last_2_ok = True
        last_2_scored_reason = None
        if yes_tier == "perfect":
            if len(home_3 or []) >= 2:
                home_last_2_scored = all(gf > 0 for gf, _ in home_3[:2])
                if not home_last_2_scored:
                    home_last_2_ok = False
                    last_2_scored_reason = "home_blanked_in_last_2"
            if len(away_3 or []) >= 2:
                away_last_2_scored = all(gf > 0 for gf, _ in away_3[:2])
                if not away_last_2_scored:
                    away_last_2_ok = False
                    last_2_scored_reason = ("away_blanked_in_last_2" if not last_2_scored_reason
                                            else f"both_blanked_in_last_2")
            if not (home_last_2_ok and away_last_2_ok):
                yes_tier = "qualified"

        yes_kelly = calculate_kelly(btts_yes_pct / 100.0, odds_yes) if yes_qualifies else 0.0
        no_kelly = calculate_kelly(btts_no_pct / 100.0, odds_no) if no_qualifies else 0.0

        yes_confidence = "HIGH" if btts_yes_pct >= 58 else "MEDIUM" if btts_yes_pct >= 52 else "LOW"
        no_confidence = "HIGH" if btts_no_pct >= 58 else "MEDIUM" if btts_no_pct >= 52 else "LOW"

        regressions = []
        if h2h_btts_yes_blocked:
            regressions.append(f"h2h non-btts bogey ({len(h2h_btts_yes_meetings)} meetings)")
        if h2h_btts_yes_blocked_2:
            regressions.append(f"h2h 2-game non-btts bogey ({len(h2h_btts_yes_meetings_2)} meetings)")
        if drought_veto:
            regressions.append(f"scoring drought ({drought_reason})")
        if wall_veto:
            regressions.append(f"defensive wall ({wall_reason})")
        if mismatch_veto:
            regressions.append(f"attack mismatch ({mismatch_reason})")
        if shutout_shock_veto:
            regressions.append(f"recent shutout shock ({shutout_shock_reason})")
        if extended_drought_veto:
            regressions.append(f"extended scoring drought ({extended_drought_reason})")
        if min_attack_veto:
            regressions.append(f"minimum attack rate ({min_attack_reason})")
        if cup_veto:
            regressions.append(f"cup tier mismatch ({cup_reason})")
        if cs_freq_veto:
            regressions.append(f"clean-sheet frequency ({cs_freq_reason})")
        if non_league_veto:
            regressions.append(f"non-league thin data ({non_league_reason})")
        if yes_tier == "qualified" and last_2_scored_reason:
            regressions.append(f"perfect tier downgrade ({last_2_scored_reason})")
        if h2h_btts_no_blocked:
            regressions.append(f"h2h btts bogey ({len(h2h_btts_no_meetings)} meetings)")

        return {
            "status": "success",
            "data": {
                "match": match,
                "yes": {
                    "score": yes_score,
                    "passed": yes_passed,
                    "details": yes_details,
                    "is_perfect": yes_perfect,
                    "tier": yes_tier,
                    "confidence_score": round(yes_conf * 100, 1),
                    "prob": btts_yes_pct,
                    "confidence": yes_confidence,
                    "kelly": round(yes_kelly * 100, 2),
                    "gate_passed": yes_gate,
                    "form_gate_passed": btts_gate,
                    "h2h_blocked": h2h_btts_yes_blocked,
                    "h2h_blocked_2game": h2h_btts_yes_blocked_2,
                    "h2h_meetings": len(h2h_btts_yes_meetings),
                    "h2h_meetings_2game": len(h2h_btts_yes_meetings_2),
                    "scoring_drought_veto": drought_veto,
                    "scoring_drought_reason": drought_reason,
                    "defensive_wall_veto": wall_veto,
                    "defensive_wall_reason": wall_reason,
                    "lambda_mismatch_veto": mismatch_veto,
                    "lambda_mismatch_reason": mismatch_reason,
                    "shutout_shock_veto": shutout_shock_veto,
                    "shutout_shock_reason": shutout_shock_reason,
                    "non_league_veto": non_league_veto,
                    "non_league_reason": non_league_reason,
                    "perfect_tier_downgrade_reason": last_2_scored_reason,
                    "o25tips_points": o25tips_total,
                    "o25tips_passed": o25tips_yes_ok,
                    "min_score_threshold": yes_min,
                },
                "no": {
                    "score": no_score,
                    "passed": no_passed,
                    "details": no_details,
                    "is_perfect": no_perfect,
                    "tier": no_tier,
                    "confidence_score": round(no_conf * 100, 1),
                    "prob": btts_no_pct,
                    "confidence": no_confidence,
                    "kelly": round(no_kelly * 100, 2),
                    "gate_passed": no_gate,
                    "form_gate_passed": non_btts_gate,
                    "h2h_blocked": h2h_btts_no_blocked,
                    "h2h_meetings": len(h2h_btts_no_meetings),
                    "non_league_veto": non_league_veto,
                    "non_league_reason": non_league_reason,
                    "o25tips_points": o25tips_total,
                    "o25tips_passed": o25tips_no_ok,
                    "min_score_threshold": no_min,
                },
                "poisson": {
                    "home_lambda": home_lambda,
                    "away_lambda": away_lambda,
                    "combined_lambda": round(home_lambda + away_lambda, 2),
                    "btts_yes_prob": btts_yes_pct,
                    "btts_no_prob": btts_no_pct,
                },
                "o25tips": o25tips_details,
                "guards": {
                    "weak_roi_league": weak,
                    "chaos_btts_bonus": _chaos_btts_bonus(home_6, away_6),
                    "compact_btts_no_bonus": _compact_btts_no_bonus(home_6, away_6),
                    "btts_gate_passed": btts_gate,
                    "non_btts_gate_passed": non_btts_gate,
                    "h2h_btts_yes_blocked": h2h_btts_yes_blocked,
                    "h2h_btts_yes_blocked_2game": h2h_btts_yes_blocked_2,
                    "h2h_btts_no_blocked": h2h_btts_no_blocked,
                    "h2h_btts_yes_meetings": len(h2h_btts_yes_meetings),
                    "h2h_btts_yes_meetings_2game": len(h2h_btts_yes_meetings_2),
                    "h2h_btts_no_meetings": len(h2h_btts_no_meetings),
                    "scoring_drought_veto": drought_veto,
                    "scoring_drought_reason": drought_reason,
                    "defensive_wall_veto": wall_veto,
                    "defensive_wall_reason": wall_reason,
                    "lambda_mismatch_veto": mismatch_veto,
                    "lambda_mismatch_reason": mismatch_reason,
                    "shutout_shock_veto": shutout_shock_veto,
                    "shutout_shock_reason": shutout_shock_reason,
                    "non_league_veto": non_league_veto,
                    "non_league_reason": non_league_reason,
                    "perfect_tier_downgrade_reason": last_2_scored_reason,
                    "regression_penalty_applied": regressions,
                },
            },
        }
    except Exception as e:
        logger.error(f"Processing failed for {match.get('home')} vs {match.get('away')}: {e}", exc_info=True)
        return {"status": "error"}


# =============================================================================
# REPORTING
# =============================================================================
def _append_btts_pick(lines, idx, item, side, odds, detailed, compact=False):
    m = item["match"]
    p = item["poisson"]
    tgt = item[side]
    prob_key = "btts_yes_prob" if side == "yes" else "btts_no_prob"
    label = "BTTS Yes" if side == "yes" else "BTTS No"
    market = MARKET_BTTS_YES if side == "yes" else MARKET_BTTS_NO
    max_score = MAX_BTTS_YES_SCORE + 1 if side == "yes" else MAX_BTTS_NO_SCORE + 1
    if compact:
        lines.append(format_compact_pick_line(
            m["home"], m["away"], "BTTS+" if side == "yes" else "BTTS-",
            tgt.get("tier"), p[prob_key], m.get("date"),
        ))
        return
    extra = None
    if detailed:
        extra = format_vip_extra_lines(
            tgt["kelly"], odds, tgt["score"], max_score,
            home_lambda=p["home_lambda"], away_lambda=p["away_lambda"],
        )
    categories = describe_pick_categories(
        m["home"], m["away"], m.get("league", ""),
        market=market,
        tier=tgt.get("tier"),
        weak_roi_league=bool((item.get("guards") or {}).get("weak_roi_league")),
    )
    lines.extend(format_pick_block(
        idx, m["home"], m["away"], m["date"],
        f"{label} · {format_confidence_label(tgt['confidence'])} ({p[prob_key]}%)",
        extra,
        league=m.get("league"),
        categories=categories,
    ))


def build_report(yes_perfect, yes_qualified, yes_close,
                 no_perfect, no_qualified, no_close,
                 scanned_dates, odds_yes, odds_no, detailed=False, compact=False,
                 include_yesterday=True, include_header=True, include_footer=True,
                 report_date=None):
    included_yes = [
        item for item in (yes_perfect + yes_qualified + yes_close)
        if not is_static_blocked_fixture(item.get("match", {}))
    ]
    included_no = [
        item for item in (no_perfect + no_qualified + no_close)
        if not is_static_blocked_fixture(item.get("match", {}))
    ]
    if report_date:
        included_yes = filter_pick_items_by_date(included_yes, report_date)
        included_no = filter_pick_items_by_date(included_no, report_date)
    base_date = scanned_dates[0] if scanned_dates else datetime.now().strftime("%Y-%m-%d")

    lines = []
    if report_date and not include_header and not compact:
        lines.append(f"📅 Picks for {report_date}")
        lines.append("")
    if not compact:
        if include_header:
            lines.append("⚽️ BTTS picks")
            lines.append("")
            if len(scanned_dates) > 1:
                lines.append(f"Dates: {scanned_dates[0]} to {scanned_dates[-1]}")
            else:
                lines.append(f"Date: {base_date}")
            lines.append("")
            if include_yesterday:
                append_yesterday_section(lines, "btts", detailed=detailed)
    elif compact:
        def _append_compact_tier_group(tiers_items, side_label, market_name, prob_key_name):
            has_group = False
            for tier_header, items in tiers_items:
                if not items:
                    continue
                if not has_group:
                    lines.append(f"▸ {side_label}")
                    has_group = True
                lines.append(f"  {tier_header}")
                for item in items:
                    lines.append(f"  {format_compact_pick_line(
                        item['match']['home'], item['match']['away'],
                        market_name,
                        (item.get(market_name) or {}).get('tier'),
                        (item.get('poisson') or {}).get(prob_key_name),
                        item['match'].get('date'),
                    )}")
            return has_group

        y_perfect = [p for p in included_yes if p in yes_perfect]
        y_qualified = [p for p in included_yes if p in yes_qualified]
        y_close = [p for p in included_yes if p in yes_close]
        n_perfect = [p for p in included_no if p in no_perfect]
        n_qualified = [p for p in included_no if p in no_qualified]
        n_close = [p for p in included_no if p in no_close]

        any_yes = _append_compact_tier_group([
            (COMPACT_TIER_HEADER_PREMIUM, y_perfect),
            (COMPACT_TIER_HEADER_STRONG, y_qualified),
            (COMPACT_TIER_HEADER_WATCH, y_close),
        ], "BTTS YES (BTTS+)", "yes", "btts_yes_prob")
        if any_yes and (n_perfect or n_qualified or n_close):
            lines.append("")
        _append_compact_tier_group([
            (COMPACT_TIER_HEADER_PREMIUM, n_perfect),
            (COMPACT_TIER_HEADER_STRONG, n_qualified),
            (COMPACT_TIER_HEADER_WATCH, n_close),
        ], "BTTS NO (BTTS-)", "no", "btts_no_prob")

    if not compact:
        if included_yes:
            lines.append("")
            lines.append("🟢 BTTS Yes")
            lines.append("")
            y_perfect = [p for p in included_yes if p in yes_perfect]
            y_qualified = [p for p in included_yes if p in yes_qualified]
            y_close = [p for p in included_yes if p in yes_close]
            idx = 1
            if y_perfect:
                lines.append(f"  {PICK_TIER_PREMIUM}")
                lines.append("")
                for item in y_perfect:
                    _append_btts_pick(lines, idx, item, "yes", odds_yes, detailed, compact)
                    idx += 1
            if y_qualified:
                lines.append(f"  {PICK_TIER_STRONG}")
                lines.append("")
                for item in y_qualified:
                    _append_btts_pick(lines, idx, item, "yes", odds_yes, detailed, compact)
                    idx += 1
            if y_close:
                if detailed:
                    lines.append(f"  {PICK_TIER_VALUE}")
                    lines.append("")
                for item in y_close:
                    _append_btts_pick(lines, idx, item, "yes", odds_yes, detailed, compact)
                    idx += 1

        if included_no:
            lines.append("")
            lines.append("🔴 BTTS No")
            lines.append("")
            n_perfect = [p for p in included_no if p in no_perfect]
            n_qualified = [p for p in included_no if p in no_qualified]
            n_close = [p for p in included_no if p in no_close]
            idx = 1
            if n_perfect:
                lines.append(f"  {PICK_TIER_PREMIUM}")
                lines.append("")
                for item in n_perfect:
                    _append_btts_pick(lines, idx, item, "no", odds_no, detailed, compact)
                    idx += 1
            if n_qualified:
                lines.append(f"  {PICK_TIER_STRONG}")
                lines.append("")
                for item in n_qualified:
                    _append_btts_pick(lines, idx, item, "no", odds_no, detailed, compact)
                    idx += 1
            if n_close:
                if detailed:
                    lines.append(f"  {PICK_TIER_VALUE}")
                    lines.append("")
                for item in n_close:
                    _append_btts_pick(lines, idx, item, "no", odds_no, detailed, compact)
                    idx += 1

        if include_footer:
            lines.extend(["---", "For informational purposes only", "Gamble responsibly", ""])

    report = "\n".join(lines).strip()
    if not report:
        report = "— none"
    return report, base_date, included_yes, included_no


# =============================================================================
# MAIN
# =============================================================================
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="BTTS Yes/No Predictor")
    parser.add_argument("date", nargs="?", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--bankroll", type=float, default=1000.0)
    parser.add_argument("--odds-yes", type=float, default=DEFAULT_ODDS_BTTS_YES)
    parser.add_argument("--odds-no", type=float, default=DEFAULT_ODDS_BTTS_NO)
    parser.add_argument("--clear-cache", action="store_true")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument(
        "--publish-date",
        default=None,
        help="Date for Telegram daily picks (default: today). Filters Telegram output only.",
    )
    args = parser.parse_args()

    if args.clear_cache:
        cache.clear()

    start_date = datetime.strptime(args.date, "%Y-%m-%d")
    scan_days = args.days if args.days is not None else (6 if start_date.weekday() >= 4 else 4)

    yes_perfect, yes_qualified, yes_close, yes_weak = [], [], [], []
    no_perfect, no_qualified, no_close, no_weak = [], [], [], []
    scanned_dates = []

    print(f"Starting BTTS analysis from {args.date}...")

    for day_offset in range(scan_days):
        date_str = (start_date + timedelta(days=day_offset)).strftime("%Y-%m-%d")
        scanned_dates.append(date_str)
        fixtures = fetch_soccerbase_fixtures(date_str)
        seen, unique_fixtures = set(), []
        for f in fixtures:
            key = (f["home_team_id"], f["away_team_id"], f["league"])
            if key in seen or not f["home_team_id"] or not f["away_team_id"]:
                continue
            if not args.scheduled or f["status"] == "Scheduled":
                seen.add(key)
                unique_fixtures.append(f)
        if not unique_fixtures:
            continue
        print(f"   Processing {len(unique_fixtures)} matches on {date_str}...")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(process_single_match, m, date_str, args.odds_yes, args.odds_no): m
                for m in unique_fixtures
            }
            for future in as_completed(futures):
                try:
                    res = future.result(timeout=60)
                except Exception as e:
                    logger.error(f"Future error: {e}")
                    continue
                if res["status"] != "success":
                    continue
                data = res["data"]
                yt = data["yes"].get("tier")
                nt = data["no"].get("tier")
                if yt == "perfect":
                    yes_perfect.append(data)
                elif yt == "qualified":
                    yes_qualified.append(data)
                elif yt == "close":
                    yes_close.append(data)
                elif data["yes"]["score"] >= max(1, MAX_BTTS_YES_SCORE - 4):
                    yes_weak.append(data)
                if nt == "perfect":
                    no_perfect.append(data)
                elif nt == "qualified":
                    no_qualified.append(data)
                elif nt == "close":
                    no_close.append(data)
                elif data["no"]["score"] >= max(1, MAX_BTTS_NO_SCORE - 4):
                    no_weak.append(data)

    apply_portfolio_kelly(yes_perfect + yes_qualified + yes_close, "yes", args.bankroll, MAX_TOTAL_EXPOSURE / 2)
    apply_portfolio_kelly(no_perfect + no_qualified + no_close, "no", args.bankroll, MAX_TOTAL_EXPOSURE / 2)

    free_report, base_date, included_yes, included_no = build_report(
        yes_perfect, yes_qualified, yes_close,
        no_perfect, no_qualified, no_close,
        scanned_dates, args.odds_yes, args.odds_no, detailed=False,
    )
    publish_date = args.publish_date or datetime.now().strftime("%Y-%m-%d")
    telegram_report, _, _, _ = build_report(
        yes_perfect, yes_qualified, yes_close,
        no_perfect, no_qualified, no_close,
        scanned_dates, args.odds_yes, args.odds_no,
        detailed=False, compact=False,
        include_yesterday=False, include_header=False, include_footer=False,
        report_date=publish_date,
    )
    detailed_report, _, _, _ = build_report(
        yes_perfect, yes_qualified, yes_close,
        no_perfect, no_qualified, no_close,
        scanned_dates, args.odds_yes, args.odds_no, detailed=True,
    )

    print("\n===EMAIL_START===")
    print(free_report)
    print("===EMAIL_END===")
    write_telegram_section(telegram_report, "btts_telegram.txt")

    vip_path = f"btts_vip_report_{base_date}.txt"
    with open(vip_path, "w", encoding="utf-8") as f:
        f.write(detailed_report)

    json_path = f"btts_report_{base_date}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "metadata": {
                "scanned_window": scanned_dates,
                "bankroll": args.bankroll,
                "odds_yes": args.odds_yes,
                "odds_no": args.odds_no,
                "generated_at": datetime.now().isoformat(),
            },
            "yes": {"perfect": yes_perfect, "qualified": yes_qualified, "close": yes_close, "weak": yes_weak},
            "no": {"perfect": no_perfect, "qualified": no_qualified, "close": no_close, "weak": no_weak},
        }, f, indent=2, default=str)

    try:
        btts_picks = []
        # IMPORTANT: Use original tier buckets (not build_report's included_*) so
        # statistically-blocked leagues are still recorded with published=false.
        # record_predictions() handles the published flag internally via
        # is_statistical_block_only() and fully skips only static/integrity blocks.
        all_yes = yes_perfect + yes_qualified + yes_close
        for pick in all_yes:
            tier = pick["yes"].get("tier") or (
                "perfect" if pick in yes_perfect else
                "qualified" if pick in yes_qualified else "close"
            )
            mfr = str(pick.get("o25tips", {}).get("match_favourite_rule", ""))
            favourite_skew = "R13" in mfr or "R14" in mfr or "heavy favourite" in mfr
            btts_picks.append({
                "league": pick["match"]["league"],
                "home": pick["match"]["home"],
                "away": pick["match"]["away"],
                "date": pick["match"]["date"],
                "prediction": "yes",
                "confidence": tier,
                "prob": pick["poisson"]["btts_yes_prob"],
                "home_lambda": pick["poisson"]["home_lambda"],
                "away_lambda": pick["poisson"]["away_lambda"],
                "favourite_skew": favourite_skew,
            })
        all_no = no_perfect + no_qualified + no_close
        for pick in all_no:
            tier = pick["no"].get("tier") or (
                "perfect" if pick in no_perfect else
                "qualified" if pick in no_qualified else "close"
            )
            mfr = str(pick.get("o25tips", {}).get("match_favourite_rule", ""))
            favourite_skew = "R13" in mfr or "R14" in mfr or "heavy favourite" in mfr
            btts_picks.append({
                "league": pick["match"]["league"],
                "home": pick["match"]["home"],
                "away": pick["match"]["away"],
                "date": pick["match"]["date"],
                "prediction": "no",
                "confidence": tier,
                "prob": pick["poisson"]["btts_no_prob"],
                "home_lambda": pick["poisson"]["home_lambda"],
                "away_lambda": pick["poisson"]["away_lambda"],
                "favourite_skew": favourite_skew,
            })
        stats = record_predictions(base_date, btts_picks=btts_picks)
        if stats["added"]:
            print(f"Predictions recorded ({stats['added']} new)")
        elif stats.get("skipped"):
            print(f"Predictions already recorded ({stats['skipped']} skipped)")
    except Exception as e:
        print(f"Could not record predictions: {e}")

    print(f"\nReport saved: {json_path}")
    print(f"VIP report saved: {vip_path}")


if __name__ == "__main__":
    main()
