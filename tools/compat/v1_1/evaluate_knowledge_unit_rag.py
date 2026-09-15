"""Compatibility shim -> knowledge.evaluate_knowledge_unit_rag

Legacy CLI: python integration/scripts/evaluate_knowledge_unit_rag.py
Preferred:  python -m knowledge.evaluate_knowledge_unit_rag
"""
from __future__ import annotations

from importlib import import_module
import sys

_real = import_module("personal_knowledge.domains.knowledge.evaluate_knowledge_unit_rag")

if __name__ == "__main__":
    # Do not rebind __main__; invoke real entrypoint cleanly.
    if hasattr(_real, "main") and callable(getattr(_real, "main")):
        raise SystemExit(_real.main())
    import runpy
    raise SystemExit(runpy.run_module("personal_knowledge.domains.knowledge.evaluate_knowledge_unit_rag", run_name="__main__"))

# When imported as legacy top-level name, rebind so private symbols work.
sys.modules[__name__] = _real
