# over-under: Code Review & Improvement Report

Scope: ~13,000 lines across 30+ Python files (soccer prediction / scraping /
Telegram-alerting pipeline). This report is prioritized — fix items in order.

## 0. URGENT — Security

A committed **OpenSSH private key** (`windows key inno`, plus its `.pub`
pair) was found in the repo root. `.gitignore` did not exclude it, so if
this was ever pushed to GitHub it must be treated as **compromised**:

1. Revoke/rotate the key everywhere it's authorized (deploy keys, server
   `authorized_keys`, Inno Setup signing config, etc.) and issue a new one.
2. Purge it from git history with `git filter-repo` or BFG Repo-Cleaner —
   `git rm` alone leaves it recoverable in old commits — then force-push.
3. The new `.gitignore` in this delivery blocks `*.pem`, `*.key`,
   `id_rsa*`, `id_ed25519*`, `*_key*` going forward.

Also present but empty (0 bytes), so low risk but still shouldn't be
committed: `api_key.txt`. Added to `.gitignore` regardless.

No SQL injection, `eval`/`exec`, or hardcoded API keys were found in the
Python source — that part is clean.

## 1. Repo hygiene

- **`venv/` appears to be bundled/committed.** A virtualenv should never be
  in version control (~10k+ files, platform-specific binaries). Add to
  `.gitignore` (done) and `git rm -r --cached venv/` if it's tracked.
- **Dozens of dated output files are committed as source**:
  `predictions_soccerbase_*.json`, `home_win_predictions_*.json`,
  `*_report_*.json/.txt`, `debug.log`, a `.db` cache file, and one-off
  `*_today*.txt` debug dumps. Two of the JSON files even have literal
  `[YYYY-MM-DD]` in the filename — evidence of a template-substitution bug
  in whatever produced them. These are pipeline *output*, not source, and
  bloat every clone/diff. New `.gitignore` covers this; `cleanup_dead_code.sh`
  lists exact files to remove from the working tree.
- **Confirmed-dead code**: `over25_predictor_v2.py` is literally empty;
  `working_predictor.py`, `final_predictor.py`, `soccerbase_predictor.py`,
  `home_win_hybrid.py`, `over25_hybrid.py`, `playwright_predictor.py`,
  `weekly_report.py`, `debug_scraper.py`, `_diag_today.py` are not imported
  by anything and not invoked by `daily_runner.py` or the GitHub Actions
  workflow (`.github/workflows/run_daily.yml`) — only
  `over25_soccerbase.py`, `home_win_soccerbase.py`, `btts_soccerbase.py`,
  `fetch_results.py`, `update_manual_results.py`,
  `generate_weekly_report.py`, `generate_monthly_report.py`, and
  `build_telegram_daily.py` are actually part of the live pipeline. See
  `cleanup_dead_code.sh` for the full list (dry-run by default).

## 2. Architecture — the big one: triplicated core logic

`over25_soccerbase.py`, `home_win_soccerbase.py`, and `btts_soccerbase.py`
each carry their **own copy-pasted implementation** of:

- `Cache` (SQLite TTL cache)
- `fetch()` (session + retry + randomized headers + captcha/short-page detection)
- `parse_date()`
- `calculate_kelly()` / `apply_portfolio_kelly()`
- The `UserAgent`/`HEADERS`/`Retry`/`HTTPAdapter` setup

A fourth, **unused and already-drifted** copy of the same thing sits in
`utils.py` (it's never imported anywhere — verified via grep across the
whole repo). This is the highest-risk item architecturally: every future
fix to scraping/caching/staking logic has to be applied by hand in three
files, and nothing enforces they stay in sync. `utils.py`'s independent
drift (e.g. it lacks the captcha/short-page detection the three engines
already have) is exactly what happens when that discipline lapses.

**Fix applied to all three engines** in this delivery: rewrote `utils.py`
as the single canonical implementation, and refactored all three
predictors to import from it instead of redefining `Cache`, `fetch`,
`parse_date`, `calculate_kelly`, and `apply_portfolio_kelly` locally.
Each keeps a thin wrapper only where it needs to preserve its own default
odds or call signature (e.g. `home_win`'s Kelly defaults to 2.8 odds and
takes no `bet_type`; `btts`'s takes a `side_key`; `over25`'s nests under
`"over"`/`"under"`) — the underlying math and fetch/cache behavior is now
identical and lives in one place.

Line counts:
| File | Before | After |
|---|---|---|
| `over25_soccerbase.py` | 2265 | 2120 |
| `home_win_soccerbase.py` | 1271 | 1128 |
| `btts_soccerbase.py` | 1662 | 1560 |

All three were verified to still import cleanly and produce identical
`calculate_kelly`/`apply_portfolio_kelly`/`fetch`/cache behavior after
the refactor (I stubbed `fake_useragent`, since this sandbox has no
network access to `pip install` it, but everything downstream of that
import was exercised directly).

**Bonus bug fix found in the process**: `btts_soccerbase.py`'s original
`parse_date()` had a fix the other two didn't — its regex fallback
normalized `2026/06/15`-style slashes to dashes before the final parse
attempt; `over25_soccerbase.py` and `home_win_soccerbase.py` were missing
that normalization, so a slash-formatted date falling through to the
regex fallback would have silently failed to parse in those two files.
That fix is now in `utils.py` and benefits all three.

I also caught and fixed a leftover local `parse_date()` definition in
`over25_soccerbase.py` from my first refactor pass — it was shadowing
the new import entirely, which a naive "add the import and move on"
pass would have missed. Worth flagging as a general risk: when
consolidating duplicated code like this, grep for *every* definition of
each name (`grep -n "^def parse_date"`) rather than trusting that you
found the one copy — Python will silently let the later definition win.

## 3. Smaller findings

- `requirements.txt` doesn't pin exact versions (`>=` only) — fine for a
  personal project, but for a scheduled GitHub Actions pipeline that
  auto-commits, an upstream break in `beautifulsoup4` or `playwright`
  could silently change scraping behavior. Consider pinning (`==`) with a
  periodic manual bump, or at least a `pip freeze` lockfile checked in.
- 7 bare/broad `except:`/`except Exception:` blocks across the codebase —
  most log the error (fine), but worth a pass to confirm none are
  silently swallowing failures that should halt the pipeline (a bad
  prediction silently publishing is worse than a loud crash for this use
  case).
- `docker-compose.yml` references `over25_predictor.py`, which is one of
  the dead files above — if you're not actually deploying via Docker
  (the real pipeline runs via GitHub Actions), this file is stale and
  should either be removed or updated to point at the real entry points.

## What's genuinely solid

- No SQL injection, no `eval`/`exec`, no hardcoded secrets in source.
- The GitHub Actions workflow itself is well-structured — proper
  `concurrency` guards, conditional jobs per schedule, artifact uploads
  for debugging, `git pull --rebase --autostash` before committing back
  (avoids the classic bot-commit race condition).
- `fetch()`'s captcha/short-page detection and retry/backoff strategy are
  genuinely good defensive scraping practice — this is worth keeping
  as-is and centralizing, not rewriting.
- `Cache`, `calculate_kelly`, `apply_portfolio_kelly` are simple, correct,
  and easy to test.
