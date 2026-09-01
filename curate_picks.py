from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

WEAK_FLAG_SUBSTRINGS = [
    "weak-ROI region",
    "poor league win rate on this market",
    "weak ROI history on this market",
]

CONSERVATIVE_LEAGUE_KEYWORDS = [
    "MLS", "Mexican", "Bulgarian", "Belarusian", "Brazilian Serie C",
    "Brazilian Serie D", "Ecuador Liga Pro", "Japanese J1", "Japanese J2",
    "Korean", "Swedish Superettan", "Ryman", "Southern League",
    "Unibond", "Conference North", "Conference South",
]

TIER_PREMIUM = "Premium"
TIER_SOLID = "Solid"
TIER_WATCHLIST = "Watchlist"


@dataclass
class Pick:
    fixture: str
    date: str
    league: str
    market: str
    pick: str
    confidence_pct: float
    confidence_label: str
    tier: str
    xg_home: float | None = None
    xg_away: float | None = None
    stake_pct: str = ""
    ev_per_dollar: str = ""
    has_weak_flag: bool = False
    has_thin_data: bool = False
    profile_checks: str = ""
    notes: list[str] = field(default_factory=list)
    reject_reason: str | None = None


def _match_fixture_line(line: str) -> str | None:
    m = re.match(r"^\s*\d+\.\s+(.+?)\s*$", line)
    return m.group(1).strip() if m else None


def _kv(line: str, key: str) -> str | None:
    m = re.match(r"^\s+" + re.escape(key) + r":\s*(.+?)\s*$", line)
    return m.group(1) if m else None


def _parse_pick_line(line: str) -> tuple[str, str, float, str] | None:
    m = re.match(
        r"^\s+Pick:\s+(.+?)\s*·\s*(High|Medium|Low)\s+confidence\s+\((\d+(?:\.\d+)?)\%\)",
        line,
    )
    if not m:
        return None
    pick, label, pct = m.group(1), m.group(2), float(m.group(3))
    market = "Over/Under"
    if pick.startswith("Over ") or pick.startswith("Under "):
        market = "Over/Under"
    elif pick.startswith("BTTS"):
        market = "BTTS"
    elif pick == "Home Win":
        market = "Home Win"
    return market, pick, pct, label


def _parse_xg(line: str) -> tuple[float | None, float | None]:
    m = re.match(r"^\s+xG forecast\s+—\s+(\d+(?:\.\d+)?)–(\d+(?:\.\d+)?)", line)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def _parse_tier(category_section_lines: list[str]) -> tuple[str, bool, bool]:
    tier = TIER_SOLID
    weak = False
    thin = False
    for ln in category_section_lines:
        tm = re.search(r"•\s*Tier:\s*(.+?)\s*$", ln)
        if tm:
            tstr = tm.group(1)
            if "Premium" in tstr:
                tier = TIER_PREMIUM
            elif "Watchlist" in tstr:
                tier = TIER_WATCHLIST
            else:
                tier = TIER_SOLID
        for fs in WEAK_FLAG_SUBSTRINGS:
            if fs in ln:
                weak = True
        if "THIN" in ln or "thin" in ln.lower():
            thin = True
    return tier, weak, thin


def parse_vip_report(path: Path, report_type: str) -> list[Pick]:
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    picks: list[Pick] = []
    i = 0
    n = len(text)
    in_today = False
    while i < n:
        line = text[i]
        if line.startswith("📅 TODAY"):
            in_today = True
            i += 1
            continue
        if in_today and (line.startswith("📌 YESTERDAY") or line.startswith("---") and i > 0 and "TOTAL PICKS" in " ".join(text[i:i+8])):
            in_today = False
            i += 1
            continue
        fixture = _match_fixture_line(line) if in_today else None
        if fixture:
            j = i + 1
            block = []
            while j < n and not _match_fixture_line(text[j]):
                nxt = text[j]
                if (nxt.strip().startswith("🔥") or nxt.strip().startswith("✅") or nxt.strip().startswith("👀")) and "picks" in nxt and _match_fixture_line(text[j]) is None:
                    pass
                if (nxt.startswith("┌") or nxt.startswith("│  OVER") or nxt.startswith("│  UNDER") or
                    nxt.startswith("│  BTTS") or nxt.startswith("│  HOME") or nxt.startswith("└") or
                    nxt.strip().startswith("---") or nxt.startswith("⚽️") or
                    nxt.startswith("🟢") or nxt.startswith("🔵") or
                    nxt.startswith("🏠") or nxt.startswith("⚽️ BTTS") or
                    nxt.startswith("📅 TODAY")):
                    break
                block.append(nxt)
                j += 1
            date = league = pick = market = tier = conf_label = profile = stake = ev = ""
            conf_pct = 0.0
            xgh = xga = None
            category_lines: list[str] = []
            notes: list[str] = []
            in_cat = False
            for bl in block:
                d = _kv(bl, "Date")
                if d is not None:
                    date = d; in_cat = False; continue
                lg = _kv(bl, "League")
                if lg is not None:
                    league = lg; in_cat = False; continue
                parsed = _parse_pick_line(bl)
                if parsed is not None:
                    market, pick, conf_pct, conf_label = parsed
                    in_cat = False
                    continue
                if bl.strip().startswith("Category:"):
                    in_cat = True
                    continue
                if in_cat and bl.strip().startswith("•"):
                    category_lines.append(bl)
                    continue
                if bl.strip().startswith("Suggested stake:"):
                    in_cat = False
                    stake = bl.split("Suggested stake:", 1)[1].strip()
                    continue
                if bl.strip().startswith("Value — EV:"):
                    ev = bl.split("Value — EV:", 1)[1].strip()
                    continue
                if bl.strip().startswith("xG"):
                    xgh, xga = _parse_xg(bl)
                    continue
                if bl.strip().startswith("Profile:") or bl.strip().startswith("Team strength"):
                    profile = bl.strip()
                    continue
                if bl.strip().startswith("Missed:") or bl.strip().startswith("H2H note:"):
                    notes.append(bl.strip())
            tier, weak_flag, thin_flag = _parse_tier(category_lines)
            if not date or not league or not pick:
                i = j
                continue
            p = Pick(
                fixture=fixture,
                date=date,
                league=league,
                market=market,
                pick=pick,
                confidence_pct=conf_pct,
                confidence_label=conf_label,
                tier=tier,
                xg_home=xgh,
                xg_away=xga,
                stake_pct=stake,
                ev_per_dollar=ev,
                has_weak_flag=weak_flag,
                has_thin_data=thin_flag,
                profile_checks=profile,
                notes=notes,
            )
            picks.append(p)
            i = j
        else:
            i += 1
    return picks


def filter_pick(p: Pick, today: str, rules: dict) -> str | None:
    if p.date != today:
        return f"not today ({p.date})"
    if p.tier == TIER_WATCHLIST and not rules.get("allow_watchlist", False):
        return f"tier Watchlist"
    if p.has_weak_flag and not rules.get("allow_weak_league", False):
        return "weak-ROI / poor-league-win-rate flag"
    if p.confidence_label != "High" and not rules.get("allow_medium_conf", False):
        return f"confidence label {p.confidence_label}"
    min_pct_map = rules.get("min_pct", {})
    market_min = min_pct_map.get(p.market, 0)
    if p.market == "BTTS" and p.pick.strip().lower().startswith("btts no"):
        market_min = rules.get("min_pct_btts_no", 68.0)
    if market_min and p.confidence_pct < market_min:
        return f"confidence {p.confidence_pct:.1f}% < {market_min}%"
    if p.market == "Over/Under" and p.pick.startswith("Over "):
        if p.xg_home is not None and p.xg_away is not None:
            if (p.xg_home + p.xg_away) < rules.get("ou_over_min_xg_sum", 2.5):
                return f"xG sum {p.xg_home+p.xg_away:.2f} < {rules['ou_over_min_xg_sum']}"
    if rules.get("reject_conservative_leagues", True):
        low = p.league.lower()
        for kw in CONSERVATIVE_LEAGUE_KEYWORDS:
            if kw.lower() in low:
                return f"conservative league keyword: {kw}"
    return None


def format_pick_line(idx: int, p: Pick) -> str:
    xg = ""
    if p.xg_home is not None and p.xg_away is not None:
        xg = f" · xG {p.xg_home:.2f}–{p.xg_away:.2f}"
    tier_icon = {"Premium": "🔥", "Solid": "✅", "Watchlist": "👀"}.get(p.tier, "•")
    return (
        f"  {idx:>2}. {tier_icon} {p.fixture}  ({p.league})\n"
        f"      {p.pick} · {p.confidence_label} {p.confidence_pct:.1f}%{xg}\n"
        f"      Stake {p.stake_pct}  ·  EV {p.ev_per_dollar}"
    )


DEFAULT_RULES: dict = {
    "allow_watchlist": False,
    "allow_weak_league": False,
    "allow_medium_conf": False,
    "reject_conservative_leagues": True,
    "ou_over_min_xg_sum": 2.5,
    "min_pct": {
        "Over/Under": 70.0,
        "BTTS": 70.0,
        "Home Win": 85.0,
    },
}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Curate picks from VIP reports mirroring the 2026-08-30 28.56-odds winning filter")
    ap.add_argument("--date", help="Publish date (today) to filter picks against; default now-local")
    ap.add_argument("--ou-report", help="Path to Over/Under VIP report txt")
    ap.add_argument("--btts-report", help="Path to BTTS VIP report txt")
    ap.add_argument("--hw-report", help="Path to Home Win VIP report txt")
    ap.add_argument("--out-txt", default="curated.txt", help="Human output file")
    ap.add_argument("--out-json", default="curated.json", help="JSON output file")
    args = ap.parse_args(argv)

    today = args.date
    if not today:
        today = datetime.now().strftime("%Y-%m-%d")

    def _find(candidate: str | None, pattern_glob: str) -> Path | None:
        if candidate and Path(candidate).exists():
            return Path(candidate)
        base = Path.cwd()
        dated = list(base.glob(pattern_glob.replace("*", today)))
        if dated:
            return sorted(dated, key=lambda p: p.stat().st_mtime)[-1]
        anyf = list(base.glob(pattern_glob))
        if anyf:
            return sorted(anyf, key=lambda p: p.stat().st_mtime)[-1]
        return None

    ou_path = _find(args.ou_report, f"over_under_vip_report_{today}.txt")
    btts_path = _find(args.btts_report, f"btts_vip_report_{today}.txt")
    hw_path = _find(args.hw_report, f"home_win_vip_report_{today}.txt")

    rules = dict(DEFAULT_RULES)

    parsed: list[Pick] = []
    if ou_path:
        parsed += parse_vip_report(ou_path, "ou")
    if btts_path:
        parsed += parse_vip_report(btts_path, "btts")
    if hw_path:
        parsed += parse_vip_report(hw_path, "hw")

    accepted: list[Pick] = []
    rejected: list[Pick] = []
    for p in parsed:
        reason = filter_pick(p, today, rules)
        if reason is None:
            accepted.append(p)
        else:
            p.reject_reason = reason
            rejected.append(p)

    by_market: dict[str, list[Pick]] = {}
    for p in accepted:
        by_market.setdefault(p.market, []).append(p)

    sections = []
    sections.append(f"✦ CURATED PICKS · {today}")
    sections.append(f"  Input reports:")
    if ou_path: sections.append(f"    OU   → {ou_path.name}")
    if btts_path: sections.append(f"    BTTS → {btts_path.name}")
    if hw_path: sections.append(f"    HW   → {hw_path.name}")
    sections.append("")
    sections.append("  Calibrated from the 30/08/2026 8/8 winning 28.56-odds accumulator:")
    sections.append(f"    • Tier: Premium/Solid only (Watchlist dropped)")
    sections.append(f"    • Label: High confidence only")
    sections.append(f"    • Min %: Over/Under ≥70, BTTS Yes ≥70, BTTS No ≥68, Home Win ≥85")
    sections.append(f"    • Weak-ROI / poor-win-rate leagues → blocked")
    sections.append(f"    • Over 2.5 min xG sum ≥ {rules['ou_over_min_xg_sum']}")
    sections.append(f"    • Conservative league keywords (MLS / Mexican / Bulgarian / Conference N&S etc.) → blocked")
    sections.append(f"    • Today only ({today})")
    sections.append("")
    total = 0
    for market, label in [
        ("Over/Under", "OVER / UNDER 2.5"),
        ("BTTS", "BTTS (Yes / No)"),
        ("Home Win", "HOME WIN"),
    ]:
        items = sorted(by_market.get(market, []), key=lambda p: -p.confidence_pct)
        sections.append(f"━━━ {label}  ({len(items)} accepted) ━━━")
        if not items:
            sections.append("  (none)")
        for idx, p in enumerate(items, 1):
            sections.append(format_pick_line(idx, p))
        total += len(items)
        sections.append("")

    sections.append(f"📊 Summary: {total} curated picks")
    sections.append(f"   Parsed total: {len(parsed)}   Rejected: {len(rejected)}   Accepted: {len(accepted)}")
    sections.append("")
    sections.append("---")
    sections.append("For informational purposes only · Gamble responsibly")

    out_txt = "\n".join(sections) + "\n"
    Path(args.out_txt).write_text(out_txt, encoding="utf-8")

    data = {
        "date": today,
        "rules": {
            **{k: v for k, v in rules.items() if k != "min_pct"},
            "min_pct": rules["min_pct"],
            "weak_flag_substrings": WEAK_FLAG_SUBSTRINGS,
            "conservative_league_keywords": CONSERVATIVE_LEAGUE_KEYWORDS,
        },
        "counts": {
            "parsed": len(parsed),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "accepted_by_market": {m: len(by_market.get(m, [])) for m in ["Over/Under", "BTTS", "Home Win"]},
        },
        "accepted": [asdict(p) for p in accepted],
        "rejected_top": [asdict(p) for p in sorted(rejected, key=lambda x: -x.confidence_pct)[:50]],
    }
    Path(args.out_json).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(out_txt)
    return 0


if __name__ == "__main__":
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", newline="")
    sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", newline="")
    sys.exit(main(sys.argv[1:]))
