"""Composite serving snapshot lifecycle with lazy public imports."""

from importlib import import_module

__all__ = [
    "activate_snapshot", "get_active_snapshot", "prepare_snapshot",
    "rollback_snapshot", "validate_snapshot",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    return getattr(import_module(".snapshots", __name__), name)
