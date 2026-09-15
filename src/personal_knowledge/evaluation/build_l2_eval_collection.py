"""Build an exact, non-active L2-only Chroma collection for evaluation.

The builder copies existing vectors from a source knowledge collection. It never
deletes a collection, changes the active pointer, or advances a watermark.
Existing target rows may only be completed when they are a subset of the
expected lineage IDs; unexpected rows fail closed.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from personal_knowledge.core.chroma_client import ChromaClient
from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.evaluation.retrieval_adapters import (
    L2_RUN_IDS_DEFAULT,
    collection_ids,
    l2_eval_collection_name,
    load_l2_unit_ids,
)
from personal_knowledge.evaluation.run_knowledge_eval import _read_active

ROOT = Path(__file__).resolve().parents[3]
REPORT_DIR = ROOT / "var" / "reports" / "analysis" / "evaluations" / "l2_only_collections"


@dataclass
class L2CollectionBuild:
    source_collection: str
    target_collection: str
    lineage_ids: int
    source_found: int
    existing_target: int
    written: int
    final_count: int
    missing: int
    orphan: int
    write: bool
    gate_passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def target_name(source_collection: str, lineage_ids: set[str]) -> str:
    return l2_eval_collection_name(source_collection, lineage_ids)


def _collection_exists(client: ChromaClient, name: str) -> bool:
    return name in {str(c.get("name")) for c in client.list_collections()}


def build_l2_eval_collection(
    *,
    source_collection: str = "",
    target_collection: str = "",
    write: bool = False,
    client: ChromaClient | None = None,
    report_dir: Path = REPORT_DIR,
) -> L2CollectionBuild:
    source = source_collection or _read_active()
    if not source:
        raise RuntimeError("active/source collection is unavailable")
    lineage_ids = load_l2_unit_ids(UNIFIED_DB, L2_RUN_IDS_DEFAULT)
    if not lineage_ids:
        raise RuntimeError("L2 lineage ID set is empty")
    target = target_collection or target_name(source, lineage_ids)
    if target == source:
        raise ValueError("target collection must differ from source collection")

    chroma = client or ChromaClient()
    if not _collection_exists(chroma, source):
        raise RuntimeError(f"source collection does not exist: {source}")
    source_coll = chroma.get_or_create_collection(source)

    source_rows: dict[str, tuple[list[float], str, dict[str, Any]]] = {}
    ordered = sorted(lineage_ids)
    for offset in range(0, len(ordered), 200):
        raw = source_coll.get(
            ids=ordered[offset : offset + 200],
            include=["embeddings", "documents", "metadatas"],
            timeout=120,
        )
        ids = raw.get("ids") or []
        embeddings = raw.get("embeddings") or []
        documents = raw.get("documents") or []
        metadatas = raw.get("metadatas") or []
        for idx, unit_id in enumerate(ids):
            source_rows[str(unit_id)] = (
                list(embeddings[idx]),
                str(documents[idx] or ""),
                dict(metadatas[idx] or {}),
            )
    source_found = set(source_rows)
    if source_found != lineage_ids:
        raise RuntimeError(
            f"source collection is missing {len(lineage_ids - source_found)} L2 lineage IDs"
        )

    target_exists = _collection_exists(chroma, target)
    existing_ids: set[str] = set()
    target_coll = None
    if target_exists:
        target_coll = chroma.get_or_create_collection(target)
        existing_ids = collection_ids(target_coll)
        orphans = existing_ids - lineage_ids
        if orphans:
            raise RuntimeError(
                f"target collection contains {len(orphans)} unexpected IDs"
            )

    missing_ids = sorted(lineage_ids - existing_ids)
    written = 0
    if write and missing_ids:
        target_coll = target_coll or chroma.get_or_create_collection(
            target,
            metadata={
                "hnsw:space": "cosine",
                "purpose": "phase17_l2_only_eval",
                "source_collection": source,
            },
        )
        for offset in range(0, len(missing_ids), 200):
            batch = missing_ids[offset : offset + 200]
            target_coll.upsert(
                ids=batch,
                embeddings=[source_rows[unit_id][0] for unit_id in batch],
                documents=[source_rows[unit_id][1] for unit_id in batch],
                metadatas=[source_rows[unit_id][2] for unit_id in batch],
                timeout=300,
            )
            written += len(batch)

    final_ids = collection_ids(target_coll) if write and target_coll is not None else existing_ids
    result = L2CollectionBuild(
        source_collection=source,
        target_collection=target,
        lineage_ids=len(lineage_ids),
        source_found=len(source_found),
        existing_target=len(existing_ids),
        written=written,
        final_count=len(final_ids),
        missing=len(lineage_ids - final_ids),
        orphan=len(final_ids - lineage_ids),
        write=write,
        gate_passed=write and final_ids == lineage_ids,
    )
    if write:
        report_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            **result.to_dict(),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lineage_run_ids": list(L2_RUN_IDS_DEFAULT),
        }
        (report_dir / f"{target}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="", help="source collection; default current Active")
    parser.add_argument("--target", default="", help="target collection; default deterministic name")
    parser.add_argument("--write", action="store_true", help="create/complete target collection")
    args = parser.parse_args()
    result = build_l2_eval_collection(
        source_collection=args.source,
        target_collection=args.target,
        write=args.write,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    passed = result.gate_passed if args.write else result.source_found == result.lineage_ids
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
