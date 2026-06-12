#!/usr/bin/env python3
"""
Shared utilities for soccer prediction system.
"""

import requests
import json
import time
import random
import sqlite3
import hashlib
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from fake_useragent import UserAgent
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

class Cache:
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
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_created ON cache(created_at)
            """)

    def _make_key(self, url):
        return hashlib.sha256(url.encode()).hexdigest()

    def get(self, url):
        key = self._make_key(url)
        cutoff = (datetime.now() - self.ttl).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM cache WHERE key = ? AND created_at > ?",
                (key, cutoff)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, url, value):
        key = self._make_key(url)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value) VALUES (?, ?)",
                (key, json.dumps(value))
            )

    def clear(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM cache")
        logger.info("Cache cleared.")

# Global session with retries
ua = UserAgent(
    fallback="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "DNT": "1",
    "Connection": "keep-alive",
}

def get_session():
    retry_strategy = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504]
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=10,
        pool_maxsize=10
    )
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

def random_delay(min_delay=2.5, max_delay=5.0):
    time.sleep(random.uniform(min_delay, max_delay))

def fetch(url, cache=None, use_cache=True):
    if use_cache and cache:
        cached = cache.get(url)
        if cached is not None:
            logger.debug(f"Cache hit: {url[:80]}...")
            return cached

    random_delay()
    try:
        session = get_session()
        headers = HEADERS.copy()
        headers["User-Agent"] = ua.random
        response = session.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.text
        if use_cache and cache:
            cache.set(url, data)
        return data
    except Exception as e:
        logger.error(f"Fetch failed for {url[:80]}: {e}")
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

    # Try to extract any date-like pattern as last resort
    import re
    match = re.search(r'(\d{4})[-/](\d{2})[-/](\d{2})', date_str)
    if match:
        try:
            return datetime.strptime(match.group(0), "%Y-%m-%d")
        except ValueError:
            pass

    logger.warning(f"Could not parse date: {date_str}")
    return None

def calculate_kelly(prob, decimal_odds, use_half=True):
    if prob <= 0.0 or decimal_odds <= 1.0:
        return 0.0
    kelly = (prob * decimal_odds - 1) / (decimal_odds - 1)
    return max(0.0, kelly * 0.5 if use_half else kelly)

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
            f"Portfolio Kelly scaled by {scale:.3f} "
            f"({total_kelly*100:.1f}% -> {max_exposure*100:.1f}% exposure)"
        )

    return recommendations
