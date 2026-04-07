@echo off
echo ===================================
echo   QStrauss Voice — Windows Setup
echo ===================================

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org
    pause
    exit /b 1
)

echo Python found.

:: Create virtual environment
echo.
echo Creating virtual environment...
python -m venv .venv
call .venv\Scripts\activate.bat

:: Install dependencies
echo Installing dependencies...
pip install --upgrade pip -q
pip install -r requirements.txt

echo.
echo ===================================
echo   Setup complete!
echo.
echo   To run QStrauss Voice:
echo     .venv\Scripts\activate
echo     python voice_typer.py
echo ===================================
pause
