"""
Central configuration for all prediction engines.
Single source of truth for thresholds, environment settings, and
market-specific constants to prevent drift between sibling predictors.
"""
import os

# =============================================================================
# Environment & Infrastructure (shared by all engines)
# =============================================================================
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

# Per-market cache DB paths (separate caches are fine; TTL & concurrency shared)
OVER25_CACHE_DB = "soccerbase_cache.db"
HOME_WIN_CACHE_DB = "soccerbase_cache_home.db"
BTTS_CACHE_DB = "soccerbase_cache_btts.db"
OO05_CACHE_DB = "soccerbase_cache_oo05.db"

# =============================================================================
# Tiers & Confidence (defaults — markets may override)
# =============================================================================
TIER_PREMIUM_CUTOFF = 0.62
TIER_SOLID_CUTOFF = 0.54
MIN_DATA_GAMES_DEFAULT = 3

WEIGHT_RULES_DEFAULT = 0.40
WEIGHT_MODEL_DEFAULT = 0.40
WEIGHT_EDGE_DEFAULT = 0.20

# =============================================================================
# Regression & Form (shared across markets)
# =============================================================================
MIN_FORM_HALFLIFE = 3.0
REGRESSION_OVER_STREAK = 5
REGRESSION_UNDER_STREAK = 5
REGRESSION_PENALTY = 0.08

# =============================================================================
# ROI / Weak League Penalties (shared baseline, markets may tweak)
# =============================================================================
WEAK_ROI_MULTIPLIER = 0.82

WEAK_ROI_LEAGUE_KEYWORDS = (
    "swedish allsvenskan", "allsvenskan", "superettan",
    "belarus",
    "k-league", "k league", "korean k-league",
    "league of ireland", "irish", "fai cup",
    "mexican primera", "brazilian serie a",
    "mls", "ecuador", "argentina primera", "chile primera",
)

# =============================================================================
# Over / Under 2.5
# =============================================================================
MAX_OVER_SCORE = 13
MAX_UNDER_SCORE = 12

# Under 2.5 specific caps (from over25tips.com style rules)
UNDER_HOME_SCORED_CAP = 1.2
UNDER_HOME_CONCEDED_CAP = 1.2
UNDER_AWAY_SCORED_CAP = 1.0
UNDER_AWAY_CONCEDED_CAP = 1.0

# Legacy / compatibility caps
UNDER_HOME_TOTAL_6_CAP = 10.0
UNDER_AWAY_TOTAL_6_CAP = 10.0
UNDER_HOME_OVER25_MAX = 3
UNDER_AWAY_OVER25_MAX = 3

# BTTS mini-gates reused inside OU (with thin-data fallbacks)
BTTS_MIN_6 = 3
NON_BTTS_MIN_6 = 3

# Lambda thresholds
MIN_COMBINED_LAMBDA_OVER = 2.75
MAX_COMBINED_LAMBDA_UNDER = 2.15
PREMIUM_COMBINED_LAMBDA_OVER = 3.30
PREMIUM_COMBINED_LAMBDA_UNDER = 2.00

# Weak-league lambda adjustments (OU specific)
WEAK_ROI_OVER_LAMBDA_BOOST = 0.30
WEAK_ROI_UNDER_LAMBDA_REDUCTION = 0.20

# OU H2H rates
OU_H2H_MAX_LOOKBACK = 6
OU_H2H_MIN_MEETINGS = 3
OU_H2H_OVER_BLOCK_RATE = 0.33
OU_H2H_UNDER_BLOCK_RATE = 0.67

# =============================================================================
# Home Win
# =============================================================================
HOME_WIN_DEFAULT_ODDS = 2.80
HOME_WIN_SHRINKAGE_WEIGHT = 0.65          # intentional slight difference from OU default 0.60
HOME_WIN_CACHE_TTL_HOURS = 24             # unified TTL with the rest

MAX_HOME_WIN_SCORE = 11
HW_MIN_DATA_GAMES = 3
HW_WEIGHT_RULES = 0.40
HW_WEIGHT_MODEL = 0.40
HW_WEIGHT_EDGE = 0.20
HW_TIER_PREMIUM_CUTOFF = 0.62
HW_TIER_SOLID_CUTOFF = 0.53
HW_MIN_MODEL_PROB = 0.55
HW_MIN_STRENGTH_GAP = 0.12
HW_HALFLIFE = 3.0

HW_WEAK_ROI_LEAGUE_KEYWORDS = (
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
HW_WEAK_ROI_MULTIPLIER = 0.82
HW_REGRESSION_WIN_STREAK = 5
HW_REGRESSION_PENALTY = 0.08
HW_H2H_MIN_MEETINGS = 2
HW_H2H_MAX_LOOKBACK = 6
HW_H2H_AWAY_WIN_RATIO = 2.0
HW_H2H_MIN_AWAY_WINS_FOR_ADVANTAGE = 2

# =============================================================================
# BTTS (Both Teams To Score)
# =============================================================================
MAX_BTTS_YES_SCORE = 14
MAX_BTTS_NO_SCORE = 14
# (BTTS_MIN_6 / NON_BTTS_MIN_6 already defined in OU section above)

MIN_COMBINED_LAMBDA_BTTS_YES = 2.50
MAX_COMBINED_LAMBDA_BTTS_NO = 2.80
PREMIUM_COMBINED_LAMBDA_BTTS_YES = 2.90
PREMIUM_COMBINED_LAMBDA_BTTS_NO = 2.20

DEFAULT_ODDS_BTTS_YES = 1.90
DEFAULT_ODDS_BTTS_NO = 1.85

# over25tips.com official BTTS point algorithm
MIN_O25TIPS_BTTS_YES_POINTS = 7.0
MAX_O25TIPS_BTTS_NO_POINTS = 3.0
O25TIPS_FORM_WINDOW = 6

BTTS_WEIGHT_RULES = 0.40
BTTS_WEIGHT_MODEL = 0.40
BTTS_WEIGHT_EDGE = 0.20
BTTS_TIER_PREMIUM_CUTOFF = 0.62
BTTS_TIER_SOLID_CUTOFF = 0.54
BTTS_MIN_DATA_GAMES = 5
BTTS_MIN_FORM_HALFLIFE = 3.0

BTTS_WEAK_ROI_LEAGUE_KEYWORDS = (
    "swedish allsvenskan", "allsvenskan", "superettan",
    "belarus",
    "k-league", "k league", "korean k-league",
    "league of ireland", "irish", "fai cup",
    "mexican primera", "brazilian serie a",
    "mls", "ecuador", "argentina primera", "chile primera",
)
BTTS_WEAK_ROI_MULTIPLIER = 0.82

BTTS_H2H_MAX_LOOKBACK = 6
BTTS_H2H_MIN_MEETINGS = 3
BTTS_H2H_YES_BLOCK_RATE = 0.33
BTTS_H2H_NO_BLOCK_RATE = 0.67

# =============================================================================
# OO.5 Team Goals (Over 0.5 Team Goal)
# =============================================================================
OO05_DEFAULT_ODDS_HOME = 1.45
OO05_DEFAULT_ODDS_AWAY = 1.55
OO05_MAX_TOTAL_EXPOSURE = 0.20
OO05_SHRINKAGE_WEIGHT = 0.55
OO05_MIN_DATA_GAMES = 3
OO05_MAX_SCORE = 12

# Streak gates (team scoring)
OO05_OVERALL_STREAK_10 = (8, 10)
OO05_OVERALL_STREAK_12 = (9, 12)
OO05_VENUE_STREAK_6 = (5, 6)
OO05_VENUE_STREAK_5 = (4, 5)

# Defence gates (opponent conceding) — HARD VETO
OO05_OPP_VENUE_LEAK_5 = (4, 5)
OO05_OPP_OVERALL_LEAK_10 = (8, 10)
OO05_OPP_VENUE_ELITE_5 = (5, 5)
OO05_OPP_OVERALL_ELITE_10 = (10, 10)

OO05_WEAK_ROI_KEYWORDS = (
    "youth", "u17", "u19", "u20", "u21", "amateur", "friendly",
    "pre-season", "copa do brasil sub", "qualification preliminary",
    "women reserve", "reserve", "academy", "trial", "exhibition",
)
OO05_WEAK_ROI_MULTIPLIER = 0.85

OO05_WEIGHT_RULES = 0.45
OO05_WEIGHT_MODEL = 0.35
OO05_WEIGHT_EDGE = 0.20

OO05_TIER_PREMIUM_CUTOFF = 0.65
OO05_TIER_SOLID_CUTOFF = 0.55
