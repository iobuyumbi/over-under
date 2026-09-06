#!/usr/bin/env python3
"""
OVER/UNDER 2.5 GOALS PREDICTOR - UNIFIED v5
==============================================
Over 2.5: High-scoring rules + overall goal-activity filter (last 6)
Under 2.5: Low-scoring mirror rules + overall under 2.5 in 4/6
Shrinkage xG | Portfolio Kelly | SQLite Cache
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
# copy-paste pattern is exactly what previously let this file and its
# siblings (home_win_soccerbase.py, btts_soccerbase.py) drift apart.
from utils import (
    Cache,
    build_session,
    fetch as _shared_fetch,
    parse_date,
    calculate_kelly,
    apply_portfolio_kelly,
    exponential_form_averages as _shared_exponential_form_averages,
    is_weak_roi_league as _shared_is_weak_roi_league,
    poisson_pmf as _shared_poisson_pmf,
)

# Shared Soccerbase scraping/parsing (see scraping.py) — do not redefine
# fetch_soccerbase_fixtures, fetch_soccerbase_team_results, get_team_form,
# get_team_overall_form, or _thin_count/_thin_total locally; same
# copy-paste-drift risk as the utils.py helpers above.
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
    _count_over25,
    _count_under25,
)

# Import prediction tracker
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
    MARKET_OVER25,
    MARKET_UNDER25,
)

# =============================================================================
# CONFIGURATION
# =============================================================================
CACHE_DB = "soccerbase_cache.db"
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

MAX_OVER_SCORE = 13
MAX_UNDER_SCORE = 12

# Under 2.5 thresholds (from over25tips.com official algorithm)
UNDER_HOME_SCORED_CAP = 1.2      # Max avg goals scored by home team in last 6 home
UNDER_HOME_CONCEDED_CAP = 1.2    # Max avg goals conceded by home team in last 6 home
UNDER_AWAY_SCORED_CAP = 1.0      # Max avg goals scored by away team in last 6 away
UNDER_AWAY_CONCEDED_CAP = 1.0    # Max avg goals conceded by away team in last 6 away
UNDER_HOME_TOTAL_6_CAP = 10.0    # Legacy cap for backwards compatibility
UNDER_AWAY_TOTAL_6_CAP = 10.0    # Legacy cap for backwards compatibility
UNDER_HOME_OVER25_MAX = 3        # Legacy max for backwards compatibility
UNDER_AWAY_OVER25_MAX = 3        # Legacy max for backwards compatibility

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
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
    """Last N matches home or away combined as (gf, ga) tuples."""
    return _shared_get_team_overall_form(
        team_id, fetch_soccerbase_team_results, num_matches, target_date_str, parse_date
    )



def _team_scored_every_match(form):
    sample = form[:6]
    if len(sample) < 4:
        return False
    min_required = len(sample)
    return all(gf >= 1 for gf, _ in sample[:min_required])


def _team_conceded_every_match(form):
    sample = form[:6]
    if len(sample) < 4:
        return False
    min_required = len(sample)
    return all(ga >= 1 for _, ga in sample[:min_required])


def _team_active_goal_profile(form):
    """Scored or conceded in every one of the last N matches (min 4 required)."""
    return _team_scored_every_match(form) or _team_conceded_every_match(form)


def _count_under_25_overall(form):
    return sum(1 for gf, ga in form[:6] if gf + ga < 2.5)


BTTS_MIN_6 = 3
NON_BTTS_MIN_6 = 3


# _thin_count/_thin_total and _count_* form helpers are imported from scraping.py —
# do not redefine locally; this is exactly the copy-paste drift that the shared
# module exists to prevent.


def _venue_over_gate_passes(home_6, away_6):
    """Hard gate: at least one side has ≥3/6 over 2.5 at venue (OR logic)."""
    h_over = _count_over25(home_6)
    a_over = _count_over25(away_6)
    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)
    if h_len >= 6 and a_len >= 6:
        return h_over >= 3 or a_over >= 3
    h_min = max(1, round(h_len * 0.5))
    a_min = max(1, round(a_len * 0.5))
    h_ok = h_len >= 2 and h_over >= h_min
    a_ok = a_len >= 2 and a_over >= a_min
    return h_ok or a_ok


def _high_scoring_blocks_under(home_6, away_6):
    """Hard veto on Under when either side's venue form is consistently high-scoring."""
    h_over = _count_over25(home_6)
    a_over = _count_over25(away_6)
    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)
    if h_len >= 6 and h_over >= 4:
        return True
    if a_len >= 6 and a_over >= 4:
        return True
    if h_len >= 2 and h_over >= max(2, round(h_len * 0.67)):
        return True
    if a_len >= 2 and a_over >= max(2, round(a_len * 0.67)):
        return True
    return False


def _recent_cold_blocks_over(home_3, away_3):
    """Veto Over when either team's last 2 venue games were low-scoring (≤2 goals)."""
    if len(home_3 or []) >= 2:
        if all(gf + ga <= 2 for gf, ga in home_3[:2]):
            return True
    if len(away_3 or []) >= 2:
        if all(gf + ga <= 2 for gf, ga in away_3[:2]):
            return True
    return False


def _btts_gate_passes(home_6, away_6):
    """BTTS gate with thin-data fallback: pass if home OR away meets venue BTTS threshold."""
    h_btts = _count_btts(home_6)
    a_btts = _count_btts(away_6)
    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)
    if h_len >= 6 and a_len >= 6:
        return h_btts >= BTTS_MIN_6 or a_btts >= BTTS_MIN_6
    h_min = max(1, round(h_len * 0.5))
    a_min = max(1, round(a_len * 0.5))
    h_ok = (h_len >= 6 and h_btts >= BTTS_MIN_6) or (h_len < 6 and h_len >= 2 and h_btts >= h_min)
    a_ok = (a_len >= 6 and a_btts >= BTTS_MIN_6) or (a_len < 6 and a_len >= 2 and a_btts >= a_min)
    return h_ok or a_ok


def _non_btts_gate_passes(home_6, away_6):
    """Mirror of BTTS gate: pass if home OR away has ≥3 non-BTTS in venue 6."""
    h_nb = _count_non_btts(home_6)
    a_nb = _count_non_btts(away_6)
    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)
    if h_len >= 6 and a_len >= 6:
        return h_nb >= NON_BTTS_MIN_6 or a_nb >= NON_BTTS_MIN_6
    h_min = max(1, round(h_len * 0.5))
    a_min = max(1, round(a_len * 0.5))
    h_ok = (h_len >= 6 and h_nb >= NON_BTTS_MIN_6) or (h_len < 6 and h_len >= 2 and h_nb >= h_min)
    a_ok = (a_len >= 6 and a_nb >= NON_BTTS_MIN_6) or (a_len < 6 and a_len >= 2 and a_nb >= a_min)
    return h_ok or a_ok


_OU_H2H_MAX_LOOKBACK = 6
_OU_H2H_MIN_MEETINGS = 3
_OU_H2H_OVER_BLOCK_RATE = 0.33
_OU_H2H_UNDER_BLOCK_RATE = 0.67


def get_h2h_meetings(home_team_id, away_team_id, target_date_str=None, limit=_OU_H2H_MAX_LOOKBACK):
    """Recent meetings between these sides, merged from both teams' result pages."""
    return _shared_get_h2h_meetings(
        home_team_id, away_team_id, fetch_soccerbase_team_results, target_date_str, limit
    )


def _h2h_over_blocked(home_team_id, away_team_id, target_date_str=None):
    """Block Over 2.5 when recent H2H games are consistently low-scoring.

    Blocks if:
      - >=3 H2H meetings AND <=33% went Over 2.5
      - OR the last 3 H2H meetings were ALL Under 2.5 (streak veto)
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str, limit=8)
    if len(meetings) < _OU_H2H_MIN_MEETINGS:
        return False, meetings
    
    over_count = sum(1 for m in meetings if (m.get("gf", 0) + m.get("ga", 0)) > 2.5)
    rate = over_count / len(meetings)
    
    # Recent streak check: last 3 were all Under 2.5
    last_3_under = all((m.get("gf", 0) + m.get("ga", 0)) < 2.5 for m in meetings[:3])
    
    if rate <= _OU_H2H_OVER_BLOCK_RATE or (len(meetings) >= 3 and last_3_under):
        return True, meetings
        
    return False, meetings


def _venue_h2h_bogey_veto(home_team_id, away_team_id, target_date_str=None):
    """Block Over 2.5 when H2H meetings AT THIS VENUE (current Home at home)
    are consistently low-scoring. Catches the Greuther Fürth vs Heidenheim
    bogey pattern (6/6 Under 2.5 at Fürth's venue).

    Triggers if >=2 meetings at this venue AND all were Under 2.5.
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str, limit=10)
    venue_meetings = [m for m in meetings if m.get("is_home") is True]
    
    if len(venue_meetings) < 2:
        return False, venue_meetings
        
    under_count = sum(1 for m in venue_meetings if (m.get("gf", 0) + m.get("ga", 0)) < 2.5)
    if under_count == len(venue_meetings):
        return True, venue_meetings
    return False, venue_meetings


def _h2h_under_blocked(home_team_id, away_team_id, target_date_str=None):
    """Block Under 2.5 when recent H2H games are consistently high-scoring.

    Blocks if >=3 H2H meetings AND >=67% went Over 2.5 (bogey high-scoring matchup).
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str)
    if len(meetings) < _OU_H2H_MIN_MEETINGS:
        return False, meetings
    over_count = sum(1 for m in meetings if m.get("total", m.get("gf", 0) + m.get("ga", 0)) > 2.5)
    rate = over_count / len(meetings)
    return rate >= _OU_H2H_UNDER_BLOCK_RATE, meetings


def _h2h_over_blocked_2game(home_team_id, away_team_id, target_date_str=None):
    """Relaxed H2H veto for Over 2.5: if only 2 recent meetings exist and BOTH
    were low-scoring (<=2 total goals), block. Catches derby tightness that
    the 3-meeting minimum would otherwise miss (e.g. Deveronvale vs Forres).
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str, limit=2)
    if len(meetings) == 2:
        low_scoring = sum(
            1 for m in meetings
            if m.get("total", m.get("gf", 0) + m.get("ga", 0)) <= 2
        )
        if low_scoring == 2:
            return True, meetings
    return False, meetings


def _scoring_drought_veto_over(home_3, away_3):
    """Block Over 2.5 if EITHER team failed to score in BOTH of their last 2
    venue games. Prevents picks where one attack is completely cold.

    Catches cases like:
      - Vasas 0-1 Puskas       (Vasas home blank in last 2)
      - Gainsborough 0-1 Bury  (Gainsborough home blank in last 2)
      - Tigres 2-0 Atlante     (Atlante away blank in last 2)
    """
    if len(home_3 or []) >= 2 and all(gf == 0 for gf, _ in home_3[:2]):
        return True, "home_scoring_drought_2"
    if len(away_3 or []) >= 2 and all(gf == 0 for gf, _ in away_3[:2]):
        return True, "away_scoring_drought_2"
    return False, None


def _defensive_wall_veto_over(home_3, away_3):
    """Block Over 2.5 if EITHER team kept a clean sheet in BOTH of their last
    2 venue games. Prevents picks where one defence is completely impenetrable.

    Catches cases like Tigres 2-0 Atlante (Tigres home defence posted 0 GA
    in consecutive games, so Atlante's attack faced an in-form wall).
    """
    if len(home_3 or []) >= 2 and all(ga == 0 for _, ga in home_3[:2]):
        return True, "home_defensive_wall_2"
    if len(away_3 or []) >= 2 and all(ga == 0 for _, ga in away_3[:2]):
        return True, "away_defensive_wall_2"
    return False, None


def _under_defensive_leak_veto(home_3, away_3):
    """Block Under 2.5 if EITHER team conceded 2+ goals in BOTH of their last
    2 venue games. A 'defensive wall breakdown' means the team is leaking
    goals at their venue — structurally unreliable for a low-scoring pick.

    Catches cases like:
      - CSKA 3-2 Lokomotiv   (both sides leaking 2+ recently)
      - Slask 3-3 Widzew     (both sides leaking 2+ recently)
      - Gimnasia LP 2-3 G M  (home conceded 3+ in prior venue games)
    """
    if len(home_3 or []) >= 2 and all(ga >= 2 for _, ga in home_3[:2]):
        return True, "home_defensive_leak_2"
    if len(away_3 or []) >= 2 and all(ga >= 2 for _, ga in away_3[:2]):
        return True, "away_defensive_leak_2"
    return False, None


def _recent_goal_shock_veto_under(home_3, away_3):
    """Block Under 2.5 if either team's SINGLE most recent venue match was
    a high-scoring game (3+ total goals) — regardless of how their other
    recent games went.

    Same principle as _recent_goalless_shock_veto() (Over 2.5) and
    _recent_shutout_shock_veto() (BTTS Yes), applied to the Under 2.5
    mirror-image failure mode: _under_defensive_leak_veto() above only
    fires when BOTH of the last 2 venue games leaked 2+ goals, so a
    single recent high-scoring shock game can get diluted out by an
    older, tighter game and slip through. A team's most recent match is
    a stronger live signal than a 2-game average.
    """
    if home_3 and (home_3[0][0] + home_3[0][1]) >= 3:
        return True, "home_last_match_high_scoring"
    if away_3 and (away_3[0][0] + away_3[0][1]) >= 3:
        return True, "away_last_match_high_scoring"
    return False, None


def _over_btts_participation_gate(home_3, away_3):
    """Block Over 2.5 if either team failed to score in 2+ of their last 3
    venue games. Over needs both attacks firing regularly; a side that
    blanks 2/3 caps the game in 0-1 / 1-0 / 1-1 territory.
    """
    if len(home_3 or []) >= 3:
        home_blanks = sum(1 for gf, _ in home_3 if gf == 0)
        if home_blanks >= 2:
            return True, f"home_blanked_{home_blanks}_of_3"
    if len(away_3 or []) >= 3:
        away_blanks = sum(1 for gf, _ in away_3 if gf == 0)
        if away_blanks >= 2:
            return True, f"away_blanked_{away_blanks}_of_3"
    return False, None


def _over_leak_participation_gate(home_3, away_3):
    """Block Over 2.5 if either team kept a clean sheet in 2+ of their last 3
    venue games. Over needs both defenses leaking; a side that locks down
    2/3 makes the game 1-0 / 2-0 territory.
    """
    if len(home_3 or []) >= 3:
        home_walls = sum(1 for _, ga in home_3 if ga == 0)
        if home_walls >= 2:
            return True, f"home_wall_{home_walls}_of_3"
    if len(away_3 or []) >= 3:
        away_walls = sum(1 for _, ga in away_3 if ga == 0)
        if away_walls >= 2:
            return True, f"away_wall_{away_walls}_of_3"
    return False, None


def _combined_low_event_veto(home_3, away_3):
    """Block Over if EITHER team has been in low-scoring games recently.
    
    Over 2.5 needs BOTH attacks firing. If one side is consistently
    low-event (even if not in a drought streak), the ceiling drops.
    """
    if len(home_3 or []) >= 2 and len(away_3 or []) >= 2:
        home_total = sum(gf + ga for gf, ga in home_3[:2])
        away_total = sum(gf + ga for gf, ga in away_3[:2])
        # If EITHER side averages < 2.0 goals per game in last 2 venue matches, block
        if home_total <= 4 or away_total <= 4:
            return True, f"low_event_h{home_total}_a{away_total}"
    return False, None


def _recent_goalless_shock_veto(home_3, away_3):
    """Block Over 2.5 if either team's SINGLE most recent venue match had
    0 or 1 total goals — regardless of how their other recent games went.

    Added 2026-08-25 after a 0/4 Over 2.5 day where 3 of the 4 losers
    finished 1-0/0-1/0-1 (i.e. exactly 1 total goal). The existing
    _recent_cold_blocks_over() only fires when BOTH of the last 2 venue
    games were low-scoring, so a single near-goalless match immediately
    before kickoff can be diluted out by an older, higher-scoring game
    and slip through undetected. A team that just played a near-goalless
    match is a stronger live signal of current attacking/defensive form
    than a 2-game average — most-recent-form should be able to veto on
    its own, not just contribute to an average.
    """
    if home_3 and (home_3[0][0] + home_3[0][1]) <= 1:
        return True, "home_last_match_near_goalless"
    if away_3 and (away_3[0][0] + away_3[0][1]) <= 1:
        return True, "away_last_match_near_goalless"
    return False, None


def _under_blowout_risk_veto(home_lambda, away_lambda):
    """Block Under 2.5 when one team is expected to dominate AND the
    combined xG is still moderate-to-high. One-sided games often end
    3-0, 4-0, etc. which kill Under 2.5 but can stay Under 3.5.

    Added 2026-08-30 after Chaco For Ever 3-0 San Miguel — the Under
    2.5 pick died on a one-sided blowout while the safer Under 3.5
    survived. Averages hide single-game explosions from a dominant
    favourite against a weak underdog.
    """
    if home_lambda <= 0 or away_lambda <= 0:
        return False, None
    combined = home_lambda + away_lambda
    lo, hi = sorted((home_lambda, away_lambda))
    if hi / lo >= 2.5 and combined >= 2.0:
        return True, f"one_sided_blowout_{hi:.1f}_vs_{lo:.1f}"
    return False, None


def _under_peak_game_veto(home_6, away_6):
    """Block Under 2.5 if either team had a 4+ goal game in their last 6
    venue matches. Averages hide single-game explosions — a team with
    goals 1,0,0,1,0,0 averages 0.33 but once conceded 3 in a blowout.

    Added 2026-08-30 to prevent Under 2.5 picks from dying on hidden
    high-scoring peaks that get diluted out by low-average runs.
    """
    for gf, ga in (home_6 or [])[:6]:
        if gf + ga >= 4:
            return True, f"home_peak_game_{gf}_{ga}"
    for gf, ga in (away_6 or [])[:6]:
        if gf + ga >= 4:
            return True, f"away_peak_game_{gf}_{ga}"
    return False, None


def _under_overall_over_frequency_veto(home_overall_6, away_overall_6):
    """Block Under 2.5 if EITHER team is generally involved in high-scoring games.
    
    Under 2.5 needs BOTH teams to be consistently low-scoring overall. If one side
    has >= 3 Over 2.5 games in their last 6 overall matches, the Under 2.5 pick
    is structurally risky despite their venue form.
    
    Added 2026-09-06 after Metalist 5-0 Obolon Kiev and Bukovyna 3-0 Dynamo Kyiv losses.
    """
    h = home_overall_6 or []
    a = away_overall_6 or []
    h_len = min(len(h), 6)
    a_len = min(len(a), 6)
    
    if h_len >= 4:
        h_overs = sum(1 for gf, ga in h[:h_len] if gf + ga > 2.5)
        if h_overs >= 3:
            return True, f"home_overall_overs_{h_overs}of{h_len}"
            
    if a_len >= 4:
        a_overs = sum(1 for gf, ga in a[:a_len] if gf + ga > 2.5)
        if a_overs >= 3:
            return True, f"away_overall_overs_{a_overs}of{a_len}"
            
    return False, None


def _under_recent_overall_leak_veto(home_overall_6, away_overall_6):
    """Block Under 2.5 if EITHER team has shown severe defensive vulnerability recently.
    
    If a team conceded 3+ goals in ANY of their last 3 overall matches, they are
    capable of a defensive collapse that kills the Under 2.5 bet.
    
    Added 2026-09-06 after Botosani 5-0 Sepsi and Bromley 0-5 Leyton Orient losses.
    """
    h = (home_overall_6 or [])[:3]
    a = (away_overall_6 or [])[:3]
    
    for _, ga in h:
        if ga >= 3:
            return True, f"home_overall_leaked_{ga}_in_last_3"
    for _, ga in a:
        if ga >= 3:
            return True, f"away_overall_leaked_{ga}_in_last_3"
            
    return False, None


def _under_h2h_high_scoring_veto(home_team_id, away_team_id, target_date_str=None):
    """Block Under 2.5 if H2H meetings show high-scoring potential.
    
    Blocks if:
      - The single most recent H2H was 4+ goals.
      - OR the last 2 H2H meetings were BOTH Over 2.5.
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str, limit=5)
    if not meetings:
        return False, None
        
    # Most recent shock
    m1 = meetings[0]
    if (m1.get("gf", 0) + m1.get("ga", 0)) >= 4:
        return True, "h2h_last_match_4+_goals"
        
    # Last 2 streak
    if len(meetings) >= 2:
        m2 = meetings[1]
        if (m1.get("gf", 0) + m1.get("ga", 0)) > 2.5 and (m2.get("gf", 0) + m2.get("ga", 0)) > 2.5:
            return True, "h2h_last_2_both_over"
            
    return False, None


def _derby_veto(match):
    """Block Over 2.5 for known derby/rivalry matches.
    Derbies are historically tighter, more defensive, and lower-scoring
    due to the additional intensity, cards, and cautious tactics.

    Added 2026-08-30 after Watford vs West Ham finished 1-0 (2 goals):
    a London derby that passed all algorithm checks despite derby form
    being historically lower-scoring than equivalent non-derby fixtures.
    """
    derby_pairs = {
        ("watford", "west ham"), ("west ham", "watford"),
        ("arsenal", "tottenham"), ("tottenham", "arsenal"),
        ("chelsea", "arsenal"), ("arsenal", "chelsea"),
        ("millwall", "west ham"), ("west ham", "millwall"),
        ("chelsea", "tottenham"), ("tottenham", "chelsea"),
        ("crystal palace", "brighton"), ("brighton", "crystal palace"),
        ("southampton", "portsmouth"), ("portsmouth", "southampton"),
        ("nottingham forest", "derby"), ("derby", "nottingham forest"),
        ("liverpool", "everton"), ("everton", "liverpool"),
        ("manchester united", "manchester city"), ("manchester city", "manchester united"),
        ("celtic", "rangers"), ("rangers", "celtic"),
        ("borussia dortmund", "schalke"), ("schalke", "borussia dortmund"),
        ("real madrid", "barcelona"), ("barcelona", "real madrid"),
        ("atletico madrid", "real madrid"), ("real madrid", "atletico madrid"),
        ("inter", "ac milan"), ("ac milan", "inter"),
        ("juventus", "torino"), ("torino", "juventus"),
        ("lazio", "roma"), ("roma", "lazio"),
        ("olympique lyon", "saint etienne"), ("saint etienne", "olympique lyon"),
        ("marseille", "paris saint germain"), ("paris saint germain", "marseille"),
        ("porto", "benfica"), ("benfica", "porto"),
        ("ajax", "psv"), ("psv", "ajax"),
        ("galatasaray", "fenerbahce"), ("fenerbahce", "galatasaray"),
        ("panathinaikos", "olympiacos"), ("olympiacos", "panathinaikos"),
    }
    home = match.get("home", "").lower()
    away = match.get("away", "").lower()
    if (home, away) in derby_pairs:
        return True, "derby_match"
    return False, None


def _early_season_penalty(match_date_str):
    """Reduce confidence for very early-season fixtures (first 3 weeks of Aug).
    Early-season form data is thin, teams are still finding rhythm, and
    pre-season friendly data skews averages away from competitive reality.

    Added 2026-08-30 after Watford vs West Ham loss: the Aug 29 fixture had
    only 2-3 games of competitive data for both sides, which the algorithm
    treated as equivalent to mid-season 6-game samples. Applied as a
    confidence multiplier rather than a hard block, so early-season
    fixtures can still qualify if every other signal is strong.
    """
    if not match_date_str:
        return 1.0
    try:
        dt = datetime.strptime(match_date_str, "%Y-%m-%d")
        if dt.month == 8 and dt.day <= 20:
            return 0.85
    except (ValueError, TypeError):
        pass
    return 1.0


def _scoring_consistency_veto(home_6, away_6):
    """Block Over 2.5 if EITHER team failed to score in 2+ of their last 3.
    Goal averages hide burst scoring (e.g. 3,0,3,0,0,3 avg = 1.5 but the
    team blanks half the time). Consistent scoring in recent games is a
    more reliable signal than inflated averages from isolated hat-tricks.

    Added 2026-08-30 after Watford vs West Ham: both teams had inconsistent
    scoring patterns that passed 'avg goals > X' but the actual match was
    a low-scoring affair where one side (or both) blanked.
    """
    if len(home_6 or []) >= 3:
        blanks = sum(1 for gf, ga in home_6[:3] if gf == 0)
        if blanks >= 2:
            return True, f"home_inconsistent_{blanks}_blanks_in_3"
    if len(away_6 or []) >= 3:
        blanks = sum(1 for gf, ga in away_6[:3] if ga == 0)
        if blanks >= 2:
            return True, f"away_inconsistent_{blanks}_blanks_in_3"
    return False, None


def _overall_scoring_symmetry_veto(home_overall_6, away_overall_6):
    """
    Block Over 2.5 if EITHER team's OVERALL scoring is too weak.

    Over 2.5 needs BOTH teams to score goals regularly across ALL matches,
    not just at their venue. A team with poor overall scoring form will
    drag the game under even if their venue form looks okay (e.g. Dagenham
    vs Slough 2026-08-31, where Dagenham was decent at home but blanked
    heavily in away/overall fixtures, killing the 3-1 Over-2.5 result).

    HOME overall (last 6):
      - Must average >= 1.2 goals scored per game overall
      - OR have scored in >= 4 of last 6 overall matches

    AWAY overall (last 6):
      - Must average >= 1.0 goals scored per game overall
      - OR have scored in >= 3 of last 6 overall matches

    Thin-data fallback (3-5 games): proportional thresholds.
    """
    home_overall_6 = home_overall_6 or []
    away_overall_6 = away_overall_6 or []

    # --- HOME OVERALL CHECK ---
    h_len = min(len(home_overall_6), 6)
    if h_len >= 4:
        h_gf_total = sum(gf for gf, _ in home_overall_6[:h_len])
        h_gf_avg = h_gf_total / h_len
        h_scored_in = sum(1 for gf, _ in home_overall_6[:h_len] if gf >= 1)

        h_avg_ok = h_gf_avg >= 1.2
        h_consistent_ok = h_scored_in >= max(3, round(h_len * 0.67))

        if not h_avg_ok and not h_consistent_ok:
            return True, f"home_overall_weak_{h_gf_avg:.1f}gpg_{h_scored_in}of{h_len}_scored"
    elif h_len >= 2:
        h_scored_in = sum(1 for gf, _ in home_overall_6[:h_len] if gf >= 1)
        if h_scored_in < max(1, round(h_len * 0.5)):
            return True, f"home_overall_thin_{h_scored_in}of{h_len}_scored"

    # --- AWAY OVERALL CHECK ---
    a_len = min(len(away_overall_6), 6)
    if a_len >= 4:
        a_gf_total = sum(gf for gf, _ in away_overall_6[:a_len])
        a_gf_avg = a_gf_total / a_len
        a_scored_in = sum(1 for gf, _ in away_overall_6[:a_len] if gf >= 1)

        a_avg_ok = a_gf_avg >= 1.0
        a_consistent_ok = a_scored_in >= max(3, round(a_len * 0.67))

        if not a_avg_ok and not a_consistent_ok:
            return True, f"away_overall_weak_{a_gf_avg:.1f}gpg_{a_scored_in}of{a_len}_scored"
    elif a_len >= 2:
        a_scored_in = sum(1 for gf, _ in away_overall_6[:a_len] if gf >= 1)
        if a_scored_in < max(1, round(a_len * 0.5)):
            return True, f"away_overall_thin_{a_scored_in}of{a_len}_scored"

    return False, None


def _borderline_two_goal_cluster_veto(home_overall_6, away_overall_6):
    """Block Over 2.5 when BOTH teams' recent overall matches cluster at
    EXACTLY 2 total goals.

    Aarhus vs Midtjylland (2026-09-02) finished 0-2 (2 goals) and Burnley vs
    Middlesbrough finished 1-1 (2 goals) — both lost Over 2.5 by a single
    goal. Poisson probabilities for "Over 2.5" on such matches are
    misleading because most of the scoring probability mass sits RIGHT on
    the boundary (2 goals), not above it. This veto catches that pattern.

    Fire if COMBINED both teams have >= 50% of their recent overall matches
    ending with TOTAL GOALS == 2, AND at least one team shows the pattern
    strongly (>= 3 of 6 matches clustering at exactly 2).
    """
    h = home_overall_6 or []
    a = away_overall_6 or []
    h_len = min(len(h), 6)
    a_len = min(len(a), 6)
    if h_len < 4 or a_len < 4:
        return False, None

    h_at_2 = sum(1 for gf, ga in h[:h_len] if gf + ga == 2)
    a_at_2 = sum(1 for gf, ga in a[:a_len] if gf + ga == 2)

    combined_rate = (h_at_2 + a_at_2) / (h_len + a_len)
    strong_cluster = h_at_2 >= max(3, round(h_len * 0.5)) or a_at_2 >= max(3, round(a_len * 0.5))

    if combined_rate >= 0.5 and strong_cluster:
        return True, f"cluster_at_2_h{h_at_2}of{h_len}_a{a_at_2}of{a_len}_{int(combined_rate*100)}pct"

    return False, None


def _severe_offensive_crisis_veto(home_6, away_6, home_overall_6, away_overall_6):
    """Block Over 2.5 if EITHER team is in a severe scoring crisis.

    Patterns caught:
      * Team has <= 1 goal scored TOTAL in last 4 overall matches
        (attack is basically dead — drags everything Under)
      * HOME team specifically: 0 wins in last 5 overall AND 0 goals in
        >= 2 of last 3 home matches (reigning-champ AGF Aarhus 2026-09-02:
         0 wins from 6, failed to score at home, match ended 0-2.)
    """
    # --- Overall scoring crisis (both teams): <= 1 goal in last 4 overall ---
    ho = home_overall_6 or []
    ao = away_overall_6 or []
    ho_len = min(len(ho), 4)
    ao_len = min(len(ao), 4)

    if ho_len >= 4:
        ho_gf = sum(gf for gf, _ in ho[:ho_len])
        if ho_gf <= 1:
            return True, f"home_crisis_{ho_gf}goals_in_{ho_len}overall"

    if ao_len >= 4:
        ao_gf = sum(gf for gf, _ in ao[:ao_len])
        if ao_gf <= 1:
            return True, f"away_crisis_{ao_gf}goals_in_{ao_len}overall"

    # --- Home winless + venue scoreless crisis ------------------------------
    h6_len = min(len(home_6 or []), 5)
    ho5_len = min(len(ho), 5)
    if h6_len >= 3 and ho5_len >= 5:
        home_overall_wins = 0
        for gf, ga in ho[:ho5_len]:
            if gf > ga:
                home_overall_wins += 1
        home_scoreless = 0
        for gf, _ in (home_6 or [])[:3]:
            if gf == 0:
                home_scoreless += 1
        if home_overall_wins == 0 and home_scoreless >= 2:
            return True, (f"home_winless_crisis_{home_overall_wins}w_in_{ho5_len}_"
                         f"{home_scoreless}scoreless_in_{min(3,h6_len)}home")

    return False, None


def _xg_imbalance_shutout_risk_veto(home_lambda, away_lambda,
                                    home_overall_6, away_overall_6):
    """Block Over 2.5 when one side's attack is so weak (low xG + weak
    overall scoring form) that they are likely to be SHUT OUT, leaving
    only the opponent's goals to reach the 2.5 threshold.

    Pattern: Aarhus vs Midtjylland 2026-09-02 — xG 0.91 vs 2.88.
    Home side had overall weak scoring form, was shut out (0-2 = 2 goals),
    and Over 2.5 lost. If the "underdog" side's own attack can't
    contribute, the total is capped by the favourite's scoring alone.

    Trigger if:
      * one lambda <= 1.0 (weak attack) AND that team has weak overall
        scoring form (< 1.0 gpg AND scored in < 4/6 matches)
      * AND the combined xG sum is < 4.0 (otherwise even a shutout may
        still leave enough goals for Over)
    """
    if home_lambda <= 0 or away_lambda <= 0:
        return False, None

    ho = home_overall_6 or []
    ao = away_overall_6 or []

    def _weak_overall_form(team_overall):
        n = min(len(team_overall), 6)
        if n < 4:
            return False
        gf_total = sum(gf for gf, _ in team_overall[:n])
        avg = gf_total / n
        scored_in = sum(1 for gf, _ in team_overall[:n] if gf >= 1)
        return avg < 1.0 and scored_in < max(3, round(n * 0.67))

    combined_xg = home_lambda + away_lambda
    home_risk = home_lambda <= 1.0 and _weak_overall_form(ho)
    away_risk = away_lambda <= 1.0 and _weak_overall_form(ao)

    if combined_xg < 4.0 and (home_risk or away_risk):
        side = []
        if home_risk:
            side.append(f"home_l{home_lambda:.2f}")
        if away_risk:
            side.append(f"away_l{away_lambda:.2f}")
        return True, f"shutout_risk_xg{combined_xg:.1f}_{'_'.join(side)}"

    return False, None


def _mutual_cold_attacks_defences_veto(home_3, away_3):
    """Block Over 2.5 when BOTH teams are in a simultaneous cold-attack +
    hot-defence streak. Catches the 0-0 draw pattern: Widzew vs Radomiak
    (0-0) and Slovacko vs Pardubice (0-0) — both sides blanked at
    both ends of the pitch in recent games.

    Triggers if BOTH teams have: blanked (gf==0) in >= 2 of last 3 venue
                     AND kept a CS (ga==0) in >= 2 of last 3 venue
    """
    h = home_3 or []
    a = away_3 or []
    if len(h) < 3 or len(a) < 3:
        return False, None

    h_blanks = sum(1 for gf, _ in h[:3] if gf == 0)
    a_blanks = sum(1 for gf, _ in a[:3] if gf == 0)
    h_cs = sum(1 for _, ga in h[:3] if ga == 0)
    a_cs = sum(1 for _, ga in a[:3] if ga == 0)

    both_cold_attack = h_blanks >= 2 and a_blanks >= 2
    both_hot_defence = h_cs >= 2 and a_cs >= 2

    if both_cold_attack and both_hot_defence:
        return True, (
            f"mutual_0_0_streak_hb{h_blanks}ab{a_blanks}_"
            f"hcs{h_cs}acs{a_cs}"
        )
    return False, None


def _combined_weak_attack_strong_defence_veto(home_6, away_6):
    """Block Over 2.5 when BOTH teams combine a weak attack with a strong
    defence at their respective venues. The existing individual checks
    (scoring_consistency, overall_scoring_symmetry) look at one side at
    a time — they miss the case where NEITHER side can break 1.0 gpg
    scored WHILE BOTH concede under 1.0 gpg. That combination is a
    textbook low-scoring fixture (Bristol Rovers 1-0 Rotherham: 1 total
    goal).

    Triggers if BOTH teams scored <= 1.0 gpg avg AND conceded <= 1.0 gpg
    avg in venue 6-match window.
    """
    h = home_6 or []
    a = away_6 or []
    if len(h) < 4 or len(a) < 4:
        return False, None

    hn, an = min(len(h), 6), min(len(a), 6)
    h_gf_avg = sum(gf for gf, _ in h[:hn]) / hn
    h_ga_avg = sum(ga for _, ga in h[:hn]) / hn
    a_gf_avg = sum(gf for gf, _ in a[:an]) / an
    a_ga_avg = sum(ga for _, ga in a[:an]) / an

    home_snoozer = h_gf_avg <= 1.0 and h_ga_avg <= 1.0
    away_snoozer = a_gf_avg <= 1.0 and a_ga_avg <= 1.0

    if home_snoozer and away_snoozer:
        return True, (
            f"both_low_event_h{h_gf_avg:.1f}scored_{h_ga_avg:.1f}conceded_"
            f"a{a_gf_avg:.1f}scored_{a_ga_avg:.1f}conceded"
        )
    return False, None


def _h2h_zero_goal_bogey_veto(home_team_id, away_team_id, target_date_str=None):
    """Block Over 2.5 when recent H2H meetings are EXTREMELY low-scoring:
    no more than 1 total goal per game. The existing H2H checks fire on
    <=2 total goals or <=33% Over rate, but this catches the pure 0-0 /
    1-0 bogey-derby pattern where a matchup consistently produces 0 or
    1 goal regardless of individual team form.

    Triggers if >=2 H2H meetings AND all of them had <= 1 total goal.
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str, limit=4)
    if len(meetings) < 2:
        return False, meetings
    sub1 = sum(
        1 for m in meetings
        if m.get("total", m.get("gf", 0) + m.get("ga", 0)) <= 1
    )
    if sub1 == len(meetings):
        return True, meetings
    return False, meetings


def _borderline_one_goal_cluster_veto(home_overall_6, away_overall_6):
    """Block Over 2.5 when BOTH teams' recent overall matches cluster at
    EXACTLY 1 total goal (0-1 / 1-0 / 0-0 results). The existing
    _borderline_two_goal_cluster_veto catches 2-goal boundary losses
    like Aarhus 0-2 Midtjylland, but the Widzew 0-0 / Slovacko 0-0
    losses clustered at EVEN LOWER goal counts — just 0 or 1 goal per
    match. A team that keeps producing 1-goal games overall will drag
    an O2.5 pick under even at inflated odds.

    Fire if COMBINED both teams have >= 50% of recent overall matches
    ending with <= 1 total goal, AND at least one team shows >= 3 of 6
    at <= 1 goal.
    """
    h = home_overall_6 or []
    a = away_overall_6 or []
    h_len = min(len(h), 6)
    a_len = min(len(a), 6)
    if h_len < 4 or a_len < 4:
        return False, None

    h_at_1 = sum(1 for gf, ga in h[:h_len] if gf + ga <= 1)
    a_at_1 = sum(1 for gf, ga in a[:a_len] if gf + ga <= 1)

    combined_rate = (h_at_1 + a_at_1) / (h_len + a_len)
    strong_cluster = (
        h_at_1 >= max(3, round(h_len * 0.5))
        or a_at_1 >= max(3, round(a_len * 0.5))
    )

    if combined_rate >= 0.5 and strong_cluster:
        return True, (
            f"cluster_at_0_or_1_h{h_at_1}of{h_len}_"
            f"a{a_at_1}of{a_len}_{int(combined_rate*100)}pct"
        )
    return False, None


def _one_sided_home_blank_away_midrange_veto(home_3, away_3):
    """Blocks Over 2.5 when HOME attack is dead (>=2 blanks in 3) but AWAY
    attack is in the 1-2-goals sweet spot — yields the classic 0-2 trap.

    Scunthorpe 0-2 Harrogate (05/09/2026 O2.5 loss):
      - Home attack shut out 0-0-0 profile → 2+ blanks in last 3 venue
      - Away attack scores 1-2 goals reliably but rarely busts 3+ in a single
        away match → total goals = 2, UNDER.
      The mutual_cold veto doesn't fire here because AWAY scored 2 goals
      (not blanked), so we need a one-sided counterpart that combines home's
      offensive helplessness with away's moderate, non-busting scoring.
    """
    h = (home_3 or [])[:3]
    a = (away_3 or [])[:3]
    if len(h) < 3 or len(a) < 3:
        return False, None

    h_blanks = sum(1 for gf, _ in h if gf == 0)
    if h_blanks < 2:
        return False, None

    a_gf_total = sum(gf for gf, _ in a)
    a_max_single = max((gf for gf, _ in a), default=0)
    a_has_bust = any(gf >= 4 for gf, _ in a)
    if a_gf_total <= 7 and a_max_single <= 3 and not a_has_bust:
        return True, (
            f"home_blank_{h_blanks}of3_away_midrange_"
            f"{a_gf_total}total_max{a_max_single}"
        )
    return False, None


# =============================================================================
# OVER 2.5 ALGORITHM (10-Check Rules + Overall Form)
# =============================================================================
def apply_over_algorithm(home_3, away_3, home_6, away_6, home_overall_6=None, away_overall_6=None):
    if len(home_3) < 2 or len(away_3) < 2:
        return None, None, {"error": "Insufficient data"}, False

    passed, failed, details = [], [], {}
    is_perfect = True

    hn3, an3 = len(home_3), len(away_3)

    # Home 3-game
    h_total_3 = sum(gf + ga for gf, ga in home_3)
    if h_total_3 >= _thin_total(7, 3, hn3):
        passed.append("Home total goals (last 3)"); details["Home total goals (last 3)"] = f"PASS ({h_total_3})"
    else:
        failed.append("Home total goals (last 3)"); details["Home total goals (last 3)"] = f"FAIL ({h_total_3})"; is_perfect = False

    # Added 2026-08-25: mirrors "Away last match goals" below. The Away
    # side already required its single most recent match to have >=2
    # total goals, but Home had no equivalent check — meaning a home
    # team could qualify for Over 2.5 coming off a near-goalless match
    # as long as its other numbers averaged out. Closing that asymmetry.
    prev_h_total = home_3[0][0] + home_3[0][1]
    if prev_h_total >= 2:
        passed.append("Home last match goals"); details["Home last match goals"] = f"PASS ({prev_h_total})"
    else:
        failed.append("Home last match goals"); details["Home last match goals"] = f"FAIL ({prev_h_total})"; is_perfect = False

    # Added 2026-08-25: mirrors "Away scored (last 3)" below. Same gap —
    # Away had an explicit participation check (scored in >=2/3 games)
    # with no Home equivalent.
    h_scored = sum(1 for gf, _ in home_3 if gf > 0)
    if h_scored >= _thin_count(2, 3, hn3):
        passed.append("Home scored (last 3)"); details["Home scored (last 3)"] = f"PASS ({h_scored}/{hn3})"
        if h_scored < hn3:
            is_perfect = False
    else:
        failed.append("Home scored (last 3)"); details["Home scored (last 3)"] = f"FAIL ({h_scored}/{hn3})"; is_perfect = False

    h_over_3 = sum(1 for gf, ga in home_3 if gf + ga > 2.5)
    if h_over_3 >= _thin_count(2, 3, hn3):
        passed.append("Home over 2.5 (last 3)"); details["Home over 2.5 (last 3)"] = f"PASS ({h_over_3}/{hn3})"
        if h_over_3 < hn3:
            is_perfect = False
    else:
        failed.append("Home over 2.5 (last 3)"); details["Home over 2.5 (last 3)"] = f"FAIL ({h_over_3}/{hn3})"; is_perfect = False

    # Away 3-game
    a_total_3 = sum(gf + ga for gf, ga in away_3)
    if a_total_3 >= _thin_total(7, 3, an3):
        passed.append("Away total goals (last 3)"); details["Away total goals (last 3)"] = f"PASS ({a_total_3})"
    else:
        failed.append("Away total goals (last 3)"); details["Away total goals (last 3)"] = f"FAIL ({a_total_3})"; is_perfect = False

    prev_a_total = away_3[0][0] + away_3[0][1]
    if prev_a_total >= 2:
        passed.append("Away last match goals"); details["Away last match goals"] = f"PASS ({prev_a_total})"
    else:
        failed.append("Away last match goals"); details["Away last match goals"] = f"FAIL ({prev_a_total})"; is_perfect = False

    a_scored = sum(1 for gf, _ in away_3 if gf > 0)
    if a_scored >= _thin_count(2, 3, an3):
        passed.append("Away scored (last 3)"); details["Away scored (last 3)"] = f"PASS ({a_scored}/{an3})"
        if a_scored < an3:
            is_perfect = False
    else:
        failed.append("Away scored (last 3)"); details["Away scored (last 3)"] = f"FAIL ({a_scored}/{an3})"; is_perfect = False

    a_over_3 = sum(1 for gf, ga in away_3 if gf + ga > 2.5)
    if a_over_3 >= _thin_count(2, 3, an3):
        passed.append("Away over 2.5 (last 3)"); details["Away over 2.5 (last 3)"] = f"PASS ({a_over_3}/{an3})"
        if a_over_3 < an3:
            is_perfect = False
    else:
        failed.append("Away over 2.5 (last 3)"); details["Away over 2.5 (last 3)"] = f"FAIL ({a_over_3}/{an3})"; is_perfect = False

    # 6-game checks (proportional fallback when <6 venue games available)
    h_len = min(len(home_6), 6)
    if h_len >= 6:
        h_over_6 = sum(1 for gf, ga in home_6 if gf + ga > 2.5)
        if h_over_6 >= 4:
            passed.append("Home over 2.5 (last 6)"); details["Home over 2.5 (last 6)"] = f"PASS ({h_over_6}/6)"
        else:
            failed.append("Home over 2.5 (last 6)"); details["Home over 2.5 (last 6)"] = f"FAIL ({h_over_6}/6)"; is_perfect = False

        h_total_6 = sum(gf + ga for gf, ga in home_6)
        if h_total_6 >= 18:
            passed.append("Home total goals (last 6)"); details["Home total goals (last 6)"] = f"PASS ({h_total_6})"
        else:
            failed.append("Home total goals (last 6)"); details["Home total goals (last 6)"] = f"FAIL ({h_total_6})"; is_perfect = False
    elif h_len >= 2:
        h_over_6 = sum(1 for gf, ga in home_6 if gf + ga > 2.5)
        h_min = max(1, round(h_len * 0.67))
        if h_over_6 >= h_min:
            passed.append("Home over 2.5 (last 6)"); details["Home over 2.5 (last 6)"] = f"PASS-THIN ({h_over_6}/{h_len} >= {h_min})"
            is_perfect = False
        else:
            failed.append("Home over 2.5 (last 6)"); details["Home over 2.5 (last 6)"] = f"FAIL-THIN ({h_over_6}/{h_len} < {h_min})"; is_perfect = False

        h_total_6 = sum(gf + ga for gf, ga in home_6)
        h_total_min = max(6, round(h_len * 3))
        if h_total_6 >= h_total_min:
            passed.append("Home total goals (last 6)"); details["Home total goals (last 6)"] = f"PASS-THIN ({h_total_6} >= {h_total_min})"
            is_perfect = False
        else:
            failed.append("Home total goals (last 6)"); details["Home total goals (last 6)"] = f"FAIL-THIN ({h_total_6} < {h_total_min})"; is_perfect = False

    a_len = min(len(away_6), 6)
    if a_len >= 6:
        a_over_6 = sum(1 for gf, ga in away_6 if gf + ga > 2.5)
        if a_over_6 >= 4:
            passed.append("Away over 2.5 (last 6)"); details["Away over 2.5 (last 6)"] = f"PASS ({a_over_6}/6)"
        else:
            failed.append("Away over 2.5 (last 6)"); details["Away over 2.5 (last 6)"] = f"FAIL ({a_over_6}/6)"; is_perfect = False

        a_total_6 = sum(gf + ga for gf, ga in away_6)
        if a_total_6 >= 18:
            passed.append("Away total goals (last 6)"); details["Away total goals (last 6)"] = f"PASS ({a_total_6})"
        else:
            failed.append("Away total goals (last 6)"); details["Away total goals (last 6)"] = f"FAIL ({a_total_6})"; is_perfect = False
    elif a_len >= 2:
        a_over_6 = sum(1 for gf, ga in away_6 if gf + ga > 2.5)
        a_min = max(1, round(a_len * 0.67))
        if a_over_6 >= a_min:
            passed.append("Away over 2.5 (last 6)"); details["Away over 2.5 (last 6)"] = f"PASS-THIN ({a_over_6}/{a_len} >= {a_min})"
            is_perfect = False
        else:
            failed.append("Away over 2.5 (last 6)"); details["Away over 2.5 (last 6)"] = f"FAIL-THIN ({a_over_6}/{a_len} < {a_min})"; is_perfect = False

        a_total_6 = sum(gf + ga for gf, ga in away_6)
        a_total_min = max(6, round(a_len * 3))
        if a_total_6 >= a_total_min:
            passed.append("Away total goals (last 6)"); details["Away total goals (last 6)"] = f"PASS-THIN ({a_total_6} >= {a_total_min})"
            is_perfect = False
        else:
            failed.append("Away total goals (last 6)"); details["Away total goals (last 6)"] = f"FAIL-THIN ({a_total_6} < {a_total_min})"; is_perfect = False

    home_overall_6 = home_overall_6 or []
    away_overall_6 = away_overall_6 or []
    home_active = _team_active_goal_profile(home_overall_6)
    away_active = _team_active_goal_profile(away_overall_6)
    if home_active or away_active:
        teams = []
        if home_active:
            if _team_scored_every_match(home_overall_6):
                teams.append("home scored each")
            else:
                teams.append("home conceded each")
        if away_active:
            if _team_scored_every_match(away_overall_6):
                teams.append("away scored each")
            else:
                teams.append("away conceded each")
        passed.append("Overall goal activity (6)")
        details["Overall goal activity (6)"] = f"PASS ({', '.join(teams)})"
    else:
        h_len = min(len(home_overall_6), 6)
        a_len = min(len(away_overall_6), 6)
        if h_len < 2 and a_len < 2:
            details["Overall goal activity (6)"] = f"SKIPPED (only {h_len}/{a_len} overall matches)"
        else:
            failed.append("Overall goal activity (6)")
            details["Overall goal activity (6)"] = f"FAIL (neither team had activity in all available: {h_len}/{a_len})"
            is_perfect = False

    # BTTS rules: both teams scored (home 6 home, away 6 away). Thin-data: use >=50% of available (min 2 games).
    h_btts = _count_btts(home_6)
    h_len = min(len(home_6), 6)
    if h_len >= 6:
        if h_btts >= BTTS_MIN_6:
            passed.append("Home BTTS (last 6 home)")
            details["Home BTTS (last 6 home)"] = f"PASS ({h_btts}/6)"
            if h_btts < 6:
                is_perfect = False
        else:
            failed.append("Home BTTS (last 6 home)")
            details["Home BTTS (last 6 home)"] = f"FAIL ({h_btts}/6)"
            is_perfect = False
    elif h_len >= 2:
        h_min = max(1, round(h_len * 0.5))
        if h_btts >= h_min:
            passed.append("Home BTTS (last 6 home)")
            details["Home BTTS (last 6 home)"] = f"PASS-THIN ({h_btts}/{h_len} >= {h_min})"
        else:
            failed.append("Home BTTS (last 6 home)")
            details["Home BTTS (last 6 home)"] = f"FAIL-THIN ({h_btts}/{h_len} < {h_min})"
            is_perfect = False

    a_btts = _count_btts(away_6)
    a_len = min(len(away_6), 6)
    if a_len >= 6:
        if a_btts >= BTTS_MIN_6:
            passed.append("Away BTTS (last 6 away)")
            details["Away BTTS (last 6 away)"] = f"PASS ({a_btts}/6)"
            if a_btts < 6:
                is_perfect = False
        else:
            failed.append("Away BTTS (last 6 away)")
            details["Away BTTS (last 6 away)"] = f"FAIL ({a_btts}/6)"
            is_perfect = False
    elif a_len >= 2:
        a_min = max(1, round(a_len * 0.5))
        if a_btts >= a_min:
            passed.append("Away BTTS (last 6 away)")
            details["Away BTTS (last 6 away)"] = f"PASS-THIN ({a_btts}/{a_len} >= {a_min})"
        else:
            failed.append("Away BTTS (last 6 away)")
            details["Away BTTS (last 6 away)"] = f"FAIL-THIN ({a_btts}/{a_len} < {a_min})"
            is_perfect = False

    return passed, failed, details, is_perfect


# =============================================================================
# UNDER 2.5 ALGORITHM (Mirror Rules - Defensive Caps)
# =============================================================================
def apply_under_algorithm(home_3, away_3):
    """
    Official Under 2.5 Goals Algorithm from over25tips.com:

    3-GAME HOME CHECKS:
    - Previous three home games must have ended under 2.5 in at least two of three.
    - One or more of the score lines must contain 0 goals (home or away side) in any of the three games.

    3-GAME AWAY CHECKS:
    - Last three away games must be under 2.5 in at least two of three.
    - The AWAY team must not have scored in at least one of the last three away games.
    """
    if len(home_3) < 2 or len(away_3) < 2:
        return None, None, {"error": "Insufficient data"}, False

    passed, failed, details = [], [], {}
    is_perfect = True

    hn3, an3 = len(home_3), len(away_3)

    # --- 3-GAME HOME CHECKS ---
    # At least 2 of 3 home games under 2.5
    h_under_3 = sum(1 for gf, ga in home_3 if gf + ga < 2.5)
    if h_under_3 >= _thin_count(2, 3, hn3):
        passed.append("Home under 2.5 (last 3)"); details["Home under 2.5 (last 3)"] = f"PASS ({h_under_3}/{hn3} under 2.5)"
        if h_under_3 < hn3:
            is_perfect = False
    else:
        failed.append("Home under 2.5 (last 3)"); details["Home under 2.5 (last 3)"] = f"FAIL ({h_under_3}/{hn3} under 2.5)"; is_perfect = False

    # At least one score line has 0 goals in the 3 home games
    h_has_zero = any(gf == 0 or ga == 0 for gf, ga in home_3)
    if h_has_zero:
        passed.append("Home has 0-goal side (last 3)"); details["Home has 0-goal side (last 3)"] = "PASS (at least one 0-goal side)"
    else:
        failed.append("Home has 0-goal side (last 3)"); details["Home has 0-goal side (last 3)"] = "FAIL (no zero-goal sides)"; is_perfect = False

    # --- 3-GAME AWAY CHECKS ---
    # At least 2 of 3 away games under 2.5
    a_under_3 = sum(1 for gf, ga in away_3 if gf + ga < 2.5)
    if a_under_3 >= _thin_count(2, 3, an3):
        passed.append("Away under 2.5 (last 3)"); details["Away under 2.5 (last 3)"] = f"PASS ({a_under_3}/{an3} under 2.5)"
        if a_under_3 < an3:
            is_perfect = False
    else:
        failed.append("Away under 2.5 (last 3)"); details["Away under 2.5 (last 3)"] = f"FAIL ({a_under_3}/{an3} under 2.5)"; is_perfect = False

    # Away team did NOT score in at least 1 of last 3 away games
    a_blanked = sum(1 for gf, _ in away_3 if gf == 0)
    if a_blanked >= _thin_count(1, 3, an3):
        passed.append("Away blanked (last 3)"); details["Away blanked (last 3)"] = f"PASS ({a_blanked}/{an3} away games with 0 scored)"
    else:
        failed.append("Away blanked (last 3)"); details["Away blanked (last 3)"] = f"FAIL ({a_blanked}/{an3} away games with 0 scored)"; is_perfect = False

    return passed, failed, details, is_perfect


def apply_under_6game_checks(home_6, away_6):
    """
    6-game average checks for Under 2.5 (supplementary to 3-game rules).
    These are NOT part of the official over25tips.com algorithm but add
    statistical rigor for classification tiers.

    Added UC5/UC6 (non-BTTS / clean-sheet games): direct opposite of Over's BTTS checks.
    A match where at least one team blanked (gf=0 OR ga=0) is the statistical
    complement of a BTTS match and strongly correlates with Under 2.5 outcomes.
    """
    passed, failed, details = [], [], {}
    is_perfect = True

    home_6 = home_6 or []
    away_6 = away_6 or []

    h_len = min(len(home_6), 6)
    a_len = min(len(away_6), 6)

    if h_len >= 6 and a_len >= 6:
        # Home scored average <= 1.2
        hs = sum(gf for gf, _ in home_6) / 6
        if hs <= 1.2:
            passed.append("Home scored avg (last 6)"); details["Home scored avg (last 6)"] = f"PASS (HS={hs:.2f} <= 1.2)"
        else:
            failed.append("Home scored avg (last 6)"); details["Home scored avg (last 6)"] = f"FAIL (HS={hs:.2f} > 1.2)"; is_perfect = False

        # Home conceded average <= 1.2
        hc = sum(ga for _, ga in home_6) / 6
        if hc <= 1.2:
            passed.append("Home conceded avg (last 6)"); details["Home conceded avg (last 6)"] = f"PASS (HC={hc:.2f} <= 1.2)"
        else:
            failed.append("Home conceded avg (last 6)"); details["Home conceded avg (last 6)"] = f"FAIL (HC={hc:.2f} > 1.2)"; is_perfect = False

        # Away scored average <= 1.0
        a_s = sum(gf for gf, _ in away_6) / 6
        if a_s <= 1.0:
            passed.append("Away scored avg (last 6)"); details["Away scored avg (last 6)"] = f"PASS (AS={a_s:.2f} <= 1.0)"
        else:
            failed.append("Away scored avg (last 6)"); details["Away scored avg (last 6)"] = f"FAIL (AS={a_s:.2f} > 1.0)"; is_perfect = False

        # Away conceded average <= 1.0
        a_c = sum(ga for _, ga in away_6) / 6
        if a_c <= 1.0:
            passed.append("Away conceded avg (last 6)"); details["Away conceded avg (last 6)"] = f"PASS (AC={a_c:.2f} <= 1.0)"
        else:
            failed.append("Away conceded avg (last 6)"); details["Away conceded avg (last 6)"] = f"FAIL (AC={a_c:.2f} > 1.0)"; is_perfect = False

        # Non-BTTS 6-game (home): at least NON_BTTS_MIN_6 matches where one team blanked (opposite of Over BTTS)
        h_non_btts = _count_non_btts(home_6)
        if h_non_btts >= NON_BTTS_MIN_6:
            passed.append("Home non-BTTS (last 6 home)")
            details["Home non-BTTS (last 6 home)"] = f"PASS ({h_non_btts}/6 blanked matches)"
            if h_non_btts < 6:
                is_perfect = False
        else:
            failed.append("Home non-BTTS (last 6 home)")
            details["Home non-BTTS (last 6 home)"] = f"FAIL ({h_non_btts}/6 blanked matches < {NON_BTTS_MIN_6})"
            is_perfect = False

        # Non-BTTS 6-game (away): at least NON_BTTS_MIN_6 matches where one team blanked
        a_non_btts = _count_non_btts(away_6)
        if a_non_btts >= NON_BTTS_MIN_6:
            passed.append("Away non-BTTS (last 6 away)")
            details["Away non-BTTS (last 6 away)"] = f"PASS ({a_non_btts}/6 blanked matches)"
            if a_non_btts < 6:
                is_perfect = False
        else:
            failed.append("Away non-BTTS (last 6 away)")
            details["Away non-BTTS (last 6 away)"] = f"FAIL ({a_non_btts}/6 blanked matches < {NON_BTTS_MIN_6})"
            is_perfect = False
    else:
        # Thin-data pass: proportional non-BTTS check on what's available (min 2 games, >=50% non-BTTS)
        details["UC1-4"] = f"SKIPPED-THIN ({h_len}/{a_len} 6-game averages)"
        if h_len >= 2:
            h_non_btts = _count_non_btts(home_6)
            h_min = max(1, round(h_len * 0.5))
            if h_non_btts >= h_min:
                passed.append("Home non-BTTS (last 6 home)")
                details["Home non-BTTS (last 6 home)"] = f"PASS-THIN ({h_non_btts}/{h_len} >= {h_min})"
            else:
                failed.append("Home non-BTTS (last 6 home)")
                details["Home non-BTTS (last 6 home)"] = f"FAIL-THIN ({h_non_btts}/{h_len} < {h_min})"
                is_perfect = False

        if a_len >= 2:
            a_non_btts = _count_non_btts(away_6)
            a_min = max(1, round(a_len * 0.5))
            if a_non_btts >= a_min:
                passed.append("Away non-BTTS (last 6 away)")
                details["Away non-BTTS (last 6 away)"] = f"PASS-THIN ({a_non_btts}/{a_len} >= {a_min})"
            else:
                failed.append("Away non-BTTS (last 6 away)")
                details["Away non-BTTS (last 6 away)"] = f"FAIL-THIN ({a_non_btts}/{a_len} < {a_min})"
                is_perfect = False

    return passed, failed, details, is_perfect


def apply_under_overall_checks(home_overall_6, away_overall_6):
    """Each team must have under 2.5 in at least 4 of last 6 overall matches. Thin-data fallback included."""
    passed, failed, details = [], [], {}
    is_perfect = True

    home_overall_6 = home_overall_6 or []
    away_overall_6 = away_overall_6 or []

    h_len = min(len(home_overall_6), 6)
    if h_len >= 6:
        h_under = _count_under_25_overall(home_overall_6)
        if h_under >= 4:
            passed.append("Home under 2.5 overall (6)")
            details["Home under 2.5 overall (6)"] = f"PASS ({h_under}/6 under 2.5)"
            if h_under < 6:
                is_perfect = False
        else:
            failed.append("Home under 2.5 overall (6)")
            details["Home under 2.5 overall (6)"] = f"FAIL ({h_under}/6 under 2.5)"
            is_perfect = False
    elif h_len >= 2:
        h_under = _count_under_25_overall(home_overall_6)
        h_min = max(1, round(h_len * 0.6))
        if h_under >= h_min:
            passed.append("Home under 2.5 overall (6)")
            details["Home under 2.5 overall (6)"] = f"PASS-THIN ({h_under}/{h_len} >= {h_min})"
        else:
            failed.append("Home under 2.5 overall (6)")
            details["Home under 2.5 overall (6)"] = f"FAIL-THIN ({h_under}/{h_len} < {h_min})"
            is_perfect = False

    a_len = min(len(away_overall_6), 6)
    if a_len >= 6:
        a_under = _count_under_25_overall(away_overall_6)
        if a_under >= 4:
            passed.append("Away under 2.5 overall (6)")
            details["Away under 2.5 overall (6)"] = f"PASS ({a_under}/6 under 2.5)"
            if a_under < 6:
                is_perfect = False
        else:
            failed.append("Away under 2.5 overall (6)")
            details["Away under 2.5 overall (6)"] = f"FAIL ({a_under}/6 under 2.5)"
            is_perfect = False
    elif a_len >= 2:
        a_under = _count_under_25_overall(away_overall_6)
        a_min = max(1, round(a_len * 0.6))
        if a_under >= a_min:
            passed.append("Away under 2.5 overall (6)")
            details["Away under 2.5 overall (6)"] = f"PASS-THIN ({a_under}/{a_len} >= {a_min})"
        else:
            failed.append("Away under 2.5 overall (6)")
            details["Away under 2.5 overall (6)"] = f"FAIL-THIN ({a_under}/{a_len} < {a_min})"
            is_perfect = False

    return passed, failed, details, is_perfect


# =============================================================================
# POISSON MODEL (with Dixon-Coles correction)
# =============================================================================
DIXON_COLES_RHO = -0.13

_MIN_DATA_GAMES = 5
_MIN_COMBINED_LAMBDA_OVER = 2.75
_MAX_COMBINED_LAMBDA_UNDER = 2.15
_PREMIUM_COMBINED_LAMBDA_OVER = 3.30
_PREMIUM_COMBINED_LAMBDA_UNDER = 2.00

IMPLIED_ODDS_OVER = 1.95
IMPLIED_ODDS_UNDER = 1.85

_WEIGHT_RULES = 0.40
_WEIGHT_MODEL = 0.40
_WEIGHT_EDGE = 0.20

_TIER_PREMIUM_CUTOFF = 0.62
_TIER_SOLID_CUTOFF = 0.54

_MIN_FORM_HALFLIFE = 3.0

_WEAK_ROI_LEAGUE_KEYWORDS = (
    "swedish allsvenskan",
    "allsvenskan",
    "superettan",
    "belarus",
    "k-league",
    "k league",
    "league of ireland",
    "fai cup",
    "mexican primera",
    "brazilian serie a",
    "mls",
    "ecuador",
    "argentina primera",
    "chile primera",
)
_WEAK_ROI_MULTIPLIER = 0.82
_WEAK_ROI_OVER_LAMBDA_BOOST = 0.30
_WEAK_ROI_UNDER_LAMBDA_REDUCTION = 0.20

_REGRESSION_OVER_STREAK = 5
_REGRESSION_UNDER_STREAK = 5
_REGRESSION_PENALTY = 0.08

_LEAGUE_BASELINE_CACHE = {}


def _exponential_form_averages(form_tuples, halflife=_MIN_FORM_HALFLIFE):
    """Weighted average of (gf, ga) with exponential decay.

    form_tuples[0] is the most recent match (weight=1.0); each older match
    is multiplied by 0.5 ** (n / halflife) for match index n going back.
    Returns (weighted_gf_per_game, weighted_ga_per_game, effective_sample_weight).
    """
    return _shared_exponential_form_averages(form_tuples, halflife)


def _is_weak_roi_league(league_name):
    return _shared_is_weak_roi_league(league_name, _WEAK_ROI_LEAGUE_KEYWORDS)


def _over_streak_count(form_tuples):
    """How many of the most recent form games were consecutively Over 2.5?"""
    n = 0
    for gf, ga in form_tuples:
        if gf + ga > 2.5:
            n += 1
        else:
            break
    return n


def _under_streak_count(form_tuples):
    n = 0
    for gf, ga in form_tuples:
        if gf + ga < 2.5:
            n += 1
        else:
            break
    return n


def regression_penalty(home_6, away_6, side):
    """0..1 multiplicative penalty applied to confidence score; 1.0 = no penalty."""
    if side == "over":
        h_streak = _over_streak_count(home_6 or [])
        a_streak = _over_streak_count(away_6 or [])
        if max(h_streak, a_streak) >= _REGRESSION_OVER_STREAK:
            return 1.0 - _REGRESSION_PENALTY
    elif side == "under":
        h_streak = _under_streak_count(home_6 or [])
        a_streak = _under_streak_count(away_6 or [])
        if max(h_streak, a_streak) >= _REGRESSION_UNDER_STREAK:
            return 1.0 - _REGRESSION_PENALTY
    return 1.0


def _chaos_rule_over(home_6, away_6):
    """Both sides concede heavily: 85% of their matches involve goals against.

    Returns True if BOTH teams conceded on average >= 1.2 goals/game in 6-game
    form. This is the 'chaotic open game' signal independent of who scores.
    """
    if len(home_6 or []) < 2 or len(away_6 or []) < 2:
        return False
    hc = sum(ga for _, ga in home_6) / max(len(home_6), 1)
    ac = sum(ga for _, ga in away_6) / max(len(away_6), 1)
    return hc >= 1.2 and ac >= 1.2


def _compact_rule_under(home_6, away_6):
    """Both sides attack AND defense are stingy.

    Returns True if BOTH teams scored <= 1.0/game AND conceded <= 0.9/game
    average over their form window.
    """
    if len(home_6 or []) < 2 or len(away_6 or []) < 2:
        return False
    hs = sum(gf for gf, _ in home_6) / max(len(home_6), 1)
    hc = sum(ga for _, ga in home_6) / max(len(home_6), 1)
    as_ = sum(gf for gf, _ in away_6) / max(len(away_6), 1)
    ac = sum(ga for _, ga in away_6) / max(len(away_6), 1)
    return hs <= 1.0 and hc <= 0.9 and as_ <= 1.0 and ac <= 0.9



def poisson_pmf(k, lam):
    return _shared_poisson_pmf(k, lam)


def _dixon_coles_tau(h, a, lh, la, rho):
    if (h, a) == (0, 0):
        return max(0.01, 1.0 - rho * lh * la)
    if (h, a) == (1, 0):
        return 1.0 + rho * la
    if (h, a) == (0, 1):
        return 1.0 + rho * lh
    if (h, a) == (1, 1):
        return 1.0 - rho
    return 1.0


def calculate_poisson_over25(home_lambda, away_lambda, max_goals=10):
    over_prob = 0.0
    total = 0.0
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            joint = poisson_pmf(h, home_lambda) * poisson_pmf(a, away_lambda)
            if joint <= 0:
                continue
            tau = _dixon_coles_tau(h, a, home_lambda, away_lambda, DIXON_COLES_RHO)
            p = joint * tau
            total += p
            if h + a > 2:
                over_prob += p
    if total > 0:
        over_prob /= total
    return round(over_prob * 100, 1)


def calculate_poisson_under25(home_lambda, away_lambda, max_goals=10):
    over_prob = calculate_poisson_over25(home_lambda, away_lambda, max_goals) / 100.0
    return round((1.0 - over_prob) * 100, 1)


HISTORY_FILE_FALLBACK = "prediction_history.json"


def _load_league_baselines():
    """Compute per-league home/away goals baselines from prediction_history settled picks.

    Falls back to global defaults if a league has < 5 settled matches or the file
    is missing. Returns a dict: league_name -> (h_att, a_att, h_def, a_def)
    """
    if _LEAGUE_BASELINE_CACHE:
        return _LEAGUE_BASELINE_CACHE
    default = (1.45, 1.20, 1.35, 1.25)
    history_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), HISTORY_FILE_FALLBACK)
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
    for market in ("home_win", "over_under"):
        for row in data.get(market, []) or []:
            final_score = row.get("final_score")
            if not final_score or "-" not in str(final_score):
                continue
            try:
                hg, ag = str(final_score).split("-", 1)
                hg = int(hg.strip())
                ag = int(ag.strip())
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
    global_h_gf = sum(s["h_gf"] for s in league_stats.values())
    global_h_ga = sum(s["h_ga"] for s in league_stats.values())
    global_a_gf = sum(s["a_gf"] for s in league_stats.values())
    global_a_ga = sum(s["a_ga"] for s in league_stats.values())
    global_n = max(1, sum(s["n"] for s in league_stats.values()))
    fallback = (
        global_h_gf / global_n,
        global_a_gf / global_n,
        global_h_ga / global_n,
        global_a_ga / global_n,
    )
    if not all(fallback) or fallback[0] < 0.6 or fallback[0] > 2.5:
        fallback = default
    _LEAGUE_BASELINE_CACHE["_default"] = fallback
    for lg, s in league_stats.items():
        n = s["n"]
        if n < 5:
            _LEAGUE_BASELINE_CACHE[lg] = fallback
            continue
        ha = s["h_gf"] / n
        aa = s["a_gf"] / n
        hd = s["h_ga"] / n
        ad = s["a_ga"] / n
        if ha < 0.5 or aa < 0.4 or hd < 0.4 or ad < 0.4:
            _LEAGUE_BASELINE_CACHE[lg] = fallback
            continue
        _LEAGUE_BASELINE_CACHE[lg] = (ha, aa, hd, ad)
    return _LEAGUE_BASELINE_CACHE


def _league_baselines(league_name):
    cache = _load_league_baselines()
    return cache.get(league_name, cache.get("_default", (1.45, 1.20, 1.35, 1.25)))


# =============================================================================
# EXPECTED GOALS (Shrinkage Estimator, per-league baselines)
# =============================================================================
def get_match_lambdas(home_6, away_6, league_name=None):
    """
    Calculate cross-matched Poisson lambdas.

    Uses exponential-decay weighted form averages (recent games count ~3x more
    than games from 6 weeks back) plus per-league baselines from history.
    Weak-ROI leagues have baselines nudged to make the combined-lambda gate
    harder to satisfy on the historically unprofitable side.
    """
    bl = _league_baselines(league_name or "")
    home_baseline_attack, away_baseline_attack, home_baseline_defense, away_baseline_defense = bl

    weak_league = _is_weak_roi_league(league_name)

    n_home = max(len(home_6 or []), 1)
    n_away = max(len(away_6 or []), 1)

    h_gf_avg, h_ga_avg, _ = _exponential_form_averages(home_6 or [])
    if not (home_6 or []):
        h_gf_avg, h_ga_avg = home_baseline_attack, away_baseline_defense
    a_gf_avg, a_ga_avg, _ = _exponential_form_averages(away_6 or [])
    if not (away_6 or []):
        a_gf_avg, a_ga_avg = away_baseline_attack, home_baseline_defense

    adaptive_shrinkage = SHRINKAGE_WEIGHT
    if len(home_6 or []) < _MIN_DATA_GAMES or len(away_6 or []) < _MIN_DATA_GAMES:
        adaptive_shrinkage = max(0.45, SHRINKAGE_WEIGHT - 0.15)

    h_attack = adaptive_shrinkage * h_gf_avg + (1 - adaptive_shrinkage) * home_baseline_attack
    h_defense = adaptive_shrinkage * h_ga_avg + (1 - adaptive_shrinkage) * away_baseline_defense

    a_attack = adaptive_shrinkage * a_gf_avg + (1 - adaptive_shrinkage) * away_baseline_attack
    a_defense = adaptive_shrinkage * a_ga_avg + (1 - adaptive_shrinkage) * home_baseline_defense

    home_lambda = h_attack * (a_defense / max(0.5, home_baseline_attack))
    away_lambda = a_attack * (h_defense / max(0.5, away_baseline_attack))

    if weak_league:
        home_lambda -= _WEAK_ROI_OVER_LAMBDA_BOOST / 2.0
        away_lambda -= _WEAK_ROI_OVER_LAMBDA_BOOST / 2.0

    return (
        round(max(0.5, min(3.8, home_lambda)), 2),
        round(max(0.5, min(3.8, away_lambda)), 2),
    )


def data_volume_penalty(home_6, away_6):
    """Return a 0..1 multiplier. 1.0 = full data, <1.0 = thin-data penalty."""
    n = min(len(home_6 or []), len(away_6 or []))
    if n >= _MIN_DATA_GAMES:
        return 1.0
    if n >= 4:
        return 0.97
    if n >= 3:
        return 0.90
    if n >= 2:
        return 0.82
    return 0.70


def lambda_gate_passes(home_lambda, away_lambda, side):
    combined = home_lambda + away_lambda
    if side == "over":
        return combined >= _MIN_COMBINED_LAMBDA_OVER
    if side == "under":
        return combined <= _MAX_COMBINED_LAMBDA_UNDER
    return True


def compute_confidence_score(rule_score, max_score, model_prob_pct, decimal_odds, data_mult=1.0):
    """Weighted 0..1 confidence score combining rules, Poisson probability and value edge."""
    rule_component = max(0.0, min(1.0, (rule_score / max(max_score, 1))))
    model_component = max(0.0, min(1.0, (model_prob_pct / 100.0)))
    implied = 1.0 / max(1.05, decimal_odds)
    edge_component = max(0.0, min(1.0, ((model_prob_pct / 100.0) - implied) + 0.5))
    raw = _WEIGHT_RULES * rule_component + _WEIGHT_MODEL * model_component + _WEIGHT_EDGE * edge_component
    return max(0.0, min(1.0, raw * data_mult))


def tier_from_confidence(score, side, home_lambda, away_lambda, is_perfect=True):
    combined = home_lambda + away_lambda
    premium_ok = False
    if side == "over" and combined >= _PREMIUM_COMBINED_LAMBDA_OVER:
        premium_ok = True
    elif side == "under" and combined <= _PREMIUM_COMBINED_LAMBDA_UNDER:
        premium_ok = True
    if score >= _TIER_PREMIUM_CUTOFF and premium_ok and is_perfect:
        return "perfect"
    if score >= _TIER_SOLID_CUTOFF:
        return "qualified"
    return "close"


# Kelly-stake helpers (calculate_kelly, apply_portfolio_kelly) are imported
# from utils.py at the top of this file — do not redefine them here.
# NOTE: this predictor previously called calculate_kelly() with its own
# default decimal_odds=2.0, while home_win/btts each used a different
# market-appropriate default (2.8 / 1.90). Every call site in this file
# already passes explicit odds, so this default was never actually used —
# but if you add a new call site, pass decimal_odds explicitly rather than
# relying on a default, since the "right" default differs per market.


# =============================================================================
# MATCH PROCESSING
# =============================================================================
def process_single_match(match, target_date, default_odds_over=2.0, default_odds_under=1.85):
    try:
        league_name = match.get("league", "")

        home_3 = get_team_form(match["home_team_id"], True, 3, target_date)
        away_3 = get_team_form(match["away_team_id"], False, 3, target_date)
        home_6 = get_team_form(match["home_team_id"], True, 6, target_date)
        away_6 = get_team_form(match["away_team_id"], False, 6, target_date)
        home_overall_6 = get_team_overall_form(match["home_team_id"], 6, target_date)
        away_overall_6 = get_team_overall_form(match["away_team_id"], 6, target_date)

        data_mult = data_volume_penalty(home_6, away_6)

        over_passed, over_failed, over_details, over_is_perfect = apply_over_algorithm(
            home_3, away_3, home_6, away_6, home_overall_6, away_overall_6
        )

        over_score = len(over_passed) if over_passed else 0
        if _chaos_rule_over(home_6, away_6):
            over_score += 1

        home_btts_6 = _count_btts(home_6)
        away_btts_6 = _count_btts(away_6)
        home_non_btts_6 = _count_non_btts(home_6)
        away_non_btts_6 = _count_non_btts(away_6)
        btts_gate = _btts_gate_passes(home_6, away_6)
        non_btts_gate = _non_btts_gate_passes(home_6, away_6)
        venue_over_gate = _venue_over_gate_passes(home_6, away_6)
        high_scoring_under_block = _high_scoring_blocks_under(home_6, away_6)
        recent_cold_over_block = _recent_cold_blocks_over(home_3, away_3)
        h2h_over_blocked, h2h_over_meetings = _h2h_over_blocked(
            match["home_team_id"], match["away_team_id"], target_date
        )
        h2h_over_blocked_2, h2h_over_meetings_2 = _h2h_over_blocked_2game(
            match["home_team_id"], match["away_team_id"], target_date
        )
        h2h_under_blocked, h2h_under_meetings = _h2h_under_blocked(
            match["home_team_id"], match["away_team_id"], target_date
        )
        scoring_drought_veto, scoring_drought_reason = _scoring_drought_veto_over(home_3, away_3)
        defensive_wall_veto, defensive_wall_reason = _defensive_wall_veto_over(home_3, away_3)
        under_leak_veto, under_leak_reason = _under_defensive_leak_veto(home_3, away_3)
        under_goal_shock_veto, under_goal_shock_reason = _recent_goal_shock_veto_under(home_3, away_3)
        over_btts_gate, over_btts_reason = _over_btts_participation_gate(home_3, away_3)
        over_leak_gate, over_leak_reason = _over_leak_participation_gate(home_3, away_3)
        over_low_event_veto, over_low_event_reason = _combined_low_event_veto(home_3, away_3)
        over_goalless_shock_veto, over_goalless_shock_reason = _recent_goalless_shock_veto(home_3, away_3)
        under_peak_game_veto, under_peak_game_reason = _under_peak_game_veto(home_6, away_6)
        derby_veto, derby_reason = _derby_veto(match)
        scoring_consistency_veto, scoring_consistency_reason = _scoring_consistency_veto(home_6, away_6)
        overall_scoring_veto, overall_scoring_reason = _overall_scoring_symmetry_veto(
            home_overall_6, away_overall_6
        )
        borderline_cluster_veto, borderline_cluster_reason = _borderline_two_goal_cluster_veto(
            home_overall_6, away_overall_6
        )
        severe_crisis_veto, severe_crisis_reason = _severe_offensive_crisis_veto(
            home_6, away_6, home_overall_6, away_overall_6
        )
        mutual_cold_veto, mutual_cold_reason = _mutual_cold_attacks_defences_veto(home_3, away_3)
        combined_weak_veto, combined_weak_reason = _combined_weak_attack_strong_defence_veto(home_6, away_6)
        venue_h2h_veto, venue_h2h_meetings = _venue_h2h_bogey_veto(
            match["home_team_id"], match["away_team_id"], target_date
        )
        h2h_zero_bogey_veto, h2h_zero_bogey_meetings = _h2h_zero_goal_bogey_veto(
            match["home_team_id"], match["away_team_id"], target_date
        )
        borderline_one_veto, borderline_one_reason = _borderline_one_goal_cluster_veto(
            home_overall_6, away_overall_6
        )
        one_sided_blank_veto, one_sided_blank_reason = _one_sided_home_blank_away_midrange_veto(
            home_3, away_3
        )

        under3_result = apply_under_algorithm(home_3, away_3)
        if under3_result[0] is None:
            under3_passed, under3_failed, under3_details, under3_perfect = [], [], {}, False
        else:
            under3_passed, under3_failed, under3_details, under3_perfect = under3_result

        under6_result = apply_under_6game_checks(home_6, away_6)
        if under6_result[0] is None:
            under6_passed, under6_failed, under6_details, under6_perfect = [], [], {}, False
        else:
            under6_passed, under6_failed, under6_details, under6_perfect = under6_result

        under_overall_passed, under_overall_failed, under_overall_details, under_overall_perfect = (
            apply_under_overall_checks(home_overall_6, away_overall_6)
        )

        under_passed = under3_passed + under6_passed + under_overall_passed
        under_failed = under3_failed + under6_failed + under_overall_failed
        under_details = {**under3_details, **under6_details, **under_overall_details}
        under_is_perfect = under3_perfect and under6_perfect and under_overall_perfect

        under_score = len(under_passed) if under_passed else 0
        if _compact_rule_under(home_6, away_6):
            under_score += 1

        home_lambda, away_lambda = get_match_lambdas(home_6, away_6, league_name=league_name)
        under_blowout_veto, under_blowout_reason = _under_blowout_risk_veto(home_lambda, away_lambda)
        xg_imbalance_veto, xg_imbalance_reason = _xg_imbalance_shutout_risk_veto(
            home_lambda, away_lambda, home_overall_6, away_overall_6
        )
        under_overall_overs_veto, under_overall_overs_reason = _under_overall_over_frequency_veto(
            home_overall_6, away_overall_6
        )
        under_overall_leak_veto, under_overall_leak_reason = _under_recent_overall_leak_veto(
            home_overall_6, away_overall_6
        )
        h2h_under_high_scoring_veto, h2h_under_high_scoring_reason = _under_h2h_high_scoring_veto(
            match["home_team_id"], match["away_team_id"], target_date
        )

        over_gate = lambda_gate_passes(home_lambda, away_lambda, "over")
        under_gate = lambda_gate_passes(home_lambda, away_lambda, "under")

        data_quality_min = min(
            len(home_6), len(away_6),
            len(home_overall_6), len(away_overall_6)
        )
        thin_data_gap = max(0, 6 - data_quality_min)
        base_over_min = MAX_OVER_SCORE - 3 if _is_weak_roi_league(league_name) else MAX_OVER_SCORE - 4
        base_under_min = MAX_UNDER_SCORE - 2 if _is_weak_roi_league(league_name) else MAX_UNDER_SCORE - 3
        over_min_score = max(6, base_over_min - thin_data_gap)
        under_min_score = max(5, base_under_min - thin_data_gap)

        # Early-season gate: if either team has fewer than 3 home games played,
        # require a higher minimum score to compensate for thin data noise
        early_season = len(home_6) < 3 or len(away_6) < 3
        if early_season:
            over_min_score += 1
            under_min_score += 1

        over_qualifies = (
            bool(over_passed) and over_score >= over_min_score and over_gate
            and btts_gate and venue_over_gate and not recent_cold_over_block
            and not h2h_over_blocked and not h2h_over_blocked_2
            and not scoring_drought_veto and not defensive_wall_veto
            and not over_btts_gate and not over_leak_gate
            and not over_low_event_veto and not over_goalless_shock_veto
            and not derby_veto and not scoring_consistency_veto
            and not overall_scoring_veto
            and not borderline_cluster_veto and not severe_crisis_veto
            and not xg_imbalance_veto
            and not mutual_cold_veto and not combined_weak_veto
            and not h2h_zero_bogey_veto and not borderline_one_veto
            and not one_sided_blank_veto and not venue_h2h_veto
        )
        under_qualifies = (
            bool(under_passed) and under_score >= under_min_score and under_gate
            and non_btts_gate and not high_scoring_under_block
            and not h2h_under_blocked
            and not under_leak_veto
            and not under_goal_shock_veto
            and not under_blowout_veto
            and not under_peak_game_veto
            and not under_overall_overs_veto
            and not under_overall_leak_veto
            and not h2h_under_high_scoring_veto
        )

        if over_qualifies or under_qualifies:
            over25_prob_pct = calculate_poisson_over25(home_lambda, away_lambda)
            under25_prob_pct = calculate_poisson_under25(home_lambda, away_lambda)
        else:
            combined = home_lambda + away_lambda
            if combined > 2.5:
                over25_prob_pct = round(max(50.0, 50.0 + (combined - 2.5) * 15), 1)
                under25_prob_pct = round(100.0 - over25_prob_pct, 1)
            else:
                under25_prob_pct = round(max(50.0, 50.0 + (2.5 - combined) * 15), 1)
                over25_prob_pct = round(100.0 - under25_prob_pct, 1)

        over25_prob = over25_prob_pct / 100.0
        under25_prob = under25_prob_pct / 100.0

        over_confidence = (
            "HIGH" if over25_prob >= 0.58
            else "MEDIUM" if over25_prob >= 0.52
            else "LOW"
        )
        under_confidence = (
            "HIGH" if under25_prob >= 0.58
            else "MEDIUM" if under25_prob >= 0.52
            else "LOW"
        )

        league_mult = _WEAK_ROI_MULTIPLIER if _is_weak_roi_league(league_name) else 1.0
        over_regression_penalty = regression_penalty(home_6, away_6, "over")
        under_regression_penalty = regression_penalty(home_6, away_6, "under")
        early_mult = _early_season_penalty(match.get("date"))

        over_final_mult = data_mult * league_mult * over_regression_penalty * early_mult
        under_final_mult = data_mult * league_mult * under_regression_penalty * early_mult

        over_conf_score = compute_confidence_score(
            over_score, MAX_OVER_SCORE, over25_prob_pct, default_odds_over, over_final_mult
        )
        under_conf_score = compute_confidence_score(
            under_score, MAX_UNDER_SCORE, under25_prob_pct, default_odds_under, under_final_mult
        )

        over_tier = tier_from_confidence(over_conf_score, "over", home_lambda, away_lambda, over_is_perfect)
        under_tier = tier_from_confidence(under_conf_score, "under", home_lambda, away_lambda, under_is_perfect)

        over_kelly = calculate_kelly(over25_prob, default_odds_over, use_half=True)
        under_kelly = calculate_kelly(under25_prob, default_odds_under, use_half=True)

        if not over_qualifies:
            over_tier = None
            over_kelly = 0.0
        if not under_qualifies:
            under_tier = None
            under_kelly = 0.0

        regressions = []
        if over_regression_penalty < 1.0:
            regressions.append("over streak")
        if under_regression_penalty < 1.0:
            regressions.append("under streak")
        if recent_cold_over_block:
            regressions.append("recent cold scoring form")
        if high_scoring_under_block:
            regressions.append("high-scoring venue form")
        if h2h_over_blocked:
            regressions.append(f"h2h low-scoring bogey ({len(h2h_over_meetings)} meetings)")
        if h2h_over_blocked_2:
            regressions.append(f"h2h 2-game low-scoring bogey ({len(h2h_over_meetings_2)} meetings)")
        if h2h_under_blocked:
            regressions.append(f"h2h high-scoring bogey ({len(h2h_under_meetings)} meetings)")
        if scoring_drought_veto:
            regressions.append(f"scoring drought ({scoring_drought_reason})")
        if defensive_wall_veto:
            regressions.append(f"defensive wall ({defensive_wall_reason})")
        if over_btts_gate:
            regressions.append(f"bt participation gate ({over_btts_reason})")
        if over_leak_gate:
            regressions.append(f"leak participation gate ({over_leak_reason})")
        if over_low_event_veto:
            regressions.append(f"combined low event ({over_low_event_reason})")
        if over_goalless_shock_veto:
            regressions.append(f"recent goalless shock ({over_goalless_shock_reason})")
        if under_leak_veto:
            regressions.append(f"under defensive leak ({under_leak_reason})")
        if under_goal_shock_veto:
            regressions.append(f"under recent goal shock ({under_goal_shock_reason})")
        if under_blowout_veto:
            regressions.append(f"under blowout risk ({under_blowout_reason})")
        if under_peak_game_veto:
            regressions.append(f"under peak game veto ({under_peak_game_reason})")
        if under_overall_overs_veto:
            regressions.append(f"under overall overs ({under_overall_overs_reason})")
        if under_overall_leak_veto:
            regressions.append(f"under overall defensive leak ({under_overall_leak_reason})")
        if h2h_under_high_scoring_veto:
            regressions.append(f"h2h high-scoring under veto ({h2h_under_high_scoring_reason})")
        if derby_veto:
            regressions.append(f"derby match ({derby_reason})")
        if scoring_consistency_veto:
            regressions.append(f"scoring inconsistency ({scoring_consistency_reason})")
        if overall_scoring_veto:
            regressions.append(f"overall scoring weak ({overall_scoring_reason})")
        if borderline_cluster_veto:
            regressions.append(f"2-goal boundary cluster ({borderline_cluster_reason})")
        if severe_crisis_veto:
            regressions.append(f"severe offensive crisis ({severe_crisis_reason})")
        if xg_imbalance_veto:
            regressions.append(f"xG imbalance shutout risk ({xg_imbalance_reason})")
        if mutual_cold_veto:
            regressions.append(f"mutual 0-0 streak ({mutual_cold_reason})")
        if combined_weak_veto:
            regressions.append(f"combined weak attack+defence ({combined_weak_reason})")
        if h2h_zero_bogey_veto:
            regressions.append(f"h2h 0-or-1-goal bogey ({len(h2h_zero_bogey_meetings)} meetings)")
        if borderline_one_veto:
            regressions.append(f"0-or-1-goal cluster ({borderline_one_reason})")
        if one_sided_blank_veto:
            regressions.append(f"home blank + away mid-range ({one_sided_blank_reason})")
        if early_mult < 1.0:
            regressions.append(f"early-season penalty (x{early_mult})")

        return {
            "status": "success",
            "data": {
                "match": match,
                "over": {
                    "score": over_score,
                    "passed": over_passed,
                    "details": over_details,
                    "is_perfect": over_is_perfect,
                    "tier": over_tier,
                    "confidence_score": round(over_conf_score * 100, 1),
                    "prob": over25_prob_pct,
                    "confidence": over_confidence,
                    "kelly": round(over_kelly * 100, 2),
                    "gate_passed": over_gate,
                    "btts_gate_passed": btts_gate,
                    "venue_over_gate_passed": venue_over_gate,
                    "recent_cold_blocked": recent_cold_over_block,
                    "h2h_blocked": h2h_over_blocked,
                    "h2h_blocked_2game": h2h_over_blocked_2,
                    "h2h_meetings": len(h2h_over_meetings),
                    "h2h_meetings_2game": len(h2h_over_meetings_2),
                    "scoring_drought_veto": scoring_drought_veto,
                    "scoring_drought_reason": scoring_drought_reason,
                    "defensive_wall_veto": defensive_wall_veto,
                    "defensive_wall_reason": defensive_wall_reason,
                    "btts_participation_gate": over_btts_gate,
                    "btts_participation_reason": over_btts_reason,
                    "leak_participation_gate": over_leak_gate,
                    "leak_participation_reason": over_leak_reason,
                    "low_event_veto": over_low_event_veto,
                    "low_event_reason": over_low_event_reason,
                    "goalless_shock_veto": over_goalless_shock_veto,
                    "goalless_shock_reason": over_goalless_shock_reason,
                    "derby_veto": derby_veto,
                    "derby_reason": derby_reason,
                    "scoring_consistency_veto": scoring_consistency_veto,
                    "scoring_consistency_reason": scoring_consistency_reason,
                    "borderline_cluster_veto": borderline_cluster_veto,
                    "borderline_cluster_reason": borderline_cluster_reason,
                    "severe_crisis_veto": severe_crisis_veto,
                    "severe_crisis_reason": severe_crisis_reason,
                    "xg_imbalance_veto": xg_imbalance_veto,
                    "xg_imbalance_reason": xg_imbalance_reason,
                    "mutual_cold_veto": mutual_cold_veto,
                    "mutual_cold_reason": mutual_cold_reason,
                    "combined_weak_veto": combined_weak_veto,
                    "combined_weak_reason": combined_weak_reason,
                    "h2h_zero_bogey_veto": h2h_zero_bogey_veto,
                    "h2h_zero_bogey_meetings_count": len(h2h_zero_bogey_meetings),
                    "borderline_one_veto": borderline_one_veto,
                    "borderline_one_reason": borderline_one_reason,
                    "one_sided_blank_veto": one_sided_blank_veto,
                    "one_sided_blank_reason": one_sided_blank_reason,
                    "early_season_mult": round(early_mult, 2),
                    "home_btts_6": home_btts_6,
                    "away_btts_6": away_btts_6,
                    "data_mult": round(data_mult, 2),
                    "weak_league_mult": round(league_mult, 2),
                    "regression_mult": round(over_regression_penalty, 2),
                    "min_score_threshold": over_min_score,
                },
                "under": {
                    "score": under_score,
                    "passed": under_passed,
                    "details": under_details,
                    "is_perfect": under_is_perfect,
                    "tier": under_tier,
                    "confidence_score": round(under_conf_score * 100, 1),
                    "prob": under25_prob_pct,
                    "confidence": under_confidence,
                    "kelly": round(under_kelly * 100, 2),
                    "gate_passed": under_gate,
                    "non_btts_gate_passed": non_btts_gate,
                    "high_scoring_blocked": high_scoring_under_block,
                    "h2h_blocked": h2h_under_blocked,
                    "h2h_meetings": len(h2h_under_meetings),
                    "defensive_leak_veto": under_leak_veto,
                    "defensive_leak_reason": under_leak_reason,
                    "goal_shock_veto": under_goal_shock_veto,
                    "goal_shock_reason": under_goal_shock_reason,
                    "blowout_risk_veto": under_blowout_veto,
                    "blowout_risk_reason": under_blowout_reason,
                    "peak_game_veto": under_peak_game_veto,
                    "peak_game_reason": under_peak_game_reason,
                    "early_season_mult": round(early_mult, 2),
                    "home_non_btts_6": home_non_btts_6,
                    "away_non_btts_6": away_non_btts_6,
                    "data_mult": round(data_mult, 2),
                    "weak_league_mult": round(league_mult, 2),
                    "regression_mult": round(under_regression_penalty, 2),
                    "min_score_threshold": under_min_score,
                },
                "poisson": {
                    "home_lambda": home_lambda,
                    "away_lambda": away_lambda,
                    "combined_lambda": round(home_lambda + away_lambda, 2),
                    "over25_prob": over25_prob_pct,
                    "under25_prob": under25_prob_pct,
                },
                "guards": {
                    "weak_roi_league": _is_weak_roi_league(league_name),
                    "chaos_rule_over": _chaos_rule_over(home_6, away_6),
                    "compact_rule_under": _compact_rule_under(home_6, away_6),
                    "btts_gate_passed": btts_gate,
                    "non_btts_gate_passed": non_btts_gate,
                    "high_scoring_blocked": high_scoring_under_block,
                    "h2h_over_blocked": h2h_over_blocked,
                    "h2h_over_blocked_2game": h2h_over_blocked_2,
                    "h2h_under_blocked": h2h_under_blocked,
                    "h2h_over_meetings": len(h2h_over_meetings),
                    "h2h_over_meetings_2game": len(h2h_over_meetings_2),
                    "h2h_under_meetings": len(h2h_under_meetings),
                    "home_btts_6": home_btts_6,
                    "away_btts_6": away_btts_6,
                    "home_non_btts_6": home_non_btts_6,
                    "away_non_btts_6": away_non_btts_6,
                    "scoring_drought_veto": scoring_drought_veto,
                    "scoring_drought_reason": scoring_drought_reason,
                    "defensive_wall_veto": defensive_wall_veto,
                    "defensive_wall_reason": defensive_wall_reason,
                    "over_btts_participation_gate": over_btts_gate,
                    "over_btts_participation_reason": over_btts_reason,
                    "over_leak_participation_gate": over_leak_gate,
                    "over_leak_participation_reason": over_leak_reason,
                    "over_low_event_veto": over_low_event_veto,
                    "over_low_event_reason": over_low_event_reason,
                    "over_goalless_shock_veto": over_goalless_shock_veto,
                    "over_goalless_shock_reason": over_goalless_shock_reason,
                    "under_defensive_leak_veto": under_leak_veto,
                    "under_defensive_leak_reason": under_leak_reason,
                    "under_goal_shock_veto": under_goal_shock_veto,
                    "under_goal_shock_reason": under_goal_shock_reason,
                    "under_blowout_risk_veto": under_blowout_veto,
                    "under_blowout_risk_reason": under_blowout_reason,
                    "under_peak_game_veto": under_peak_game_veto,
                    "under_peak_game_reason": under_peak_game_reason,
                    "derby_veto": derby_veto,
                    "derby_reason": derby_reason,
                    "scoring_consistency_veto": scoring_consistency_veto,
                    "scoring_consistency_reason": scoring_consistency_reason,
                    "overall_scoring_veto": overall_scoring_veto,
                    "overall_scoring_reason": overall_scoring_reason,
                    "borderline_cluster_veto": borderline_cluster_veto,
                    "borderline_cluster_reason": borderline_cluster_reason,
                    "severe_crisis_veto": severe_crisis_veto,
                    "severe_crisis_reason": severe_crisis_reason,
                    "xg_imbalance_veto": xg_imbalance_veto,
                    "xg_imbalance_reason": xg_imbalance_reason,
                    "mutual_cold_veto": mutual_cold_veto,
                    "mutual_cold_reason": mutual_cold_reason,
                    "combined_weak_veto": combined_weak_veto,
                    "combined_weak_reason": combined_weak_reason,
                    "h2h_zero_bogey_veto": h2h_zero_bogey_veto,
                    "h2h_zero_bogey_meetings_count": len(h2h_zero_bogey_meetings),
                    "borderline_one_veto": borderline_one_veto,
                    "borderline_one_reason": borderline_one_reason,
                    "one_sided_blank_veto": one_sided_blank_veto,
                    "one_sided_blank_reason": one_sided_blank_reason,
                    "early_season_mult": round(early_mult, 2),
                    "recent_cold_blocked": recent_cold_over_block,
                    "regression_penalty_applied": regressions,
                },
            }
        }
    except Exception as e:
        logger.error(
            f"Processing failed for "
            f"{match.get('home', 'N/A')} vs {match.get('away', 'N/A')}: {e}",
            exc_info=True
        )
        return {"status": "error"}


# =============================================================================
# REPORTING
# =============================================================================
def _append_ou_pick(lines, idx, item, side, odds, detailed, compact=False):
    """Append one over/under pick with consistent spacing."""
    m = item["match"]
    p = item["poisson"]
    tgt = item[side]
    prob_key = "over25_prob" if side == "over" else "under25_prob"
    max_score = MAX_OVER_SCORE if side == "over" else MAX_UNDER_SCORE
    market_label = "Over 2.5" if side == "over" else "Under 2.5"
    market = MARKET_OVER25 if side == "over" else MARKET_UNDER25
    if compact:
        lines.append(format_compact_pick_line(
            m["home"], m["away"], "O2.5" if side == "over" else "U2.5",
            tgt.get("tier"), p[prob_key], m.get("date"),
        ))
        return
    extra = None
    if detailed:
        h2h_blocked_flag = tgt.get("h2h_blocked")
        h2h_n = tgt.get("h2h_meetings", 0)
        h2h_note = None
        if h2h_blocked_flag and h2h_n:
            h2h_note = f"qualifies with H2H bogey flag raised ({h2h_n} meetings)"
        elif h2h_n and h2h_n >= 3:
            h2h_note = f"{h2h_n} H2H meetings logged"
        extra = format_vip_extra_lines(
            tgt["kelly"], odds, tgt["score"], max_score,
            home_lambda=p["home_lambda"], away_lambda=p["away_lambda"],
            model_prob=p[prob_key],
            market=market,
            h2h_note=h2h_note,
            rule_details=tgt.get("details"),
        )
    categories = describe_pick_categories(
        m["home"], m["away"], m.get("league", ""),
        market=market,
        tier=tgt.get("tier"),
        weak_roi_league=bool((item.get("guards") or {}).get("weak_roi_league")),
    )
    lines.extend(format_pick_block(
        idx, m["home"], m["away"], m["date"],
        f"{market_label} · {format_confidence_label(tgt['confidence'])} ({p[prob_key]}%)",
        extra,
        league=m.get("league"),
        categories=categories,
    ))


def build_report(over_perfect, over_qualified, over_close, over_weak,
               under_perfect, under_qualified, under_close, under_weak,
               scanned_dates, bankroll, odds_over, odds_under, detailed=False, compact=False,
               include_yesterday=True, include_header=True, include_footer=True,
               report_date=None):
    """
    Build a clean, mobile-friendly report - both channels show all picks, free is simplified
    Returns: (report, base_date, included_over, included_under)
    """
    included_over = [
        item for item in (over_perfect + over_qualified + over_close)
        if not is_static_blocked_fixture(item.get("match", {}))
    ]
    included_under = [
        item for item in (under_perfect + under_qualified + under_close)
        if not is_static_blocked_fixture(item.get("match", {}))
    ]
    if report_date:
        included_over = filter_pick_items_by_date(included_over, report_date)
        included_under = filter_pick_items_by_date(included_under, report_date)
    included_dates = scanned_dates
    
    base_date = scanned_dates[0] if scanned_dates else datetime.now().strftime("%Y-%m-%d")

    lines = []
    if report_date and not include_header and not compact:
        lines.append(f"📅 Picks for {report_date}")
        lines.append("")
    if not compact:
        if detailed and include_header:
            lines.extend(format_vip_banner("Over / Under 2.5", base_date, included_dates))
        if include_header:
            lines.append("⚽️ Over/Under 2.5 picks")
            lines.append("")
            if len(included_dates) > 1:
                lines.append(f"Dates: {included_dates[0]} to {included_dates[-1]}")
            else:
                lines.append(f"Date: {base_date}")
            lines.append("")
            if include_yesterday:
                append_yesterday_section(lines, "over_under", detailed=detailed)
    elif compact:
        def _append_compact_tier_group(tiers_items, side_label):
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
                        'over' if side_label == 'OVER 2.5' else 'under',
                        (item['over'] if side_label == 'OVER 2.5' else item['under']).get('tier'),
                        (item['poisson']['over25_prob'] if side_label == 'OVER 2.5' else item['poisson']['under25_prob']),
                        item['match'].get('date'),
                    )}")
            return has_group

        over_perfect_tier = [p for p in included_over if p in over_perfect]
        over_strong_tier = [p for p in included_over if p in over_qualified]
        over_watch_tier = [p for p in included_over if p in over_close]
        under_perfect_tier = [p for p in included_under if p in under_perfect]
        under_strong_tier = [p for p in included_under if p in under_qualified]
        under_watch_tier = [p for p in included_under if p in under_close]

        any_over = _append_compact_tier_group([
            (COMPACT_TIER_HEADER_PREMIUM, over_perfect_tier),
            (COMPACT_TIER_HEADER_STRONG, over_strong_tier),
            (COMPACT_TIER_HEADER_WATCH, over_watch_tier),
        ], "OVER 2.5")
        if any_over and (under_perfect_tier or under_strong_tier or under_watch_tier):
            lines.append("")
        _append_compact_tier_group([
            (COMPACT_TIER_HEADER_PREMIUM, under_perfect_tier),
            (COMPACT_TIER_HEADER_STRONG, under_strong_tier),
            (COMPACT_TIER_HEADER_WATCH, under_watch_tier),
        ], "UNDER 2.5")

    if not compact:
        # Over 2.5 section (full detail)
        if included_over:
            lines.append("")
            lines.append("🟢 Over 2.5 goals")
            lines.append("")
            included_over_perfect = [p for p in included_over if p in over_perfect]
            included_over_qualified = [p for p in included_over if p in over_qualified]
            included_over_close = [p for p in included_over if p in over_close]
            if included_over_perfect:
                lines.append(f"  {PICK_TIER_PREMIUM}")
                lines.append("")
                for i, item in enumerate(included_over_perfect, 1):
                    _append_ou_pick(lines, i, item, "over", odds_over, detailed, compact)
            if included_over_qualified:
                lines.append(f"  {PICK_TIER_STRONG}")
                lines.append("")
                start_idx = len(included_over_perfect) + 1
                for i, item in enumerate(included_over_qualified, start_idx):
                    _append_ou_pick(lines, i, item, "over", odds_over, detailed, compact)
            if included_over_close:
                if detailed:
                    lines.append(f"  {PICK_TIER_VALUE}")
                    lines.append("")
                start_idx = len(included_over_perfect) + len(included_over_qualified) + 1
                for i, item in enumerate(included_over_close, start_idx):
                    _append_ou_pick(lines, i, item, "over", odds_over, detailed, compact)

        # Under 2.5 section (full detail)
        if included_under:
            lines.append("")
            lines.append("🔵 Under 2.5 goals")
            lines.append("")
            included_under_perfect = [p for p in included_under if p in under_perfect]
            included_under_qualified = [p for p in included_under if p in under_qualified]
            included_under_close = [p for p in included_under if p in under_close]
            if included_under_perfect:
                lines.append(f"  {PICK_TIER_PREMIUM}")
                lines.append("")
                for i, item in enumerate(included_under_perfect, 1):
                    _append_ou_pick(lines, i, item, "under", odds_under, detailed, compact)
            if included_under_qualified:
                lines.append(f"  {PICK_TIER_STRONG}")
                lines.append("")
                start_idx = len(included_under_perfect) + 1
                for i, item in enumerate(included_under_qualified, start_idx):
                    _append_ou_pick(lines, i, item, "under", odds_under, detailed, compact)
            if included_under_close:
                if detailed:
                    lines.append(f"  {PICK_TIER_VALUE}")
                    lines.append("")
                start_idx = len(included_under_perfect) + len(included_under_qualified) + 1
                for i, item in enumerate(included_under_close, start_idx):
                    _append_ou_pick(lines, i, item, "under", odds_under, detailed, compact)

        if include_footer:
            if detailed:
                lines.extend(format_vip_summary(
                    "OVER 2.5  ·  Pick summary",
                    over_perfect, over_qualified, over_close,
                ))
                lines.extend(format_vip_summary(
                    "UNDER 2.5  ·  Pick summary",
                    under_perfect, under_qualified, under_close,
                ))
            lines.append("---")
            lines.append("For informational purposes only")
            lines.append("Gamble responsibly")
            lines.append("")

    report = "\n".join(lines).strip()
    if not report:
        report = "— none"
    return report, base_date, included_over, included_under

# =============================================================================
# MAIN
# =============================================================================
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(
        description="Over/Under 2.5 Goals Predictor with Unified Analysis"
    )
    parser.add_argument(
        "date",
        nargs="?",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Only include scheduled (upcoming) matches"
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        default=1000.0,
        help="Total bankroll in currency units"
    )
    parser.add_argument(
        "--odds-over",
        type=float,
        default=2.0,
        help="Average decimal odds for Over 2.5"
    )
    parser.add_argument(
        "--odds-under",
        type=float,
        default=1.85,
        help="Average decimal odds for Under 2.5"
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear the SQLite cache before running"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of days to scan starting from the date (default: 4, weekends: 6)"
    )
    parser.add_argument(
        "--publish-date",
        default=None,
        help="Date for Telegram daily picks (default: today). Filters Telegram output only.",
    )
    args = parser.parse_args()

    if args.clear_cache:
        cache.clear()

    start_date = datetime.strptime(args.date, "%Y-%m-%d")
    scan_days = args.days
    if scan_days is None:
        scan_days = 6 if start_date.weekday() >= 4 else 4

    # Over 2.5 buckets
    over_perfect, over_qualified, over_close, over_weak = [], [], [], []
    # Under 2.5 buckets
    under_perfect, under_qualified, under_close, under_weak = [], [], [], []

    scanned_dates = []

    print(f"Starting Over/Under 2.5 analysis from {args.date}...")

    for day_offset in range(scan_days):
        current_date = start_date + timedelta(days=day_offset)
        date_str = current_date.strftime("%Y-%m-%d")
        scanned_dates.append(date_str)

        fixtures = fetch_soccerbase_fixtures(date_str)
        seen = set()
        unique_fixtures = []
        for f in fixtures:
            key = (f["home_team_id"], f["away_team_id"], f["league"])
            if key not in seen and f["home_team_id"] and f["away_team_id"]:
                if not args.scheduled or f["status"] == "Scheduled":
                    seen.add(key)
                    unique_fixtures.append(f)

        if not unique_fixtures:
            logger.info(f"No fixtures to process on {date_str}")
            continue

        print(f"   Processing {len(unique_fixtures)} matches on {date_str}...")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    process_single_match, match, date_str, args.odds_over, args.odds_under
                ): match
                for match in unique_fixtures
            }
            for future in as_completed(futures):
                try:
                    res = future.result(timeout=60)
                except Exception as e:
                    logger.error(f"Future timeout/error: {e}")
                    continue

                if res["status"] == "insufficient":
                    pass
                elif res["status"] == "success":
                    data = res["data"]

                    over_tier = data["over"]["tier"]
                    if over_tier == "perfect":
                        over_perfect.append(data)
                    elif over_tier == "qualified":
                        over_qualified.append(data)
                    elif over_tier == "close":
                        over_close.append(data)
                    elif data["over"]["score"] >= max(1, MAX_OVER_SCORE - 4):
                        over_weak.append(data)

                    under_tier = data["under"]["tier"]
                    if under_tier == "perfect":
                        under_perfect.append(data)
                    elif under_tier == "qualified":
                        under_qualified.append(data)
                    elif under_tier == "close":
                        under_close.append(data)
                    elif data["under"]["score"] >= max(1, MAX_UNDER_SCORE - 3):
                        under_weak.append(data)

    # Apply portfolio Kelly cap separately for Over and Under
    apply_portfolio_kelly(over_perfect + over_qualified + over_close, "over", args.bankroll, MAX_TOTAL_EXPOSURE / 2)
    apply_portfolio_kelly(under_perfect + under_qualified + under_close, "under", args.bankroll, MAX_TOTAL_EXPOSURE / 2)

    # Build and output reports (both free and detailed)
    free_report, base_date, included_over, included_under = build_report(
        over_perfect, over_qualified, over_close, over_weak,
        under_perfect, under_qualified, under_close, under_weak,
        scanned_dates, args.bankroll, args.odds_over, args.odds_under, detailed=False
    )
    publish_date = args.publish_date or datetime.now().strftime("%Y-%m-%d")
    telegram_report, _, _, _ = build_report(
        over_perfect, over_qualified, over_close, over_weak,
        under_perfect, under_qualified, under_close, under_weak,
        scanned_dates, args.bankroll, args.odds_over, args.odds_under,
        detailed=False, compact=False,
        include_yesterday=False, include_header=False, include_footer=False,
        report_date=publish_date,
    )
    detailed_report, _, _, _ = build_report(
        over_perfect, over_qualified, over_close, over_weak,
        under_perfect, under_qualified, under_close, under_weak,
        scanned_dates, args.bankroll, args.odds_over, args.odds_under, detailed=True
    )

    # Output free report (default)
    print("\n===EMAIL_START===")
    print(free_report)
    print("===EMAIL_END===")
    write_telegram_section(telegram_report, "ou_telegram.txt")

    # Save detailed report to file
    detailed_report_path = f"over_under_vip_report_{base_date}.txt"
    with open(detailed_report_path, "w", encoding="utf-8") as f:
        f.write(detailed_report)

    # Save JSON
    output_path = f"over_under_25_report_{base_date}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "metadata": {
                "scanned_window": scanned_dates,
                "bankroll": args.bankroll,
                "odds_over": args.odds_over,
                "odds_under": args.odds_under,
                "max_exposure": MAX_TOTAL_EXPOSURE,
                "under_caps": {
                    "home_total_6": UNDER_HOME_TOTAL_6_CAP,
                    "away_total_6": UNDER_AWAY_TOTAL_6_CAP,
                    "home_over25_max": UNDER_HOME_OVER25_MAX,
                    "away_over25_max": UNDER_AWAY_OVER25_MAX
                },
                "generated_at": datetime.now().isoformat()
            },
            "over": {
                "perfect": over_perfect,
                "qualified": over_qualified,
                "close": over_close,
                "weak": over_weak
            },
            "under": {
                "perfect": under_perfect,
                "qualified": under_qualified,
                "close": under_close,
                "weak": under_weak
            }
        }, f, indent=2, default=str)

    # Record predictions for tracking
    # IMPORTANT: Use original tier buckets (not build_report's included_*) so
    # statistically-blocked leagues are still recorded with published=false.
    # record_predictions() handles the published flag internally via
    # is_statistical_block_only() and fully skips only static/integrity blocks.
    try:
        ou_picks = []
        all_over = over_perfect + over_qualified + over_close
        for pick in all_over:
            tier = pick["over"].get("tier") or (
                "perfect" if pick in over_perfect else
                "qualified" if pick in over_qualified else "close"
            )
            ou_picks.append({
                "league": pick["match"]["league"],
                "home": pick["match"]["home"],
                "away": pick["match"]["away"],
                "date": pick["match"]["date"],
                "prediction": "over",
                "confidence": tier,
            })
        all_under = under_perfect + under_qualified + under_close
        for pick in all_under:
            tier = pick["under"].get("tier") or (
                "perfect" if pick in under_perfect else
                "qualified" if pick in under_qualified else "close"
            )
            ou_picks.append({
                "league": pick["match"]["league"],
                "home": pick["match"]["home"],
                "away": pick["match"]["away"],
                "date": pick["match"]["date"],
                "prediction": "under",
                "confidence": tier,
            })
        stats = record_predictions(base_date, [], ou_picks)
        if stats["added"]:
            print(f"Predictions recorded for performance tracking ({stats['added']} new)")
        elif stats["skipped"]:
            print(f"Predictions already recorded ({stats['skipped']} skipped)")
    except Exception as e:
        print(f"Could not record predictions: {e}")

    print(f"\nReport saved: {output_path}")
    print(f"VIP report saved: {detailed_report_path}")


if __name__ == "__main__":
    main()
