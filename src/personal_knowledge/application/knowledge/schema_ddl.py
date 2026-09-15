"""Canonical knowledge-unit schema DDL (OC-10).

Re-export facade: the canonical SQL constants live in the neutral
``personal_knowledge.core.schema_ddl`` module so consumers outside the
application layer (e.g. the intelligence decision CLI sandbox) can reference
them without importing into ``application``.
"""
from __future__ import annotations

from personal_knowledge.core.schema_ddl import (  # noqa: F401
    EXTRACTION_GATES_TABLE_SQL,
    INVENTORY_REGISTRY_TABLE_SQL,
    RUN_ITEMS_TABLE_SQL,
    SCHEMA_SQL,
)

__all__ = [
    "EXTRACTION_GATES_TABLE_SQL",
    "INVENTORY_REGISTRY_TABLE_SQL",
    "RUN_ITEMS_TABLE_SQL",
    "SCHEMA_SQL",
]
