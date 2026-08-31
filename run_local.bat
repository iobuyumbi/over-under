@echo off
setlocal
set "RUN_DATE=%~1"
if not defined RUN_DATE (
  for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "RUN_DATE=%%I"
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
echo === TELEGRAM BUILD ===
python build_telegram_daily.py --date %RUN_DATE% --ou-output ou_telegram.txt --btts-output btts_telegram.txt --hw-output hw_telegram.txt --out telegram.txt
if errorlevel 1 goto :error
echo --- done ---

echo.
echo === LOCAL TELEGRAM SEND (optional, gated on env vars) ===
if defined TELEGRAM_BOT_TOKEN (
  if defined TELEGRAM_CHAT_ID (
    echo   TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set — sending...
    set "DATE=%RUN_DATE%"
    python send_local_telegram.py
    if errorlevel 1 (
      echo   WARNING: send_local_telegram.py returned non-zero (see above)
    ) else (
      echo   --- send complete ---
    )
  ) else (
    echo   Skipping: TELEGRAM_CHAT_ID not set.
  )
) else (
  echo   Skipping: TELEGRAM_BOT_TOKEN not set. To enable locally:
  echo     set TELEGRAM_BOT_TOKEN=your_token_here
  echo     set TELEGRAM_CHAT_ID=your_chat_id_here
  echo     set TELEGRAM_VIP_CHAT_ID=optional_vip_chat_id
)

echo.
echo ============================================================
echo              DAILY PREDICTIONS for %RUN_DATE%
echo ============================================================
echo.
type telegram.txt
echo.
echo ============================================================
echo  Files saved: ou.txt, hw.txt, btts.txt, telegram.txt
echo  Telegram sections: ou_telegram.txt, btts_telegram.txt, hw_telegram.txt
echo  VIP reports: btts_vip_report_*.txt, over_under_vip_report_*.txt, home_win_vip_report_*.txt
echo  To resend to Telegram: set TELEGRAM_BOT_TOKEN ^&^& set TELEGRAM_CHAT_ID ^&^& set DATE=%RUN_DATE% ^&^& python send_local_telegram.py
echo ============================================================
pause
exit /b 0

:error
echo.
echo ############################################################
echo Pipeline failed with exit code %errorlevel%.
echo Check error files: ou_errors.txt, hw_errors.txt, btts_errors.txt
echo ############################################################
pause
exit /b %errorlevel%
