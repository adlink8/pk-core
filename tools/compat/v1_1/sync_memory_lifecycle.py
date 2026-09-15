"""Compatibility shim -> memory.sync_memory_lifecycle

Legacy CLI: python integration/scripts/sync_memory_lifecycle.py
Preferred:  python -m memory.sync_memory_lifecycle
"""
from __future__ import annotations

from importlib import import_module
import sys

_real = import_module("personal_knowledge.domains.memory.sync_memory_lifecycle")

if __name__ == "__main__":
    # Do not rebind __main__; invoke real entrypoint cleanly.
    if hasattr(_real, "main") and callable(getattr(_real, "main")):
        raise SystemExit(_real.main())
    import runpy
    raise SystemExit(runpy.run_module("personal_knowledge.domains.memory.sync_memory_lifecycle", run_name="__main__"))

# When imported as legacy top-level name, rebind so private symbols work.
sys.modules[__name__] = _real
