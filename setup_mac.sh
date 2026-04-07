#!/bin/bash
# QStrauss Voice — Mac Setup
# Requires: Python 3.10+, Homebrew

set -e

echo "==================================="
echo "  QStrauss Voice — Mac Setup"
echo "==================================="

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 not found. Install from https://python.org"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Python $PYTHON_VERSION found."

# Create virtual environment
echo ""
echo "Creating virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt

echo ""
echo "==================================="
echo "  Setup complete!"
echo ""
echo "  To run QStrauss Voice:"
echo "    source .venv/bin/activate"
echo "    python voice_typer.py"
echo ""
echo "  IMPORTANT: On macOS you must grant"
echo "  Accessibility + Microphone access"
echo "  to Terminal (or your IDE) in:"
echo "  System Settings → Privacy & Security"
echo "==================================="
