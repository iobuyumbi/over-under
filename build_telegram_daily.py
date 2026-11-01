#!/usr/bin/env python3
"""Build the Telegram messages from predictor TELEGRAM markers.

Two messages are produced:

* the free-tier daily message (``build_daily_message``) -- the raw per-market
  sections, forwarded as-is; and
* the VIP board (``build_vip_board``) -- every fixture, split by market, then by
  side within the market (Over vs Under, BTTS Yes vs No, home vs away goal),
  then grouped by confidence level, all rendered as monospace tables, with a
  yesterday results table split the same way (per-market-side W-L-P and win
  rate) prepended when the caller supplies the breakdown.
"""

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


def build_daily_message(date, ou_body, btts_body, hw_body, oo05_body):
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
        ("🎯 OVER 0.5 TEAM GOAL", oo05_body),
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


_FIXTURE_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$")
_LEAGUE_RE = re.compile(r"^\s+League:\s*(.+?)\s*$")
_PICK_RE = re.compile(
    r"^\s+Pick:\s+(.+?)\s*·\s*(High|Medium|Low)\s+confidence\s+\((\d+(?:\.\d+)?)\%\)\s*$"
)
_TIER_ICON = {"Premium": "🔥", "Solid": "✅", "Watchlist": "👀"}
_CONF_GROUPS = (
    ("🟢 HIGH CONFIDENCE", "High"),
    ("🟡 MEDIUM CONFIDENCE", "Medium"),
    ("🔵 LOW CONFIDENCE", "Low"),
)
_MARKETS = ("Over/Under 2.5", "BTTS", "Home Win", "Over 0.5 Team Goal")
_MARKET_TITLES = {
    "Over/Under 2.5": "⚽️ OVER / UNDER 2.5",
    "BTTS": "🎯 BTTS (YES / NO)",
    "Home Win": "🏠 HOME WIN",
    "Over 0.5 Team Goal": "🎯 OVER 0.5 TEAM GOAL",
}
# Side within a market, so Over is never mixed with Under, Yes with No, or the
# home-team goal with the away-team goal.
_VARIANT_ORDER = {
    "Over/Under 2.5": ("Over 2.5", "Under 2.5"),
    "BTTS": ("BTTS Yes", "BTTS No"),
    "Over 0.5 Team Goal": ("Home to Score", "Away to Score"),
    "Home Win": (),
}
_VARIANT_TITLES = {
    "Over 2.5": "⬆️ OVER 2.5",
    "Under 2.5": "⬇️ UNDER 2.5",
    "BTTS Yes": "✅ BTTS YES",
    "BTTS No": "🚫 BTTS NO",
    "Home to Score": "🏠 HOME TO SCORE",
    "Away to Score": "✈️ AWAY TO SCORE",
}


def _variant_label(market, pick):
    text = (pick or "").lower()
    if market == "Over/Under 2.5":
        return "Over 2.5" if text.startswith("over") else "Under 2.5"
    if market == "BTTS":
        return "BTTS Yes" if "yes" in text else "BTTS No"
    if market == "Over 0.5 Team Goal":
        return "Away to Score" if "away" in text else "Home to Score"
    return ""


def parse_telegram_section(body, market):
    """Parse a per-market Telegram section into one dict per fixture.

    All four engines emit the same block layout::

          1. Amedspor vs Eyupspor
             League: Turkish Super Lig
             Pick: Home Win · High confidence (90.7%)
             Category:
               • Tier: 🔥 Premium
    """
    picks = []
    current = None
    for raw in (body or "").splitlines():
        line = raw.rstrip()
        fixture = _FIXTURE_RE.match(line)
        if fixture:
            if current:
                picks.append(current)
            current = {
                "fixture": fixture.group(1).strip(),
                "market": market,
                "league": "",
                "pick": "",
                "label": "",
                "pct": 0.0,
                "tier": "Solid",
                "caution": False,
            }
            continue
        if current is None:
            continue
        league = _LEAGUE_RE.match(line)
        if league:
            current["league"] = league.group(1).strip()
            continue
        pick = _PICK_RE.match(line)
        if pick:
            current["pick"] = pick.group(1).strip()
            current["label"] = pick.group(2)
            current["pct"] = float(pick.group(3))
            continue
        if "• Tier:" in line:
            if "Premium" in line:
                current["tier"] = "Premium"
            elif "Watchlist" in line:
                current["tier"] = "Watchlist"
            else:
                current["tier"] = "Solid"
            continue
        if "weak-ROI" in line or "weak ROI" in line or "Auto-flag" in line:
            current["caution"] = True
    if current:
        picks.append(current)
    return [p for p in picks if p["pick"]]


def _clip(text, width):
    text = str(text)
    return text if len(text) <= width else text[: width - 1] + "…"


def _render_table(rows, headers, caps):
    """Fixed-width, space-aligned table (rendered inside a <pre> block)."""
    clipped = [[_clip(c, caps[i]) for i, c in enumerate(r)] for r in rows]
    widths = [len(h) for h in headers]
    for r in clipped:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))

    def render(cols):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)).rstrip()

    head = render(headers)
    out = [head, "-" * len(head)]
    out.extend(render(r) for r in clipped)
    return out


def _render_conf_groups(subset, headers, caps):
    """HIGH/MEDIUM/LOW blocks (each its own table) for one market or side."""
    lines = []
    for title, label in _CONF_GROUPS:
        group = [p for p in subset if p["label"] == label]
        if not group:
            continue
        group.sort(key=lambda p: (-p["pct"], p["fixture"]))
        lines.append(f"━━━ {title}  ({len(group)}) ━━━")
        rows = []
        for idx, p in enumerate(group, 1):
            # The tier icon lives in the trailing column: emoji occupy two
            # display columns but count as one char, so keeping them last
            # stops the width mismatch from misaligning earlier columns.
            icon = _TIER_ICON.get(p["tier"], "•")
            rows.append([
                str(idx),
                p["fixture"],
                p["league"],
                f"{p['pct']:.1f}%",
                icon + ("⚠" if p["caution"] else ""),
            ])
        lines.extend(_render_table(rows, headers, caps))
        lines.append("")
    return lines


def _render_results_block(rows):
    """Yesterday's record per market side, as a monospace table.

    Emoji stay out of the rows (only the block header uses them): an emoji
    occupies two display columns but counts as one character, which would
    break the alignment of the fixed-width table.
    """
    if not rows:
        return ["— no settled picks yesterday", ""]

    headers = ["Market / Side", "W", "L", "P", "Pend", "Win%"]
    caps = [16, 3, 3, 3, 4, 6]
    table = [
        [
            row["label"],
            str(row["wins"]),
            str(row["losses"]),
            str(row["pushes"]),
            str(row["pending"]),
            "-" if row["win_rate"] is None else f"{row['win_rate']:.1f}%",
        ]
        for row in rows
    ]

    wins = sum(r["wins"] for r in rows)
    losses = sum(r["losses"] for r in rows)
    pushes = sum(r["pushes"] for r in rows)
    pending = sum(r["pending"] for r in rows)
    decided = wins + losses
    table.append([
        "TOTAL",
        str(wins),
        str(losses),
        str(pushes),
        str(pending),
        "-" if not decided else f"{100.0 * wins / decided:.1f}%",
    ])

    lines = _render_table(table, headers, caps)
    lines.append("")
    lines.append("W-L-P = win-loss-push (void) · Pend awaits settlement · Win% = W/(W+L)")
    lines.append("")
    return lines


def build_vip_board(date, ou_body, btts_body, hw_body, oo05_body, results=None):
    """Every fixture, split by market, then by side, then by confidence level.

    Markets are never blended, and neither are the sides inside a market:
    Over is listed apart from Under, BTTS Yes apart from BTTS No, and the
    home-team goal apart from the away-team goal.

    Pass ``results`` (the ``(yesterday_date, rows)`` tuple from
    ``prediction_tracker.get_yesterday_market_breakdown()``) to prepend a
    yesterday block that splits the record the same way; when omitted the
    board is today's picks only.
    """
    picks = []
    picks += parse_telegram_section(ou_body, "Over/Under 2.5")
    picks += parse_telegram_section(btts_body, "BTTS")
    picks += parse_telegram_section(hw_body, "Home Win")
    picks += parse_telegram_section(oo05_body, "Over 0.5 Team Goal")

    counts = {}
    for p in picks:
        counts[p["market"]] = counts.get(p["market"], 0) + 1

    lines = [
        f"🏆 VIP BOARD · {date}",
        "Every fixture, split by market and side, grouped by confidence.",
        "",
        "📊 " + " · ".join(f"{m} {counts.get(m, 0)}" for m in _MARKETS),
        f"   Total: {len(picks)} picks",
        "",
        "Tier: 🔥 Premium · ✅ Solid · 👀 Watchlist    ⚠ weak-ROI caution",
        "",
    ]

    if results is not None:
        results_date, results_rows = results
        lines.append("═══════════════════════════")
        lines.append(f"📊 YESTERDAY · {results_date}")
        lines.append("═══════════════════════════")
        lines.extend(_render_results_block(results_rows))

    # The pick itself is dropped from the table: the market, side and
    # confidence headers already state it, so the columns stay narrow.
    headers = ["#", "Match", "League", "Conf", "Tier"]
    caps = [3, 32, 20, 7, 4]

    for market in _MARKETS:
        market_picks = [p for p in picks if p["market"] == market]
        lines.append("═══════════════════════════")
        lines.append(f"{_MARKET_TITLES[market]}  ({len(market_picks)})")
        lines.append("═══════════════════════════")
        if not market_picks:
            lines.append("— none")
            lines.append("")
            continue

        variants = _VARIANT_ORDER.get(market, ())
        if not variants:
            lines.extend(_render_conf_groups(market_picks, headers, caps))
            continue

        for variant in variants:
            subset = [p for p in market_picks if _variant_label(market, p["pick"]) == variant]
            if not subset:
                continue
            lines.append(f"── {_VARIANT_TITLES[variant]}  ({len(subset)}) ──")
            lines.extend(_render_conf_groups(subset, headers, caps))

    lines.append("───────────")
    lines.append("Info only · gamble responsibly")
    return "\n".join(lines).strip()


def split_pre_chunks(body, limit=3500):
    """Split a plain-text body into <=limit pieces on line boundaries.

    Used by the senders to wrap each piece in its own <pre> block so a
    multi-chunk monospace table stays aligned and valid HTML.
    """
    chunks = []
    current = []
    size = 0
    for line in (body or "").split("\n"):
        add = len(line) + 1
        if current and size + add > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += add
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def main():
    parser = argparse.ArgumentParser(description="Build compact Telegram daily message")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--ou-output", default="ou_telegram.txt")
    parser.add_argument("--btts-output", default="btts_telegram.txt")
    parser.add_argument("--hw-output", default="hw_telegram.txt")
    parser.add_argument("--oo05-output", default="oo05_telegram.txt")
    parser.add_argument("--out", default="telegram_daily.txt")
    args = parser.parse_args()

    ou = read_telegram_section(args.ou_output)
    btts = read_telegram_section(args.btts_output)
    hw = read_telegram_section(args.hw_output)
    oo05 = read_telegram_section(args.oo05_output)
    message = build_daily_message(args.date, ou, btts, hw, oo05)

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
