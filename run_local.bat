@echo off
setlocal
set "RUN_DATE=%~1"
if not defined RUN_DATE (
  for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "RUN_DATE=%%I"
)

REM --- Load variables from .env (gitignored) into this cmd session ---
REM Only sets a VAR if that VAR is not already defined in the environment,
REM so explicit set-on-the-shell overrides win. Skips blank lines and
REM lines starting with '#'. Values are read verbatim after the first '='.
if exist .env (
  for /f "usebackq eol=# tokens=1,* delims==" %%K in (".env") do (
    set "_K=%%K"
    if defined _K (
      call set "_K=%%_K: =%%"
      if not defined %%_K%% set "%%_K%%=%%L"
    )
  )
  set "_K="
)

echo Running pipeline for %RUN_DATE%...
echo.

echo === FETCH RESULTS ===
python fetch_results.py --days 3
if errorlevel 1 goto :error
echo --- done ---

echo.
echo === UPDATE MANUAL (pending only, last 7 days) ===
python update_manual_results.py
if errorlevel 1 goto :error
echo --- done ---

echo.
echo === OVER/UNDER 2.5 ===
python over25_soccerbase.py --publish-date %RUN_DATE% > ou.txt 2> ou_errors.txt
if errorlevel 1 goto :error
echo --- done ---

echo.
echo === HOME WIN ===
python home_win_soccerbase.py --publish-date %RUN_DATE% > hw.txt 2> hw_errors.txt
if errorlevel 1 goto :error
echo --- done ---

echo.
echo === BTTS ===
python btts_soccerbase.py --publish-date %RUN_DATE% > btts.txt 2> btts_errors.txt
if errorlevel 1 goto :error
echo --- done ---

echo.
echo === OVER 0.5 TEAM GOAL ===
python oo05_soccerbase.py --publish-date %RUN_DATE% > oo05.txt 2> oo05_errors.txt
if errorlevel 1 goto :error
echo --- done ---

echo.
echo === TELEGRAM BUILD ===
python build_telegram_daily.py --date %RUN_DATE% --ou-output ou_telegram.txt --btts-output btts_telegram.txt --hw-output hw_telegram.txt --oo05-output oo05_telegram.txt --out telegram.txt
if errorlevel 1 goto :error
echo --- done ---

echo.
echo === CURATED PICKS (research-calibrated filter) ===
python curate_picks.py --date %RUN_DATE% --out-txt curated.txt --out-json curated.json
if errorlevel 1 (
  echo   WARNING: curate_picks.py returned non-zero (see above; optional step)
) else (
  echo   --- done (curated.txt + curated.json saved) ---
)

echo.
echo === LOCAL TELEGRAM SEND (optional, loaded from .env) ===
set "DATE=%RUN_DATE%"
python send_local_telegram.py
if errorlevel 1 (
  echo   WARNING: Telegram was not sent. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env.
) else (
  echo   --- send complete ---
)

echo.
echo === AUTO-COMMIT + PUSH (optional, only if local git is configured) ===
set "GIT_SKIP_AUTO_PUSH=%~2"
if /i "%GIT_SKIP_AUTO_PUSH%"=="--no-push" goto skip_git

git status --porcelain > _git_status.txt 2>&1
for /f %%A in ('findstr /R "." _git_status.txt ^| find /C /V ""') do set "DIRTY=%%A"
del _git_status.txt 2>nul

if "%DIRTY%"=="0" (
  echo   (no working changes — nothing to commit or push)
) else (
  git add -A 2>nul
  git config user.name >nul 2>&1
  if errorlevel 1 git config user.name "Local Run (run_local.bat)"
  git config user.email >nul 2>&1
  if errorlevel 1 git config user.email "local@run-local.invalid"
  echo   -- pulling latest origin/main (rebase + autostash) --
  git pull --rebase --autostash origin main 2>&1
  if errorlevel 1 (
    echo   WARNING: git pull --rebase failed ^(possible merge conflicts^). Skipping commit + push; resolve conflicts manually, then commit/push by hand, or re-run with --no-push 2nd arg and retry git operations.
    goto skip_git
  )
  git commit -m "Local run: predictions, reports, curated picks, results update (%RUN_DATE%)" 2>&1
  if errorlevel 1 (
    echo   (nothing new to commit after pull rebase)
  ) else (
    echo   --- commit done ---
    set "PUSH_URL="
    set "PUSH_TARGET=origin main"
    if defined GITHUB_PAT (
      REM Build authenticated push URL from current origin remote + GITHUB_PAT.
      REM Never store the token in git config; we pass it inline only for this push.
      for /f "usebackq delims=" %%U in (`git remote get-url origin 2^>nul`) do set "ORIGIN_URL=%%U"
      if defined ORIGIN_URL (
        REM Case 1: HTTPS URL like https://github.com/OWNER/REPO.git
        echo "%ORIGIN_URL%" | findstr /ri "https://github.com/" >nul && (
          for /f "tokens=2,* delims=/" %%A in ("%ORIGIN_URL%") do set "REST=%%B"
          if defined REST (
            set "PUSH_URL=https://x-access-token:%GITHUB_PAT%@github.com/!REST!"
          )
        )
        REM Case 2: SSH URL like git@github.com:OWNER/REPO.git
        if not defined PUSH_URL (
          echo "%ORIGIN_URL%" | findstr /ri "git@github.com:" >nul && (
            for /f "tokens=2 delims=:" %%A in ("%ORIGIN_URL%") do set "PATH_PART=%%A"
            if defined PATH_PART (
              set "PUSH_URL=https://x-access-token:%GITHUB_PAT%@github.com/!PATH_PART!"
            )
          )
        )
      )
      if defined PUSH_URL (
        echo   -- using authenticated push URL from GITHUB_PAT in .env --
        set "PUSH_TARGET=!PUSH_URL! main"
      ) else (
        echo   WARNING: GITHUB_PAT set but could not rewrite origin URL ^(origin='!ORIGIN_URL!'^). Trying plain 'git push origin main' fallback.
      )
    )
    REM Enable delayed expansion only inside this push block so !REST! etc. work.
    setlocal enabledelayedexpansion
    git push !PUSH_TARGET! 2>&1
    if errorlevel 1 (
      endlocal
      echo   WARNING: git push failed. If auth error, verify GITHUB_PAT in .env has 'repo' scope, or set it manually then re-run.
    ) else (
      endlocal
      echo   --- push complete ---
    )
    set "PUSH_URL="
    set "PUSH_TARGET="
    set "ORIGIN_URL="
    set "REST="
    set "PATH_PART="
  )
)
:skip_git

echo.
echo ============================================================
echo              DAILY PREDICTIONS for %RUN_DATE%
echo ============================================================
echo.
type telegram.txt
echo.
echo ============================================================
echo  Files saved: ou.txt, hw.txt, btts.txt, oo05.txt, telegram.txt, curated.txt, curated.json
echo  Telegram sections: ou_telegram.txt, btts_telegram.txt, hw_telegram.txt, oo05_telegram.txt
echo  VIP reports: btts_vip_report_*.txt, over_under_vip_report_*.txt, home_win_vip_report_*.txt, over05_team_goal_vip_report_*.txt
echo  To resend to Telegram: set TELEGRAM_BOT_TOKEN ^&^& set TELEGRAM_CHAT_ID ^&^& set DATE=%RUN_DATE% ^&^& python send_local_telegram.py
echo  To skip git push next run: run_local.bat %RUN_DATE% --no-push
echo ============================================================
pause
exit /b 0

:error
echo.
echo ############################################################
echo Pipeline failed with exit code %errorlevel%.
echo Check error files: ou_errors.txt, hw_errors.txt, btts_errors.txt, oo05_errors.txt
echo ############################################################
pause
exit /b %errorlevel%
