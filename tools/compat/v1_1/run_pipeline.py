"""Compatibility shim -> pipeline.run_pipeline

Legacy CLI: python integration/scripts/run_pipeline.py
Preferred:  python -m pipeline.run_pipeline
"""
from __future__ import annotations

from importlib import import_module
import sys

_real = import_module("personal_knowledge.application.run_pipeline")

if __name__ == "__main__":
    # Do not rebind __main__; invoke real entrypoint cleanly.
    if hasattr(_real, "main") and callable(getattr(_real, "main")):
        raise SystemExit(_real.main())
    import runpy
    raise SystemExit(runpy.run_module("personal_knowledge.application.run_pipeline", run_name="__main__"))

# When imported as legacy top-level name, rebind so private symbols work.
sys.modules[__name__] = _real
