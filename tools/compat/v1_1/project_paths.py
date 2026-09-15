"""Compatibility shim -> core.project_paths

Legacy CLI: python integration/scripts/project_paths.py
Preferred:  python -m core.project_paths
"""
from __future__ import annotations

from importlib import import_module
import sys

_real = import_module("personal_knowledge.core.project_paths")

if __name__ == "__main__":
    # Do not rebind __main__; invoke real entrypoint cleanly.
    if hasattr(_real, "main") and callable(getattr(_real, "main")):
        raise SystemExit(_real.main())
    import runpy
    raise SystemExit(runpy.run_module("personal_knowledge.core.project_paths", run_name="__main__"))

# When imported as legacy top-level name, rebind so private symbols work.
sys.modules[__name__] = _real
