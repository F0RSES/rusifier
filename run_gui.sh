#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
export LD_LIBRARY_PATH="$PWD/.venv/lib/python3.13/site-packages/PySide6/Qt/lib:/usr/lib/x86_64-linux-gnu"
python -m src.gui.main
