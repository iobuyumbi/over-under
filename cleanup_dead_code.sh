#!/usr/bin/env bash
# Review this list before running. Nothing here is deleted automatically -
# uncomment the `git rm` lines you agree with, or run with --apply.
#
# Evidence for "dead": not imported by any other .py file, not referenced
# by .github/workflows/run_daily.yml, and not the file daily_runner.py or
# docker-compose.yml actually invoke for the live pipeline.
set -euo pipefail

DEAD_FILES=(
  # Empty file
  "over25_predictor_v2.py"          # 0 bytes

  # Superseded by *_soccerbase.py (per README's own file table) -
  # not referenced by daily_runner.py, the GH Actions workflow, or each other
  "working_predictor.py"
  "final_predictor.py"
  "over25_predictor.py"             # only referenced by docker-compose.yml,
                                     # which itself is not used by the GH Actions pipeline -
                                     # confirm you're not deploying via Docker before removing
  "soccerbase_predictor.py"
  "home_win_hybrid.py"
  "over25_hybrid.py"
  "playwright_predictor.py"

  # Superseded by generate_weekly_report.py / generate_monthly_report.py
  "weekly_report.py"

  # One-off debug/diagnostic scripts, not part of the pipeline
  "debug_scraper.py"
  "_diag_today.py"
  "_diag_out.txt"

  # Dated snapshot outputs that should never have been committed (see .gitignore) -
  # safe to delete from the working tree; keep in git history if you want them
  "home_win_predictions_2026-05-29.json"
  "home_win_predictions_2026-05-30.json"
  "home_win_predictions_2026-05-31.json"
  "home_win_predictions_2026-06-01.json"
  "home_win_predictions_2026-06-02.json"
  "home_win_predictions_2026-06-03.json"
  "home_win_predictions_2026-06-04.json"
  "home_win_predictions_2026-06-05.json"
  "home_win_predictions_2026-06-06.json"
  "home_win_predictions_2026-06-07.json"
  "home_win_predictions_2026-06-08.json"
  "home_win_predictions_2026-06-09.json"
  "home_win_predictions_2026-06-10.json"
  "home_win_predictions_2026-06-11.json"
  "home_win_predictions_2026-06-12.json"
  "home_win_predictions_2026-06-13.json"
  "home_win_predictions_2026-06-14.json"
  "home_win_predictions_2026-06-15.json"
  "predictions_2026-05-25.json"
  "predictions_soccerbase_2026-05-21.json"
  "predictions_soccerbase_2026-05-22.json"
  "predictions_soccerbase_2026-05-23.json"
  "predictions_soccerbase_2026-05-24.json"
  "predictions_soccerbase_2026-05-25.json"
  "predictions_soccerbase_2026-05-26.json"
  "predictions_soccerbase_2026-05-27.json"
  "predictions_soccerbase_2026-05-28.json"
  "predictions_soccerbase_2026-05-29.json"
  "predictions_soccerbase_2026-05-30.json"
  "predictions_soccerbase_2026-05-31.json"
  "predictions_soccerbase_2026-06-13.json"
  "predictions_soccerbase_[2026-05-26].json"   # literal "[YYYY-MM-DD]" template bug output
  "predictions_soccerbase_[YYYY-MM-DD].json"   # a run where the date substitution never happened
  "btts_report_2026-06-01.json"
  "btts_report_2026-08-20.json"
  "btts_vip_report_2026-06-01.txt"
  "btts_vip_report_2026-08-20.txt"
  "over25_report_2026-06-11.json"

  # Local debug/log artifacts
  "debug.log"
  "btts_today.txt"
  "btts_today_err.txt"
  "hw_today.txt"
  "hw_today2.txt"
  "hw_today2_err.txt"
  "hw_today_err.txt"
  "ou_today.txt"
  "ou_today_err.txt"
  "telegram_today.txt"
  "telegram_today_test.txt"

  # Stray, unrelated file - looks like an accidental export dropped in the repo root
  "table-1779673196371.csv"

  # SECURITY: private key committed to the repo - see security notes.
  # Do NOT just delete this - rotate/revoke the key and purge it from git
  # history (git filter-repo / BFG) BEFORE removing it from the working tree,
  # or the compromised key remains recoverable from old commits.
  "windows key inno"
  "windows key inno.pub"
)

REVIEW_FIRST=(
  # Ambiguous - check with the maintainer before removing
  "test_api_keys.py"       # dev utility, references working_predictor.py
  "test_system.py"         # dev utility
  "setup_helper.py"        # references the empty over25_predictor_v2.py
  "backfill_history.py"    # one-off migration script - keep if you may need it again
  "analyze_alternative_thresholds.py"
  "analyze_prediction_history.py"
  "retrospective_analysis.py"
  "team_data_manager.py"
  "docker-compose.yml"     # only used if you actually deploy via Docker; the
                            # live pipeline runs via GitHub Actions instead
)

echo "== Confirmed dead / stray files (${#DEAD_FILES[@]}) =="
for f in "${DEAD_FILES[@]}"; do echo "  $f"; done

echo
echo "== Review before deciding (${#REVIEW_FIRST[@]}) =="
for f in "${REVIEW_FIRST[@]}"; do echo "  $f"; done

if [[ "${1:-}" == "--apply" ]]; then
  echo
  echo "Removing confirmed dead files..."
  for f in "${DEAD_FILES[@]}"; do
    if [[ -e "$f" ]]; then
      git rm -f -- "$f" 2>/dev/null || rm -f -- "$f"
    fi
  done
  echo "Done. Review 'git status' before committing."
else
  echo
  echo "Dry run only. Re-run as: ./cleanup_dead_code.sh --apply"
fi
