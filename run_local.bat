@echo off
setlocal
set "RUN_DATE=%~1"
if not defined RUN_DATE (
  for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "RUN_DATE=%%I"
)
echo Running pipeline for %RUN_DATE%...

echo === FETCH RESULTS ===
python fetch_results.py --days 3
if errorlevel 1 goto :error

echo.
echo === UPDATE MANUAL (pending only, last 7 days) ===
python update_manual_results.py
if errorlevel 1 goto :error

echo.
echo === OVER/UNDER 2.5 ===
python over25_soccerbase.py --publish-date %RUN_DATE% > ou.txt 2> ou_errors.txt
if errorlevel 1 goto :error

echo.
echo === HOME WIN ===
python home_win_soccerbase.py --publish-date %RUN_DATE% > hw.txt 2> hw_errors.txt
if errorlevel 1 goto :error

echo.
echo === BTTS ===
python btts_soccerbase.py --publish-date %RUN_DATE% > btts.txt 2> btts_errors.txt
if errorlevel 1 goto :error

echo.
echo === TELEGRAM BUILD ===
python build_telegram_daily.py --date %RUN_DATE% --ou-output ou.txt --btts-output btts.txt --hw-output hw.txt --out telegram.txt
if errorlevel 1 goto :error

echo.
echo Done! Check telegram.txt
exit /b 0

:error
echo Pipeline failed with exit code %errorlevel%.
exit /b %errorlevel%
