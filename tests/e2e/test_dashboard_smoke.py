"""P1: dashboard 模块可 import，路径常量正确。"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def test_dashboard_import_and_paths() -> None:
    import personal_knowledge.services.dashboard as dash

    assert dash.ROOT == _ROOT
    assert dash.INTEGRATED_DB.name == "personal_system.sqlite"
    assert "integration" in dash.INTEGRATED_DB.parts
    # rules 应可从分包路径解析
    assert hasattr(dash, "_rules")
    assert hasattr(dash._rules, "PURE_TOPIC_RULES") or hasattr(dash._rules, "__file__")
