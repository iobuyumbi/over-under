"""
Central configuration for all prediction engines.
Centralizes constants, thresholds, and environment settings to prevent drift.
"""
import os

# --- Environment & Infrastructure ---
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

# --- Market Specific Constants ---

# Over/Under 2.5
MAX_OVER_SCORE = 13
MAX_UNDER_SCORE = 12

# BTTS
MAX_BTTS_YES_SCORE = 14
MAX_BTTS_NO_SCORE = 14
BTTS_MIN_6 = 3
NON_BTTS_MIN_6 = 3

# --- Tiers & Confidence ---
TIER_PREMIUM_CUTOFF = 0.62
TIER_SOLID_CUTOFF = 0.54

# --- Regression & Form ---
MIN_FORM_HALFLIFE = 3.0
REGRESSION_OVER_STREAK = 5
REGRESSION_UNDER_STREAK = 5
REGRESSION_PENALTY = 0.08

# --- ROI & League Penalty ---
WEAK_ROI_MULTIPLIER = 0.82
WEAK_ROI_LEAGUE_KEYWORDS = (
    "swedish allsvenskan", "allsvenskan", "superettan",
    "belarus",
    "k-league", "k league", "korean k-league",
    "league of ireland", "irish", "fai cup",
    "mexican primera", "brazilian serie a",
    "mls", "ecuador", "argentina primera", "chile primera",
)

# --- Lambda Thresholds ---
MIN_COMBINED_LAMBDA_OVER = 2.75
MAX_COMBINED_LAMBDA_UNDER = 2.15
PREMIUM_COMBINED_LAMBDA_OVER = 3.30
PREMIUM_COMBINED_LAMBDA_UNDER = 2.00
