#!/usr/bin/env python3
"""GitHub Actions daily publisher: read COMMITTED reports and send to Telegram.

Intended to be called ONLY from the `publish-daily` job in
.github/workflows/run_daily.yml.  The prediction engines NEVER run in
CI — run_local.bat runs them locally, commits the publish inputs
(telegram.txt and the 4 per-market *_telegram.txt sections), and pushes
to origin/main.  This CI script then just:

  1. Reads the already-committed report files from disk.
  2. Builds the free-tier message from the 4 per-market telegram sections
     (exactly the same way build_telegram_daily.py / send_local_telegram.py
     do locally — no duplicate assembly logic).
  3. Builds the VIP board from those same sections: every fixture across all
     four markets as a confidence-grouped monospace table, preceded by
     yesterday's per-market-side record (W-L-P and win rate).
  4. Posts each message to its corresponding chat ID via secrets env vars
     (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_VIP_CHAT_ID).

Env vars:
    DATE                 - YYYY-MM-DD publish date (free-tier header)
    TELEGRAM_BOT_TOKEN   - Bot token (GitHub secret)
    TELEGRAM_CHAT_ID     - Free-tier channel ID (GitHub secret)
    TELEGRAM_VIP_CHAT_ID - VIP channel ID (GitHub secret, optional)
"""

import html
import os
import sys
import time

import requests

from build_telegram_daily import (
    build_daily_message,
    build_vip_board,
    read_telegram_section,
    split_pre_chunks,
)
from prediction_tracker import get_yesterday_market_breakdown

# Telegram caps sends to a single chat (~20 msgs/min); space chunks out and
# retry with backoff on HTTP 429 so a long message never fails the CI job.
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 5
INTER_CHUNK_DELAY_SECONDS = 3


def _env(name, default=None):
    v = os.environ.get(name)
    return default if v in (None, "") else v


def _retry_after(resp, fallback):
    """Telegram sends ``parameters.retry_after`` (seconds) on HTTP 429."""
    try:
        params = (resp.json() or {}).get("parameters") or {}
        wait = params.get("retry_after")
        if isinstance(wait, (int, float)) and wait > 0:
            return float(wait)
    except (ValueError, AttributeError):
        pass
    return fallback


def _send_one(token, chat_id, payload, index, total, label):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
                timeout=30,
            )
        except requests.RequestException as e:
            print(f"[{label}] chunk {index}/{total} chat {chat_id}: network error: {e}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF_BASE_SECONDS * attempt)
                continue
            return False

        if resp.status_code == 200:
            print(f"[{label}] chunk {index}/{total} chat {chat_id}: OK")
            return True

        if resp.status_code == 429:
            wait = _retry_after(resp, BACKOFF_BASE_SECONDS * attempt)
            if attempt < MAX_ATTEMPTS:
                print(
                    f"[{label}] chunk {index}/{total} chat {chat_id}: HTTP 429 "
                    f"rate limited, retrying in {wait:.0f}s (attempt {attempt}/{MAX_ATTEMPTS})"
                )
                time.sleep(wait)
                continue
            print(f"[{label}] chunk {index}/{total} chat {chat_id}: HTTP 429: giving up after {MAX_ATTEMPTS} attempts")
            return False

        body = resp.text[:300]
        print(f"[{label}] chunk {index}/{total} chat {chat_id}: HTTP {resp.status_code} {body}")
        return False

    return False


def _send_chunks(token, chat_id, text, label="message", as_pre=False):
    if not text or not text.strip():
        print(f"[{label}] Empty, skipping chat {chat_id}.")
        return True
    if as_pre:
        # Monospace table: wrap each line-bounded chunk in its own <pre>.
        payloads = [(f"<pre>{html.escape(c)}</pre>", "HTML") for c in split_pre_chunks(text)]
    else:
        payloads = [(text[i:i + 3900], None) for i in range(0, len(text), 3900)]

    ok = True
    for ci, (chunk, parse_mode) in enumerate(payloads, 1):
        payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if not _send_one(token, chat_id, payload, ci, len(payloads), label):
            ok = False
        if ci < len(payloads):
            time.sleep(INTER_CHUNK_DELAY_SECONDS)
    return ok


def build_free_message(date):
    ou = read_telegram_section("ou_telegram.txt")
    btts = read_telegram_section("btts_telegram.txt")
    hw = read_telegram_section("hw_telegram.txt")
    oo05 = read_telegram_section("oo05_telegram.txt")
    return build_daily_message(date, ou, btts, hw, oo05)


def build_vip_message(date):
    """VIP board = every fixture from all four committed market sections.

    Built from the same *_telegram.txt inputs the free message uses, so the
    VIP channel shows the full board (not just a curated subset) rendered as
    a confidence-grouped monospace table, preceded by yesterday's record
    split by market and side.
    """
    ou = read_telegram_section("ou_telegram.txt")
    btts = read_telegram_section("btts_telegram.txt")
    hw = read_telegram_section("hw_telegram.txt")
    oo05 = read_telegram_section("oo05_telegram.txt")
    return build_vip_board(
        date, ou, btts, hw, oo05, results=get_yesterday_market_breakdown()
    )


def main():
    token = _env("TELEGRAM_BOT_TOKEN")
    free_chat = _env("TELEGRAM_CHAT_ID")
    vip_chat = _env("TELEGRAM_VIP_CHAT_ID")
    date = _env("DATE") or __import__("datetime").datetime.now().strftime("%Y-%m-%d")

    if not token or not free_chat:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required env vars.")
        print("       (Set them as GitHub repository secrets.)")
        return 1

    print(f"Publish date: {date}")
    free_msg = build_free_message(date)
    vip_msg = build_vip_message(date) if vip_chat else ""

    print(f"Free-tier message: {len(free_msg or '')} chars")
    print(f"VIP message: {len(vip_msg) if vip_msg else 0} chars")

    ok = _send_chunks(token, free_chat, free_msg, label="FREE")
    if vip_chat:
        ok_vip = _send_chunks(token, vip_chat, vip_msg, label="VIP", as_pre=True)
        ok = ok and ok_vip

    return 0 if ok else 3


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
