import html
import os
import sys
import requests
from dotenv import load_dotenv

from build_telegram_daily import split_pre_chunks

load_dotenv()


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chats = [os.getenv("TELEGRAM_CHAT_ID"), os.getenv("TELEGRAM_VIP_CHAT_ID")]

    if not token or not any(chats):
        print("Skipping Telegram results summary: missing bot token or chat id.")
        return 0

    if os.path.exists("selected_results_report.txt"):
        with open("selected_results_report.txt", "r", encoding="utf-8", errors="replace") as f:
            report = f.read().strip()
    else:
        report = "No selected-match result report was generated."

    message = f"📊 Results\n\n{report}" if report else "📊 Results\n\nNo updates."

    # The results are space-aligned tables, so send each line-bounded chunk as
    # its own monospace <pre> block (same rendering as the daily board) to keep
    # the columns lined up.
    payloads = [(f"<pre>{html.escape(c)}</pre>", "HTML") for c in split_pre_chunks(message)]

    for chat_id in filter(None, chats):
        for chunk, parse_mode in payloads:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": "true",
                },
                timeout=30,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
