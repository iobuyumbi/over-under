#!/usr/bin/env python3
"""Consolidate dated report artifacts into one tracked archive.

The pipeline emits one dated file per report (weekly/monthly performance
reports, per-market VIP reports, per-market JSON reports). Over time these
pile up in the working tree -- and because they are gitignored, the GitHub
Actions ``git add weekly_report_*.txt`` step fails and nothing ever commits.

This script fixes that by keeping a single tracked file instead:

  1. Every *performance* report (``weekly_report_*.txt`` /
     ``monthly_report_*.txt``) is archived into ``reports_history.md``,
     newest first, de-duplicated by a stable ``<!-- report: ... -->`` marker.
  2. Every dated report artifact it recognises (performance reports plus the
     regenerable per-day VIP / JSON output) is deleted from the tree.

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

PREAMBLE = (
    "# Report History\n\n"
    "Consolidated archive of generated performance reports (newest first).\n"
    "Maintained automatically by `consolidate_reports.py` -- do not edit by hand.\n"
)

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
