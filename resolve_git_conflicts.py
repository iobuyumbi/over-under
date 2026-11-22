#!/usr/bin/env python3
"""Auto-resolve the conflict markers that `git pull --rebase --autostash`
leaves behind in the generated files.

`run_local.bat` pulls origin/main with --autostash before every commit+push.
When a file changed both locally (this run) and on origin (the CI commit), the
stash pop applies as a 3-way merge and can leave conflict markers in the
working tree while the pull still reports success. Committing those markers
corrupted prediction_history.json on 2026-11-07, so this helper runs first and
either merges the two sides or aborts cleanly (never leaving a half-resolved
tree that the next Telegram send would choke on).

Resolution rules:
  * prediction_history.json  union of both sides, keyed on the record identity,
                             preferring the settled / most recent copy.
  * manual_results.csv       union of both sides, keyed on date+home+away,
                             preferring a row that carries a score.
  * regenerable outputs      *_telegram.txt, curated.json, curated.txt,
                             telegram.txt, reports_history.md -> take one side.
  * anything else            left untouched so a human can look at it.

Exit codes:
  0  nothing to do (no conflicts found)
  1  unexpected error
  2  conflicts remain unresolved (caller must abort / ask a human)
  3  conflicts were found and successfully resolved
"""

import csv
import io
import json
import os
import subprocess
import sys

DATA_JSON = "prediction_history.json"
DATA_CSV = "manual_results.csv"
REGENERABLE = {
    "curated.json",
    "curated.txt",
    "telegram.txt",
    "reports_history.md",
    "btts_telegram.txt",
    "hw_telegram.txt",
    "oo05_telegram.txt",
    "ou_telegram.txt",
}

NO_CONFLICTS = 0
ERROR = 1
UNRESOLVED = 2
RESOLVED = 3


def git(*args):
    """Run git and return the CompletedProcess (text decoded as UTF-8)."""
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def stage_blob(num, path):
    """Contents of index stage `num` for `path`, or None when that stage is absent."""
    r = git("show", f":{num}:{path}")
    return r.stdout if r.returncode == 0 else None


def unmerged_paths():
    r = git("diff", "--name-only", "--diff-filter=U")
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


def paths_with_markers():
    r = git("grep", "-l", "-e", "^<<<<<<< ", "-e", "^>>>>>>> ", "--", ".")
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


def is_regenerable(path):
    base = os.path.basename(path)
    return base in REGENERABLE or base.endswith("_telegram.txt")


# --------------------------------------------------------------------------- #
# union helpers
# --------------------------------------------------------------------------- #
def rec_key(rec):
    return (
        rec.get("date"),
        rec.get("type"),
        rec.get("home_team"),
        rec.get("away_team"),
        rec.get("prediction"),
        rec.get("recorded_at"),
    )


def is_settled(rec):
    return rec.get("result") not in (None, "", "pending")


def stamp(rec):
    return rec.get("updated_at") or rec.get("recorded_at") or ""


def better(a, b):
    """Pick the copy worth keeping: settled beats pending, then newest, then longest."""
    if is_settled(a) != is_settled(b):
        return a if is_settled(a) else b
    if stamp(a) != stamp(b):
        return a if stamp(a) > stamp(b) else b
    return a if len(a) >= len(b) else b


def merge_records(records_a, records_b):
    """Union two record lists, keyed on identity, preferring the better copy."""
    seen = {}
    order = []
    for records in (records_a, records_b):
        for rec in records or []:
            if not isinstance(rec, dict):
                continue
            key = rec_key(rec)
            if key in seen:
                seen[key] = better(seen[key], rec)
            else:
                seen[key] = rec
                order.append(key)
    return [seen[key] for key in order]


def merge_history_json(text_a, text_b):
    a = json.loads(text_a) if text_a else {}
    b = json.loads(text_b) if text_b else {}

    if isinstance(a, list) or isinstance(b, list):
        a = a if isinstance(a, list) else []
        b = b if isinstance(b, list) else []
        return json.dumps(merge_records(a, b), indent=2, ensure_ascii=False) + "\n"

    keys = []
    for doc in (a, b):
        for key in doc:
            if key not in keys:
                keys.append(key)

    merged = {}
    for key in keys:
        va, vb = a.get(key), b.get(key)
        if isinstance(va, list) or isinstance(vb, list):
            merged[key] = merge_records(
                va if isinstance(va, list) else [],
                vb if isinstance(vb, list) else [],
            )
        else:
            merged[key] = va if va is not None else vb
    return json.dumps(merged, indent=2, ensure_ascii=False) + "\n"


def merge_results_csv(text_a, text_b):
    """Union of both CSV sides, keyed on date+home+away, preferring a real score."""
    rows = {}
    order = []
    for text in (text_a, text_b):
        if not text:
            continue
        for i, row in enumerate(csv.reader(io.StringIO(text))):
            if not row or len(row) < 3:
                continue
            if i == 0 and row[0].strip().lower() == "date":
                continue  # header
            key = tuple(cell.strip() for cell in row[:3])
            score = row[3].strip() if len(row) > 3 else ""
            if key in rows:
                if not rows[key][3] and score:
                    rows[key] = (key[0], key[1], key[2], score)
            else:
                rows[key] = (key[0], key[1], key[2], score)
                order.append(key)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["date", "home_team", "away_team", "score"])
    for key in order:
        writer.writerow(rows[key])
    return buf.getvalue()


def split_markers(text):
    """Split a conflicted working-tree file into its 'ours' and 'theirs' sides."""
    ours, theirs = [], []
    state = "both"
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if stripped.startswith("<<<<<<<"):
            state = "ours"
            continue
        if stripped.startswith("|||||||"):
            state = "base"
            continue
        if stripped.startswith("=======") and state in ("ours", "base"):
            state = "theirs"
            continue
        if stripped.startswith(">>>>>>>") and state == "theirs":
            state = "both"
            continue
        if state in ("both", "ours"):
            ours.append(line)
        if state in ("both", "theirs"):
            theirs.append(line)
    return "".join(ours), "".join(theirs)


def write_file(path, content):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def resolve_one(path):
    """Resolve a single conflicted path. Returns True when it was written cleanly."""
    ours = stage_blob(2, path)
    theirs = stage_blob(3, path)
    base = os.path.basename(path)

    if ours is None and theirs is None:
        # No index stages: fall back to the working-tree markers when the file
        # is line-oriented enough to split safely.
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                ours, theirs = split_markers(fh.read())
        except OSError:
            return False

    ours = ours or ""
    theirs = theirs or ""

    if base == DATA_JSON:
        content = merge_history_json(ours, theirs)
    elif base == DATA_CSV:
        content = merge_results_csv(ours, theirs)
    elif is_regenerable(path):
        # Regenerable boards: the engines rebuild these on the next run, so any
        # one clean side is fine.
        content = theirs if theirs.strip() else ours
    else:
        return False

    write_file(path, content)
    return True


def git_dir():
    r = git("rev-parse", "--absolute-git-dir")
    return r.stdout.strip() if r.returncode == 0 else ".git"


def op_state():
    gd = git_dir()
    if os.path.isdir(os.path.join(gd, "rebase-merge")) or os.path.isdir(
        os.path.join(gd, "rebase-apply")
    ):
        return "rebase"
    if os.path.exists(os.path.join(gd, "MERGE_HEAD")):
        return "merge"
    if os.path.exists(os.path.join(gd, "CHERRY_PICK_HEAD")):
        return "cherry-pick"
    return None


def drop_autostash():
    """After a conflicted autostash pop, the stash is kept; drop it once merged."""
    r = git("stash", "list", "-1", "--format=%s")
    subject = r.stdout.strip()
    if subject and "autostash" in subject.lower():
        print("  - dropping the leftover autostash entry (its content is merged)")
        git("stash", "drop", "stash@{0}")


def continue_operation():
    """Finish an in-progress rebase/cherry-pick, or abort it to stay clean."""
    state = op_state()
    if state is None:
        drop_autostash()
        return RESOLVED

    env = dict(os.environ, GIT_EDITOR="true")
    if state == "rebase":
        cmd = ["git", "rebase", "--continue"]
    elif state == "cherry-pick":
        cmd = ["git", "cherry-pick", "--continue"]
    elif state == "merge":
        # The caller's `git commit` completes a merge commit.
        print("  - merge in progress; the caller's commit will finalise it")
        return RESOLVED
    else:
        return RESOLVED

    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    if r.returncode == 0:
        return RESOLVED

    print(f"  ! {state} --continue failed:")
    print((r.stdout or "") + (r.stderr or ""))
    print(f"  - aborting the {state} to leave the tree in its pre-pull state")
    git(state, "--abort")
    return ERROR


def main():
    conflicted = sorted(unmerged_paths() | paths_with_markers())
    if not conflicted:
        print("resolve_git_conflicts: no conflicts detected.")
        return NO_CONFLICTS

    print(f"resolve_git_conflicts: {len(conflicted)} conflicted file(s):")
    for path in conflicted:
        print(f"  * {path}")

    unresolved = []
    for path in conflicted:
        try:
            ok = resolve_one(path)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"  ! {path}: {exc}")
            ok = False
        if ok:
            git("add", "--", path)
            print(f"  + resolved {path}")
        else:
            unresolved.append(path)

    if unresolved:
        print("resolve_git_conflicts: could not resolve these automatically:")
        for path in unresolved:
            print(f"    {path}")
        return UNRESOLVED

    return continue_operation()


if __name__ == "__main__":
    sys.exit(main())
