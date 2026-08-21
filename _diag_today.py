#!/usr/bin/env python3
"""Diagnose why today may have zero published picks."""
import json
from collections import Counter
from datetime import datetime

TODAY = datetime.now().strftime("%Y-%m-%d")

with open("prediction_history.json", encoding="utf-8") as f:
    h = json.load(f)

print(f"Today (local): {TODAY}\n")

for section in ("home_win", "over_under", "btts"):
    picks = [p for p in h.get(section, []) if str(p.get("date", ""))[:10] == TODAY]
    pub = [p for p in picks if p.get("published", True) is not False]
    track = [p for p in picks if p.get("published") is False]
    print(f"{section}: {len(picks)} recorded, {len(pub)} published, {len(track)} track-only")
    for p in pub[:5]:
        print(f"  PUB  {p.get('home_team')} vs {p.get('away_team')} [{p.get('confidence')}] {p.get('league')}")
    for p in track[:3]:
        print(f"  TRACK {p.get('home_team')} vs {p.get('away_team')} [{p.get('confidence')}] {p.get('league')}")

print("\n--- Running predictors (compact output) ---")
import subprocess
import sys

for script, label in [
    ("over25_soccerbase.py", "O/U"),
    ("btts_soccerbase.py", "BTTS"),
    ("home_win_soccerbase.py", "HW"),
]:
    print(f"\n{label}:")
    r = subprocess.run(
        [sys.executable, script, TODAY, "--days", "1"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    out = r.stdout or ""
    err = (r.stderr or "")[-800:]
    for marker in ("===TELEGRAM_START===", "===TELEGRAM_END===", "===EMAIL_START==="):
        pass
    import re
    tg = re.search(r"===TELEGRAM_START===(.*?)===TELEGRAM_END===", out, re.DOTALL)
    body = tg.group(1).strip() if tg else "(no telegram block)"
    print("  Telegram:", body[:400] if body else "empty")
    if "Processing" in out:
        for line in out.splitlines():
            if "Processing" in line or "Skipped" in line or "Recorded" in line:
                print(" ", line.strip())
    if r.returncode != 0:
        print("  EXIT", r.returncode)
    if err and ("Error" in err or "Traceback" in err):
        print("  STDERR:", err[-400:])
