@echo off
REM WINDOWS INSTALLER FOR OVER 2.5 PREDICTOR
REM Run as Administrator

echo Installing Over 2.5 Goals Predictor...

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python not found. Install from python.org first.
    pause
    exit /b 1
)

echo 📦 Installing packages...
pip install requests beautifulsoup4

echo.
echo ✅ Done!
echo.
echo Run predictions with:
echo   python over25_predictor.py
echo.
echo Set up Task Scheduler for daily runs.
echo See SETUP_GUIDE.txt for details.
pause
