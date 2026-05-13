#!/usr/bin/env python3
"""Entry point that lets you run `python run.py ...` from the project root.

Examples:
    python run.py --once --dry-run --no-scrape   # smoke-test the math
    python run.py --once                          # one cycle, scrape + email
    python run.py                                  # loop forever
"""
import sys
from pathlib import Path

# Make the src/ package importable when running from the project root.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from src.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
