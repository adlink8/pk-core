"""Compatibility entry for the canonical source migration executor."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from personal_knowledge.governance.apply_source_migration import main  # noqa: E402


if __name__ == "__main__":
    main()
