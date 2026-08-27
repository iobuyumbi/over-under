#!/usr/bin/env python3
"""
HOME WIN PREDICTOR - PRODUCTION HARDENED v5
=============================================
11-check rule system | Shrinkage strength | Logistic prob | Portfolio Kelly | SQLite Cache
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
# (over25_soccerbase.py, btts_soccerbase.py) drift apart before.
from utils import (
    Cache,
    build_session,
    fetch as _shared_fetch,
    parse_date,
    calculate_kelly as _shared_calculate_kelly,
    apply_portfolio_kelly as _shared_apply_portfolio_kelly,
)

# Shared Soccerbase fixture/results scraping (see scraping.py). NOTE: this
# file's own get_team_form()/get_team_overall_form() below are NOT swapped
# for scraping.py's versions — they return full match dicts (this file's
# rule algorithm reads match["result"]) rather than the (gf, ga) tuples
# over25/btts use, so the shapes don't match. Only the actual HTML
# scraping (which returns dicts either way) is shared here.
from scraping import (
    fetch_soccerbase_fixtures as _shared_fetch_fixtures,
    fetch_soccerbase_team_results as _shared_fetch_team_results,
    _thin_count,
    _thin_total,
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
    MARKET_HOME_WIN,
)

# =============================================================================
# CONFIGURATION
# =============================================================================
CACHE_DB = "soccerbase_cache_home.db"
CACHE_TTL_HOURS = 24
MAX_WORKERS = 4
REQUEST_DELAY_MIN = 2.5
REQUEST_DELAY_MAX = 5.0
MAX_TOTAL_EXPOSURE = 0.25
SHRINKAGE_WEIGHT = 0.65

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




# =============================================================================
# FORM & DATA HELPERS
# =============================================================================
# parse_date() is imported from utils.py — do not redefine it locally.

MAX_HOME_WIN_SCORE = 11

HW_MIN_DATA_GAMES = 3
HW_WEIGHT_RULES = 0.40
HW_WEIGHT_MODEL = 0.40
HW_WEIGHT_EDGE = 0.20
HW_TIER_PREMIUM_CUTOFF = 0.62
HW_TIER_SOLID_CUTOFF = 0.53
HW_MIN_MODEL_PROB = 0.55
HW_MIN_STRENGTH_GAP = 0.12

_HW_HALFLIFE = 3.0

_HW_WEAK_ROI_LEAGUE_KEYWORDS = (
    "swedish allsvenskan",
    "allsvenskan",
    "belarus",
    "k-league 1",
    "k league 1",
    "korean k-league 1",
    "league of ireland",
    "fai cup",
    "mexican primera apertura",
    "brazilian serie a",
    "mls",
)
_HW_WEAK_ROI_MULTIPLIER = 0.82

_HW_REGRESSION_WIN_STREAK = 5
_HW_REGRESSION_PENALTY = 0.08
_HW_H2H_MIN_MEETINGS = 2
_HW_H2H_MAX_LOOKBACK = 6
_HW_H2H_AWAY_WIN_RATIO = 2.0
_HW_H2H_MIN_AWAY_WINS_FOR_ADVANTAGE = 2

_HW_LEAGUE_BASELINE_CACHE = {}


def get_h2h_meetings(home_team_id, away_team_id, target_date_str=None, limit=_HW_H2H_MAX_LOOKBACK):
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
                flipped_result = {"W": "L", "L": "W", "D": "D"}.get(match.get("result"), match.get("result"))
                perspective = {
                    **match,
                    "gf": match.get("ga"),
                    "ga": match.get("gf"),
                    "result": flipped_result,
                    "is_home": not match.get("is_home"),
                }
            collected[key] = perspective
    meetings = sorted(collected.values(), key=lambda m: m.get("date_str") or "", reverse=True)
    return meetings[:limit]


def _h2h_home_win_blocked(home_team_id, away_team_id, target_date_str=None):
    """Block home-win when H2H is unfavourable.

    Triggers when:
      (a) home side is winless in recent H2H with >=1 away win (bogey opponent), OR
      (b) away team has a clear H2H advantage: >=2 away wins AND away wins >= 2x home wins.
    """
    meetings = get_h2h_meetings(home_team_id, away_team_id, target_date_str)
    if not meetings:
        return False, meetings, "none"
    home_wins = sum(1 for m in meetings if m.get("result") == "W")
    away_wins = sum(1 for m in meetings if m.get("result") == "L")
    if len(meetings) >= _HW_H2H_MIN_MEETINGS and home_wins == 0 and away_wins >= 1:
        return True, meetings, "home_winless"
    if len(meetings) == 1 and meetings[0].get("is_home") and meetings[0].get("result") == "L":
        return True, meetings, "single_home_loss"
    if (len(meetings) >= _HW_H2H_MIN_MEETINGS
            and away_wins >= _HW_H2H_MIN_AWAY_WINS_FOR_ADVANTAGE
            and away_wins >= _HW_H2H_AWAY_WIN_RATIO * max(1, home_wins)):
        return True, meetings, "away_h2h_advantage"
    return False, meetings, "ok"


def _hw_is_weak_roi(league_name):
    n = str(league_name or "").strip().lower()
    return any(k in n for k in _HW_WEAK_ROI_LEAGUE_KEYWORDS)


def _hw_weighted_win_rate(form, halflife=_HW_HALFLIFE):
    """Exponential-decay weighted win rate.  form[0] is most recent."""
    if not form:
        return 0.5, 0.0
    w = 0.0
    w_wins = 0.0
    for idx, m in enumerate(form):
        wt = 0.5 ** (idx / halflife)
        w += wt
        if m.get("result") == "W":
            w_wins += wt
    if w <= 0:
        return 0.5, 0.0
    return w_wins / w, w


def _hw_win_streak(form):
    n = 0
    for m in form or []:
        if m.get("result") == "W":
            n += 1
        else:
            break
    return n


def _hw_regression_penalty(home_form, away_form):
    h_streak = _hw_win_streak(home_form or [])
    if h_streak >= _HW_REGRESSION_WIN_STREAK:
        return 1.0 - _HW_REGRESSION_PENALTY
    return 1.0


# _thin_count/_thin_total are imported from scraping.py — do not redefine locally.


def _hw_load_league_baselines():
    """Compute per-league home win rate baselines from prediction_history settled picks.

    Returns dict: league_name -> baseline_home_win_rate (0..1).
    Falls back to 0.50 for leagues with < 5 settled matches or if the history file
    is missing/invalid.
    """
    if _HW_LEAGUE_BASELINE_CACHE:
        return _HW_LEAGUE_BASELINE_CACHE
    default = 0.50
    history_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prediction_history.json")
    if not os.path.exists(history_path):
        _HW_LEAGUE_BASELINE_CACHE["_default"] = default
        return _HW_LEAGUE_BASELINE_CACHE
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        _HW_LEAGUE_BASELINE_CACHE["_default"] = default
        return _HW_LEAGUE_BASELINE_CACHE
    league_stats = defaultdict(lambda: {"hw": 0, "n": 0})
    for row in data.get("home_win", []) or []:
        result = row.get("result")
        lg = row.get("league", "")
        if not lg:
            continue
        league_stats[lg]["n"] += 1
        if result == "win":
            league_stats[lg]["hw"] += 1
    global_hw = sum(s["hw"] for s in league_stats.values())
    global_n = max(1, sum(s["n"] for s in league_stats.values()))
    fallback = max(0.35, min(0.65, global_hw / global_n))
    _HW_LEAGUE_BASELINE_CACHE["_default"] = fallback
    for lg, s in league_stats.items():
        n = s["n"]
        if n < 5:
            _HW_LEAGUE_BASELINE_CACHE[lg] = fallback
            continue
        rate = s["hw"] / n
        _HW_LEAGUE_BASELINE_CACHE[lg] = max(0.35, min(0.65, rate))
    return _HW_LEAGUE_BASELINE_CACHE


def _hw_league_baseline(league_name):
    cache = _hw_load_league_baselines()
    return cache.get(league_name, cache.get("_default", 0.50))


def get_team_form(team_id, is_home=True, num_matches=6, target_date_str=None):
    all_matches = fetch_soccerbase_team_results(team_id)
    form = []
    target_dt = parse_date(target_date_str) if target_date_str else None

    for match in all_matches:
        match_dt = parse_date(match.get("date_str"))
        if target_dt and match_dt and match_dt >= target_dt:
            continue
        if match["is_home"] == is_home:
            form.append(match)
            if len(form) >= num_matches:
                break
    return form


def get_team_overall_form(team_id, num_matches=5, target_date_str=None):
    """Last N matches home or away combined."""
    all_matches = fetch_soccerbase_team_results(team_id)
    form = []
    target_dt = parse_date(target_date_str) if target_date_str else None

    for match in all_matches:
        match_dt = parse_date(match.get("date_str"))
        if target_dt and match_dt and match_dt >= target_dt:
            continue
        form.append(match)
        if len(form) >= num_matches:
            break
    return form


def _form_record_summary(form):
    wins = sum(1 for m in form if m["result"] == "W")
    losses = sum(1 for m in form if m["result"] == "L")
    draws = len(form) - wins - losses
    return wins, losses, draws


# =============================================================================
# HOME WIN ALGORITHM (11 Checks - Official Rules + Overall Form)
# =============================================================================
def apply_home_win_algorithm(home_data_6, away_data_6, home_overall_5=None, away_overall_5=None):
    if len(home_data_6) < HW_MIN_DATA_GAMES or len(away_data_6) < HW_MIN_DATA_GAMES:
        return None, None, {"error": "Insufficient data"}, False

    passed, failed, details = [], [], {}
    is_perfect = True

    hn = min(len(home_data_6), 6)
    an = min(len(away_data_6), 6)
    home_win = home_data_6[:hn]
    away_win = away_data_6[:an]

    # Home Checks
    home_not_lost = sum(1 for m in home_win if m["result"] != "L")
    hnl_need = _thin_count(5, 6, hn)
    if home_not_lost >= hnl_need:
        passed.append("Home form (no losses)"); details["Home form (no losses)"] = f"PASS ({home_not_lost}/{hn} No Losses >= {hnl_need})"
        if home_not_lost < hn:
            is_perfect = False
    else:
        failed.append("Home form (no losses)"); details["Home form (no losses)"] = f"FAIL ({home_not_lost}/{hn} < {hnl_need})"; is_perfect = False

    home_gf = sum(m["gf"] for m in home_win)
    hgf_need = _thin_total(10, 6, hn)
    if home_gf >= hgf_need:
        passed.append("Home goals scored"); details["Home goals scored"] = f"PASS ({home_gf} GF >= {hgf_need})"
    else:
        failed.append("Home goals scored"); details["Home goals scored"] = f"FAIL ({home_gf} < {hgf_need})"; is_perfect = False

    home_ga = sum(m["ga"] for m in home_win)
    hga_cap = _thin_total(5, 6, hn)
    if home_ga <= hga_cap:
        passed.append("Home goals conceded"); details["Home goals conceded"] = f"PASS ({home_ga} GA <= {hga_cap})"
    else:
        failed.append("Home goals conceded"); details["Home goals conceded"] = f"FAIL ({home_ga} > {hga_cap})"; is_perfect = False

    home_wins = sum(1 for m in home_win if m["result"] == "W")
    hw_need = _thin_count(3, 6, hn)
    if home_wins >= hw_need:
        passed.append("Home wins"); details["Home wins"] = f"PASS ({home_wins}/{hn} Wins >= {hw_need})"
    else:
        failed.append("Home wins"); details["Home wins"] = f"FAIL ({home_wins} < {hw_need})"; is_perfect = False

    last_n = min(2, hn)
    last_n_wins = sum(1 for m in home_win[:last_n] if m["result"] == "W")
    lr_need = max(1, round(last_n * 0.8))
    if last_n_wins >= lr_need:
        passed.append("Home recent form"); details["Home recent form"] = f"PASS (Won {last_n_wins}/{last_n} >= {lr_need})"
    else:
        failed.append("Home recent form"); details["Home recent form"] = f"FAIL ({last_n_wins}/{last_n} < {lr_need})"; is_perfect = False

    # Away Checks
    away_losses = sum(1 for m in away_win if m["result"] == "L")
    al_need = _thin_count(2, 6, an)
    if away_losses >= al_need:
        passed.append("Away losses"); details["Away losses"] = f"PASS ({away_losses}/{an} Losses >= {al_need})"
    else:
        failed.append("Away losses"); details["Away losses"] = f"FAIL ({away_losses} < {al_need})"; is_perfect = False

    away_ga = sum(m["ga"] for m in away_win)
    aga_need = _thin_total(10, 6, an)
    if away_ga >= aga_need:
        passed.append("Away goals conceded"); details["Away goals conceded"] = f"PASS ({away_ga} GA >= {aga_need})"
    else:
        failed.append("Away goals conceded"); details["Away goals conceded"] = f"FAIL ({away_ga} < {aga_need})"; is_perfect = False

    away_gf = sum(m["gf"] for m in away_win)
    agf_cap = _thin_total(5, 6, an)
    if away_gf <= agf_cap:
        passed.append("Away goals scored"); details["Away goals scored"] = f"PASS ({away_gf} GF <= {agf_cap})"
    else:
        failed.append("Away goals scored"); details["Away goals scored"] = f"FAIL ({away_gf} > {agf_cap})"; is_perfect = False

    away_wins = sum(1 for m in away_win if m["result"] == "W")
    aw_cap = _thin_count(2, 6, an)
    if away_wins <= aw_cap:
        passed.append("Away wins"); details["Away wins"] = f"PASS ({away_wins}/{an} Wins <= {aw_cap})"
    else:
        failed.append("Away wins"); details["Away wins"] = f"FAIL ({away_wins} > {aw_cap})"; is_perfect = False

    # Overall form (last 5, home or away combined) — thin-data fallback for 2+ matches
    home_overall_5 = home_overall_5 or []
    ho_n = min(len(home_overall_5), 5)
    if ho_n >= 5:
        hw, hl, _ = _form_record_summary(home_overall_5[:5])
        home_overall_ok = hl <= 1 or (hl == 2 and hw == 3)
        if home_overall_ok:
            passed.append("Home overall form (5)")
            details["Home overall form (5)"] = f"PASS ({hw}W-{hl}L in 5)"
            if hl == 2:
                is_perfect = False
        else:
            failed.append("Home overall form (5)")
            details["Home overall form (5)"] = f"FAIL ({hw}W-{hl}L in 5)"
            is_perfect = False
    elif ho_n >= 2:
        hw, hl, _ = _form_record_summary(home_overall_5[:ho_n])
        losses_cap = max(1, round(ho_n * 0.4))
        home_overall_ok = hl <= losses_cap
        if home_overall_ok:
            passed.append("Home overall form (5)")
            details["Home overall form (5)"] = f"PASS-THIN ({hw}W-{hl}L in {ho_n}, HL <= {losses_cap})"
            is_perfect = False
        else:
            failed.append("Home overall form (5)")
            details["Home overall form (5)"] = f"FAIL-THIN ({hw}W-{hl}L in {ho_n}, HL > {losses_cap})"
            is_perfect = False
    else:
        details["Home overall form (5)"] = f"SKIPPED (only {ho_n}/5 matches)"

    away_overall_5 = away_overall_5 or []
    ao_n = min(len(away_overall_5), 5)
    if ao_n >= 5:
        aw, al, _ = _form_record_summary(away_overall_5[:5])
        away_overall_ok = al >= 2 and aw <= 2
        if away_overall_ok:
            passed.append("Away overall form (5)")
            details["Away overall form (5)"] = f"PASS ({aw}W-{al}L in 5)"
        else:
            failed.append("Away overall form (5)")
            details["Away overall form (5)"] = f"FAIL ({aw}W-{al}L in 5)"
            is_perfect = False
    elif ao_n >= 2:
        aw, al, _ = _form_record_summary(away_overall_5[:ao_n])
        losses_need = max(1, round(ao_n * 0.4))
        wins_cap = max(1, round(ao_n * 0.4))
        away_overall_ok = al >= losses_need and aw <= wins_cap
        if away_overall_ok:
            passed.append("Away overall form (5)")
            details["Away overall form (5)"] = f"PASS-THIN ({aw}W-{al}L in {ao_n}, AL >= {losses_need}, AW <= {wins_cap})"
            is_perfect = False
        else:
            failed.append("Away overall form (5)")
            details["Away overall form (5)"] = f"FAIL-THIN ({aw}W-{al}L in {ao_n}, AL >= {losses_need}, AW <= {wins_cap})"
            is_perfect = False
    else:
        details["Away overall form (5)"] = f"SKIPPED (only {ao_n}/5 matches)"

    return passed, failed, details, is_perfect


# =============================================================================
# STRENGTH MODEL (Shrinkage Estimator, per-league baselines)
# =============================================================================
def get_team_strength(form_data, is_home=True, league_name=None):
    """
    Shrinkage estimator for team strength based on win rate.
    Blends exponential-decay weighted win rate with per-league baselines.
    Extra shrinkage on thin form data.
    """
    baseline = _hw_league_baseline(league_name or "") if is_home else (1.0 - _hw_league_baseline(league_name or ""))

    sample = (form_data or [])[:6]
    if not sample:
        return round(baseline, 3)

    n = len(sample)
    win_rate, eff_weight = _hw_weighted_win_rate(sample)

    adaptive_shrinkage = SHRINKAGE_WEIGHT
    if n < HW_MIN_DATA_GAMES:
        adaptive_shrinkage = max(0.45, SHRINKAGE_WEIGHT - 0.15)

    strength = adaptive_shrinkage * win_rate + (1 - adaptive_shrinkage) * baseline
    return round(max(0.1, min(0.95, strength)), 3)


def _away_strength_veto(home_strength, away_strength):
    """Block Home Win if away team is significantly stronger.

    Even with good home form, a historically dominant away side
    can hold the home team to a draw or steal a win.
    Triggers on either a 1.4x strength ratio or a 0.4 absolute gap.
    """
    if away_strength <= 0 or home_strength <= 0:
        return False, None
    if away_strength > home_strength * 1.4:
        return True, f"away_strength_{away_strength:.2f}_vs_home_{home_strength:.2f}"
    if away_strength > home_strength + 0.4:
        return True, f"away_strength_gap_{away_strength - home_strength:.2f}"
    return False, None


def _draw_risk_veto(home_data_6, away_data_6):
    """Block Home Win when the away side scores consistently even if their
    total-goal volume is low, AND the home defence isn't airtight enough
    to compensate.

    Added 2026-08-25 after a loss where the home team won 2-1... no —
    drew 2-2: home scored plenty, but the away side matched them enough
    to force a draw. apply_home_win_algorithm()'s "Away goals scored"
    check caps the SUM of away goals over 6 games, which a team can pass
    while still scoring in most individual matches (e.g. 1-1-0-1-1-0 = 4
    total, well under a cap of 5, but scored in 4/6 games — a live,
    frequent scoring threat, not a blunt one). Consistency of scoring is
    a different signal than total volume and this closes that gap.

    Only fires when the home defence ALSO isn't clearly elite (conceded
    in more than 1 of the last 6), since an away side that scores often
    against a genuinely airtight home defence is still low draw-risk.
    """
    away = (away_data_6 or [])[:6]
    home = (home_data_6 or [])[:6]
    if len(away) < 4 or len(home) < 4:
        return False, None

    away_scored_count = sum(1 for m in away if m.get("gf", 0) > 0)
    away_scored_rate = away_scored_count / len(away)
    if away_scored_rate < (4 / 6):
        return False, None

    home_conceded_count = sum(1 for m in home if m.get("ga", 0) > 0)
    if home_conceded_count <= 1:
        # Home defence is airtight enough to absorb a persistent-but-low-volume
        # away attack; don't veto on scoring consistency alone.
        return False, None

    return True, f"away_scored_{away_scored_count}_of_{len(away)}_home_conceded_{home_conceded_count}_of_{len(home)}"


def hw_data_volume_penalty(home_form, away_form, home_overall, away_overall):
    n = min(
        len(home_form or []),
        len(away_form or []),
        len(home_overall or []),
        len(away_overall or []),
    )
    if n >= HW_MIN_DATA_GAMES:
        return 1.0
    if n >= 2:
        return 0.72
    return 0.50


def hw_model_gate_passes(home_strength, away_strength, model_prob_pct):
    strength_gap_ok = (home_strength - away_strength) >= HW_MIN_STRENGTH_GAP
    prob_ok = (model_prob_pct / 100.0) >= HW_MIN_MODEL_PROB
    return strength_gap_ok and prob_ok


def hw_compute_confidence_score(rule_score, max_score, model_prob_pct, decimal_odds, data_mult=1.0):
    rule_component = max(0.0, min(1.0, rule_score / max(max_score, 1)))
    model_component = max(0.0, min(1.0, model_prob_pct / 100.0))
    implied = 1.0 / max(1.05, decimal_odds)
    edge_component = max(0.0, min(1.0, (model_prob_pct / 100.0 - implied) + 0.5))
    raw = (
        HW_WEIGHT_RULES * rule_component
        + HW_WEIGHT_MODEL * model_component
        + HW_WEIGHT_EDGE * edge_component
    )
    return max(0.0, min(1.0, raw * data_mult))


def hw_tier_from_confidence(score, gate_passes, is_perfect=True):
    if score >= HW_TIER_PREMIUM_CUTOFF and gate_passes and is_perfect:
        return "perfect"
    if score >= HW_TIER_SOLID_CUTOFF:
        return "qualified"
    return "close"


def calculate_home_win_prob(home_strength, away_strength):
    diff = home_strength - away_strength
    prob = 1.0 / (1.0 + math.exp(-4.0 * diff))
    return round(prob * 100, 1)


def get_confidence(prob):
    if prob >= 70:
        return "HIGH"
    elif prob >= 58:
        return "MEDIUM"
    else:
        return "LOW"


# =============================================================================
# KELLY CRITERION
# =============================================================================
# calculate_kelly/apply_portfolio_kelly logic itself is imported from
# utils.py. These are thin wrappers that preserve this file's original
# call signatures (market-specific default odds of 2.8; no bet_type
# param since home-win recommendations aren't nested under "over"/"under").
def calculate_kelly(prob, decimal_odds=2.8, use_half=True):
    return _shared_calculate_kelly(prob, decimal_odds, use_half)


def apply_portfolio_kelly(recommendations, bankroll, max_exposure=MAX_TOTAL_EXPOSURE):
    return _shared_apply_portfolio_kelly(recommendations, None, bankroll, max_exposure)


# =============================================================================
# MATCH PROCESSING
# =============================================================================
def process_single_match(match, target_date, default_odds=2.8):
    try:
        league_name = match.get("league", "")
        home_form = get_team_form(match["home_team_id"], True, 6, target_date)
        away_form = get_team_form(match["away_team_id"], False, 6, target_date)
        home_overall_5 = get_team_overall_form(match["home_team_id"], 5, target_date)
        away_overall_5 = get_team_overall_form(match["away_team_id"], 5, target_date)

        if len(home_form) < HW_MIN_DATA_GAMES or len(away_form) < HW_MIN_DATA_GAMES:
            return {"status": "insufficient"}

        data_mult = hw_data_volume_penalty(home_form, away_form, home_overall_5, away_overall_5)

        passed, failed, details, is_perfect = apply_home_win_algorithm(
            home_form, away_form, home_overall_5, away_overall_5
        )
        if passed is None:
            return {"status": "insufficient"}

        home_strength = get_team_strength(home_form, True, league_name=league_name)
        away_strength = get_team_strength(away_form, False, league_name=league_name)
        home_win_prob = calculate_home_win_prob(home_strength, away_strength)
        confidence = get_confidence(home_win_prob)

        score = len(passed)
        gate_passes = hw_model_gate_passes(home_strength, away_strength, home_win_prob)
        h2h_blocked, h2h_meetings, h2h_reason = _h2h_home_win_blocked(
            match["home_team_id"], match["away_team_id"], target_date
        )
        away_strength_veto, away_strength_reason = _away_strength_veto(home_strength, away_strength)
        draw_risk_veto, draw_risk_reason = _draw_risk_veto(home_form, away_form)

        weak_league = _hw_is_weak_roi(league_name)
        min_score = MAX_HOME_WIN_SCORE - 1 if weak_league else MAX_HOME_WIN_SCORE - 2
        qualifies = (
            score >= min_score and gate_passes and not h2h_blocked
            and not away_strength_veto and not draw_risk_veto
        )

        league_mult = _HW_WEAK_ROI_MULTIPLIER if weak_league else 1.0
        reg_mult = _hw_regression_penalty(home_form, away_form)
        final_mult = data_mult * league_mult * reg_mult

        conf_score = hw_compute_confidence_score(
            score, MAX_HOME_WIN_SCORE, home_win_prob, default_odds, final_mult
        )
        tier = hw_tier_from_confidence(conf_score, gate_passes, is_perfect) if qualifies else None

        if not qualifies:
            tier = None

        kelly_half = calculate_kelly(home_win_prob / 100, default_odds)
        if not qualifies:
            kelly_half = 0.0

        regressions = []
        if reg_mult < 1.0:
            regressions.append("home win streak")
        if h2h_blocked:
            regressions.append(f"h2h {h2h_reason} (home winless/bogey or away h2h advantage)")
        if away_strength_veto:
            regressions.append(f"away strength veto ({away_strength_reason})")
        if draw_risk_veto:
            regressions.append(f"draw risk ({draw_risk_reason})")

        return {
            "status": "success",
            "data": {
                "match": match,
                "score": score,
                "passed": passed,
                "details": details,
                "is_perfect": is_perfect,
                "tier": tier,
                "confidence_score": round(conf_score * 100, 1),
                "model": {
                    "home_strength": home_strength,
                    "away_strength": away_strength,
                    "strength_gap": round(home_strength - away_strength, 3),
                    "home_win_prob": home_win_prob,
                    "confidence": confidence,
                },
                "gate_passed": gate_passes,
                "h2h_blocked": h2h_blocked,
                "h2h_meetings": len(h2h_meetings),
                "away_strength_veto": away_strength_veto,
                "away_strength_reason": away_strength_reason,
                "draw_risk_veto": draw_risk_veto,
                "draw_risk_reason": draw_risk_reason,
                "data_mult": round(data_mult, 2),
                "weak_league_mult": round(league_mult, 2),
                "regression_mult": round(reg_mult, 2),
                "min_score_threshold": min_score,
                "kelly": round(kelly_half * 100, 2),
                "guards": {
                    "weak_roi_league": weak_league,
                    "away_strength_veto": away_strength_veto,
                    "away_strength_reason": away_strength_reason,
                    "draw_risk_veto": draw_risk_veto,
                    "draw_risk_reason": draw_risk_reason,
                    "home_strength": round(home_strength, 3),
                    "away_strength": round(away_strength, 3),
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
def build_report(perfect, qualified, close_calls, scanned_dates, bankroll, odds, detailed=False, compact=False,
                 include_yesterday=True, include_header=True, include_footer=True, report_date=None):
    """
    Build a clean, mobile-friendly report with all qualifying picks across scanned days.
    Returns: (report, base_date, included_perfect, included_qualified, included_close)
    """
    included_perfect = [item for item in perfect if not is_static_blocked_fixture(item.get("match", {}))]
    included_qualified = [item for item in qualified if not is_static_blocked_fixture(item.get("match", {}))]
    included_close = [item for item in close_calls if not is_static_blocked_fixture(item.get("match", {}))]
    if report_date:
        included_perfect = filter_pick_items_by_date(included_perfect, report_date)
        included_qualified = filter_pick_items_by_date(included_qualified, report_date)
        included_close = filter_pick_items_by_date(included_close, report_date)
    included_dates = scanned_dates

    base_date = scanned_dates[0] if scanned_dates else datetime.now().strftime("%Y-%m-%d")

    # Clean report (mobile-friendly)
    lines = []
    if report_date and not include_header and not compact:
        lines.append(f"📅 Picks for {report_date}")
        lines.append("")
    if not compact:
        if detailed and include_header:
            lines.extend(format_vip_banner("Home Win", base_date, included_dates))
        if include_header:
            lines.append("🏠 Home Win picks")
            lines.append("")
            if len(included_dates) > 1:
                lines.append(f"Dates: {included_dates[0]} to {included_dates[-1]}")
            else:
                lines.append(f"Date: {base_date}")
            lines.append("")
            if include_yesterday:
                append_yesterday_section(lines, "home_win", detailed=detailed)

    show_date = len(included_dates) > 1

    pick_idx = [0]

    def append_items(items, tier):
        for item in items:
            pick_idx[0] += 1
            m = item["match"]
            p = item["model"]
            if compact:
                lines.append(f"  {format_compact_pick_line(
                    m['home'], m['away'], 'HW', tier, p['home_win_prob'],
                    m['date'] if show_date else None,
                )}")
                continue
            extra = None
            if detailed:
                h2h_blocked_flag = item.get("h2h_blocked")
                h2h_n = item.get("h2h_meetings", 0)
                h2h_note = None
                if h2h_blocked_flag and h2h_n:
                    h2h_note = f"qualifies with H2H bogey flag raised ({h2h_n} meetings)"
                elif h2h_n and h2h_n >= 2:
                    h2h_note = f"{h2h_n} H2H meetings logged"
                extra = format_vip_extra_lines(
                    item["kelly"], odds, item["score"], MAX_HOME_WIN_SCORE,
                    home_strength=p["home_strength"], away_strength=p["away_strength"],
                    model_prob=p["home_win_prob"],
                    market=MARKET_HOME_WIN,
                    h2h_note=h2h_note,
                    rule_details=item.get("details"),
                )
            categories = describe_pick_categories(
                m["home"], m["away"], m.get("league", ""),
                market=MARKET_HOME_WIN,
                tier=tier,
                weak_roi_league=bool((item.get("guards") or {}).get("weak_roi_league")),
            )
            lines.extend(format_pick_block(
                pick_idx[0], m["home"], m["away"], m["date"],
                f"Home Win · {format_confidence_label(p['confidence'])} ({p['home_win_prob']}%)",
                extra,
                league=m.get("league"),
                categories=categories,
            ))

    if compact:
        groups = [
            (COMPACT_TIER_HEADER_PREMIUM, included_perfect),
            (COMPACT_TIER_HEADER_STRONG, included_qualified),
            (COMPACT_TIER_HEADER_WATCH, included_close),
        ]
        for tier_header, items in groups:
            if not items:
                continue
            lines.append(f"  {tier_header}")
            append_items(items, tier_header.split()[1].lower() if tier_header.split()[1].lower() in
                         ("perfect", "qualified", "close") else (
                             "perfect" if "Premium" in tier_header else
                             "qualified" if "Solid" in tier_header else "close"))
    else:
        if included_perfect:
            lines.append("")
            lines.append(f"  {PICK_TIER_PREMIUM}")
            lines.append("")
            append_items(included_perfect, "perfect")
        if included_qualified:
            lines.append("")
            lines.append(f"  {PICK_TIER_STRONG}")
            lines.append("")
            append_items(included_qualified, "qualified")
        if included_close:
            lines.append("")
            lines.append(f"  {PICK_TIER_VALUE}")
            lines.append("")
            append_items(included_close, "close")

    if not compact and include_footer:
        if detailed:
            lines.extend(format_vip_summary(
                "HOME WIN  ·  Pick summary",
                perfect, qualified, close_calls,
            ))
        lines.append("---")
        lines.append("For informational purposes only")
        lines.append("Gamble responsibly")
        lines.append("")

    report = "\n".join(lines).strip()
    if not report:
        report = "— none"
    return report, base_date, included_perfect, included_qualified, included_close


# =============================================================================
# MAIN
# =============================================================================
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(
        description="Home Win Predictor with Strength Model + Kelly"
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
        "--odds",
        type=float,
        default=2.8,
        help="Average decimal odds for Home Win"
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
    perfect, qualified, close_calls = [], [], []
    scanned_dates = []

    print(f"Starting Home Win analysis from {args.date}...")

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
                executor.submit(process_single_match, match, date_str, args.odds): match
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
                    tier = data.get("tier")
                    if tier == "perfect":
                        perfect.append(data)
                    elif tier == "qualified":
                        qualified.append(data)
                    elif tier == "close":
                        close_calls.append(data)
                    elif data["score"] >= MAX_HOME_WIN_SCORE - 2:
                        close_calls.append(data)

    # Apply portfolio Kelly cap
    all_recs = perfect + qualified + close_calls
    apply_portfolio_kelly(all_recs, args.bankroll, MAX_TOTAL_EXPOSURE)

    # Build and output reports (both free and detailed)
    free_report, base_date, included_perfect, included_qualified, included_close = build_report(
        perfect, qualified, close_calls, scanned_dates, args.bankroll, args.odds, detailed=False
    )
    publish_date = args.publish_date or datetime.now().strftime("%Y-%m-%d")
    telegram_report, _, _, _, _ = build_report(
        perfect, qualified, close_calls, scanned_dates, args.bankroll, args.odds,
        detailed=False, compact=False,
        include_yesterday=False, include_header=False, include_footer=False,
        report_date=publish_date,
    )
    detailed_report, _, _, _, _ = build_report(
        perfect, qualified, close_calls, scanned_dates, args.bankroll, args.odds, detailed=True
    )

    # Output free report (default)
    print("\n===EMAIL_START===")
    print(free_report)
    print("===EMAIL_END===")
    from prediction_tracker import write_telegram_section
    telegram_body = write_telegram_section(telegram_report, "hw_telegram.txt")

    # Save detailed report to file
    detailed_report_path = f"home_win_vip_report_{base_date}.txt"
    with open(detailed_report_path, "w", encoding="utf-8") as f:
        f.write(detailed_report)

    # Save JSON
    output_path = f"home_win_report_{base_date}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "metadata": {
                "scanned_window": scanned_dates,
                "bankroll": args.bankroll,
                "odds": args.odds,
                "max_exposure": MAX_TOTAL_EXPOSURE,
                "generated_at": datetime.now().isoformat()
            },
            "perfect": perfect,
            "qualified": qualified,
            "close_calls": close_calls
        }, f, indent=2, default=str)

    # Record predictions for tracking
    # IMPORTANT: Use original tier buckets (not build_report's included_*) so
    # statistically-blocked leagues are still recorded with published=false.
    # record_predictions() handles the published flag internally via
    # is_statistical_block_only() and fully skips only static/integrity blocks.
    try:
        hw_picks = []
        all_hw = perfect + qualified + close_calls
        for pick in all_hw:
            tier = pick.get("tier") or (
                "perfect" if pick in perfect else
                "qualified" if pick in qualified else "close"
            )
            hw_picks.append({
                "league": pick["match"]["league"],
                "home": pick["match"]["home"],
                "away": pick["match"]["away"],
                "date": pick["match"]["date"],
                "confidence": tier,
            })
        stats = record_predictions(base_date, hw_picks, [])
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
