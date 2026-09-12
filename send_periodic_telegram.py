#!/usr/bin/env python3
"""GitHub Actions periodic publisher: read weekly/monthly report file and send to Telegram.

Intended to be called ONLY from the `weekly-report` / `monthly-report` jobs
in .github/workflows/run_daily.yml.  Those jobs settle results (light
fetch_results calls — no prediction engine scrapes) and then call this
script to post the generated weekly_report_YYYY-MM-DD.txt /
monthly_report_YYYY-MM-DD.txt files that are already committed.

Env vars:
    REPORT_FILE          - Path to the report txt to publish (passed by GHA step)
    PERIOD               - "weekly" or "monthly" — cosmetic, for logs only
    TELEGRAM_BOT_TOKEN   - Bot token (GitHub secret)
    TELEGRAM_CHAT_ID     - Free-tier channel ID (GitHub secret)
    TELEGRAM_VIP_CHAT_ID - VIP channel ID (GitHub secret, optional)
"""

import os
import sys
import requests


def _env(name, default=None):
    v = os.environ.get(name)
    return default if v in (None, "") else v


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return ""


def _send_chunks(token, chat_id, text, label="periodic"):
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


def main():
    token = _env("TELEGRAM_BOT_TOKEN")
    free_chat = _env("TELEGRAM_CHAT_ID")
    vip_chat = _env("TELEGRAM_VIP_CHAT_ID")
    report_file = _env("REPORT_FILE")
    period = (_env("PERIOD") or "periodic").lower()

    if not token or not free_chat:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required env vars.")
        print("       (Set them as GitHub repository secrets.)")
        return 1

    if not report_file:
        print(f"ERROR: No REPORT_FILE provided for {period} send step.")
        print("       (The generate_X_report.py step likely failed earlier.)")
        return 2

    body = _read(report_file)
    if not body or not body.strip():
        print(f"ERROR: Report file '{report_file}' is empty or unreadable.")
        return 2

    print(f"{period.capitalize()} report: {report_file} ({len(body)} chars)")
    header = f"━━━ {period.upper()} PERFORMANCE REPORT ━━━\n\n"
    msg = header + body

    ok = _send_chunks(token, free_chat, msg, label=f"{period}-FREE")
    if vip_chat:
        ok_vip = _send_chunks(token, vip_chat, msg, label=f"{period}-VIP")
        ok = ok and ok_vip
    return 0 if ok else 3


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
