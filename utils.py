#!/usr/bin/env python3
"""
Shared utilities for the soccer prediction system.

This is the SINGLE canonical implementation of scraping, caching, date
parsing, and Kelly-stake helpers used by all three predictors
(over25_soccerbase.py, home_win_soccerbase.py, btts_soccerbase.py).

Previously each predictor carried its own copy-pasted version of this
code (~90 lines each), which had already begun to drift between files.
Import from here instead of redefining these helpers locally.
"""

import hashlib
import json
import logging
import math
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup  # noqa: F401  (re-exported for convenience)
from fake_useragent import UserAgent
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_REQUEST_DELAY_MIN = 2.5
DEFAULT_REQUEST_DELAY_MAX = 5.0
DEFAULT_TIMEOUT_SECONDS = 20
MIN_VALID_PAGE_BYTES = 1500
BLOCKED_PAGE_MARKERS = ("captcha", "verify you are human", "access denied", "blocked")

_ua = UserAgent(
    fallback="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "DNT": "1",
    "Connection": "keep-alive",
}


def get_random_headers():
    """Return a fresh header dict with a randomized User-Agent."""
    headers = BASE_HEADERS.copy()
    headers["User-Agent"] = _ua.random
    return headers


def build_session(pool_size=10, total_retries=4, backoff_factor=1.5):
    """
    Build a requests.Session with connection pooling and retry-on-failure
    for transient errors (429/500/502/503/504).

    Build ONE session per process and reuse it — do not call this per
    request, it discards connection pooling and defeats its own purpose.
    """
    retry_strategy = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=pool_size,
        pool_maxsize=pool_size,
    )
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def random_delay(min_delay=DEFAULT_REQUEST_DELAY_MIN, max_delay=DEFAULT_REQUEST_DELAY_MAX):
    """Sleep a random interval to avoid hammering the source site."""
    time.sleep(random.uniform(min_delay, max_delay))


class Cache:
    """SQLite-backed TTL cache for fetched HTML, keyed by URL hash."""

    def __init__(self, db_path, ttl_hours=24):
        self.db_path = db_path
        self.ttl = timedelta(hours=ttl_hours)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON cache(created_at)")

    @staticmethod
    def _make_key(url):
        return hashlib.sha256(url.encode()).hexdigest()

    def get(self, url):
        key = self._make_key(url)
        cutoff = (datetime.now() - self.ttl).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM cache WHERE key = ? AND created_at > ?",
                (key, cutoff),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, url, value):
        key = self._make_key(url)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )

    def clear(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM cache")
        logger.info("Cache cleared: %s", self.db_path)


def fetch(url, session, cache=None, use_cache=True,
          min_delay=DEFAULT_REQUEST_DELAY_MIN, max_delay=DEFAULT_REQUEST_DELAY_MAX,
          timeout=DEFAULT_TIMEOUT_SECONDS):
    """
    Fetch a URL's HTML with caching, randomized headers/delay, and basic
    anti-bot detection (captcha pages / suspiciously short responses are
    treated as failures, not cached, and return None).
    """
    if use_cache and cache:
        cached = cache.get(url)
        if cached is not None:
            logger.debug("Cache hit: %s...", url[:80])
            return cached

    random_delay(min_delay, max_delay)
    try:
        response = session.get(url, headers=get_random_headers(), timeout=timeout)
        response.raise_for_status()
        data = response.text

        lower = data.lower()
        if any(marker in lower for marker in BLOCKED_PAGE_MARKERS):
            logger.error("BLOCKED by %s — possible captcha", url[:80])
            return None
        if len(data) < MIN_VALID_PAGE_BYTES:
            logger.error("SUSPICIOUS short page (%d bytes) for %s", len(data), url[:80])
            return None

        if use_cache and cache:
            cache.set(url, data)
        return data
    except Exception as e:
        logger.error("Fetch failed for %s: %s", url[:80], e)
        return None


def parse_date(date_str):
    """
    Parse date string with multiple format fallbacks.
    Handles Soccerbase variations: 2026-06-15, 15-Jun-26, 2026/06/15, etc.
    """
    if not date_str:
        return None

    date_str = str(date_str).strip()

    for fmt in ("%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str, fmt)
        except (ValueError, TypeError):
            continue

    match = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", date_str)
    if match:
        try:
            # Normalize "/" to "-" first: without this, a slash-separated
            # match (e.g. "2026/06/15") fails %Y-%m-%d parsing here. This
            # bug existed in two of the three predictors that used to
            # carry their own copy of this function; btts_soccerbase.py's
            # copy already had the fix, so it's applied here for all three.
            return datetime.strptime(match.group(0).replace("/", "-")[:10], "%Y-%m-%d")
        except ValueError:
            pass

    logger.warning("Could not parse date: %s", date_str)
    return None


def calculate_kelly(prob, decimal_odds, use_half=True):
    """Fractional Kelly stake as a fraction of bankroll (0.0-1.0)."""
    if prob <= 0.0 or decimal_odds <= 1.0:
        return 0.0
    kelly = (prob * decimal_odds - 1) / (decimal_odds - 1)
    return max(0.0, kelly * 0.5 if use_half else kelly)


def implied_probability(decimal_odds):
    """Convert decimal odds to the bookmaker's implied probability (0-1),
    with no overround/margin removal — this is the raw implied probability,
    not a "true" de-vigged probability. Returns None for invalid odds.
    """
    if not decimal_odds or decimal_odds <= 1.0:
        return None
    return 1.0 / decimal_odds


def value_gate_passes(model_prob_pct, decimal_odds, min_edge_pct=0.0):
    """Optional cross-check against a market price, modeled on how sites
    like Statarea (bookmaker coefficients as a model input) and
    MyGameOdds (model probability AND a minimum market price both
    required) validate a tip before publishing it — see
    RESEARCH_PREDICTION_SITES.md for the sourcing.

    model_prob_pct: this project's own model probability, as a 0-100 number.
    decimal_odds: a real market price for the same outcome, if you have one.
    min_edge_pct: how many percentage points the model must clear the
      market's implied probability by (0 = just needs to beat it).

    Returns True (gate passes / no odds supplied, doesn't block anything)
    when decimal_odds is None — this function is opt-in. Nothing in this
    codebase currently scrapes live per-match odds (the --odds CLI flags
    across all three engines are a single flat manual number applied to
    every pick for staking math only, not a per-match market signal), so
    this is wired up but inert until a real odds feed is added upstream.
    """
    if decimal_odds is None:
        return True
    implied = implied_probability(decimal_odds)
    if implied is None:
        return True
    return (model_prob_pct / 100.0) >= (implied + min_edge_pct / 100.0)


def apply_portfolio_kelly(recommendations, bet_type, bankroll, max_exposure):
    """
    Scale Kelly fractions so total exposure does not exceed max_exposure.
    bet_type: "over" or "under" - targets the correct nested dictionary.
    For home win, pass None as bet_type.
    """
    if not recommendations or bankroll <= 0:
        return recommendations

    if bet_type:
        total_kelly = sum(r[bet_type]["kelly"] / 100 for r in recommendations)
    else:
        total_kelly = sum(r["kelly"] / 100 for r in recommendations)

    if total_kelly <= 0:
        return recommendations

    if total_kelly > max_exposure:
        scale = max_exposure / total_kelly
        for r in recommendations:
            if bet_type:
                r[bet_type]["kelly"] = round(r[bet_type]["kelly"] * scale, 2)
            else:
                r["kelly"] = round(r["kelly"] * scale, 2)
        logger.info(
            "Portfolio Kelly scaled by %.3f (%.1f%% -> %.1f%% exposure)",
            scale, total_kelly * 100, max_exposure * 100,
        )

    return recommendations


def exponential_form_averages(form_tuples, halflife=3.0):
    """Weighted average of (gf, ga) with exponential decay.

    form_tuples[0] is the most recent match (weight=1.0); each older match
    is multiplied by 0.5 ** (n / halflife) for match index n going back.
    Returns (weighted_gf_per_game, weighted_ga_per_game, effective_sample_weight).
    """
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


def is_weak_roi_league(league_name, keywords):
    """Return True if league_name matches any of the case-insensitive keywords."""
    name = str(league_name or "").strip().lower()
    return any(k in name for k in keywords)


def poisson_pmf(k, lam):
    """Poisson probability mass function: P(k | lam)."""
    if lam <= 0:
        return 0.0
    return (math.exp(-lam) * (lam ** k)) / math.factorial(k)
