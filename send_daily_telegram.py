#!/usr/bin/env python3
"""GitHub Actions daily publisher: read COMMITTED reports and send to Telegram.

Intended to be called ONLY from the `publish-daily` job in
.github/workflows/run_daily.yml.  The prediction engines NEVER run in
CI — run_local.bat runs them locally, commits all report artifacts
(ou_telegram.txt, ..., curated.json, date-stamped VIP .txt/.json reports),
and pushes to origin/main.  This CI script then just:

  1. Reads the already-committed report files from disk.
  2. Builds the free-tier message from the 4 per-market telegram sections
     (exactly the same way build_telegram_daily.py / send_local_telegram.py
     do locally — no duplicate assembly logic).
  3. Builds the VIP-tier message from the date-stamped VIP reports that
     run_local.bat produced.
  4. Posts each message to its corresponding chat ID via secrets env vars
     (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_VIP_CHAT_ID).

Env vars:
    DATE                 - YYYY-MM-DD publish date (to match VIP files)
    TELEGRAM_BOT_TOKEN   - Bot token (GitHub secret)
    TELEGRAM_CHAT_ID     - Free-tier channel ID (GitHub secret)
    TELEGRAM_VIP_CHAT_ID - VIP channel ID (GitHub secret, optional)
"""

import os
import sys
import glob
import requests

from build_telegram_daily import build_daily_message, read_telegram_section


def _env(name, default=None):
    v = os.environ.get(name)
    return default if v in (None, "") else v


def _send_chunks(token, chat_id, text, label="message"):
    if not text or not text.strip():
        print(f"[{label}] Empty, skipping chat {chat_id}.")
        return True
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
    ok = True
    for ci, chunk in enumerate(chunks, 1):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"},
                timeout=30,
            )
            if resp.status_code != 200:
                body = resp.text[:300]
                print(f"[{label}] chunk {ci}/{len(chunks)} chat {chat_id}: HTTP {resp.status_code} {body}")
                ok = False
            else:
                print(f"[{label}] chunk {ci}/{len(chunks)} chat {chat_id}: OK")
        except requests.RequestException as e:
            print(f"[{label}] chunk {ci}/{len(chunks)} chat {chat_id}: network error: {e}")
            ok = False
    return ok


def _latest(pattern):
    """Pick the newest file matching a glob (today's dated file if present)."""
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return files[0] if files else None


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return ""


def build_free_message(date):
    ou = read_telegram_section("ou_telegram.txt")
    btts = read_telegram_section("btts_telegram.txt")
    hw = read_telegram_section("hw_telegram.txt")
    oo05 = read_telegram_section("oo05_telegram.txt")
    return build_daily_message(date, ou, btts, hw, oo05)


def build_vip_message(date):
    parts = [f"DAILY PREDICTIONS (VIP) — {date}", ""]
    found_any = False
    for label, pattern in [
        ("OVER / UNDER 2.5", f"over_under_vip_report_{date}.txt"),
        ("BTTS (YES / NO)",   f"btts_vip_report_{date}.txt"),
        ("HOME WIN",          f"home_win_vip_report_{date}.txt"),
        ("OVER 0.5 TEAM GOAL", f"over05_team_goal_vip_report_{date}.txt"),
    ]:
        body = _read(pattern)
        parts.append(f"━━━ {label} ━━━")
        if body and body.strip():
            parts.append(body.strip())
            found_any = True
        else:
            parts.append("No qualifying VIP picks today.")
        parts.append("")
    if not found_any:
        # Fall back to latest dated VIP file regardless of date mismatch.
        for pattern in [
            "over_under_vip_report_*.txt", "btts_vip_report_*.txt",
            "home_win_vip_report_*.txt", "over05_team_goal_vip_report_*.txt",
        ]:
            latest = _latest(pattern)
            if latest:
                body = _read(latest)
                if body and body.strip():
                    parts.append(f"(fallback: {os.path.basename(latest)})")
                    parts.append(body.strip())
                    parts.append("")
                    found_any = True
    if not found_any:
        parts.append("No VIP reports available.")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


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
        ok_vip = _send_chunks(token, vip_chat, vip_msg, label="VIP")
        ok = ok and ok_vip

    return 0 if ok else 3


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
