import os
import sys
import requests


def send(token, chat_id, text):
    if not chat_id or not text:
        return
    max_len = 3900
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]
    for chunk in chunks:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": chunk},
            timeout=30,
        )


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    report_file = os.getenv("REPORT_FILE")
    if not token or not report_file or not os.path.exists(report_file):
        print("Skipping Telegram periodic report.")
        return 0
    with open(report_file, "r", encoding="utf-8") as f:
        report = f.read().strip()
    for chat_id in (os.getenv("TELEGRAM_CHAT_ID"), os.getenv("TELEGRAM_VIP_CHAT_ID")):
        send(token, chat_id, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
