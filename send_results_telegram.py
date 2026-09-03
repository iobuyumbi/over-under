import os
import sys
import requests


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
    max_len = 3900
    chunks = [message[i:i + max_len] for i in range(0, len(message), max_len)]

    for chat_id in filter(None, chats):
        for chunk in chunks:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": chunk},
                timeout=30,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
