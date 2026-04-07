@echo off
:: QStrauss Voice — Build Windows .exe
:: Produces: dist\QStrauss Voice.exe

echo ===================================
echo   QStrauss Voice — Windows Build
echo ===================================

if not exist ".venv" (
    echo Run setup_win.bat first.
    pause & exit /b 1
)

call .venv\Scripts\activate.bat

pip install pyinstaller pystray Pillow -q

echo Building .exe...

pyinstaller ^
  --noconfirm ^
  --windowed ^
  --name "QStrauss Voice" ^
  --add-data "dictionary.json;." ^
  --hidden-import "faster_whisper" ^
  --hidden-import "ctranslate2" ^
  --hidden-import "tokenizers" ^
  --hidden-import "huggingface_hub" ^
  --hidden-import "pystray" ^
  --hidden-import "pynput.keyboard._win32" ^
  --hidden-import "pynput.mouse._win32" ^
  --collect-all "faster_whisper" ^
  --collect-all "ctranslate2" ^
  voice_typer.py

echo.
echo ===================================
echo   Build complete!
echo.
echo   App: dist\QStrauss Voice.exe
echo.
echo   Distribute the entire dist\ folder
echo   (the .exe needs its sibling files)
echo ===================================
pause
