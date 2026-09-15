"""Compatibility wrapper for the canonical governance preflight."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from personal_knowledge.governance.preflight import Finding, main

__all__ = ["Finding", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
