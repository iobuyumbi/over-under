#!/usr/bin/env python3
"""
OVER 0.5 TEAM GOAL PREDICTOR — v2.2 (DEFENCE-GATE INTEGRATED)
================================================================
HARD REQUIREMENT: Opponent must be conceding frequently.

Scoring streak alone is not enough. A team that scores in 10 straight
games but faces a defence that kept 4 clean sheets in the last 5 will
likely be shut out. The defence gate prevents this mismatch.

STREAK GATE (team must pass ONE):
  • Overall: scored in >= 8/10 or >= 9/12
  • Venue: scored in >= 5/6 or >= 4/5

DEFENCE GATE (opponent must pass ONE) — HARD VETO:
  • Venue: opponent conceded in >= 4/5 of last venue games
  • Overall: opponent conceded in >= 8/10 of last overall games
  • Elite: opponent conceded in >= 5/5 venue OR >= 10/10 overall

If opponent fails the defence gate, the pick is VETOED regardless of
how strong the scoring streak is.
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
    Cache, build_session, fetch as _shared_fetch, parse_date,
    calculate_kelly, apply_portfolio_kelly,
    exponential_form_averages as _shared_exponential_form_averages,
    is_weak_roi_league as _shared_is_weak_roi_league,
    poisson_pmf as _shared_poisson_pmf,
    conceded_in_n_of_m as _shared_conceded_in_n_of_m,
    opponent_concession_gate as _shared_opponent_concession_gate,
)
from scraping import (
    fetch_soccerbase_fixtures as _shared_fetch_fixtures,
    fetch_soccerbase_team_results as _shared_fetch_team_results,
    get_team_form as _shared_get_team_form,
    get_team_overall_form as _shared_get_team_overall_form,
    get_h2h_meetings as _shared_get_h2h_meetings,
    _thin_count, _thin_total,
)
from prediction_tracker import (
    record_predictions, format_vip_extra_lines, format_pick_block,
    format_compact_pick_line, format_confidence_label, describe_pick_categories,
    filter_pick_items_by_date, is_static_blocked_fixture, write_telegram_section,
    append_yesterday_section, format_vip_banner, format_vip_summary,
    PICK_TIER_PREMIUM, PICK_TIER_STRONG, PICK_TIER_VALUE,
    COMPACT_TIER_HEADER_PREMIUM, COMPACT_TIER_HEADER_STRONG, COMPACT_TIER_HEADER_WATCH,
)

# =============================================================================
# CONFIG
# =============================================================================
CACHE_DB = "soccerbase_cache_oo05.db"
CACHE_TTL_HOURS = 24
MAX_WORKERS = 4
REQUEST_DELAY_MIN = 2.5
REQUEST_DELAY_MAX = 5.0
MAX_TOTAL_EXPOSURE = 0.20
DEFAULT_ODDS_HOME = 1.45
DEFAULT_ODDS_AWAY = 1.55

if os.getenv("CI"):
    MAX_WORKERS = 2
    REQUEST_DELAY_MIN = 4.0
    REQUEST_DELAY_MAX = 8.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
session = build_session()
cache = Cache(db_path=CACHE_DB, ttl_hours=CACHE_TTL_HOURS)

MARKET_HOME_TG = "home_team_goals"
MARKET_AWAY_TG = "away_team_goals"
MARKET_LABEL_HOME = "Home to Score"
MARKET_LABEL_AWAY = "Away to Score"
SHORT_MARKET_HOME = "HTS"
SHORT_MARKET_AWAY = "ATS"

# STREAK GATES (team scoring)
OVERALL_STREAK_10 = (8, 10)
OVERALL_STREAK_12 = (9, 12)
VENUE_STREAK_6 = (5, 6)
VENUE_STREAK_5 = (4, 5)

# DEFENCE GATES (opponent conceding) — HARD VETO
OPP_VENUE_LEAK_5 = (4, 5)      # conceded in >= 4 of last 5 venue
OPP_OVERALL_LEAK_10 = (8, 10)  # conceded in >= 8 of last 10 overall
OPP_VENUE_ELITE_5 = (5, 5)     # conceded in 5/5 venue (elite leak)
OPP_OVERALL_ELITE_10 = (10, 10) # conceded in 10/10 overall (elite leak)

MIN_DATA_GAMES = 3
MAX_SCORE = 12

_WEAK_ROI_KEYWORDS = [
    "youth", "u17", "u19", "u20", "u21", "amateur", "friendly",
    "pre-season", "copa do brasil sub", "qualification preliminary",
    "women reserve", "reserve", "academy", "trial", "exhibition",
]
_WEAK_ROI_MULTIPLIER = 0.85

_WEIGHT_RULES = 0.45
_WEIGHT_MODEL = 0.35
_WEIGHT_EDGE = 0.20

_TIER_PREMIUM_CUTOFF = 0.65
_TIER_SOLID_CUTOFF = 0.55

SHRINKAGE_WEIGHT = 0.55


def fetch(url, use_cache=True):
    return _shared_fetch(url, session, cache, use_cache=use_cache,
                         min_delay=REQUEST_DELAY_MIN, max_delay=REQUEST_DELAY_MAX)

def fetch_soccerbase_team_results(team_id):
    return _shared_fetch_team_results(team_id, fetch)

def fetch_soccerbase_fixtures(date_str):
    return _shared_fetch_fixtures(date_str, fetch)

def get_team_form(team_id, is_home=True, num_matches=20, target_date_str=None):
    return _shared_get_team_form(team_id, fetch_soccerbase_team_results, is_home, num_matches, target_date_str, parse_date)

def get_team_overall_form(team_id, num_matches=20, target_date_str=None):
    return _shared_get_team_overall_form(team_id, fetch_soccerbase_team_results, num_matches, target_date_str, parse_date)

def get_h2h_meetings(home_team_id, away_team_id, target_date_str=None, limit=8):
    return _shared_get_h2h_meetings(home_team_id, away_team_id, fetch_soccerbase_team_results, target_date_str, limit=limit)

def _is_weak_roi_league(league_name):
    return _shared_is_weak_roi_league(league_name, _WEAK_ROI_KEYWORDS)


# =============================================================================
# STREAK & DEFENCE GATES
# =============================================================================

def _scored_in_n_of_m(form, n, m):
    sample = (form or [])[:m]
    if len(sample) < min(3, m):
        return False, 0, len(sample)
    scored = sum(1 for gf, _ in sample if gf >= 1)
    return scored >= n, scored, len(sample)


def _conceded_in_n_of_m(form, n, m):
    """[DEPRECATED 2026-09-12] Kept as thin wrapper around shared
    utils.conceded_in_n_of_m() for any stray callers outside the main
    check_defence_gate() path. New code should import from utils directly.
    """
    return _shared_conceded_in_n_of_m(form, n, m)


def check_streak_gate(overall_form, venue_form):
    """Team scoring gate. Returns (passed, label, scored, sample_size)."""
    for need, of in [OVERALL_STREAK_10, OVERALL_STREAK_12]:
        passed, scored, n = _scored_in_n_of_m(overall_form, need, of)
        if passed:
            return True, f"overall_{scored}/{n}", scored, n
    for need, of in [VENUE_STREAK_6, VENUE_STREAK_5]:
        passed, scored, n = _scored_in_n_of_m(venue_form, need, of)
        if passed:
            return True, f"venue_{scored}/{n}", scored, n
    o_scored = sum(1 for gf, _ in (overall_form or [])[:10] if gf >= 1)
    v_scored = sum(1 for gf, _ in (venue_form or [])[:6] if gf >= 1)
    return False, f"best_o{o_scored}/10_v{v_scored}/6", o_scored, min(len(overall_form or []), 10)


def check_defence_gate(opp_venue_form, opp_overall_form):
    """HARD VETO: Opponent must be conceding frequently.

    Returns: (passed, label, is_elite)
    is_elite = True if opponent conceded 5/5 venue OR 10/10 overall

    [REFACTORED 2026-09-12] Delegates to the shared
    utils.opponent_concession_gate() with O0.5's hardcoded 5/10 windows.
    The gate logic was copy-pasted independently in oo05 / home_win /
    (newly) over25; centralizing it prevents drift between the three.
    """
    return _shared_opponent_concession_gate(
        opp_venue_form, opp_overall_form,
        venue_leak=OPP_VENUE_LEAK_5,
        venue_elite=OPP_VENUE_ELITE_5,
        overall_leak=OPP_OVERALL_LEAK_10,
        overall_elite=OPP_OVERALL_ELITE_10,
    )


# =============================================================================
# RULE ENGINE
# =============================================================================

def apply_algorithm(team_form_20, opp_form_6, team_ov_20, opp_ov_10, team_v_6, opp_v_6, is_home=True):
    passed, failed, details = [], [], {}
    is_perfect = True

    # 0 — STREAK GATE
    streak_pass, streak_label, _, _ = check_streak_gate(team_ov_20, team_v_6)
    if streak_pass:
        passed.append(f"Streak gate ({streak_label})")
        details["Streak gate"] = f"PASS ({streak_label})"
    else:
        failed.append(f"Streak gate ({streak_label})")
        details["Streak gate"] = f"FAIL ({streak_label})"
        is_perfect = False

    # 0b — DEFENCE GATE (HARD VETO)
    def_pass, def_label, def_elite = check_defence_gate(opp_v_6, opp_ov_10)
    if def_pass:
        passed.append(f"Defence gate ({def_label})")
        details["Defence gate"] = f"PASS ({def_label})"
        if not def_elite:
            is_perfect = False  # standard pass = not perfect
    else:
        failed.append(f"Defence gate ({def_label})")
        details["Defence gate"] = f"FAIL ({def_label}) — HARD VETO"
        is_perfect = False

    if len(team_form_20 or []) < 2 or len(opp_form_6 or []) < 2:
        return None, None, {"error": "Insufficient data"}, False

    recent_run, _ = _current_scored_run(team_form_20)
    team_scored_v6 = sum(1 for gf, _ in (team_v_6 or [])[:6] if gf >= 1)
    opp_cs_v6, opp_cs_n = _clean_sheets_in_last_n(opp_v_6, 6)
    opp_cs_v6 = opp_cs_v6 if opp_cs_v6 is not None else 2
    opp_concede_run = _consecutive_conceded_run(opp_v_6)

    # 1 — RECENT RUN >= 3
    if recent_run >= 3:
        passed.append("Recent 3-match scoring run"); details["Recent 3-match scoring run"] = f"PASS ({recent_run})"
    else:
        failed.append("Recent 3-match scoring run"); details["Recent 3-match scoring run"] = f"FAIL ({recent_run})"; is_perfect = False

    # 2 — OPPONENT CS <= 2/6
    if opp_cs_n >= 4 and opp_cs_v6 <= 2:
        passed.append("Opponent leaky venue (CS<=2/6)"); details["Opponent leaky venue (CS<=2/6)"] = f"PASS (CS {opp_cs_v6}/{opp_cs_n})"
    else:
        failed.append("Opponent leaky venue (CS<=2/6)"); details["Opponent leaky venue (CS<=2/6)"] = f"FAIL (CS {opp_cs_v6}/{opp_cs_n})"; is_perfect = False

    # 3 — OPPONENT CONCEDED RUN >= 2
    if opp_concede_run >= 2:
        passed.append("Opponent concede run (>=2)"); details["Opponent concede run (>=2)"] = f"PASS ({opp_concede_run})"
    else:
        failed.append("Opponent concede run (>=2)"); details["Opponent concede run (>=2)"] = f"FAIL ({opp_concede_run})"; is_perfect = False

    # 4 — TEAM SCORED VENUE >= 4/6
    s6_n = min(len(team_v_6 or []), 6)
    if s6_n >= 3 and team_scored_v6 >= _thin_count(4, 6, s6_n):
        passed.append("Team venue scored (>=4/6)"); details["Team venue scored (>=4/6)"] = f"PASS ({team_scored_v6}/{s6_n})"
    else:
        failed.append("Team venue scored (>=4/6)"); details["Team venue scored (>=4/6)"] = f"FAIL ({team_scored_v6}/{s6_n})"; is_perfect = False

    # 5 — OPPONENT OVERALL CONCEDED >= 4/10
    opp_ov_cs10, opp_ov_n10 = _clean_sheets_in_last_n(opp_ov_10, 10)
    if opp_ov_cs10 is not None and opp_ov_n10 >= 5:
        if (opp_ov_n10 - opp_ov_cs10) >= 4:
            passed.append("Opponent overall conceded (>=4/10)"); details["Opponent overall conceded (>=4/10)"] = "PASS"
        else:
            failed.append("Opponent overall conceded (>=4/10)"); details["Opponent overall conceded (>=4/10)"] = "FAIL"; is_perfect = False
    else:
        details["Opponent overall conceded (>=4/10)"] = "SKIP"

    # 6 — COMBINED GPG >= 1.3
    sf, of = (team_v_6 or [])[:6], (opp_v_6 or [])[:6]
    avg = (sum(gf+ga for gf,ga in sf) + sum(gf+ga for gf,ga in of)) / max(1, len(sf)+len(of))
    if avg >= 1.3:
        passed.append("Combined GPG (>=1.3)"); details["Combined GPG (>=1.3)"] = f"PASS ({avg:.2f})"
    else:
        failed.append("Combined GPG (>=1.3)"); details["Combined GPG (>=1.3)"] = f"FAIL ({avg:.2f})"; is_perfect = False

    # 7 — BOTH TEAMS SCORED OVERALL >= 3/6
    so, oo = (team_ov_20 or [])[:6], (opp_ov_10 or [])[:6]
    bs1 = sum(1 for gf,_ in so if gf>=1) if len(so)>=3 else 0
    bs2 = sum(1 for gf,_ in oo if gf>=1) if len(oo)>=3 else 0
    if len(so)>=3 and len(oo)>=3 and bs1>=3 and bs2>=3:
        passed.append("Both scored overall (>=3/6)"); details["Both scored overall (>=3/6)"] = f"PASS ({bs1}v{bs2})"
    else:
        failed.append("Both scored overall (>=3/6)"); details["Both scored overall (>=3/6)"] = f"FAIL ({bs1}v{bs2})"; is_perfect = False

    # 8 — AWAY SCORED ROAD >= 2/6
    away_form = team_v_6 if not is_home else opp_v_6
    away_s6 = sum(1 for gf,_ in (away_form or [])[:6] if gf>=1)
    away_n = min(len(away_form or []), 6)
    if away_n >= 3 and away_s6 >= _thin_count(2, 6, away_n):
        passed.append("Away scored road (>=2/6)"); details["Away scored road (>=2/6)"] = f"PASS ({away_s6}/{away_n})"
    else:
        failed.append("Away scored road (>=2/6)"); details["Away scored road (>=2/6)"] = f"FAIL ({away_s6}/{away_n})"; is_perfect = False

    # 9 — HOME DEFENCE NOT ELITE (CS<=4/6)
    home_form = opp_v_6 if is_home else team_v_6
    home_cs, home_n = _clean_sheets_in_last_n(home_form, 6)
    if home_cs is not None and home_n >= 4 and home_cs <= 4:
        passed.append("Home defence not elite (CS<=4/6)"); details["Home defence not elite (CS<=4/6)"] = f"PASS (CS {home_cs}/{home_n})"
    elif home_cs is not None:
        failed.append("Home defence not elite (CS<=4/6)"); details["Home defence not elite (CS<=4/6)"] = f"FAIL (CS {home_cs}/{home_n})"; is_perfect = False
    else:
        details["Home defence not elite (CS<=4/6)"] = "SKIP"

    # 10 — NO DOUBLE-BLANK SHOCK
    def _dbl_blank(f):
        s = (f or [])[:2]
        return len(s)==2 and all(gf==0 for gf,_ in s)
    if not _dbl_blank(team_ov_20) and not _dbl_blank(opp_ov_10):
        passed.append("No double-blank shock"); details["No double-blank shock"] = "PASS"
    else:
        failed.append("No double-blank shock"); details["No double-blank shock"] = "FAIL"; is_perfect = False

    # 11 — ELITE STREAK BONUS
    elite, _, _ = _scored_in_n_of_m(team_ov_20, 9, 10)
    if elite:
        passed.append("Elite overall streak (>=9/10)"); details["Elite overall streak (>=9/10)"] = "BONUS"
    else:
        details["Elite overall streak (>=9/10)"] = "NEUTRAL"

    return passed, failed, details, is_perfect


def _current_scored_run(form):
    if not form: return 0, 0
    run = 0
    for gf, _ in form:
        if gf >= 1: run += 1
        else: break
    return run, len(form)

def _clean_sheets_in_last_n(form, n=6):
    sample = (form or [])[:n]
    if not sample: return None, None
    return sum(1 for _, ga in sample if ga == 0), len(sample)

def _consecutive_conceded_run(form):
    run = 0
    for _, ga in (form or []):
        if ga >= 1: run += 1
        else: break
    return run


# =============================================================================
# POISSON MODEL (unchanged from v2.1)
# =============================================================================
_LEAGUE_BASELINE_CACHE = {}

def _exponential_form_averages(form_tuples, halflife=3.0):
    return _shared_exponential_form_averages(form_tuples, halflife)

def _load_league_baselines():
    if _LEAGUE_BASELINE_CACHE: return _LEAGUE_BASELINE_CACHE
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
    for market in ("home_win", "over_under", "over15", "team_goals", "over05_tg"):
        for row in data.get(market, []) or []:
            final_score = row.get("final_score")
            if not final_score or "-" not in str(final_score): continue
            try:
                hg, ag = str(final_score).split("-", 1)
                hg = int(hg.strip()); ag = int(ag.strip())
            except ValueError:
                continue
            lg = row.get("league", "")
            if not lg: continue
            s = league_stats[lg]
            s["h_gf"] += hg; s["h_ga"] += ag; s["a_gf"] += ag; s["a_ga"] += hg; s["n"] += 1
    global_n = max(1, sum(s["n"] for s in league_stats.values()))
    fallback = (
        sum(s["h_gf"] for s in league_stats.values()) / global_n,
        sum(s["a_gf"] for s in league_stats.values()) / global_n,
        sum(s["h_ga"] for s in league_stats.values()) / global_n,
        sum(s["a_ga"] for s in league_stats.values()) / global_n,
    )
    if not all(fallback) or fallback[0] < 0.6 or fallback[0] > 2.5:
        fallback = default
    _LEAGUE_BASELINE_CACHE["_default"] = fallback
    for lg, s in league_stats.items():
        n = s["n"]
        if n < 5:
            _LEAGUE_BASELINE_CACHE[lg] = fallback
            continue
        ha = s["h_gf"] / n; aa = s["a_gf"] / n; hd = s["h_ga"] / n; ad = s["a_ga"] / n
        if ha < 0.5 or aa < 0.4 or hd < 0.4 or ad < 0.4:
            _LEAGUE_BASELINE_CACHE[lg] = fallback
            continue
        _LEAGUE_BASELINE_CACHE[lg] = (ha, aa, hd, ad)
    return _LEAGUE_BASELINE_CACHE

def _league_baselines(league_name):
    cache = _load_league_baselines()
    return cache.get(league_name, cache.get("_default", (1.45, 1.20, 1.35, 1.25)))

def get_team_xg(team_6, opp_6, league_name=None, is_home=True):
    bl = _league_baselines(league_name or "")
    home_baseline_attack, away_baseline_attack, home_baseline_defense, away_baseline_defense = bl
    t_gf_avg, _, _ = _exponential_form_averages(team_6 or [])
    if not (team_6 or []):
        t_gf_avg = home_baseline_attack if is_home else away_baseline_attack
    _, o_ga_avg, _ = _exponential_form_averages(opp_6 or [])
    if not (opp_6 or []):
        o_ga_avg = away_baseline_defense if is_home else home_baseline_defense
    adaptive_shrinkage = SHRINKAGE_WEIGHT
    if len(team_6 or []) < MIN_DATA_GAMES or len(opp_6 or []) < MIN_DATA_GAMES:
        adaptive_shrinkage = max(0.40, SHRINKAGE_WEIGHT - 0.15)
    team_attack = adaptive_shrinkage * t_gf_avg + (1 - adaptive_shrinkage) * (home_baseline_attack if is_home else away_baseline_attack)
    opp_defence = adaptive_shrinkage * o_ga_avg + (1 - adaptive_shrinkage) * (away_baseline_defense if is_home else home_baseline_defense)
    baseline_att = home_baseline_attack if is_home else away_baseline_attack
    team_xg = team_attack * (opp_defence / max(0.5, baseline_att))
    return round(max(0.2, min(3.5, team_xg)), 2)

def calculate_poisson_team_over05(team_xg, max_goals=6):
    prob_0 = _shared_poisson_pmf(0, team_xg)
    return round((1.0 - prob_0) * 100, 1)

def data_volume_penalty(streak_20, opp_6, streak_ov_20, opp_ov_10):
    n = min(len(streak_20 or []), len(opp_6 or []), len(streak_ov_20 or []), len(opp_ov_10 or []))
    if n >= MIN_DATA_GAMES: return 1.0
    if n >= 4: return 0.97
    if n >= 3: return 0.92
    if n >= 2: return 0.85
    return 0.75

def compute_confidence_score(rule_score, max_score, model_prob_pct, decimal_odds, data_mult=1.0):
    rule_component = max(0.0, min(1.0, rule_score / max(max_score, 1)))
    model_component = max(0.0, min(1.0, model_prob_pct / 100.0))
    implied = 1.0 / max(1.02, decimal_odds)
    edge_component = max(0.0, min(1.0, (model_prob_pct / 100.0 - implied) + 0.5))
    raw = _WEIGHT_RULES * rule_component + _WEIGHT_MODEL * model_component + _WEIGHT_EDGE * edge_component
    return max(0.0, min(1.0, raw * data_mult))

def tier_from_confidence(score, is_perfect, streak_label, combined_gpg, def_elite):
    is_elite_streak = "overall_9" in streak_label or "overall_8" in streak_label
    premium_ok = is_perfect and is_elite_streak and combined_gpg >= 2.0 and def_elite
    if score >= _TIER_PREMIUM_CUTOFF and premium_ok:
        return "perfect"
    if score >= _TIER_SOLID_CUTOFF:
        return "qualified"
    return "close"

def _early_season_penalty(match_date_str):
    if not match_date_str: return 1.0
    try:
        dt = datetime.strptime(match_date_str, "%Y-%m-%d")
        if dt.month == 8 and dt.day <= 20: return 0.88
    except (ValueError, TypeError): pass
    return 1.0


# =============================================================================
# HARD VETOES
# =============================================================================

def _h2h_shutout_bogey_veto(team_id, opponent_id, target_date, is_home_perspective):
    meetings = get_h2h_meetings(team_id, opponent_id, target_date, limit=6)
    if len(meetings) < 2: return False, meetings, None
    shutouts = 0
    for m in meetings:
        team_gf = m.get("gf", 0) if is_home_perspective else m.get("ga", 0)
        if team_gf == 0: shutouts += 1
    if len(meetings) >= 3 and shutouts >= 2:
        return True, meetings, f"h2h_shutout_{shutouts}_of_{len(meetings)}"
    if len(meetings) == 2 and shutouts == 2:
        return True, meetings, "h2h_both_shutouts"
    return False, meetings, None

def _combined_mutual_cold_start_veto(streak_ov_6, opp_ov_6):
    so, oo = (streak_ov_6 or [])[:2], (opp_ov_6 or [])[:2]
    if len(so) < 2 or len(oo) < 2: return False, None
    if all(gf == 0 for gf, _ in so) and all(gf == 0 for gf, _ in oo):
        return True, "both_teams_double_blank_last_2"
    return False, None

def _scoring_drought_veto(team_3):
    if len(team_3 or []) >= 2 and all(gf == 0 for gf, _ in team_3[:2]):
        return True, "scoring_drought_2"
    return False, None

def _opponent_defensive_wall_veto(opp_3):
    if len(opp_3 or []) >= 2 and all(ga == 0 for _, ga in opp_3[:2]):
        return True, "opponent_defensive_wall_2"
    return False, None

def _derby_veto(match):
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


# =============================================================================
# MATCH PROCESSING
# =============================================================================

def process_single_match(match, target_date, default_odds_home=DEFAULT_ODDS_HOME, default_odds_away=DEFAULT_ODDS_AWAY):
    try:
        league_name = match.get("league", "")
        home_form_20 = get_team_form(match["home_team_id"], True, 20, target_date)
        away_form_20 = get_team_form(match["away_team_id"], False, 20, target_date)
        home_overall_20 = get_team_overall_form(match["home_team_id"], 20, target_date)
        away_overall_20 = get_team_overall_form(match["away_team_id"], 20, target_date)

        if len(home_form_20 or []) < 2 or len(away_form_20 or []) < 2:
            return {"status": "insufficient"}

        # --- HOME TEAM ---
        home_passed, home_failed, home_details, home_is_perfect = apply_algorithm(
            home_form_20, away_form_20[:6], home_overall_20, away_overall_20[:10],
            home_form_20[:6], away_form_20[:6], is_home=True,
        )
        home_score = len(home_passed) if home_passed else 0
        home_streak_pass, home_streak_label, _, _ = check_streak_gate(home_overall_20, home_form_20[:6])
        home_def_pass, home_def_label, home_def_elite = check_defence_gate(away_form_20[:6], away_overall_20[:10])

        home_xg = get_team_xg(home_form_20[:6], away_form_20[:6], league_name=league_name, is_home=True)
        home_prob_pct = calculate_poisson_team_over05(home_xg)

        home_h2h_veto, _, home_h2h_reason = _h2h_shutout_bogey_veto(match["home_team_id"], match["away_team_id"], target_date, True)
        home_cold_veto, home_cold_reason = _combined_mutual_cold_start_veto(home_overall_20[:6], away_overall_20[:6])
        home_drought_veto, home_drought_reason = _scoring_drought_veto(home_form_20[:3])
        home_opp_wall_veto, home_opp_wall_reason = _opponent_defensive_wall_veto(away_form_20[:3])
        derby_veto, derby_reason = _derby_veto(match)

        home_data_mult = data_volume_penalty(home_form_20, away_form_20[:6], home_overall_20, away_overall_20[:10])
        home_league_mult = _WEAK_ROI_MULTIPLIER if _is_weak_roi_league(league_name) else 1.0
        home_early_mult = _early_season_penalty(match.get("date"))
        home_final_mult = home_data_mult * home_league_mult * home_early_mult

        sf, of = (home_form_20[:6] or []), (away_form_20[:6] or [])
        home_combined_gpg = (sum(gf+ga for gf,ga in sf) + sum(gf+ga for gf,ga in of)) / max(1, len(sf)+len(of))

        home_conf_score = compute_confidence_score(home_score, MAX_SCORE, home_prob_pct, default_odds_home, home_final_mult)
        home_min_score = MAX_SCORE - 5 if _is_weak_roi_league(league_name) else MAX_SCORE - 6
        home_qualifies = (
            home_passed is not None and home_score >= home_min_score
            and home_streak_pass and home_def_pass  # BOTH gates required
            and not home_h2h_veto and not home_cold_veto
            and not home_drought_veto and not home_opp_wall_veto and not derby_veto
        )
        home_tier = tier_from_confidence(home_conf_score, home_is_perfect, home_streak_label, home_combined_gpg, home_def_elite) if home_qualifies else None
        home_kelly = calculate_kelly(home_prob_pct / 100, default_odds_home, use_half=True) if home_qualifies else 0.0

        # --- AWAY TEAM ---
        away_passed, away_failed, away_details, away_is_perfect = apply_algorithm(
            away_form_20, home_form_20[:6], away_overall_20, home_overall_20[:10],
            away_form_20[:6], home_form_20[:6], is_home=False,
        )
        away_score = len(away_passed) if away_passed else 0
        away_streak_pass, away_streak_label, _, _ = check_streak_gate(away_overall_20, away_form_20[:6])
        away_def_pass, away_def_label, away_def_elite = check_defence_gate(home_form_20[:6], home_overall_20[:10])

        away_xg = get_team_xg(away_form_20[:6], home_form_20[:6], league_name=league_name, is_home=False)
        away_prob_pct = calculate_poisson_team_over05(away_xg)

        away_h2h_veto, _, away_h2h_reason = _h2h_shutout_bogey_veto(match["away_team_id"], match["home_team_id"], target_date, False)
        away_cold_veto, away_cold_reason = _combined_mutual_cold_start_veto(away_overall_20[:6], home_overall_20[:6])
        away_drought_veto, away_drought_reason = _scoring_drought_veto(away_form_20[:3])
        away_opp_wall_veto, away_opp_wall_reason = _opponent_defensive_wall_veto(home_form_20[:3])

        away_data_mult = data_volume_penalty(away_form_20, home_form_20[:6], away_overall_20, home_overall_20[:10])
        away_final_mult = away_data_mult * home_league_mult * home_early_mult

        away_combined_gpg = (sum(gf+ga for gf,ga in (away_form_20[:6] or [])) + sum(gf+ga for gf,ga in (home_form_20[:6] or []))) / max(1, len(away_form_20[:6] or [])+len(home_form_20[:6] or []))

        away_conf_score = compute_confidence_score(away_score, MAX_SCORE, away_prob_pct, default_odds_away, away_final_mult)
        away_min_score = home_min_score
        away_qualifies = (
            away_passed is not None and away_score >= away_min_score
            and away_streak_pass and away_def_pass  # BOTH gates required
            and not away_h2h_veto and not away_cold_veto
            and not away_drought_veto and not away_opp_wall_veto and not derby_veto
        )
        away_tier = tier_from_confidence(away_conf_score, away_is_perfect, away_streak_label, away_combined_gpg, away_def_elite) if away_qualifies else None
        away_kelly = calculate_kelly(away_prob_pct / 100, default_odds_away, use_half=True) if away_qualifies else 0.0

        # Regressions
        home_regressions = []
        if not home_streak_pass: home_regressions.append(f"streak gate ({home_streak_label})")
        if not home_def_pass: home_regressions.append(f"defence gate ({home_def_label})")
        if home_h2h_veto: home_regressions.append(f"h2h shutout ({home_h2h_reason})")
        if home_cold_veto: home_regressions.append(f"cold start ({home_cold_reason})")
        if home_drought_veto: home_regressions.append(f"drought ({home_drought_reason})")
        if home_opp_wall_veto: home_regressions.append(f"opp wall ({home_opp_wall_reason})")
        if derby_veto: home_regressions.append(f"derby ({derby_reason})")
        if home_early_mult < 1.0: home_regressions.append(f"early-season (x{home_early_mult})")

        away_regressions = []
        if not away_streak_pass: away_regressions.append(f"streak gate ({away_streak_label})")
        if not away_def_pass: away_regressions.append(f"defence gate ({away_def_label})")
        if away_h2h_veto: away_regressions.append(f"h2h shutout ({away_h2h_reason})")
        if away_cold_veto: away_regressions.append(f"cold start ({away_cold_reason})")
        if away_drought_veto: away_regressions.append(f"drought ({away_drought_reason})")
        if away_opp_wall_veto: away_regressions.append(f"opp wall ({away_opp_wall_reason})")
        if derby_veto: away_regressions.append(f"derby ({derby_reason})")
        if home_early_mult < 1.0: away_regressions.append(f"early-season (x{home_early_mult})")

        return {
            "status": "success",
            "data": {
                "match": match,
                "home_team_goals": {
                    "score": home_score, "passed": home_passed, "failed": home_failed,
                    "details": home_details, "is_perfect": home_is_perfect,
                    "tier": home_tier, "confidence_score": round(home_conf_score * 100, 1),
                    "prob": home_prob_pct, "confidence": "HIGH" if home_prob_pct >= 78 else ("MEDIUM" if home_prob_pct >= 68 else "LOW"),
                    "kelly": round(home_kelly * 100, 2), "xg": home_xg,
                    "streak_label": home_streak_label, "streak_passed": home_streak_pass,
                    "defence_label": home_def_label, "defence_passed": home_def_pass, "defence_elite": home_def_elite,
                    "combined_gpg": round(home_combined_gpg, 2),
                    "h2h_veto": home_h2h_veto, "h2h_reason": home_h2h_reason,
                    "cold_veto": home_cold_veto, "cold_reason": home_cold_reason,
                    "drought_veto": home_drought_veto, "drought_reason": home_drought_reason,
                    "opp_wall_veto": home_opp_wall_veto, "opp_wall_reason": home_opp_wall_reason,
                    "derby_veto": derby_veto, "derby_reason": derby_reason,
                    "early_season_mult": round(home_early_mult, 2),
                    "data_mult": round(home_data_mult, 2), "weak_league_mult": round(home_league_mult, 2),
                    "min_score_threshold": home_min_score, "regressions": home_regressions,
                },
                "away_team_goals": {
                    "score": away_score, "passed": away_passed, "failed": away_failed,
                    "details": away_details, "is_perfect": away_is_perfect,
                    "tier": away_tier, "confidence_score": round(away_conf_score * 100, 1),
                    "prob": away_prob_pct, "confidence": "HIGH" if away_prob_pct >= 75 else ("MEDIUM" if away_prob_pct >= 65 else "LOW"),
                    "kelly": round(away_kelly * 100, 2), "xg": away_xg,
                    "streak_label": away_streak_label, "streak_passed": away_streak_pass,
                    "defence_label": away_def_label, "defence_passed": away_def_pass, "defence_elite": away_def_elite,
                    "combined_gpg": round(away_combined_gpg, 2),
                    "h2h_veto": away_h2h_veto, "h2h_reason": away_h2h_reason,
                    "cold_veto": away_cold_veto, "cold_reason": away_cold_reason,
                    "drought_veto": away_drought_veto, "drought_reason": away_drought_reason,
                    "opp_wall_veto": away_opp_wall_veto, "opp_wall_reason": away_opp_wall_reason,
                    "derby_veto": derby_veto, "derby_reason": derby_reason,
                    "early_season_mult": round(home_early_mult, 2),
                    "data_mult": round(away_data_mult, 2), "weak_league_mult": round(home_league_mult, 2),
                    "min_score_threshold": away_min_score, "regressions": away_regressions,
                },
            }
        }
    except Exception as e:
        logger.error(f"Processing failed for {match.get('home', 'N/A')} vs {match.get('away', 'N/A')}: {e}", exc_info=True)
        return {"status": "error"}


# =============================================================================
# REPORTING (same as v2.1)
# =============================================================================

def _append_pick(lines, idx, item, side, odds, detailed, compact=False):
    m = item["match"]
    tgt = item[f"{side}_team_goals"]
    label = MARKET_LABEL_HOME if side == "home" else MARKET_LABEL_AWAY
    short = SHORT_MARKET_HOME if side == "home" else SHORT_MARKET_AWAY
    if compact:
        lines.append(format_compact_pick_line(m["home"], m["away"], short, tgt.get("tier"), tgt["prob"], m.get("date")))
        return
    extra = None
    if detailed:
        h2h_veto = tgt.get("h2h_veto")
        h2h_note = "H2H shutout flag" if h2h_veto else "no H2H flags"
        streak_note = tgt.get("streak_label", "unknown")
        def_note = tgt.get("defence_label", "unknown")
        elite_mark = "🔥" if tgt.get("defence_elite") else ""
        extra = format_vip_extra_lines(
            tgt["kelly"], odds, tgt["score"], MAX_SCORE,
            home_lambda=tgt["xg"], away_lambda=None, model_prob=tgt["prob"],
            market="team_goals",
            h2h_note=f"streak={streak_note} · defence={def_note}{elite_mark} · {h2h_note}",
            rule_details=tgt.get("details"),
        )
    categories = describe_pick_categories(m["home"], m["away"], m.get("league", ""), market="team_goals", tier=tgt.get("tier"), weak_roi_league=bool(tgt.get("weak_league_mult", 1.0) < 1.0))
    lines.extend(format_pick_block(idx, m["home"], m["away"], m["date"],
        f"{label} · {format_confidence_label(tgt['confidence'])} ({tgt['prob']}%)", extra, league=m.get("league"), categories=categories))


def build_report(home_perfect, home_qualified, home_close, home_weak,
                 away_perfect, away_qualified, away_close, away_weak,
                 scanned_dates, bankroll, odds_home, odds_away,
                 detailed=False, compact=False, include_yesterday=True, include_header=True, include_footer=True, report_date=None):
    included_home = [item for item in (home_perfect + home_qualified + home_close) if not is_static_blocked_fixture(item.get("match", {}))]
    included_away = [item for item in (away_perfect + away_qualified + away_close) if not is_static_blocked_fixture(item.get("match", {}))]
    if report_date:
        included_home = filter_pick_items_by_date(included_home, report_date)
        included_away = filter_pick_items_by_date(included_away, report_date)
    base_date = scanned_dates[0] if scanned_dates else datetime.now().strftime("%Y-%m-%d")
    lines = []
    if report_date and not include_header and not compact:
        lines.append(f"📅 Picks for {report_date}"); lines.append("")
    if not compact:
        if detailed and include_header:
            lines.extend(format_vip_banner("Team Goals Over 0.5", base_date, scanned_dates))
        if include_header:
            lines.append("🎯 Team Goals Over 0.5"); lines.append("")
            lines.append(f"Date: {base_date}" if len(scanned_dates) == 1 else f"Dates: {scanned_dates[0]} to {scanned_dates[-1]}")
            lines.append("")
            if include_yesterday:
                append_yesterday_section(lines, "over05_tg", detailed=detailed)
    if compact:
        def _group(items, side_key, side_label):
            has_group = False
            groups = [
                (COMPACT_TIER_HEADER_PREMIUM, [p for p in items if p in (home_perfect if side_key=="home" else away_perfect)]),
                (COMPACT_TIER_HEADER_STRONG, [p for p in items if p in (home_qualified if side_key=="home" else away_qualified)]),
                (COMPACT_TIER_HEADER_WATCH, [p for p in items if p in (home_close if side_key=="home" else away_close)]),
            ]
            for tier_header, tier_items in groups:
                if not tier_items: continue
                if not has_group:
                    lines.append(f"▸ {side_label.upper()}"); has_group = True
                lines.append(f"  {tier_header}")
                for it in tier_items:
                    lines.append(f"  {format_compact_pick_line(it['match']['home'], it['match']['away'], SHORT_MARKET_HOME if side_key == 'home' else SHORT_MARKET_AWAY, it[f'{side_key}_team_goals'].get('tier'), it[f'{side_key}_team_goals']['prob'], it['match'].get('date'))}")
            return has_group
        has_home = _group(included_home, "home", MARKET_LABEL_HOME)
        if has_home and included_away: lines.append("")
        _group(included_away, "away", MARKET_LABEL_AWAY)
    else:
        if included_home:
            lines.append(""); lines.append("🏠 Home Team to Score"); lines.append("")
            hp = [p for p in included_home if p in home_perfect]
            hq = [p for p in included_home if p in home_qualified]
            hc = [p for p in included_home if p in home_close]
            if hp: lines.append(f"  {PICK_TIER_PREMIUM}"); lines.append("")
            for i, it in enumerate(hp, 1): _append_pick(lines, i, it, "home", odds_home, detailed, compact)
            if hq: lines.append(f"  {PICK_TIER_STRONG}"); lines.append("")
            for i, it in enumerate(hq, len(hp)+1): _append_pick(lines, i, it, "home", odds_home, detailed, compact)
            if hc and detailed: lines.append(f"  {PICK_TIER_VALUE}"); lines.append("")
            for i, it in enumerate(hc, len(hp)+len(hq)+1): _append_pick(lines, i, it, "home", odds_home, detailed, compact)
        if included_away:
            lines.append(""); lines.append("✈️ Away Team to Score"); lines.append("")
            ap = [p for p in included_away if p in away_perfect]
            aq = [p for p in included_away if p in away_qualified]
            ac = [p for p in included_away if p in away_close]
            if ap: lines.append(f"  {PICK_TIER_PREMIUM}"); lines.append("")
            for i, it in enumerate(ap, 1): _append_pick(lines, i, it, "away", odds_away, detailed, compact)
            if aq: lines.append(f"  {PICK_TIER_STRONG}"); lines.append("")
            for i, it in enumerate(aq, len(ap)+1): _append_pick(lines, i, it, "away", odds_away, detailed, compact)
            if ac and detailed: lines.append(f"  {PICK_TIER_VALUE}"); lines.append("")
            for i, it in enumerate(ac, len(ap)+len(aq)+1): _append_pick(lines, i, it, "away", odds_away, detailed, compact)
    if not compact and include_footer:
        if detailed:
            lines.extend(format_vip_summary("HOME TO SCORE · Pick summary", home_perfect, home_qualified, home_close))
            lines.extend(format_vip_summary("AWAY TO SCORE · Pick summary", away_perfect, away_qualified, away_close))
        lines.append("---"); lines.append("For informational purposes only"); lines.append("Gamble responsibly"); lines.append("")
    report = "\n".join(lines).strip()
    if not report: report = "— none"
    return report, base_date, included_home, included_away


# =============================================================================
# MAIN
# =============================================================================

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="Team Goals Over 0.5 Predictor v2.2 (Defence-Gate)")
    parser.add_argument("date", nargs="?", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--bankroll", type=float, default=1000.0)
    parser.add_argument("--odds-home", type=float, default=DEFAULT_ODDS_HOME)
    parser.add_argument("--odds-away", type=float, default=DEFAULT_ODDS_AWAY)
    parser.add_argument("--clear-cache", action="store_true")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--publish-date", default=None)
    args = parser.parse_args()

    if args.clear_cache: cache.clear()
    start_date = datetime.strptime(args.date, "%Y-%m-%d")
    scan_days = args.days if args.days is not None else (6 if start_date.weekday() >= 4 else 4)

    home_perfect, home_qualified, home_close, home_weak = [], [], [], []
    away_perfect, away_qualified, away_close, away_weak = [], [], [], []
    scanned_dates = []

    print(f"Starting Team Goals O0.5 v2.2 (defence-gate) from {args.date}...")

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
                if not args.scheduled or f.get("status", "").lower() == "scheduled":
                    seen.add(key); unique_fixtures.append(f)
        if not unique_fixtures:
            logger.info(f"No fixtures on {date_str}"); continue
        print(f"   Processing {len(unique_fixtures)} matches on {date_str}...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(process_single_match, match, date_str, args.odds_home, args.odds_away): match for match in unique_fixtures}
            for future in as_completed(futures):
                try:
                    res = future.result(timeout=60)
                except Exception as e:
                    logger.error(f"Future error: {e}"); continue
                if res["status"] == "insufficient": continue
                if res["status"] == "success":
                    data = res["data"]
                    ht = data["home_team_goals"]["tier"]
                    if ht == "perfect": home_perfect.append(data)
                    elif ht == "qualified": home_qualified.append(data)
                    elif ht == "close": home_close.append(data)
                    elif data["home_team_goals"]["score"] >= max(1, MAX_SCORE - 3): home_weak.append(data)
                    at = data["away_team_goals"]["tier"]
                    if at == "perfect": away_perfect.append(data)
                    elif at == "qualified": away_qualified.append(data)
                    elif at == "close": away_close.append(data)
                    elif data["away_team_goals"]["score"] >= max(1, MAX_SCORE - 3): away_weak.append(data)

    apply_portfolio_kelly(home_perfect + home_qualified + home_close, "home_team_goals", args.bankroll, MAX_TOTAL_EXPOSURE / 2)
    apply_portfolio_kelly(away_perfect + away_qualified + away_close, "away_team_goals", args.bankroll, MAX_TOTAL_EXPOSURE / 2)

    free_report, base_date, included_home, included_away = build_report(
        home_perfect, home_qualified, home_close, home_weak,
        away_perfect, away_qualified, away_close, away_weak,
        scanned_dates, args.bankroll, args.odds_home, args.odds_away, detailed=False)
    publish_date = args.publish_date or datetime.now().strftime("%Y-%m-%d")
    telegram_report, _, _, _ = build_report(
        home_perfect, home_qualified, home_close, home_weak,
        away_perfect, away_qualified, away_close, away_weak,
        scanned_dates, args.bankroll, args.odds_home, args.odds_away,
        detailed=False, compact=False, include_yesterday=False, include_header=False, include_footer=False, report_date=publish_date)
    detailed_report, _, _, _ = build_report(
        home_perfect, home_qualified, home_close, home_weak,
        away_perfect, away_qualified, away_close, away_weak,
        scanned_dates, args.bankroll, args.odds_home, args.odds_away, detailed=True)

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
            "metadata": {"scanned_window": scanned_dates, "bankroll": args.bankroll, "odds_home": args.odds_home, "odds_away": args.odds_away, "generated_at": datetime.now().isoformat()},
            "home_team_goals": {"perfect": home_perfect, "qualified": home_qualified, "close": home_close, "weak": home_weak},
            "away_team_goals": {"perfect": away_perfect, "qualified": away_qualified, "close": away_close, "weak": away_weak},
        }, f, indent=2, ensure_ascii=False, default=str)

    try:
        picks = []
        for pick in home_perfect + home_qualified + home_close:
            tier = pick["home_team_goals"].get("tier") or ("perfect" if pick in home_perfect else "qualified" if pick in home_qualified else "close")
            picks.append({"league": pick["match"]["league"], "home": pick["match"]["home"], "away": pick["match"]["away"], "date": pick["match"]["date"], "prediction": "home_team_goals", "confidence": tier})
        for pick in away_perfect + away_qualified + away_close:
            tier = pick["away_team_goals"].get("tier") or ("perfect" if pick in away_perfect else "qualified" if pick in away_qualified else "close")
            picks.append({"league": pick["match"]["league"], "home": pick["match"]["home"], "away": pick["match"]["away"], "date": pick["match"]["date"], "prediction": "away_team_goals", "confidence": tier})
        # ``record_predictions`` stores this market under the oo05 section.
        # Keep the side in ``prediction``: settlement needs to know whether
        # the home or away team, rather than either team, was backed to score.
        stats = record_predictions(base_date, oo05_picks=picks)
        if stats.get("added"): print(f"Predictions recorded ({stats['added']} new)")
        elif stats.get("skipped"): print(f"Predictions already recorded ({stats['skipped']} skipped)")
    except Exception as e:
        print(f"Could not record predictions: {e}")

    total_picks = len(included_home) + len(included_away)
    print(f"\n[OK] {total_picks} Team Goals O0.5 picks for {base_date}")
    print(f"   Home to score: {len(included_home)}")
    print(f"   Away to score: {len(included_away)}")
    print(f"Report saved: {output_path}")
    print(f"VIP report saved: {detailed_report_path}")


if __name__ == "__main__":
    main()
