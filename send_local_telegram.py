#!/usr/bin/env python3
"""Send the assembled Telegram daily message to Telegram from a local run.

Usage:
  set TELEGRAM_BOT_TOKEN=xxx
  set TELEGRAM_CHAT_ID=yyy
  set TELEGRAM_VIP_CHAT_ID=zzz  (optional)
  set DATE=2026-08-30            (optional, defaults to today)
  python send_local_telegram.py

The script reads the four per-market Telegram section files
(ou_telegram.txt, btts_telegram.txt, hw_telegram.txt, oo05_telegram.txt) that each
predictor writes and posts two messages:

  * the free-tier chat (TELEGRAM_CHAT_ID) gets the assembled daily message; and
  * the VIP chat (TELEGRAM_VIP_CHAT_ID) gets the full board -- every fixture
    from all four markets, rendered as a confidence-grouped monospace table.

This is the local-only counterpart to the inline Telegram posting
that lives inside the GitHub Actions workflow (run_daily.yml).
"""

import html
import os
import sys
import time

import requests
from dotenv import load_dotenv

from build_telegram_daily import (
    build_daily_message,
    build_vip_board,
    read_telegram_section,
    split_pre_chunks,
)

# Local credentials live in .env (which is ignored by Git). Environment
# variables still take precedence, which keeps GitHub Actions unchanged.
load_dotenv()

MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 5
INTER_CHUNK_DELAY_SECONDS = 3


def _env_or(name, default=None):
    return os.getenv(name, default)


def _retry_after(resp, fallback):
    try:
        wait = (resp.json() or {}).get("parameters", {}).get("retry_after")
        if isinstance(wait, (int, float)) and wait > 0:
            return float(wait)
    except (ValueError, AttributeError):
        pass
    return fallback


def _send(TOKEN, chat_id, text, label, as_pre=False):
    if as_pre:
        payloads = [(f"<pre>{html.escape(c)}</pre>", "HTML") for c in split_pre_chunks(text)]
    else:
        payloads = [(text[i:i + 3900], None) for i in range(0, len(text), 3900)]

    ok = True
    for ci, (chunk, parse_mode) in enumerate(payloads, 1):
        data = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            data["parse_mode"] = parse_mode
        sent = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    data=data,
                    timeout=30,
                )
            except requests.RequestException as e:
                print(f"  [{label}] chunk {ci}/{len(payloads)} -> chat {chat_id}: network error: {e}")
                if attempt < MAX_ATTEMPTS:
                    time.sleep(BACKOFF_BASE_SECONDS * attempt)
                    continue
                break
            if resp.status_code == 200:
                sent = True
                break
            if resp.status_code == 429:
                wait = _retry_after(resp, BACKOFF_BASE_SECONDS * attempt)
                if attempt < MAX_ATTEMPTS:
                    print(f"  [{label}] chunk {ci}/{len(payloads)}: HTTP 429, retrying in {wait:.0f}s")
                    time.sleep(wait)
                    continue
                print(f"  [{label}] chunk {ci}/{len(payloads)}: HTTP 429: giving up after {MAX_ATTEMPTS} attempts")
                break
            print(f"  [{label}] chunk {ci}/{len(payloads)} -> chat {chat_id}: HTTP {resp.status_code} {resp.text[:200]}")
            break
        if not sent:
            ok = False
        if ci < len(payloads):
            time.sleep(INTER_CHUNK_DELAY_SECONDS)
    if ok:
        print(f"Sent {len(payloads)} chunk(s) to chat {chat_id} ({label})")
    return ok


def main():
    TOKEN = _env_or("TELEGRAM_BOT_TOKEN")
    CHAT = _env_or("TELEGRAM_CHAT_ID")
    VIP = _env_or("TELEGRAM_VIP_CHAT_ID")
    DATE = _env_or("DATE") or __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    OU_FILE = _env_or("OU_TELEGRAM_FILE", "ou_telegram.txt")
    BTTS_FILE = _env_or("BTTS_TELEGRAM_FILE", "btts_telegram.txt")
    HW_FILE = _env_or("HW_TELEGRAM_FILE", "hw_telegram.txt")
    OO05_FILE = _env_or("OO05_TELEGRAM_FILE", "oo05_telegram.txt")

    if not TOKEN or not CHAT:
        print("ERROR: Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars before running.")
        print("  Example (Windows cmd):  set TELEGRAM_BOT_TOKEN=123456:ABCxyz && set TELEGRAM_CHAT_ID=-1001234567890")
        return 1

    if ":" not in TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN looks invalid (expected '<digits>:<secret>').")
        print("  Telegram returns HTTP 404 for every call when the token is a placeholder.")
        print("  Put the real token from @BotFather in .env, e.g. TELEGRAM_BOT_TOKEN=123456789:AA...")
        return 4

    ou_body = read_telegram_section(OU_FILE)
    btts_body = read_telegram_section(BTTS_FILE)
    hw_body = read_telegram_section(HW_FILE)
    oo05_body = read_telegram_section(OO05_FILE)

    msg = build_daily_message(DATE, ou_body, btts_body, hw_body, oo05_body)

    if not msg or msg.strip() == "":
        print("ERROR: Assembled Telegram message is empty.")
        print("  Check that at least one of these files exists and has content:")
        print(f"    {OU_FILE}, {BTTS_FILE}, {HW_FILE}, {OO05_FILE}")
        return 2

    ok = _send(TOKEN, CHAT, msg, "FREE")

    if VIP:
        vip_msg = build_vip_board(DATE, ou_body, btts_body, hw_body, oo05_body)
        ok = _send(TOKEN, VIP, vip_msg, "VIP", as_pre=True) and ok

    if not ok:
        print("HINT: HTTP 404 = bad/placeholder TELEGRAM_BOT_TOKEN; "
              "HTTP 400 'chat not found' = bot is not a member of the chat.")

    return 0 if ok else 3


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
