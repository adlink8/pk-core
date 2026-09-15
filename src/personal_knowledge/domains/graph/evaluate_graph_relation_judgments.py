"""Re-export facade — retained for backward compatibility during the 2026-08-13 cleanup window.
Canonical location: evaluation/graph/evaluate_graph_relation_judgments.py
Do not add new logic here; import from the canonical path in new code.
"""
from importlib import import_module as _import_module
import sys as _sys

_canonical = _import_module("personal_knowledge.evaluation.graph.evaluate_graph_relation_judgments")
_sys.modules[__name__] = _canonical
