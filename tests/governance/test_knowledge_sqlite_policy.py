"""Keep knowledge-product write paths on the FK-enforcing connection policy."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / "src" / "personal_knowledge" / "application" / "knowledge"
RAW_WRITABLE_CONNECT = re.compile(r"sqlite3\.connect\((?!f?[\"']file:).*\)")
ALLOWED_SPECIALISTS = {
    "migrate_add_knowledge_unit_tables.py",
}


def test_knowledge_write_paths_use_fk_connection_factory() -> None:
    violations: list[str] = []
    for path in sorted(KNOWLEDGE.glob("*.py")):
        if path.name in ALLOWED_SPECIALISTS:
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if RAW_WRITABLE_CONNECT.search(line):
                violations.append(f"{path.name}:{line_no}")
    assert violations == []
