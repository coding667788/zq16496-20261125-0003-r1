#!/usr/bin/env python3
"""Convenience launcher: ``python vaultdrill.py evaluate CASE PLAN``."""

import sys
from pathlib import Path

# Make the package importable even under ``python -I vaultdrill.py``. This inserts
# the trusted launcher directory, never the caller's current working directory.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from vaultdrill.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
