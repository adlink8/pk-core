"""Strongly typed cross-database Personal/External decision-context binding (OC-10).

Re-export facade. The canonical implementation now lives in the
``external_context`` package (``personal_knowledge.external_context.binding``)
because the binding contract depends on the external snapshot read primitives.
Keep all decision-analysis consumers importing from
``personal_knowledge.intelligence.decision.context_binding`` so existing tests
and call sites are unaffected; no new logic belongs here.
"""
from __future__ import annotations

from personal_knowledge.external_context.binding import (  # noqa: F401
    DecisionContextBinding,
    DecisionContextBindingError,
    DecisionContextPolicy,
    create_decision_context_binding,
    read_decision_context_binding,
    validate_decision_context_binding,
)

__all__ = [
    "DecisionContextBinding", "DecisionContextBindingError", "DecisionContextPolicy",
    "create_decision_context_binding", "read_decision_context_binding",
    "validate_decision_context_binding",
]
