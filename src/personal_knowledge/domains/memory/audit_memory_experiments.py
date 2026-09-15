"""Re-export facade — retained for backward compatibility during the 2026-08-13 cleanup window.
Canonical location: evaluation/memory/audit_memory_experiments.py
Do not add new logic here; import from the canonical path in new code.
"""
from importlib import import_module as _import_module
import sys as _sys

_canonical = _import_module("personal_knowledge.evaluation.memory.audit_memory_experiments")
_sys.modules[__name__] = _canonical
