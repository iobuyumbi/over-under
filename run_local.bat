@echo off
setlocal
cd /d "%~dp0"

REM ============================================================
REM  Soccer Picks -- local launcher
REM
REM  Just double-click this file and pick a numbered option.
REM
REM  Power users can skip the menu by passing arguments:
REM    run_local.bat [YYYY-MM-DD] [--no-send] [--no-push]
REM      --no-send  build the board but let CI deliver it to Telegram
REM      --no-push  build everything but do not commit or push
REM ============================================================

REM ---- Run date: first non-flag argument, else today ----
set "RUN_DATE=%~1"
if "%RUN_DATE:~0,1%"=="-" set "RUN_DATE="
if not defined RUN_DATE (
  for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "RUN_DATE=%%I"
)

REM ---- Optional flags, any of the first three positions ----
set "SKIP_SEND="
set "SKIP_PUSH="
if /i "%~1"=="--no-send" set "SKIP_SEND=1"
if /i "%~2"=="--no-send" set "SKIP_SEND=1"
if /i "%~3"=="--no-send" set "SKIP_SEND=1"
if /i "%~1"=="--no-push" set "SKIP_PUSH=1"
if /i "%~2"=="--no-push" set "SKIP_PUSH=1"
if /i "%~3"=="--no-push" set "SKIP_PUSH=1"

REM ---- Load variables from .env (gitignored) into this cmd session ----
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

REM ---- Any argument means "run the full pipeline, no menu" ----
if not "%~1"=="" goto run_all

REM Interactive menu. Delayed expansion is enabled here (the .env load above
REM already ran without it) so the choice can be re-read through for /f, which
REM strips the stray CR that `set /p` keeps when stdin is redirected.
setlocal enabledelayedexpansion

:menu
cls
echo ============================================================
echo   SOCCER PICKS  -  local launcher
echo   run date: %RUN_DATE%
echo ------------------------------------------------------------
echo    [1]  Run everything     results + 4 markets + board + send + push
echo    [2]  Run everything, no Telegram send   let CI publish it
echo    [3]  Dry run            no send, no push  -  safe test run
echo    [4]  Rebuild the board  engines + board + curated only
echo    [5]  Send the board to Telegram now
echo    [6]  Fetch and settle results only
echo    [7]  Consolidate reports only
echo    [8]  Commit and push only
echo    [9]  Run the test suite
echo    [10] Open this folder
echo    [0]  Exit
echo ============================================================
set "CHOICE="
set /p "CHOICE=Choose: "
for /f "delims=" %%C in ("!CHOICE!") do set "CHOICE=%%C"
if "!CHOICE!"=="" exit /b 0
if "!CHOICE!"=="1" goto m_full
if "!CHOICE!"=="2" goto m_nosend
if "!CHOICE!"=="3" goto m_dry
if "!CHOICE!"=="4" goto m_board
if "!CHOICE!"=="5" goto m_send
if "!CHOICE!"=="6" goto m_fetch
if "!CHOICE!"=="7" goto m_consolidate
if "!CHOICE!"=="8" goto m_git
if "!CHOICE!"=="9" goto m_tests
if "!CHOICE!"=="10" goto m_open
if "!CHOICE!"=="0" exit /b 0
goto menu

:m_full
set "SKIP_SEND="
set "SKIP_PUSH="
set "INTERACTIVE=1"
goto run_all

:m_nosend
set "SKIP_SEND=1"
set "SKIP_PUSH="
set "INTERACTIVE=1"
goto run_all

:m_dry
set "SKIP_SEND=1"
set "SKIP_PUSH=1"
set "INTERACTIVE=1"
goto run_all

:m_board
echo.
echo === REBUILD BOARD ONLY (no fetch, no send, no push) ===
call :step_engines
if errorlevel 1 goto :error
call :step_board
if errorlevel 1 goto :error
call :step_curated
echo.
pause
goto menu

:m_send
call :step_send
echo.
pause
goto menu

:m_fetch
call :step_fetch
if errorlevel 1 goto :error
call :step_update_manual
if errorlevel 1 goto :error
echo.
pause
goto menu

:m_consolidate
call :step_consolidate
echo.
pause
goto menu

:m_git
call :step_git
echo.
pause
goto menu

:m_tests
echo.
echo === TEST SUITE ===
python test_regressions.py
echo.
pause
goto menu

:m_open
start "" "%~dp0"
goto menu

REM ============================================================
REM  Full pipeline
REM ============================================================
:run_all
echo.
echo ============================================================
echo   Running pipeline for %RUN_DATE%
echo ============================================================

call :step_fetch
if errorlevel 1 goto :error
call :step_update_manual
if errorlevel 1 goto :error
call :step_engines
if errorlevel 1 goto :error
call :step_board
if errorlevel 1 goto :error
call :step_curated

if defined SKIP_SEND (
  echo.
  echo === TELEGRAM SEND (skipped) ===
  echo   --no-send: the CI publish-daily job will deliver this board --
) else (
  call :step_send
)

call :step_consolidate

if defined SKIP_PUSH (
  echo.
  echo === COMMIT + PUSH (skipped) ===
  echo   --no-push: nothing was uploaded --
) else (
  call :step_git
)

goto :show_board

:show_board
echo.
echo ============================================================
echo              DAILY PREDICTIONS for %RUN_DATE%
echo ============================================================
echo.
type telegram.txt
echo.
echo ============================================================
echo  Files: ou.txt, hw.txt, btts.txt, oo05.txt, telegram.txt, curated.txt, curated.json
echo  Telegram sections: ou_telegram.txt, btts_telegram.txt, hw_telegram.txt, oo05_telegram.txt
echo  VIP reports: btts_vip_report_*.txt, over_under_vip_report_*.txt, home_win_vip_report_*.txt, over05_team_goal_vip_report_*.txt
echo  Resend to Telegram:  run_local.bat %RUN_DATE% --no-push  then pick [5]
echo ============================================================
if defined INTERACTIVE goto menu_pause
pause
exit /b 0

:menu_pause
echo.
pause
goto menu

REM ============================================================
REM  Pipeline steps (each returns non-zero errorlevel on failure)
REM ============================================================
:step_fetch
echo.
echo === FETCH RESULTS ===
python fetch_results.py --days 3
if errorlevel 1 exit /b 1
echo --- done ---
exit /b 0

:step_update_manual
echo.
echo === UPDATE MANUAL (auto-fill scores, mark never-played rows) ===
python update_manual_results.py
if errorlevel 1 exit /b 1
echo --- done ---
exit /b 0

:step_engines
echo.
echo === OVER/UNDER 2.5 ===
python over25_soccerbase.py --publish-date %RUN_DATE% > ou.txt 2> ou_errors.txt
if errorlevel 1 exit /b 1
echo --- done ---
echo.
echo === HOME WIN ===
python home_win_soccerbase.py --publish-date %RUN_DATE% > hw.txt 2> hw_errors.txt
if errorlevel 1 exit /b 1
echo --- done ---
echo.
echo === BTTS ===
python btts_soccerbase.py --publish-date %RUN_DATE% > btts.txt 2> btts_errors.txt
if errorlevel 1 exit /b 1
echo --- done ---
echo.
echo === OVER 0.5 TEAM GOAL ===
python oo05_soccerbase.py --publish-date %RUN_DATE% > oo05.txt 2> oo05_errors.txt
if errorlevel 1 exit /b 1
echo --- done ---
exit /b 0

:step_board
echo.
echo === TELEGRAM BUILD ===
python build_telegram_daily.py --date %RUN_DATE% --ou-output ou_telegram.txt --btts-output btts_telegram.txt --hw-output hw_telegram.txt --oo05-output oo05_telegram.txt --out telegram.txt
if errorlevel 1 exit /b 1
echo --- done ---
exit /b 0

:step_curated
echo.
echo === CURATED PICKS (research-calibrated filter) ===
python curate_picks.py --date %RUN_DATE% --out-txt curated.txt --out-json curated.json
if errorlevel 1 (
  echo   WARNING: curate_picks.py returned non-zero ^(see above; optional step^)
) else (
  echo   --- done ^(curated.txt + curated.json saved^) ---
)
exit /b 0

:step_send
echo.
echo === TELEGRAM SEND (credentials from .env) ===
set "DATE=%RUN_DATE%"
python send_local_telegram.py
if errorlevel 1 (
  echo   WARNING: Telegram was not sent. Check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env.
) else (
  echo   --- send complete ---
)
exit /b 0

:step_consolidate
echo.
echo === CONSOLIDATE REPORTS (archive pick digests + prune dated files) ===
REM Runs AFTER curate_picks.py (which reads the *_vip_report_*.txt files) and
REM AFTER the Telegram send, so it never deletes an input still in use.
python consolidate_reports.py
if errorlevel 1 (
  echo   WARNING: consolidate_reports.py returned non-zero ^(see above; optional step^)
) else (
  echo   --- done ^(reports_history.md updated; dated report files pruned^) ---
)
exit /b 0

:step_git
echo.
echo === COMMIT + PUSH ===
git status --porcelain > _git_status.txt 2>&1
for /f %%A in ('findstr /R "." _git_status.txt ^| find /C /V ""') do set "DIRTY=%%A"
del _git_status.txt 2>nul

if "%DIRTY%"=="0" (
  echo   no working changes - nothing to commit or push
  exit /b 0
)

git add -A 2>nul
git config user.name >nul 2>&1
if errorlevel 1 git config user.name "Local Run (run_local.bat)"
git config user.email >nul 2>&1
if errorlevel 1 git config user.email "local@run-local.invalid"

echo   -- pulling latest origin/main (rebase + autostash) --
git pull --rebase --autostash origin main 2>&1
if errorlevel 1 (
  echo   WARNING: git pull --rebase failed ^(possible merge conflicts^).
  echo   Resolve conflicts manually, then commit and push by hand.
  exit /b 0
)

git commit -m "Local run: predictions, reports, curated picks, results update (%RUN_DATE%)" 2>&1
if errorlevel 1 (
  echo   nothing new to commit after pull rebase
  exit /b 0
)
echo   --- commit done ---

REM Authenticated push: build an inline https URL from GITHUB_PAT when present,
REM so the token is never written to git config. Delayed expansion is enabled
REM for this block only, which is what makes !VAR! inside the if-blocks work.
setlocal enabledelayedexpansion
set "PUSH_TARGET=origin main"
if defined GITHUB_PAT (
  set "PUSH_URL="
  for /f "usebackq delims=" %%U in (`git remote get-url origin 2^>nul`) do set "ORIGIN_URL=%%U"
  if defined ORIGIN_URL (
    echo !ORIGIN_URL! | findstr /ri "https://github.com/" >nul && (
      for /f "tokens=2,* delims=/" %%A in ("!ORIGIN_URL!") do set "REST=%%B"
      if defined REST set "PUSH_URL=https://x-access-token:!GITHUB_PAT!@github.com/!REST!"
    )
    if not defined PUSH_URL (
      echo !ORIGIN_URL! | findstr /ri "git@github.com:" >nul && (
        for /f "tokens=2 delims=:" %%A in ("!ORIGIN_URL!") do set "PATH_PART=%%A"
        if defined PATH_PART set "PUSH_URL=https://x-access-token:!GITHUB_PAT!@github.com/!PATH_PART!"
      )
    )
    if defined PUSH_URL (
      echo   -- using authenticated push URL from GITHUB_PAT in .env --
      set "PUSH_TARGET=!PUSH_URL! main"
    ) else (
      echo   WARNING: GITHUB_PAT is set but the origin URL could not be rewritten; trying a plain push.
    )
  )
)
git push !PUSH_TARGET! 2>&1
if errorlevel 1 (
  echo   WARNING: git push failed. If it is an auth error, verify GITHUB_PAT in .env has 'repo' scope.
) else (
  echo   --- push complete ---
)
endlocal
exit /b 0

REM ============================================================
REM  Failure handler
REM ============================================================
:error
set "ERRLEV=%errorlevel%"
echo.
echo ############################################################
echo Pipeline failed with exit code %ERRLEV%.
echo Check error files: ou_errors.txt, hw_errors.txt, btts_errors.txt, oo05_errors.txt
echo ############################################################
pause
if defined INTERACTIVE goto menu
exit /b %ERRLEV%
