"""source adapter 基类与 canonical record 契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable


@dataclass(frozen=True)
class CanonicalRecord:
    """统一后的最小记录形状。"""
    source_type: str
    source_id: str
    title: str
    content: str
    created_at: str
    updated_at: str
    metadata: dict
    source_path: str
    source_hash: str

    def to_dict(self) -> dict:
        return asdict(self)


class SourceAdapter:
    source_type: str = ""

    def iter_records(self, limit: int | None = None) -> Iterable[CanonicalRecord]:
        """产出 canonical record。"""
        raise NotImplementedError
