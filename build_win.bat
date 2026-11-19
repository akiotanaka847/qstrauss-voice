@echo off
:: QStrauss Voice — Build Windows .exe
:: Produces: dist\QStrauss Voice\QStrauss Voice.exe

echo ===================================
echo   QStrauss Voice — Windows Build
echo ===================================

if exist ".venv" (
    call .venv\Scripts\activate.bat
) else if not defined CI (
    echo Run setup_win.bat first.
    pause & exit /b 1
) else (
    echo Sin .venv, usando el Python del entorno ^(CI^)
)

pip install pyinstaller pystray Pillow pywebview -q

echo Building .exe...

pyinstaller ^
  --noconfirm ^
  --console ^
  --name "QStrauss Voice" ^
  --icon "resources\QStraussVoice.ico" ^
  --add-data "dictionary.json;." ^
  --add-data "resources;resources" ^
  --add-data "settings_window_win.py;." ^
  --add-data "vad.py;." ^
  --add-data "terms.py;." ^
  --add-data "learn.py;." ^
  --hidden-import "vad" ^
  --hidden-import "terms" ^
  --hidden-import "learn" ^
  --hidden-import "spylls" ^
  --hidden-import "onnxruntime" ^
  --hidden-import "faster_whisper" ^
  --hidden-import "ctranslate2" ^
  --hidden-import "tokenizers" ^
  --hidden-import "huggingface_hub" ^
  --hidden-import "sounddevice" ^
  --hidden-import "pynput.keyboard._win32" ^
  --hidden-import "pynput.mouse._win32" ^
  --hidden-import "pystray._win32" ^
  --hidden-import "webview" ^
  --collect-all "webview" ^
  --collect-all "spylls" ^
  --collect-all "onnxruntime" ^
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

if defined CI (
    echo Empaquetando...
    if exist QStrauss-Voice-win.zip del QStrauss-Voice-win.zip
    powershell -Command "Compress-Archive -Path 'dist\QStrauss Voice' -DestinationPath QStrauss-Voice-win.zip"
    exit /b 0
)

pause
