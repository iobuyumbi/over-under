# CI Failure Diagnosis + Code Review (2026-09-04)

## The CI failure: root cause found

The error shown (`pwsh: command not found` on the "Send Telegram" step)
is just the **last** symptom, not the actual scope of the problem.
`.github/workflows/run_daily.yml` had **24 separate steps** across three
jobs (`daily-predictions`, `weekly-report`, `monthly-report`) all set to
`shell: pwsh` — and all three of those jobs run on `runs-on: self-hosted`,
which (based on `run_local.bat` and the `.gitignore` comments referencing
`C:\Inno Project\...`) is your own Windows machine.

**What actually happened in that run**: every `shell: pwsh` step failed
immediately with the same "command not found" error — most of them have
`continue-on-error: true` so the job kept going (explaining "11 errors and
3 warnings" — each failed step is one annotation), but the final "Send
Telegram" step doesn't have `continue-on-error`, so *that's* the one that
actually marked the whole job as failed. In practice, **the entire
daily-predictions job did nothing that day** — no fetch, no predictions,
no commit, no Telegram message — because every single pwsh-shelled step
failed at the shell-launch stage, before any of your actual script logic
ever ran.

**Why**: `pwsh` is PowerShell 7+, a separate install from Windows'
built-in `powershell.exe` (PowerShell 5.1). `run_local.bat` calls plain
`powershell`, which strongly suggests pwsh was either never installed on
this machine, or isn't on the PATH available to the Actions Runner
Windows Service specifically (self-hosted runner services often run
under a more restricted PATH than your interactive user account, so pwsh
can "work fine" when you open a terminal yourself while still not being
visible to the runner — a common self-hosted gotcha).

### The fix

I converted all 24 steps in the three self-hosted jobs from PowerShell
syntax to bash (`Test-Path` → `[ -f ... ]`, `Get-Content` → `cat`,
`$LASTEXITCODE` → `$?`/`${PIPESTATUS[0]}`, etc.), and changed
`shell: pwsh` → `shell: bash` throughout. This is the more robust fix
versus just installing pwsh, for one concrete reason: your runner already
needs `git` for the `git pull`/`git push` steps, and Git for Windows
bundles Git Bash — so `bash` is very likely **already present on this
exact machine with zero new installs**, whereas pwsh would be a new
dependency to install and keep working across whatever else changes on
that PC over time.

I validated every converted script block with `bash -n` (syntax check,
all 24 passed) and specifically tested the trickiest piece — the
`===EMAIL_START===`/`===EMAIL_END===` body extraction — against both a
normal case and a "markers missing" edge case, confirming it matches the
original PowerShell's behavior exactly (excludes the marker lines
themselves, and correctly produces no body file when markers are
missing/malformed, rather than silently including everything to EOF).

**If bash isn't available on the runner for some reason**, the fallback
is installing PowerShell 7 directly (`winget install --id
Microsoft.PowerShell -e` on Windows) and confirming it's on the PATH the
runner *service* uses (not just your own terminal) — but try the bash
version first since it likely needs no runner changes at all.

## Still-unresolved from earlier reviews — flagging again

Two things from the first review are **still present** in this upload,
worth repeating plainly:

1. **The committed SSH private key** (`windows key inno` /
   `windows key inno.pub`) is still in the repo. If this was ever pushed
   to GitHub, it needs to be rotated and purged from git history — a
   `.gitignore` entry (which is already in place) only stops *future*
   commits, it doesn't undo history that's already public.
2. **`venv/` is still tracked** (17MB) despite being in `.gitignore` now
   — same reason: adding a `.gitignore` rule doesn't retroactively untrack
   files. Needs `git rm -r --cached venv/` run once, then committed.

## What's new and good since the last review

You've clearly kept building on this independently (or had someone else
continue it) — this isn't the same repo I last reviewed:

- **`exponential_form_averages`** in `utils.py` — recency-weighted (half-
  life decay) goal averaging, properly shared via `utils.py` and wired
  into both `over25_soccerbase.py` and `btts_soccerbase.py` with the
  correct thin-wrapper pattern. This is a genuinely good statistical
  improvement over flat averages, and it's implemented cleanly.
- **`is_weak_roi_league`** in `utils.py` — this consolidates exactly the
  naming inconsistency I flagged before (`_hw_is_weak_roi` vs
  `_is_weak_roi_league`). It was applied to `over25_soccerbase.py` and
  `btts_soccerbase.py`, but **`home_win_soccerbase.py`'s
  `_hw_is_weak_roi` was still a separate, un-deduplicated copy** — I
  fixed this in `home_win_soccerbase.py` (now imports and wraps the
  shared version, confirmed functionally identical, just a different
  keyword list, verified with a working import/call test).
- **`poisson_pmf`** in `utils.py` — shared Poisson PMF, used correctly in
  `over25_soccerbase.py`; `home_win_soccerbase.py` doesn't use Poisson at
  all (it's strength-score based, not lambda-based), so no gap there —
  confirmed this is a legitimate design difference, not an oversight.
- **`send_daily_telegram.py`** — clean, simple, properly handles missing
  env vars with a clear fatal-error exit code rather than failing
  silently. No issues found.

All three predictor engines plus `utils.py`, `scraping.py`, and
`prediction_tracker.py` were re-verified end to end (syntax + actual
import/runtime test together) after the `home_win_soccerbase.py` fix —
everything still imports and runs cleanly.
