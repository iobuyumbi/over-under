#!/usr/bin/env python3
"""Send the assembled Telegram daily message to Telegram from a local run.

Usage:
  set TELEGRAM_BOT_TOKEN=xxx
  set TELEGRAM_CHAT_ID=yyy
  set TELEGRAM_VIP_CHAT_ID=zzz  (optional)
  set DATE=2026-08-30            (optional, defaults to today)
  python send_local_telegram.py

The script reads the three per-market Telegram section files
(ou_telegram.txt, btts_telegram.txt, hw_telegram.txt) that each
predictor writes, assembles them via build_telegram_daily.py, and
posts the assembled message to the configured chat IDs.

This is the local-only counterpart to the inline Telegram posting
that lives inside the GitHub Actions workflow (run_daily.yml).
"""

import os
import sys
import requests

from build_telegram_daily import build_daily_message, read_telegram_section


def _env_or(name, default=None):
    return os.getenv(name, default)


def main():
    TOKEN = _env_or("TELEGRAM_BOT_TOKEN")
    CHAT = _env_or("TELEGRAM_CHAT_ID")
    VIP = _env_or("TELEGRAM_VIP_CHAT_ID")
    DATE = _env_or("DATE") or __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    OU_FILE = _env_or("OU_TELEGRAM_FILE", "ou_telegram.txt")
    BTTS_FILE = _env_or("BTTS_TELEGRAM_FILE", "btts_telegram.txt")
    HW_FILE = _env_or("HW_TELEGRAM_FILE", "hw_telegram.txt")

    if not TOKEN or not CHAT:
        print("ERROR: Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars before running.")
        print("  Example (Windows cmd):  set TELEGRAM_BOT_TOKEN=123456:ABCxyz && set TELEGRAM_CHAT_ID=-1001234567890")
        return 1

    ou_body = read_telegram_section(OU_FILE)
    btts_body = read_telegram_section(BTTS_FILE)
    hw_body = read_telegram_section(HW_FILE)

    msg = build_daily_message(DATE, ou_body, btts_body, hw_body)

    if not msg or msg.strip() == "":
        print("ERROR: Assembled Telegram message is empty.")
        print("  Check that at least one of these files exists and has content:")
        print(f"    {OU_FILE}, {BTTS_FILE}, {HW_FILE}")
        return 2

    targets = [cid for cid in [CHAT, VIP] if cid]
    ok = True
    for cid in targets:
        chunks = [msg[i:i + 3900] for i in range(0, len(msg), 3900)]
        for ci, chunk in enumerate(chunks, 1):
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    data={"chat_id": cid, "text": chunk},
                    timeout=30,
                )
                if resp.status_code != 200:
                    print(f"  chunk {ci}/{len(chunks)} -> chat {cid}: HTTP {resp.status_code} {resp.text[:200]}")
                    ok = False
            except requests.RequestException as e:
                print(f"  chunk {ci}/{len(chunks)} -> chat {cid}: network error: {e}")
                ok = False
        print(f"Sent {len(chunks)} chunk(s) to chat {cid}")

    return 0 if ok else 3


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
