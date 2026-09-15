"""Versioned evaluation contracts for Phase 17.

Schemas are intentionally strict and fail-closed: missing required fields or
checksum mismatches raise ContractError / return non-zero from CLI helpers.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ALLOWED_MODES = ("raw", "l1", "l2_only", "l1_l2", "hybrid")
ALLOWED_SPLITS = (
    "synthetic",
    "dev",
    "frozen_test",
    "regression_slice",
    "holdout_15_02",
    "comprehensive_v1",
    "cross_turn",
    "paraphrase",
    "no_answer",
    "conflict_temporal",
    "privacy",
    "google",
)


class ContractError(ValueError):
    """Raised when a contract object fails validation."""


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_checksum(payload: Any) -> str:
    """SHA256 of canonical JSON (or UTF-8 text)."""
    if isinstance(payload, (bytes, bytearray)):
        data = bytes(payload)
    elif isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = _stable_json(payload).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def file_checksum(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class EvalCase:
    """One evaluation query with gold and scenario metadata."""

    id: str
    split: str
    query: str
    gold_evidence_refs: list[str] = field(default_factory=list)
    gold_unit_ids: list[str] = field(default_factory=list)
    gold_title_substrings: list[str] = field(default_factory=list)
    allowed_unit_types: list[str] = field(default_factory=list)
    expected_abstain: bool = False
    expected_conflict: bool = False
    privacy_sensitive: bool = False
    secret_ineligible: bool = False
    scenario: str = ""
    suite_tag: str = ""
    group: str = ""
    notes: str = ""
    gold_provenance: str = ""
    forbid_subject_substrings: list[str] = field(default_factory=list)
    expected_layer: str = ""
    requires_cross_turn: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.id or not str(self.id).strip():
            raise ContractError("EvalCase.id is required")
        if not self.query or not str(self.query).strip():
            raise ContractError(f"EvalCase {self.id}: query is required")
        if not self.split or not str(self.split).strip():
            raise ContractError(f"EvalCase {self.id}: split is required")
        if not isinstance(self.expected_abstain, bool):
            raise ContractError(f"EvalCase {self.id}: expected_abstain must be bool")
        if self.expected_abstain and (self.gold_evidence_refs or self.gold_unit_ids):
            # Allowed but unusual; keep soft — scorer treats abstain primarily.
            pass
        if self.privacy_sensitive and self.secret_ineligible is False:
            # privacy flag alone is fine
            pass

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvalCase":
        if not isinstance(raw, Mapping):
            raise ContractError("EvalCase must be a mapping")
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        # Accept legacy field aliases
        if "suite_tag" not in data and raw.get("tag"):
            data["suite_tag"] = raw["tag"]
        if "scenario" not in data or not data.get("scenario"):
            data["scenario"] = (
                raw.get("scenario")
                or raw.get("suite_tag")
                or raw.get("group")
                or raw.get("split")
                or ""
            )
        return cls(**data)


@dataclass
class DatasetManifest:
    """Immutable dataset snapshot descriptor."""

    dataset_id: str
    version: str
    cases_path: str
    case_count: int
    checksum: str
    splits: dict[str, int] = field(default_factory=dict)
    scenarios: dict[str, int] = field(default_factory=dict)
    private: bool = False
    notes: str = ""

    def validate(self) -> None:
        if not self.dataset_id:
            raise ContractError("DatasetManifest.dataset_id is required")
        if not self.version:
            raise ContractError("DatasetManifest.version is required")
        if not self.cases_path:
            raise ContractError("DatasetManifest.cases_path is required")
        if self.case_count < 0:
            raise ContractError("DatasetManifest.case_count must be >= 0")
        if not re.fullmatch(r"[0-9a-f]{64}", self.checksum or ""):
            raise ContractError("DatasetManifest.checksum must be sha256 hex")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DatasetManifest":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        obj = cls(**data)
        obj.validate()
        return obj


@dataclass
class EvalTarget:
    """One retrieval target (mode + collection/filter config)."""

    mode: str
    target_id: str = ""
    collection: str = ""
    collection_checksum: str = ""
    embed_model: str = "bge-small-zh-v1.5"
    top_k: int = 5
    fallback_policy: str = ""
    lineage_filter: dict[str, Any] = field(default_factory=dict)
    blocked: bool = False
    blocked_reason: str = ""

    def __post_init__(self) -> None:
        if not self.target_id:
            self.target_id = f"{self.mode}:{self.collection or 'default'}"
        self.validate()

    def validate(self) -> None:
        if self.mode not in ALLOWED_MODES:
            raise ContractError(f"EvalTarget.mode must be one of {ALLOWED_MODES}")
        if self.top_k < 1:
            raise ContractError("EvalTarget.top_k must be >= 1")
        if self.blocked and not self.blocked_reason:
            raise ContractError("blocked targets require blocked_reason")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvalTarget":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        return cls(**data)


@dataclass
class EvalRunManifest:
    """Immutable run descriptor shared by all modes in one benchmark."""

    run_id: str
    dataset_checksum: str
    config_checksum: str
    scorer_version: str
    top_k: int
    modes: list[str]
    targets: list[dict[str, Any]] = field(default_factory=list)
    embed_model: str = "bge-small-zh-v1.5"
    created_at: str = ""
    policy_version: str = "v1"
    notes: str = ""

    def validate(self) -> None:
        if not self.run_id:
            raise ContractError("EvalRunManifest.run_id is required")
        if not re.fullmatch(r"[0-9a-f]{64}", self.dataset_checksum or ""):
            raise ContractError("dataset_checksum must be sha256 hex")
        if not re.fullmatch(r"[0-9a-f]{64}", self.config_checksum or ""):
            raise ContractError("config_checksum must be sha256 hex")
        if not self.scorer_version:
            raise ContractError("scorer_version is required")
        if self.top_k < 1:
            raise ContractError("top_k must be >= 1")
        if not self.modes:
            raise ContractError("modes is required")
        for m in self.modes:
            if m not in ALLOWED_MODES:
                raise ContractError(f"unknown mode {m}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvalRunManifest":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        obj = cls(**data)
        obj.validate()
        return obj


def load_cases_jsonl(path: Path) -> list[EvalCase]:
    if not path.exists():
        raise ContractError(f"cases file not found: {path}")
    cases: list[EvalCase] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            raise ContractError(f"{path}:{i}: invalid JSON: {e}") from e
        try:
            cases.append(EvalCase.from_dict(raw))
        except ContractError as e:
            raise ContractError(f"{path}:{i}: {e}") from e
    return cases


def cases_checksum(cases: Sequence[EvalCase | Mapping[str, Any]]) -> str:
    payload = [
        c.to_dict() if isinstance(c, EvalCase) else dict(c) for c in cases
    ]
    return content_checksum(payload)


def build_dataset_manifest(
    dataset_id: str,
    version: str,
    cases_path: Path,
    *,
    private: bool = False,
    notes: str = "",
) -> DatasetManifest:
    cases = load_cases_jsonl(cases_path)
    splits: dict[str, int] = {}
    scenarios: dict[str, int] = {}
    for c in cases:
        splits[c.split] = splits.get(c.split, 0) + 1
        key = c.scenario or c.suite_tag or c.group or "untagged"
        scenarios[key] = scenarios.get(key, 0) + 1
    return DatasetManifest(
        dataset_id=dataset_id,
        version=version,
        cases_path=str(cases_path).replace("\\", "/"),
        case_count=len(cases),
        checksum=cases_checksum(cases),
        splits=splits,
        scenarios=scenarios,
        private=private,
        notes=notes,
    )


def audit_dataset(
    cases: Sequence[EvalCase],
    *,
    require_gold_resolvable: bool = False,
    resolvable_refs: set[str] | None = None,
) -> dict[str, Any]:
    """Structural audit: duplicates, empty gold for non-abstain, leakage soft checks."""
    errors: list[str] = []
    warnings: list[str] = []
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        errors.append("duplicate case ids")
    by_split: dict[str, set[str]] = {}
    for c in cases:
        by_split.setdefault(c.split, set()).add(c.id)
        if not c.expected_abstain and not (
            c.gold_evidence_refs or c.gold_unit_ids or c.gold_title_substrings
        ):
            warnings.append(f"{c.id}: non-abstain case has no gold refs")
        if require_gold_resolvable and resolvable_refs is not None:
            for ref in c.gold_evidence_refs:
                if ref not in resolvable_refs and not ref.startswith("syn-"):
                    errors.append(f"{c.id}: unresolvable gold ref {ref}")
    # split leakage: identical query text across dev and frozen
    frozen_q = {c.query.strip() for c in cases if "frozen" in c.split}
    dev_q = {c.query.strip() for c in cases if c.split == "dev"}
    leak = frozen_q & dev_q
    if leak:
        errors.append(f"split leakage: {len(leak)} shared queries between dev and frozen")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "case_count": len(cases),
        "splits": {k: len(v) for k, v in by_split.items()},
        "leakage_shared_queries": len(leak),
    }


def compute_run_id(
    dataset_checksum: str,
    config_checksum: str,
    scorer_version: str,
    modes: Iterable[str],
    top_k: int,
) -> str:
    payload = {
        "dataset_checksum": dataset_checksum,
        "config_checksum": config_checksum,
        "scorer_version": scorer_version,
        "modes": list(modes),
        "top_k": top_k,
    }
    return content_checksum(payload)


def config_checksum(config: Mapping[str, Any]) -> str:
    return content_checksum(dict(config))


def dump_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
