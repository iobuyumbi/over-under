#!/usr/bin/env python3
"""Build one compact Telegram message from predictor TELEGRAM markers."""

import argparse
import re
import sys
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
            return extract_marker(f.read(), "===TELEGRAM_START===", "===TELEGRAM_END===")
    except OSError:
        return ""


def build_daily_message(date, ou_body, btts_body, hw_body):
    lines = [f"📅 Picks · {date}"]

    yesterday = build_telegram_yesterday_block()
    if yesterday:
        lines.append("")
        lines.extend(yesterday)

    sections = [
        ("O/U 2.5", ou_body),
        ("BTTS", btts_body),
        ("Home Win", hw_body),
    ]
    for title, body in sections:
        lines.append("")
        lines.append(title)
        if body and body.strip() and body.strip() != "— none":
            lines.append(body.strip())
        else:
            lines.append("— none")

    lines.append("")
    lines.append("Info only · gamble responsibly")
    return "\n".join(lines).strip()


def main():
    parser = argparse.ArgumentParser(description="Build compact Telegram daily message")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--ou-output", default="output.txt")
    parser.add_argument("--btts-output", default="btts_output.txt")
    parser.add_argument("--hw-output", default="hw_output.txt")
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
