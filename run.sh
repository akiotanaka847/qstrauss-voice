#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
# exec -a sets argv[0] BEFORE the Python interpreter starts
# macOS reads argv[0] for the menu bar / dock / Cmd+Tab name
exec -a "QStrauss Voice" python voice_typer.py
