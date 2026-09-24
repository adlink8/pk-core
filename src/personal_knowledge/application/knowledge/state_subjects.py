"""Shared state-subject registry and deterministic matching helpers.

The registry's ``prefix`` rule means that the normalized subject starts with
the normalized pattern.  A longer subject pattern never matches a shorter
subject in the reverse direction.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


DEFAULT_STATE_SUBJECTS = (
    Path(__file__).resolve().parents[4] / "assets" / "knowledge" / "state_subjects.yaml"
)
_MATCH_MODES = {"exact", "prefix"}


class StateSubjectsError(ValueError):
    """Invalid or unreadable state subject registry."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        message = reason if not detail else f"{reason}: {detail}"
        super().__init__(message)


def normalize_subject(text: str | None) -> str:
    """Lowercase a subject and remove whitespace/punctuation, preserving CJK."""

    return re.sub(r"[^\w]+", "", (text or "").lower())


def load_state_subjects(path: Path = DEFAULT_STATE_SUBJECTS) -> dict[str, Any]:
    """Load and validate the versioned YAML registry."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StateSubjectsError("registry_unreadable", str(exc)) from exc

    if not isinstance(raw, dict):
        raise StateSubjectsError("invalid_registry_root")
    families = raw.get("families")
    if not isinstance(families, list) or not families:
        raise StateSubjectsError("invalid_families")

    for family in families:
        if not isinstance(family, dict) or not isinstance(family.get("name"), str):
            raise StateSubjectsError("invalid_family")
        subjects = family.get("subjects")
        if not isinstance(subjects, list) or not subjects:
            raise StateSubjectsError("invalid_subjects", str(family.get("name")))
        for rule in subjects:
            if not isinstance(rule, dict) or not isinstance(rule.get("pattern"), str):
                raise StateSubjectsError("invalid_subject_rule", str(family["name"]))
            if rule.get("match") not in _MATCH_MODES:
                raise StateSubjectsError("invalid_match_mode", str(rule.get("match")))

    return raw


def match_state_subject(subject: str | None, rules: dict[str, Any]) -> str | None:
    """Return the first matching family, preferring exact over prefix rules."""

    normalized = normalize_subject(subject)
    if not normalized:
        return None
    families = rules.get("families", [])
    for mode in ("exact", "prefix"):
        for family in families:
            for rule in family.get("subjects", []):
                pattern = normalize_subject(rule.get("pattern"))
                if rule.get("match") == mode and (
                    normalized == pattern if mode == "exact" else normalized.startswith(pattern)
                ):
                    return family["name"]
    return None


__all__ = [
    "DEFAULT_STATE_SUBJECTS",
    "StateSubjectsError",
    "load_state_subjects",
    "match_state_subject",
    "normalize_subject",
]
