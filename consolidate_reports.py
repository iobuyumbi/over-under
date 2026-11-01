#!/usr/bin/env python3
"""Consolidate dated report artifacts into one tracked archive.

The pipeline emits one dated file per report (weekly/monthly performance
reports, per-market VIP reports, per-market JSON reports). Over time these
pile up in the working tree -- and because they are gitignored, the GitHub
Actions ``git add weekly_report_*.txt`` step fails and nothing ever commits.

This script fixes that by keeping a single tracked file instead:

  1. Every *performance* report (``weekly_report_*.txt`` /
     ``monthly_report_*.txt``) is archived into ``reports_history.md``
     verbatim, newest first, de-duplicated by a stable
     ``<!-- report: ... -->`` marker.
  2. Every per-market VIP text report is reduced to a compact one-line-per-pick
     digest for its own date and archived the same way -- so the dated pick
     history survives without the ~200 KB/day of full report text.
  3. Every dated report artifact it recognises (performance reports, VIP text
     reports, per-market JSON) is deleted from the tree.

It is idempotent: a second run reports "already archived" and deletes nothing
new. Deliberately does NOT touch ``reports_history.md`` itself.

Usage:
    python consolidate_reports.py             # archive + prune
    python consolidate_reports.py --dry-run   # show what would change
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONSOLIDATED = ROOT / "reports_history.md"

# (kind, glob): the text content of these is archived into reports_history.md.
INGEST = (
    ("weekly", "weekly_report_*.txt"),
    ("monthly", "monthly_report_*.txt"),
)

# Every dated report artifact that is pruned once archived. Regenerable
# pipeline output (VIP reports, per-market JSON) is deleted, not archived --
# the durable record for those lives in prediction_history.json.
DELETE_GLOBS = (
    "weekly_report_*.txt",
    "weekly_report_*.json",
    "monthly_report_*.txt",
    "monthly_report_*.json",
    "*_vip_report_*.txt",
    "*_report_*.json",
)

# (glob, label): the four per-market VIP text reports. Each is reduced to a
# compact one-line-per-pick digest (kept in ``reports_history.md``) and then
# pruned with the rest. Order here is the digest display order.
VIP_REPORTS = (
    ("over_under_vip_report_*.txt", "Over/Under 2.5"),
    ("btts_vip_report_*.txt", "BTTS"),
    ("home_win_vip_report_*.txt", "Home Win"),
    ("over05_team_goal_vip_report_*.txt", "Team Goals Over 0.5"),
)

PREAMBLE = (
    "# Report History\n\n"
    "Consolidated archive of generated reports (newest first): weekly/monthly\n"
    "performance reports plus a compact per-day digest of the market picks.\n"
    "Maintained automatically by `consolidate_reports.py` -- do not edit by hand.\n"
)

# Per-pick fields inside a VIP text report (all four markets share this layout).
_FIXTURE_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$")
_VIP_DATE_RE = re.compile(r"^\s+Date:\s*(\d{4}-\d{2}-\d{2})\s*$")
_VIP_LEAGUE_RE = re.compile(r"^\s+League:\s*(.+?)\s*$")
_VIP_PICK_RE = re.compile(
    r"^\s+Pick:\s+(.+?)\s*·\s*(High|Medium|Low)\s+confidence\s+\((\d+(?:\.\d+)?)\%\)"
)
_VIP_TIER_RE = re.compile(r"•\s*Tier:\s*(.+?)\s*$")
_TIER_ICON = {"Premium": "🔥", "Solid": "✅", "Watchlist": "👀"}

SECTION_SPLIT_RE = re.compile(r"<!-- report: (.+?) -->\n")
DATE_RE = re.compile(r"(\d{4}-\d{2}(?:-\d{2})?)")


def _date_of(name: str) -> str:
    match = DATE_RE.search(name)
    return match.group(1) if match else "0000-00-00"


def _existing_sections(text: str) -> dict:
    """Parse a previously written archive back into {key: body}."""
    parts = SECTION_SPLIT_RE.split(text)
    sections = {}
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].strip()] = parts[i + 1].strip("\n")
    return sections


def _render(key: str, body: str) -> str:
    return f"<!-- report: {key} -->\n{body.rstrip()}\n"


def _sort_key(key: str):
    kind, _, date = key.partition(" ")
    return (date, kind)


def _vip_digest_entries(path: Path, report_date: str) -> list:
    """Return one compact line per pick in ``path`` whose date is ``report_date``.

    Each market report covers a multi-day window, so only picks dated the
    report's own day are kept -- that keeps the digest small and stops future
    picks being duplicated in the next day's report.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        print(f"  ! could not read {path.name}: {exc}")
        return []

    entries: list = []
    pick: dict = {}

    def flush():
        if pick.get("date") != report_date or not pick.get("pick"):
            return
        icon = _TIER_ICON.get(pick.get("tier", ""), "•")
        league = f" ({pick['league']})" if pick.get("league") else ""
        entries.append(
            f"{icon} {pick['fixture']}{league} — {pick['pick']} · "
            f"{pick['label']} {pick['pct']:.1f}%"
        )

    for line in lines:
        fixture = _FIXTURE_RE.match(line)
        if fixture:
            flush()
            pick = {"fixture": fixture.group(1)}
            continue
        if not pick:
            continue
        m = _VIP_DATE_RE.match(line) or _VIP_LEAGUE_RE.match(line)
        if m and line.strip().startswith("Date:"):
            pick["date"] = m.group(1)
            continue
        if m:
            pick["league"] = m.group(1)
            continue
        m = _VIP_PICK_RE.match(line)
        if m:
            pick["pick"], pick["label"] = m.group(1), m.group(2)
            pick["pct"] = float(m.group(3))
            continue
        m = _VIP_TIER_RE.search(line)
        if m:
            tier = m.group(1)
            pick["tier"] = "Premium" if "Premium" in tier else (
                "Watchlist" if "Watchlist" in tier else "Solid"
            )
    flush()
    return entries


def _render_daily_digest(date: str, markets: dict) -> str:
    out = [f"## Daily Picks — {date}", ""]
    for _, label in VIP_REPORTS:
        entries = markets.get(label)
        if not entries:
            continue
        out.append(f"### {label}  ({len(entries)})")
        out.extend(f"  {entry}" for entry in entries)
        out.append("")
    return "\n".join(out).rstrip()


def _discover() -> tuple:
    """Return (archive_text, ingested_files, already_archived, to_delete)."""
    sections = {}
    if CONSOLIDATED.exists():
        sections = _existing_sections(CONSOLIDATED.read_text(encoding="utf-8"))

    ingested, already = [], []
    for kind, pattern in INGEST:
        paths = sorted(ROOT.glob(pattern), key=lambda p: _date_of(p.name), reverse=True)
        for path in paths:
            key = f"{kind} {_date_of(path.name)}"
            if key in sections:
                already.append(path.name)
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"  ! could not read {path.name}: {exc}")
                continue
            title = "Weekly Report" if kind == "weekly" else "Monthly Report"
            sections[key] = f"## {title} \u2014 {_date_of(path.name)}\n\n{body.strip()}"
            ingested.append(path.name)

    # Per-market VIP reports -> one compact dated digest per report date.
    digests: dict = {}
    for pattern, label in VIP_REPORTS:
        for path in ROOT.glob(pattern):
            day = _date_of(path.name)
            if day == "0000-00-00":
                continue
            entries = _vip_digest_entries(path, day)
            if entries:
                digests.setdefault(day, {})[label] = entries

    for day in sorted(digests, reverse=True):
        key = f"daily {day}"
        if key in sections:
            already.append(key)
            continue
        sections[key] = _render_daily_digest(day, digests[day])
        ingested.append(f"daily digest {day}")

    to_delete, seen = [], set()
    for pattern in DELETE_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if path.name == CONSOLIDATED.name or path in seen:
                continue
            seen.add(path)
            to_delete.append(path)

    ordered = sorted(sections, key=_sort_key, reverse=True)
    archive = PREAMBLE + "\n" + "\n\n".join(_render(k, sections[k]) for k in ordered)
    return archive, ingested, already, to_delete


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Consolidate dated report files.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without touching any file.",
    )
    args = parser.parse_args(argv)

    archive, ingested, already, to_delete = _discover()

    print(f"Ingested {len(ingested)} new report(s); {len(already)} already archived.")
    for name in ingested:
        print(f"  + {name}")
    print(f"Pruning {len(to_delete)} dated report file(s):")
    for path in to_delete:
        print(f"  - {path.name}")

    if args.dry_run:
        print("\n[dry-run] no files changed.")
        return 0

    CONSOLIDATED.write_text(archive, encoding="utf-8")
    failures = 0
    for path in to_delete:
        try:
            path.unlink()
        except OSError as exc:
            failures += 1
            print(f"  ! could not delete {path.name}: {exc}")

    print(f"\nArchive written: {CONSOLIDATED.name}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
