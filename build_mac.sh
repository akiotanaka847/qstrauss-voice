#!/bin/bash
# QStrauss Voice — Build macOS .app
# Produces: dist/QStrauss Voice.app  (drag to Applications to install)

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "==================================="
echo "  QStrauss Voice — Mac Build"
echo "==================================="

# Activate venv
if [ ! -d ".venv" ]; then
    echo "Run ./setup_mac.sh first."
    exit 1
fi
source .venv/bin/activate

# Install build tool
pip install pyinstaller Pillow -q

echo "Building .app bundle..."

pyinstaller \
  --noconfirm \
  --windowed \
  --name "QStrauss Voice" \
  --icon "resources/QStraussVoice.icns" \
  --add-data "dictionary.json:." \
  --add-data "resources:resources" \
  --add-data "overlay.py:." \
  --add-data "settings_window.py:." \
  --hidden-import "faster_whisper" \
  --hidden-import "ctranslate2" \
  --hidden-import "tokenizers" \
  --hidden-import "huggingface_hub" \
  --hidden-import "sounddevice" \
  --hidden-import "pynput.keyboard._darwin" \
  --hidden-import "pynput.mouse._darwin" \
  --hidden-import "objc" \
  --hidden-import "AppKit" \
  --hidden-import "Foundation" \
  --hidden-import "Quartz" \
  --hidden-import "WebKit" \
  --hidden-import "overlay" \
  --hidden-import "settings_window" \
  --collect-all "faster_whisper" \
  --collect-all "ctranslate2" \
  --collect-all "pyobjc-framework-Cocoa" \
  --collect-all "pyobjc-framework-WebKit" \
  --collect-all "pyobjc-framework-Quartz" \
  voice_typer.py

# Inject permissions into Info.plist so macOS prompts automatically
PLIST="dist/QStrauss Voice.app/Contents/Info.plist"

/usr/libexec/PlistBuddy -c \
  "Add :NSMicrophoneUsageDescription string 'QStrauss Voice needs microphone access to transcribe your voice.'" \
  "$PLIST" 2>/dev/null || \
/usr/libexec/PlistBuddy -c \
  "Set :NSMicrophoneUsageDescription 'QStrauss Voice needs microphone access to transcribe your voice.'" \
  "$PLIST"

/usr/libexec/PlistBuddy -c \
  "Add :LSUIElement bool true" \
  "$PLIST" 2>/dev/null || \
/usr/libexec/PlistBuddy -c \
  "Set :LSUIElement true" \
  "$PLIST"

/usr/libexec/PlistBuddy -c \
  "Set :CFBundleIdentifier com.qstrauss.voice" \
  "$PLIST"

/usr/libexec/PlistBuddy -c \
  "Set :CFBundleDisplayName QStrauss Voice" \
  "$PLIST" 2>/dev/null || \
/usr/libexec/PlistBuddy -c \
  "Add :CFBundleDisplayName string 'QStrauss Voice'" \
  "$PLIST"

# Codesign the app with entitlements (required for microphone permission dialog)
echo "Signing app with entitlements..."
codesign --deep --force --sign - \
  --entitlements "$SCRIPT_DIR/entitlements.plist" \
  "dist/QStrauss Voice.app"

echo ""
echo "==================================="
echo "  Build complete!"
echo ""
echo "  App: dist/QStrauss Voice.app"
echo ""
echo "  To install:"
echo "    cp -r 'dist/QStrauss Voice.app' /Applications/"
echo ""
echo "  First launch will ask for:"
echo "    - Microphone access (required)"
echo "    - Accessibility access (for hotkey)"
echo "      System Settings → Privacy → Accessibility"
echo "==================================="
