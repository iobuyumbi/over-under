#!/usr/bin/env python3
"""Build one compact Telegram message from predictor TELEGRAM markers."""

import argparse
import re
import sys
import textwrap
from datetime import datetime

from prediction_tracker import build_telegram_yesterday_block


def extract_marker(text, start, end):
    pattern = re.compile(re.escape(start) + r"(.*?)" + re.escape(end), re.DOTALL)
    match = pattern.search(text or "")
    return match.group(1).strip() if match else ""


def read_telegram_section(path):
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        extracted = extract_marker(content, "===TELEGRAM_START===", "===TELEGRAM_END===")
        return extracted if extracted else content.strip()
    except OSError:
        return ""


def _clean_body(body):
    if not body:
        return ""
    cleaned_lines = []
    for raw in (body or "").splitlines():
        cleaned_lines.append(raw.rstrip())
    while cleaned_lines and not cleaned_lines[0].strip():
        cleaned_lines.pop(0)
    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()
    return "\n".join(cleaned_lines)


def build_daily_message(date, ou_body, btts_body, hw_body):
    lines = [f"📅 Daily Soccer Picks · {date}", ""]

    yesterday = build_telegram_yesterday_block()
    if yesterday:
        lines.append("───────────")
        lines.append(f"📊 {yesterday[0]}")
        lines.append("")
        for line in yesterday[1:]:
            lines.append(line)

    sections = [
        ("⚽️ OVER / UNDER 2.5", ou_body),
        ("🎯 BTTS (YES / NO)", btts_body),
        ("🏠 HOME WIN", hw_body),
    ]
    for title, body in sections:
        clean = _clean_body(body) or "— none"
        lines.append("")
        lines.append("───────────")
        lines.append(title)
        lines.append("")
        lines.append(clean)

    lines.append("")
    lines.append("───────────")
    lines.append("Info only · gamble responsibly")
    return "\n".join(lines).strip()


def main():
    parser = argparse.ArgumentParser(description="Build compact Telegram daily message")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--ou-output", default="ou_telegram.txt")
    parser.add_argument("--btts-output", default="btts_telegram.txt")
    parser.add_argument("--hw-output", default="hw_telegram.txt")
    parser.add_argument("--out", default="telegram_daily.txt")
    args = parser.parse_args()

    ou = read_telegram_section(args.ou_output)
    btts = read_telegram_section(args.btts_output)
    hw = read_telegram_section(args.hw_output)
    message = build_daily_message(args.date, ou, btts, hw)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(message)
        f.write("\n")

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
