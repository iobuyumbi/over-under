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
    exponential_form_averages as _shared_exponential_form_averages,
    is_weak_roi_league as _shared_is_weak_roi_league,
    poisson_pmf as _shared_poisson_pmf,
)

# Shared Soccerbase scraping/parsing (see scraping.py) — do not redefine
# fetch_soccerbase_fixtures, fetch_soccerbase_team_results, get_team_form,
# get_team_overall_form, or _thin_count/_thin_total locally.
from scraping import (
    fetch_soccerbase_fixtures as _shared_fetch_fixtures,
    fetch_soccerbase_team_results as _shared_fetch_team_results,
    get_team_form as _shared_get_team_form,
    get_team_overall_form as _shared_get_team_overall_form,
    get_h2h_meetings as _shared_get_h2h_meetings,
    _thin_count,
    _thin_total,
    _count_btts,
    _count_non_btts,
    _count_clean_sheets,
    _count_failed_to_score,
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


# _thin_count/_thin_total and _count_* form helpers are imported from scraping.py —
# do not redefine locally; this is exactly the copy-paste drift that the shared
# module exists to prevent.


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
    return _shared_get_h2h_meetings(
        home_team_id, away_team_id, fetch_soccerbase_team_results, target_date_str, limit
    )


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


def _recent_shutout_shock_veto(home_3, away_3, home_overall_2, away_overall_2):
    """Block BTTS Yes if a team blanked (scored 0) in EITHER of their last
    2 matches overall, OR blanked in EITHER of their last 2 venue matches.

    Strengthened 2026-09-02 from "last 1 match only" to "last 2 matches"
    after Aarhus vs Midtjylland BTTS Yes loss: the original 1-game window
    let a team with persistent scoring problems slip through if their most
    recent game happened to have a fluke goal. A 2-game window catches the
    real trend without being so broad it blocks every cold-streak team.

    We ignore clean-sheets-kept (ga==0) because one good defensive game is
    not a reliable BTTS No signal."""
    # Venue: blank in EITHER of last 2 venue matches
    if home_3:
        h_blanks_venue = sum(1 for gf, _ in home_3[:2] if gf == 0)
        if h_blanks_venue >= 1 and len(home_3[:2]) >= 2:
            return True, f"home_venue_blank_{h_blanks_venue}_in_last_{len(home_3[:2])}"
    if away_3:
        a_blanks_venue = sum(1 for gf, _ in away_3[:2] if gf == 0)
        if a_blanks_venue >= 1 and len(away_3[:2]) >= 2:
            return True, f"away_venue_blank_{a_blanks_venue}_in_last_{len(away_3[:2])}"
    # Overall: blank in EITHER of last 2 overall matches
    if home_overall_2:
        h_blanks_overall = sum(1 for gf, _ in home_overall_2 if gf == 0)
        if h_blanks_overall >= 1 and len(home_overall_2) >= 2:
            return True, f"home_overall_blank_{h_blanks_overall}_in_last_{len(home_overall_2)}"
    if away_overall_2:
        a_blanks_overall = sum(1 for gf, _ in away_overall_2 if gf == 0)
        if a_blanks_overall >= 1 and len(away_overall_2) >= 2:
            return True, f"away_overall_blank_{a_blanks_overall}_in_last_{len(away_overall_2)}"
    # Fallback single-game check for the case where only 1 match of data exists
    if home_3 and len(home_3) >= 1 and home_3[0][0] == 0:
        return True, "home_last_venue_blank"
    if away_3 and len(away_3) >= 1 and away_3[0][0] == 0:
        return True, "away_last_venue_blank"
    if home_overall_2 and len(home_overall_2) >= 1 and home_overall_2[0][0] == 0:
        return True, "home_last_match_blank"
    if away_overall_2 and len(away_overall_2) >= 1 and away_overall_2[0][0] == 0:
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


def _overall_btts_symmetry_veto(home_overall_6, away_overall_6, side):
    """
    Block BTTS pick if EITHER team's OVERALL form contradicts the prediction.

    BTTS Yes: BOTH teams must have BTTS in >= 50% of their last 6 overall games.
              A team that rarely sees BTTS in their overall matches will drag
              the game under even if their venue form looks okay. Previously the
              shared Overall BTTS activity check summed both teams together at
              55% combined — a team with 0 BTTS in 6 could hide behind a partner
              at 6/6. This is per-team enforcement.

    BTTS No:  BOTH teams must have non-BTTS in >= 50% of their last 6 overall games.
              If one team is regularly involved in BTTS games overall, BTTS No
              is dangerous regardless of venue form.
    """
    home_overall_6 = home_overall_6 or []
    away_overall_6 = away_overall_6 or []

    if side == "yes":
        h_len = min(len(home_overall_6), 6)
        if h_len >= 4:
            h_btts = sum(1 for gf, ga in home_overall_6[:h_len] if gf >= 1 and ga >= 1)
            h_btts_rate = h_btts / h_len
            if h_btts_rate < 0.5:
                return True, f"home_overall_btts_weak_{h_btts}of{h_len}_{h_btts_rate:.0%}"

        a_len = min(len(away_overall_6), 6)
        if a_len >= 4:
            a_btts = sum(1 for gf, ga in away_overall_6[:a_len] if gf >= 1 and ga >= 1)
            a_btts_rate = a_btts / a_len
            if a_btts_rate < 0.5:
                return True, f"away_overall_btts_weak_{a_btts}of{a_len}_{a_btts_rate:.0%}"

    elif side == "no":
        h_len = min(len(home_overall_6), 6)
        if h_len >= 4:
            h_nb = sum(1 for gf, ga in home_overall_6[:h_len] if gf == 0 or ga == 0)
            h_nb_rate = h_nb / h_len
            if h_nb_rate < 0.5:
                return True, f"home_overall_nonbtts_weak_{h_nb}of{h_len}_{h_nb_rate:.0%}"

        a_len = min(len(away_overall_6), 6)
        if a_len >= 4:
            a_nb = sum(1 for gf, ga in away_overall_6[:a_len] if gf == 0 or ga == 0)
            a_nb_rate = a_nb / a_len
            if a_nb_rate < 0.5:
                return True, f"away_overall_nonbtts_weak_{a_nb}of{a_len}_{a_nb_rate:.0%}"

    return False, None


def _home_winless_scoreless_crisis_veto(home_overall_6, away_overall_6, home_6, away_6):
    """Block BTTS Yes when the HOME side is in a severe overall crisis that
    makes them extremely unlikely to score (and therefore makes a BTTS Yes
    impossible even if the away side scores freely).

    Motivated by Aarhus vs Midtjylland 2026-09-02: reigning Danish Superliga
    champions AGF were on a 6-game overall WINLESS streak (0 wins, 3 points)
    and had failed to score in 2 of their last 3 home matches. They were
    shut out 0-2 — BTTS Yes lost.

    Also covers a mirror AWAY-side crisis check, since BTTS Yes requires
    BOTH teams to score; if any side is this cold offensively, the pick
    cannot be trusted.

    Triggers:
      * HOME: 0 wins in last 5 overall AND blanked in >= 2 of last 3 home
      * AWAY: <= 1 goal scored TOTAL in last 4 overall (dead attack)
      * EITHER: blanked (scored 0) in >= 3 of last 6 overall (offensive
        shutout frequency too high for BTTS Yes)
    """
    ho = home_overall_6 or []
    ao = away_overall_6 or []
    h6 = home_6 or []
    a6 = away_6 or []

    # 1. Home winless + venue scoreless crisis
    ho5_len = min(len(ho), 5)
    h6_len = min(len(h6), 3)
    if ho5_len >= 5 and h6_len >= 3:
        home_overall_wins = 0
        for gf, ga in ho[:ho5_len]:
            if gf > ga:
                home_overall_wins += 1
        home_scoreless_home = 0
        for gf, _ in h6[:h6_len]:
            if gf == 0:
                home_scoreless_home += 1
        if home_overall_wins == 0 and home_scoreless_home >= 2:
            return True, (
                f"home_winless_crisis_{home_overall_wins}w_in_{ho5_len}_"
                f"{home_scoreless_home}scoreless_in_{h6_len}home"
            )

    # 2. Any team: <= 1 GF total in last 4 overall (dead attack)
    ho4_len = min(len(ho), 4)
    if ho4_len >= 4:
        ho_gf = sum(gf for gf, _ in ho[:ho4_len])
        if ho_gf <= 1:
            return True, f"home_crisis_{ho_gf}goals_in_{ho4_len}overall"

    ao4_len = min(len(ao), 4)
    if ao4_len >= 4:
        ao_gf = sum(gf for gf, _ in ao[:ao4_len])
        if ao_gf <= 1:
            return True, f"away_crisis_{ao_gf}goals_in_{ao4_len}overall"

    # 3. Any team: blanked in >= 3 of last 6 overall (offensive shutout freq)
    ho6_len = min(len(ho), 6)
    if ho6_len >= 4:
        ho_blanks = sum(1 for gf, _ in ho[:ho6_len] if gf == 0)
        if ho_blanks >= max(3, round(ho6_len * 0.5)):
            return True, f"home_shutout_freq_{ho_blanks}of{ho6_len}_overall"

    ao6_len = min(len(ao), 6)
    if ao6_len >= 4:
        ao_blanks = sum(1 for gf, _ in ao[:ao6_len] if gf == 0)
        if ao_blanks >= max(3, round(ao6_len * 0.5)):
            return True, f"away_shutout_freq_{ao_blanks}of{ao6_len}_overall"

    return False, None


def _overall_shutout_frequency_veto(home_overall_6, away_overall_6):
    """Dedicated BTTS Yes block for high offensive shutout frequency overall.

    BTTS Yes requires BOTH teams to score. If a team has been BLANKED
    (gf == 0, regardless of venue) in 3+ of their last 6 overall matches,
    they are demonstrably unreliable at finding the net, and BTTS Yes
    effectively becomes a "will the cold team suddenly score?" bet — not
    a statistical edge.

    Aarhus vs Midtjylland 2026-09-02: Aarhus had exactly this profile
    overall heading into the tie, and they delivered another 0-goal
    performance.
    """
    h = home_overall_6 or []
    a = away_overall_6 or []
    h_len = min(len(h), 6)
    a_len = min(len(a), 6)

    if h_len >= 4:
        h_blanks = sum(1 for gf, _ in h[:h_len] if gf == 0)
        if h_blanks >= max(3, round(h_len * 0.5)):
            return True, f"home_off_blanks_{h_blanks}of{h_len}_overall"

    if a_len >= 4:
        a_blanks = sum(1 for gf, _ in a[:a_len] if gf == 0)
        if a_blanks >= max(3, round(a_len * 0.5)):
            return True, f"away_off_blanks_{a_blanks}of{a_len}_overall"

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
# BTTS YES: NEW HARDENING VETOES (2026-09-06 loss post-mortem)
# Losses: Stoke 4-0 Charlton, Helsingborgs 0-2 Sandvikens
# Pattern: one side completely shut out by dominant defence
# =============================================================================

def _elite_defence_vs_dead_attack_veto(home_6, away_6):
    """Block BTTS Yes when one team has an ELITE venue defence (conceded
    <= 0.5 gpg) while the opponent has a DEAD venue attack (scored
    <= 0.5 gpg). BTTS Yes needs BOTH teams to score — a mismatch this
    extreme usually ends 3-0 / 4-0 style.

    Stoke 4-0 Charlton: Stoke home defence was elite, Charlton away
    attack was dead. BTTS Yes died on a clean sheet.
    """
    h = home_6 or []
    a = away_6 or []
    if len(h) < 4 or len(a) < 4:
        return False, None

    hn, an = min(len(h), 6), min(len(a), 6)
    h_ga_avg = sum(ga for _, ga in h[:hn]) / hn
    a_gf_avg = sum(gf for gf, _ in a[:an]) / an
    home_def_elite_away_dead = h_ga_avg <= 0.5 and a_gf_avg <= 0.5

    a_ga_avg = sum(ga for _, ga in a[:an]) / an
    h_gf_avg = sum(gf for gf, _ in h[:hn]) / hn
    away_def_elite_home_dead = a_ga_avg <= 0.5 and h_gf_avg <= 0.5

    if home_def_elite_away_dead:
        return True, (
            f"home_def_elite_{h_ga_avg:.1f}gpg_away_attack_dead_{a_gf_avg:.1f}gpg"
        )
    if away_def_elite_home_dead:
        return True, (
            f"away_def_elite_{a_ga_avg:.1f}gpg_home_attack_dead_{h_gf_avg:.1f}gpg"
        )
    return False, None


def _tightened_mismatch_with_form_veto(home_lambda, away_lambda, home_3, away_3):
    """Block BTTS Yes when the lambda mismatch is moderate (>= 1.7x) AND
    the weaker side confirms cold attacking form with at least one
    recent venue blank. The existing _lambda_mismatch_veto triggers at
    2.0x ratio or 0.8 absolute floor — Stoke vs Charlton was around
    1.85x, which slipped through at the gap's edge. A 1.7x mismatch
    combined with a recent venue blank on the weak side is enough to
    kill BTTS Yes.
    """
    if home_lambda <= 0 or away_lambda <= 0:
        return False, None
    lo, hi = sorted((home_lambda, away_lambda))
    ratio = hi / lo
    if ratio < 1.7:
        return False, None

    weak_side_is_home = (lo == home_lambda)
    if weak_side_is_home:
        weak_recent = home_3 or []
    else:
        weak_recent = away_3 or []
    if len(weak_recent) >= 2:
        weak_blanks = sum(1 for gf, _ in weak_recent[:2] if gf == 0)
        if weak_blanks >= 1:
            side = "home" if weak_side_is_home else "away"
            return True, (
                f"weak_{side}_ratio_{ratio:.1f}x_and_{weak_blanks}_recent_blanks"
            )
    return False, None


def _recent_heavy_blowout_loss_veto(home_overall_6, away_overall_6):
    """Block BTTS Yes if EITHER team lost their SINGLE most recent
    overall match by a 3+ goal margin (e.g. 0-3 / 4-0 / 0-4). A heavy
    blowout loss signals either a collapsed defence (ready to concede
    more) or a broken attack (unlikely to score). Helsingborgs 0-2
    Sandvikens: Helsingborgs had been blown out 0-4 in their prior
    fixture, confirming their attack was already dead.

    Also blocks BTTS No (see call in no_qualifies) — a team coming off
    a 4-goal thrashing often carries chaos into the next game.
    """
    ho = home_overall_6 or []
    ao = away_overall_6 or []

    if ho:
        gf, ga = ho[0]
        if abs(gf - ga) >= 3:
            return True, f"home_last_match_heavy_loss_{gf}-{ga}"
    if ao:
        gf, ga = ao[0]
        if abs(gf - ga) >= 3:
            return True, f"away_last_match_heavy_loss_{gf}-{ga}"
    return False, None


def _dominant_favourite_clean_sheet_veto(home_6, away_6):
    """Block BTTS Yes when it's a classic dominant-home / beat-up-away
    matchup that historically produces 3-0 / 4-0 results, not 2-1 /
    3-1. Specifically: home team won >= 4 of last 6 venue AND away
    team lost >= 3 of last 6 venue AND home kept >= 2 clean sheets in
    venue 6. Stoke vs Charlton was exactly this profile — Stoke's
    home form was 5W-1D with 3 CS, Charlton's away was 4 losses.

    BTTS Yes on these fixtures is a bet that a weak away side will
    suddenly score against an in-form home defence — a negative-EV bet.
    """
    h = home_6 or []
    a = away_6 or []
    if len(h) < 5 or len(a) < 5:
        return False, None

    hn, an = min(len(h), 6), min(len(a), 6)
    home_wins = sum(1 for gf, ga in h[:hn] if gf > ga)
    away_losses = sum(1 for gf, ga in a[:an] if gf < ga)
    home_cs = sum(1 for _, ga in h[:hn] if ga == 0)

    if home_wins >= 4 and away_losses >= 3 and home_cs >= 2:
        return True, (
            f"favourite_sweep_hw{home_wins}_al{away_losses}_hcs{home_cs}"
        )
    return False, None


# =============================================================================
# BTTS NO: NEW HARDENING VETOES (2026-09-06 loss post-mortem)
# Losses: Torpedo Belaz 1-2 Gomel, Arbroath 1-1 Dunfermline
# Pattern: even matchups with mutual scoring/concession frequency
# =============================================================================

def _mutual_consistent_scoring_conceding_veto(home_overall_6, away_overall_6):
    """Block BTTS No when BOTH teams consistently score AND consistently
    concede across their overall form. BTTS No requires at least one
    team to blank; if BOTH teams have scored in >= 4 of 6 overall AND
    conceded in >= 4 of 6 overall, the probability of at least one
    blank is very low.

    Arbroath 1-1 Dunfermline: both teams consistently scored and
    conceded overall. A 1-1 draw was far more likely than any clean
    sheet — BTTS No died.
    """
    ho = home_overall_6 or []
    ao = away_overall_6 or []
    ho_n = min(len(ho), 6)
    ao_n = min(len(ao), 6)
    if ho_n < 4 or ao_n < 4:
        return False, None

    ho_scored = sum(1 for gf, _ in ho[:ho_n] if gf >= 1)
    ho_conceded = sum(1 for _, ga in ho[:ho_n] if ga >= 1)
    ao_scored = sum(1 for gf, _ in ao[:ao_n] if gf >= 1)
    ao_conceded = sum(1 for _, ga in ao[:ao_n] if ga >= 1)

    home_involved = ho_scored >= max(4, round(ho_n * 0.67)) and ho_conceded >= max(4, round(ho_n * 0.67))
    away_involved = ao_scored >= max(4, round(ao_n * 0.67)) and ao_conceded >= max(4, round(ao_n * 0.67))

    if home_involved and away_involved:
        return True, (
            f"both_active_hs{ho_scored}/{ho_n}_hc{ho_conceded}/{ho_n}_"
            f"as{ao_scored}/{ao_n}_ac{ao_conceded}/{ao_n}"
        )
    return False, None


def _h2h_btts_no_blocked_2game(home_team_id, away_team_id, target_date_str=None):
    """Mirror of _h2h_btts_yes_blocked_2game for BTTS No: block BTTS No
    if only 2 recent H2H meetings exist and BOTH were BTTS (both teams
    scored). The existing 3-meeting H2H check for BTTS No fires at
    >=67% BTTS rate, so a 2/2 pure-BTTS pattern is missed.

    Torpedo Belaz vs Gomel type matchups often have limited recent H2H
    due to league movement; the 2-game form is a strong signal.
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str, limit=2)
    if len(meetings) == 2:
        btts_both = sum(1 for m in meetings
                        if m.get("gf", 0) >= 1 and m.get("ga", 0) >= 1)
        if btts_both == 2:
            return True, meetings
    return False, meetings


def _last_two_matches_both_scored_veto(home_overall_6, away_overall_6):
    """Block BTTS No when BOTH teams scored in BOTH of their last 2
    overall matches. Very recent form (last 2 matches each) is the
    strongest signal of attacking momentum — if every side's last two
    outings both had goals, the odds are against a clean sheet
    appearing.

    Arbroath 1-1 Dunfermline: both teams came into the match having
    scored in each of their last 2 fixtures. BTTS No was a bet this
    streak would simultaneously break for both sides — unlikely.
    """
    ho = home_overall_6 or []
    ao = away_overall_6 or []
    if len(ho) < 2 or len(ao) < 2:
        return False, None

    ho_scored_2 = all(gf >= 1 for gf, _ in ho[:2])
    ao_scored_2 = all(gf >= 1 for gf, _ in ao[:2])

    if ho_scored_2 and ao_scored_2:
        return True, "both_scored_last_2_each"
    return False, None


def _even_game_leaky_defences_veto(home_lambda, away_lambda, home_6, away_6):
    """Block BTTS No on EVEN (close lambda) matchups where BOTH defences
    leak at >= 1.0 goal per game at venue. BTTS No relies on a lopsided
    game where one side can be shut out; an even matchup where both
    defences leak is textbook 1-1 / 2-1 territory.

    Torpedo Belaz 1-2 Gomel: close matchup (< 1.3x lambda ratio), both
    defences conceded 1.2+ gpg at venue. Both sides scored — BTTS No
    died.
    """
    if home_lambda <= 0 or away_lambda <= 0:
        return False, None
    lo, hi = sorted((home_lambda, away_lambda))
    if hi / lo >= 1.3:
        return False, None

    h = home_6 or []
    a = away_6 or []
    if len(h) < 4 or len(a) < 4:
        return False, None
    hn, an = min(len(h), 6), min(len(a), 6)
    h_ga_avg = sum(ga for _, ga in h[:hn]) / hn
    a_ga_avg = sum(ga for _, ga in a[:an]) / an

    if h_ga_avg >= 1.0 and a_ga_avg >= 1.0:
        return True, (
            f"even_leaky_{hi/lo:.1f}ratio_h{h_ga_avg:.1f}ga_a{a_ga_avg:.1f}ga"
        )
    return False, None


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
    return _shared_exponential_form_averages(form_tuples, halflife)


def _is_weak_roi_league(league_name):
    return _shared_is_weak_roi_league(league_name, _WEAK_ROI_LEAGUE_KEYWORDS)


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
    return _shared_poisson_pmf(k, lam)


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
        home_overall_2 = get_team_overall_form(match["home_team_id"], 2, target_date)
        away_overall_2 = get_team_overall_form(match["away_team_id"], 2, target_date)
        shutout_shock_veto, shutout_shock_reason = _recent_shutout_shock_veto(
            home_3, away_3, home_overall_2, away_overall_2
        )
        extended_drought_veto, extended_drought_reason = _extended_scoring_drought_veto(home_3, away_3)
        min_attack_veto, min_attack_reason = _minimum_attack_rate_veto(home_3, away_3)
        cup_veto, cup_reason = _cup_mismatch_veto(match, home_lambda, away_lambda)
        cs_freq_veto, cs_freq_reason = _clean_sheet_frequency_veto(home_6, away_6)
        non_league_veto, non_league_reason = _non_league_reliability_veto(match, home_6, away_6)
        overall_btts_yes_veto, overall_btts_yes_reason = _overall_btts_symmetry_veto(
            home_overall_6, away_overall_6, "yes"
        )
        overall_btts_no_veto, overall_btts_no_reason = _overall_btts_symmetry_veto(
            home_overall_6, away_overall_6, "no"
        )
        home_crisis_veto, home_crisis_reason = _home_winless_scoreless_crisis_veto(
            home_overall_6, away_overall_6, home_6, away_6
        )
        shutout_freq_veto, shutout_freq_reason = _overall_shutout_frequency_veto(
            home_overall_6, away_overall_6
        )
        elite_def_veto, elite_def_reason = _elite_defence_vs_dead_attack_veto(home_6, away_6)
        tight_mismatch_veto, tight_mismatch_reason = _tightened_mismatch_with_form_veto(
            home_lambda, away_lambda, home_3, away_3
        )
        heavy_blowout_veto, heavy_blowout_reason = _recent_heavy_blowout_loss_veto(
            home_overall_6, away_overall_6
        )
        dom_fav_cs_veto, dom_fav_cs_reason = _dominant_favourite_clean_sheet_veto(home_6, away_6)
        mutual_active_veto, mutual_active_reason = _mutual_consistent_scoring_conceding_veto(
            home_overall_6, away_overall_6
        )
        h2h_btts_no_2g_veto, h2h_btts_no_2g_meetings = _h2h_btts_no_blocked_2game(
            match["home_team_id"], match["away_team_id"], target_date
        )
        last_2_scored_veto, last_2_scored_reason = _last_two_matches_both_scored_veto(
            home_overall_6, away_overall_6
        )
        even_leaky_veto, even_leaky_reason = _even_game_leaky_defences_veto(
            home_lambda, away_lambda, home_6, away_6
        )
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
            and not overall_btts_yes_veto
            and not home_crisis_veto
            and not shutout_freq_veto
            and not elite_def_veto and not tight_mismatch_veto
            and not heavy_blowout_veto and not dom_fav_cs_veto
        )
        no_qualifies = (
            bool(no_passed) and no_score >= no_min and no_gate
            and non_btts_gate and o25tips_no_ok
            and not h2h_btts_no_blocked
            and not non_league_veto
            and not overall_btts_no_veto
            and not mutual_active_veto and not h2h_btts_no_2g_veto
            and not last_2_scored_veto and not even_leaky_veto
            and not heavy_blowout_veto
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
        if overall_btts_yes_veto:
            regressions.append(f"overall BTTS symmetry ({overall_btts_yes_reason})")
        if overall_btts_no_veto:
            regressions.append(f"overall non-BTTS symmetry ({overall_btts_no_reason})")
        if home_crisis_veto:
            regressions.append(f"winless/scoreless crisis ({home_crisis_reason})")
        if shutout_freq_veto:
            regressions.append(f"overall shutout frequency ({shutout_freq_reason})")
        if elite_def_veto:
            regressions.append(f"elite defence vs dead attack ({elite_def_reason})")
        if tight_mismatch_veto:
            regressions.append(f"tight mismatch + blank ({tight_mismatch_reason})")
        if heavy_blowout_veto:
            regressions.append(f"recent heavy blowout loss ({heavy_blowout_reason})")
        if dom_fav_cs_veto:
            regressions.append(f"dominant favourite CS sweep ({dom_fav_cs_reason})")
        if mutual_active_veto:
            regressions.append(f"mutual scoring/conceding ({mutual_active_reason})")
        if h2h_btts_no_2g_veto:
            regressions.append(f"h2h 2-game btts bogey ({len(h2h_btts_no_2g_meetings)} meetings)")
        if last_2_scored_veto:
            regressions.append(f"last 2 matches both scored ({last_2_scored_reason})")
        if even_leaky_veto:
            regressions.append(f"even matchup leaky defences ({even_leaky_reason})")
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
                    "overall_btts_yes_veto": overall_btts_yes_veto,
                    "overall_btts_yes_reason": overall_btts_yes_reason,
                    "home_crisis_veto": home_crisis_veto,
                    "home_crisis_reason": home_crisis_reason,
                    "shutout_freq_veto": shutout_freq_veto,
                    "shutout_freq_reason": shutout_freq_reason,
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
                    "overall_btts_no_veto": overall_btts_no_veto,
                    "overall_btts_no_reason": overall_btts_no_reason,
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
                    "overall_btts_yes_veto": overall_btts_yes_veto,
                    "overall_btts_yes_reason": overall_btts_yes_reason,
                    "overall_btts_no_veto": overall_btts_no_veto,
                    "overall_btts_no_reason": overall_btts_no_reason,
                    "home_crisis_veto": home_crisis_veto,
                    "home_crisis_reason": home_crisis_reason,
                    "shutout_freq_veto": shutout_freq_veto,
                    "shutout_freq_reason": shutout_freq_reason,
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
