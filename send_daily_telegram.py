import os
import sys
import requests


def read_file(path):
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def send(token, chat_id, text):
    if not chat_id or not text:
        return
    max_len = 3900
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]
    for chunk in chunks:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": chunk},
                timeout=30,
            )
            print(f"Sent chunk to {chat_id}: {r.status_code}")
        except Exception as e:
            print(f"Failed to send to {chat_id}: {e}")


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    free_channel = os.getenv("TELEGRAM_CHAT_ID")
    vip_channel = os.getenv("TELEGRAM_VIP_CHAT_ID")
    date = os.getenv("DATE", "today")

    if not token:
        print("FATAL: TELEGRAM_BOT_TOKEN missing")
        return 1
    if not free_channel:
        print("FATAL: TELEGRAM_CHAT_ID missing")
        return 1

    free_report = read_file("telegram_daily.txt") or read_file("combined_free.txt") or f"⚠️ No predictions generated for {date}."
    vip_report = read_file("combined_vip.txt") or f"⚠️ VIP report unavailable for {date}."

    print(f"Free report length: {len(free_report)}")
    print(f"VIP report length: {len(vip_report)}")

    send(token, free_channel, free_report)
    if vip_channel:
        send(token, vip_channel, vip_report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
