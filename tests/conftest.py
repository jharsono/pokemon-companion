"""Shared test settings: where the app lives and how to run it as a process."""

import sys
from pathlib import Path

# Importing app.py for the unit tests must not leave a __pycache__ in the
# folder that ships to the gallery.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "pokemon-companion"
TOOLS = ROOT / "tools"
STRICT_PYTHON = [sys.executable, "-I", "-W", "error"]
