"""Compatibility shim -> memory.evaluate_memory_depth

Legacy CLI: python integration/scripts/evaluate_memory_depth.py
Preferred:  python -m memory.evaluate_memory_depth
"""
from __future__ import annotations

from importlib import import_module
import sys

_real = import_module("personal_knowledge.domains.memory.evaluate_memory_depth")

if __name__ == "__main__":
    # Do not rebind __main__; invoke real entrypoint cleanly.
    if hasattr(_real, "main") and callable(getattr(_real, "main")):
        raise SystemExit(_real.main())
    import runpy
    raise SystemExit(runpy.run_module("personal_knowledge.domains.memory.evaluate_memory_depth", run_name="__main__"))

# When imported as legacy top-level name, rebind so private symbols work.
sys.modules[__name__] = _real
