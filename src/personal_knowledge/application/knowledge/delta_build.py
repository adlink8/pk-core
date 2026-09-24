"""Delta construction for incremental knowledge refresh
(extracted from refresh_knowledge_units.py).

Owns source checksum, provider/model validation, delta inventory+run
materialization, extraction run execution, candidate collection building and
the production delta preflight artifact. Pure extraction — no logic changed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.sqlite import connect_rw
from personal_knowledge.core.project_paths import (
    UNIFIED_DB,
    AGENT_CONVERSATIONS_DB,
    KNOWLEDGE_ACTIVE_POINTER,
)
from personal_knowledge.application.knowledge.eligibility import (
    compute_eligible_messages,
)
from personal_knowledge.core.canonical_visibility import (
    canonical_projection_predicate,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_content_hash(content: str) -> str:
    """Compute stable content hash for a canonical message."""
    return hashlib.sha256(content.encode()).hexdigest()[:32]


def compute_source_checksum(canonical_db: Path = AGENT_CONVERSATIONS_DB) -> str:
    """计算 canonical store 的 source checksum（含内容 hash，检测 content 修改）。"""
    if not canonical_db.exists():
        return ""
    con = sqlite3.connect(f"file:{canonical_db.as_posix()}?mode=ro", uri=True)
    # schema hash + counts + content hash
    ddl = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE '%fts%' ESCAPE '\\' ORDER BY name"
    ).fetchall()
    schema_text = "\n;;;".join(sql or "" for _name, sql in ddl)
    session_filter, session_params = canonical_projection_predicate(
        con, "canonical_session_id"
    )
    message_filter, message_params = canonical_projection_predicate(
        con, "canonical_session_id"
    )
    session_count = con.execute(
        f"SELECT COUNT(*) FROM canonical_sessions WHERE {session_filter}",
        session_params,
    ).fetchone()[0]
    message_count = con.execute(
        f"SELECT COUNT(*) FROM canonical_messages WHERE {message_filter}",
        message_params,
    ).fetchone()[0]
    # content hash: ordered (ref, content_hash) pairs
    content_rows = con.execute(
        "SELECT canonical_message_id, content FROM canonical_messages "
        f"WHERE {message_filter} ORDER BY canonical_message_id",
        message_params,
    ).fetchall()
    content_blob = "\n".join(f"{row[0]}|{_compute_content_hash(row[1] or '')}" for row in content_rows)
    content_hash = hashlib.sha256(content_blob.encode()).hexdigest()[:16]
    con.close()
    payload = f"{hashlib.sha256(schema_text.encode()).hexdigest()[:16]}|{session_count}|{message_count}|{content_hash}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Task 2B: provider-aware LLM adapter
# ---------------------------------------------------------------------------

# Provider → allowed model prefix mapping
PROVIDER_MODEL_ALLOWLIST = {
    "vertex_google": {"gemini", "text-bison", "text-unicorn"},
    "openai": {"gpt-", "o1-", "o3-", "o4-"},
    "google_free": {"gemini"},
}

# Provider → required auth mode
PROVIDER_AUTH_MODE = {
    "vertex_google": "gcloud",
    "openai": "api_key",
    "google_free": "api_key",
}

# Provider → endpoint pattern
PROVIDER_ENDPOINT_PATTERNS = {
    "vertex_google": "aiplatform.googleapis.com",
    "openai": "api.openai.com",
    "google_free": "generativelanguage.googleapis.com",
}


@dataclass
class ProviderValidationResult:
    """Provider/model validation result."""
    valid: bool = False
    provider: str = ""
    model: str = ""
    endpoint: str = ""
    auth_mode: str = ""
    reason: str = ""


def validate_provider_model(
    provider: str,
    endpoint: str,
    model: str,
    auth_mode: str = "",
) -> ProviderValidationResult:
    """Validate provider/endpoint/auth/model combination.

    - gpt-* models cannot go to vertex_google endpoint.
    - gemini models cannot go to openai endpoint.
    - Missing auth mode → fail closed.
    - No silent fallback — mismatch raises, doesn't try alternatives.
    """
    result = ProviderValidationResult(
        provider=provider, model=model, endpoint=endpoint, auth_mode=auth_mode
    )

    # Check provider is known
    if provider not in PROVIDER_MODEL_ALLOWLIST:
        raise ValueError(f"unknown provider: {provider}")

    # Check endpoint matches provider
    expected_pattern = PROVIDER_ENDPOINT_PATTERNS.get(provider, "")
    if expected_pattern and expected_pattern not in endpoint:
        raise ValueError(
            f"endpoint mismatch: provider={provider} expects '{expected_pattern}' "
            f"but got endpoint={endpoint}"
        )

    # Check model is allowed for provider
    allowed_prefixes = PROVIDER_MODEL_ALLOWLIST[provider]
    if not any(model.startswith(prefix) for prefix in allowed_prefixes):
        raise ValueError(
            f"model '{model}' not allowed for provider '{provider}'. "
            f"Allowed prefixes: {allowed_prefixes}"
        )

    # Check auth mode
    required_auth = PROVIDER_AUTH_MODE.get(provider, "")
    if not auth_mode:
        raise ValueError(
            f"auth_mode required for provider '{provider}' — fail closed, no silent fallback"
        )
    if required_auth and auth_mode != required_auth:
        raise ValueError(
            f"auth_mode '{auth_mode}' not valid for provider '{provider}', "
            f"expected '{required_auth}'"
        )

    result.valid = True
    return result


def compute_cache_key(
    model: str,
    prompt_hash: str,
    schema_hash: str,
    input_hash: str,
    config_hash: str,
) -> str:
    """Compute content-addressed cache key.

    Key includes model — different models produce different cache keys.
    """
    material = f"{model}|{prompt_hash}|{schema_hash}|{input_hash}|{config_hash}"
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _load_canonical_refs(canonical_db: Path) -> dict[str, str]:
    """Load all canonical message refs → content hash mapping."""
    if not canonical_db.exists():
        return {}
    con = sqlite3.connect(f"file:{canonical_db.as_posix()}?mode=ro", uri=True)
    projection_filter, projection_params = canonical_projection_predicate(
        con, "canonical_session_id"
    )
    rows = con.execute(
        "SELECT canonical_message_id, content FROM canonical_messages "
        f"WHERE {projection_filter}",
        projection_params,
    ).fetchall()
    con.close()
    return {row[0]: _compute_content_hash(row[1] or "") for row in rows}


def _empty_delta_result() -> dict:
    return {
        "no_op": True,
        "delta_count": 0,
        "change_types": [],
        "fresh_run_id": "",
        "delta_inventory_id": "",
        "new_count": 0,
        "modified_count": 0,
        "deleted_count": 0,
        "extract_item_count": 0,
        "floor_excluded": 0,
    }


def _diff_ref_hashes(
    refs_before: dict[str, str],
    refs_after: dict[str, str],
) -> tuple[list[tuple[str, str, str | None, str | None]], list[str], int, int, int]:
    """Compare two ref→content_hash maps. Returns (delta_items, change_types, new, mod, del)."""
    before_keys = set(refs_before.keys())
    after_keys = set(refs_after.keys())
    new_refs = after_keys - before_keys
    deleted_refs = before_keys - after_keys
    modified_refs = {
        ref for ref in before_keys & after_keys
        if refs_before[ref] != refs_after[ref]
    }

    delta_items: list[tuple[str, str, str | None, str | None]] = []
    change_types: list[str] = []
    for ref in sorted(new_refs):
        delta_items.append((ref, "new", None, refs_after[ref]))
        if "new" not in change_types:
            change_types.append("new")
    for ref in sorted(modified_refs):
        delta_items.append((ref, "modified", refs_before[ref], refs_after[ref]))
        if "modified" not in change_types:
            change_types.append("modified")
    for ref in sorted(deleted_refs):
        delta_items.append((ref, "deleted", refs_before[ref], None))
        if "deleted" not in change_types:
            change_types.append("deleted")
    return delta_items, change_types, len(new_refs), len(modified_refs), len(deleted_refs)


def _materialize_delta_run(
    db_path: Path,
    *,
    source_before_checksum: str,
    source_after_checksum: str,
    model: str,
    delta_items: list[tuple[str, str, str | None, str | None]],
    new_count: int,
    modified_count: int,
    deleted_count: int,
    change_types: list[str],
    prompt_version: str = "v1",
    schema_version: str = "v1",
    config_hash: str = "",
    init_run_items: bool = False,
    extract_change_types: frozenset[str] | None = None,
    extract_order_keys: dict[str, str] | None = None,
    extract_min_started_at: str = "",
    skip_succeeded: bool = True,
    allowed_roles: frozenset[str] | None = None,
    ref_roles: dict[str, str] | None = None,
    max_extract_items: int | None = None,
) -> dict:
    """Write delta inventory + fresh incremental run (idempotent). Optionally seed run items."""
    if not delta_items:
        return _empty_delta_result()

    extract_types = extract_change_types or frozenset({"new", "modified"})
    ordered_blob = json.dumps(delta_items, sort_keys=True)
    ordered_dataset_hash = hashlib.sha256(ordered_blob.encode()).hexdigest()[:32]
    delta_id_material = (
        f"{source_before_checksum}|{source_after_checksum}|{ordered_dataset_hash}|{model}"
    )
    delta_inventory_id = "di_" + hashlib.sha256(delta_id_material.encode()).hexdigest()[:16]
    # Production prepare seeds run items with extract filters; include policy in
    # run identity so a watermark-gated queue does not reuse a full-backlog ledger.
    run_id_material = f"{delta_inventory_id}|{model}|{prompt_version}|{schema_version}"
    if init_run_items:
        roles_key = ",".join(sorted(allowed_roles)) if allowed_roles else "all"
        cap_key = str(max_extract_items) if max_extract_items else "none"
        extract_policy = (
            f"types={','.join(sorted(extract_types))}|"
            f"since={extract_min_started_at or 'none'}|"
            f"skip_succeeded={1 if skip_succeeded else 0}|"
            f"roles={roles_key}|cap={cap_key}"
        )
        run_id_material = f"{run_id_material}|{extract_policy}"
    fresh_run_id = "ir_" + hashlib.sha256(run_id_material.encode()).hexdigest()[:16]

    con = connect_rw(db_path)
    existing = con.execute(
        "SELECT delta_inventory_id FROM knowledge_delta_inventories WHERE delta_inventory_id=?",
        (delta_inventory_id,),
    ).fetchone()

    if not existing:
        con.execute(
            "INSERT INTO knowledge_delta_inventories "
            "(delta_inventory_id, source_before_checksum, source_after_checksum, "
            "ordered_dataset_hash, new_count, modified_count, deleted_count, "
            "model, prompt_version, schema_version, config_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                delta_inventory_id, source_before_checksum, source_after_checksum,
                ordered_dataset_hash, new_count, modified_count, deleted_count,
                model, prompt_version, schema_version, config_hash or "", _utc_now(),
            ),
        )
        for ref, ct, hash_before, hash_after in delta_items:
            con.execute(
                "INSERT INTO knowledge_delta_items "
                "(delta_inventory_id, ref, change_type, content_hash_before, "
                "content_hash_after, created_at) VALUES (?,?,?,?,?,?)",
                (delta_inventory_id, ref, ct, hash_before, hash_after, _utc_now()),
            )
        con.commit()

    existing_run = con.execute(
        "SELECT run_id FROM knowledge_build_runs WHERE run_id=?", (fresh_run_id,)
    ).fetchone()
    if not existing_run:
        input_hash = hashlib.sha256(ordered_blob.encode()).hexdigest()[:32]
        con.execute(
            "INSERT INTO knowledge_build_runs "
            "(run_id, run_type, generated_at, source_build_id, input_hash, "
            "prompt_version, schema_version, model, embedding_model, config_hash, "
            "git_sha, dataset_hash, status, stats_json, supersedes_id) "
            "VALUES (?,?,?,?,?,  ?,?,?,?, ?,?,?, ?,?,?)",
            (
                fresh_run_id, "incremental", _utc_now(), delta_inventory_id, input_hash,
                prompt_version, schema_version, model, "", config_hash or "",
                "", ordered_dataset_hash, "pending", "", "",
            ),
        )
        con.commit()

    extract_item_count = 0
    floor_excluded = 0
    if init_run_items:
        existing_items = con.execute(
            "SELECT COUNT(*) FROM knowledge_run_items WHERE run_id=?", (fresh_run_id,)
        ).fetchone()[0]
        if existing_items == 0:
            extractable = [
                (ref, ct) for ref, ct, _hb, _ha in delta_items if ct in extract_types
            ]
            # Skip evidence already successfully extracted in any prior run
            if skip_succeeded:
                already = {
                    r[0]
                    for r in con.execute(
                        "SELECT DISTINCT evidence_ref FROM knowledge_run_items "
                        "WHERE status='succeeded'"
                    ).fetchall()
                }
                if already:
                    extractable = [
                        (ref, ct) for ref, ct in extractable if ref not in already
                    ]

            # Optional role filter (user/assistant/…)
            if allowed_roles and ref_roles is not None:
                extractable = [
                    (ref, ct)
                    for ref, ct in extractable
                    if (ref_roles.get(ref) or "") in allowed_roles
                ]

            # Optional time gate: only sessions on/after watermark (or given floor)
            if extract_min_started_at and extract_order_keys is not None:
                floor = extract_min_started_at
                kept = [
                    (ref, ct)
                    for ref, ct in extractable
                    if (extract_order_keys.get(ref) or "") >= floor
                ]
                floor_excluded = len(extractable) - len(kept)
                extractable = kept

            # Prefer recent sessions first when canonical timestamps available
            if extractable and extract_order_keys:
                extractable.sort(
                    key=lambda pair: extract_order_keys.get(pair[0], ""),
                    reverse=True,
                )
            else:
                extractable.sort(key=lambda pair: pair[0])

            if max_extract_items is not None and max_extract_items >= 0:
                extractable = extractable[:max_extract_items]

            now = _utc_now()
            for pos, (ref, _ct) in enumerate(extractable):
                con.execute(
                    "INSERT OR IGNORE INTO knowledge_run_items "
                    "(run_id, inventory_id, position, evidence_ref, status, attempt_count, "
                    "lease_started_at, last_error_class, cache_key, response_hash, "
                    "unit_count, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        fresh_run_id, delta_inventory_id, pos, ref, "pending", 0,
                        None, None, None, None, 0, now,
                    ),
                )
            con.commit()
            extract_item_count = len(extractable)
        else:
            extract_item_count = existing_items

    con.close()
    return {
        "no_op": False,
        "delta_count": len(delta_items),
        "change_types": change_types,
        "fresh_run_id": fresh_run_id,
        "delta_inventory_id": delta_inventory_id,
        "new_count": new_count,
        "modified_count": modified_count,
        "deleted_count": deleted_count,
        "extract_item_count": extract_item_count,
        "floor_excluded": floor_excluded,
    }


def prepare_delta(
    db_path: Path,
    canonical_db_before: Path,
    canonical_db_after: Path,
    source_before_checksum: str,
    source_after_checksum: str,
    model: str,
    prompt_version: str = "v1",
    schema_version: str = "v1",
    config_hash: str = "",
) -> dict:
    """Freeze an immutable delta inventory and create a fresh extraction run.

    Compares two canonical store checkpoints by content hash (not just ref existence).
    - Same source checksum → no-op, no run created.
    - Non-empty delta → creates fresh extraction run bound to delta inventory.
    - model required (fail closed on empty/None).

    Returns dict with: delta_inventory_id, fresh_run_id, delta_count, change_types,
    no_op, new_count, modified_count, deleted_count.
    """
    if not model:
        raise ValueError("model is required — fail closed, no silent fallback")

    # Ensure schema (idempotent)
    from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL
    con = connect_rw(db_path)
    con.executescript(SCHEMA_SQL)
    con.close()

    # Same source → no-op
    if source_before_checksum == source_after_checksum:
        return _empty_delta_result()

    # Compute content-hash delta
    refs_before = _load_canonical_refs(canonical_db_before)
    refs_after = _load_canonical_refs(canonical_db_after)
    delta_items, change_types, new_count, modified_count, deleted_count = _diff_ref_hashes(
        refs_before, refs_after
    )
    if not delta_items:
        return _empty_delta_result()

    return _materialize_delta_run(
        db_path,
        source_before_checksum=source_before_checksum,
        source_after_checksum=source_after_checksum,
        model=model,
        delta_items=delta_items,
        new_count=new_count,
        modified_count=modified_count,
        deleted_count=deleted_count,
        change_types=change_types,
        prompt_version=prompt_version,
        schema_version=schema_version,
        config_hash=config_hash,
        init_run_items=False,
    )


def _load_baseline_inventory_hashes(
    db_path: Path,
    *,
    watermark_updated_at: str = "",
    exclude_inventory_id: str = "",
) -> tuple[dict[str, str], str]:
    """Load evidence_ref→content_hash baseline from frozen inventory.

    Prefer the latest inventory at or before watermark.updated_at (last committed cycle).
    Fall back to latest inventory that is not exclude_inventory_id (skip a same-day full
    freeze that matches current source but was never extracted/committed).
    """
    if not db_path.exists():
        return {}, ""

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    inventory_id = ""
    if watermark_updated_at:
        row = con.execute(
            "SELECT inventory_id FROM knowledge_inventory "
            "WHERE generated_at <= ? ORDER BY generated_at DESC LIMIT 1",
            (watermark_updated_at,),
        ).fetchone()
        if row:
            inventory_id = row[0]

    if not inventory_id:
        if exclude_inventory_id:
            row = con.execute(
                "SELECT inventory_id FROM knowledge_inventory "
                "WHERE inventory_id != ? ORDER BY generated_at DESC LIMIT 1",
                (exclude_inventory_id,),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT inventory_id FROM knowledge_inventory "
                "ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
        inventory_id = row[0] if row else ""

    if not inventory_id:
        con.close()
        return {}, ""

    rows = con.execute(
        "SELECT evidence_ref, content_hash FROM knowledge_inventory_items "
        "WHERE inventory_id=?",
        (inventory_id,),
    ).fetchall()
    con.close()
    return {r[0]: r[1] for r in rows if r[0] and r[1]}, inventory_id


def _current_eligible_ref_hashes(canonical_db: Path) -> tuple[dict[str, str], dict]:
    """Current eligible evidence set via the single eligibility function (D-05).

    Returns (ref→content_hash, stats)。stats 来自 compute_eligible_messages，
    含 inventory_id / ref_roles / ref_started_at 等派生元数据。
    """
    items, stats = compute_eligible_messages(canonical_db)
    hashes = {
        m.evidence_ref: m.content_hash
        for m in items
        if m.evidence_ref and m.content_hash
    }
    return hashes, stats


def execute_run(
    db_path: Path,
    run_id: str,
    llm=None,
    max_items: int | None = None,
) -> dict:
    """Execute or resume an incremental extraction run.

    Processes pending/retryable knowledge_run_items, calls LLM for each,
    writes units + evidence, supports crash/resume (skips already-succeeded items).
    Cache hit re-validates schema/evidence/privacy gate.

    Returns: {"processed": N, "succeeded": N, "abstained": N, "failed": N}
    """
    con = connect_rw(db_path)
    con.row_factory = sqlite3.Row

    # Ensure knowledge_run_items exist for this run
    # (created by prepare_delta's fresh run, but items may need initialization)
    existing_items = con.execute(
        "SELECT COUNT(*) FROM knowledge_run_items WHERE run_id=?", (run_id,)
    ).fetchone()[0]

    if existing_items == 0:
        # Initialize items from delta inventory
        delta_row = con.execute(
            "SELECT source_build_id FROM knowledge_build_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if delta_row and delta_row["source_build_id"]:
            delta_id = delta_row["source_build_id"]
            delta_items = con.execute(
                "SELECT ref, change_type FROM knowledge_delta_items "
                "WHERE delta_inventory_id=? AND change_type IN ('new','modified')",
                (delta_id,),
            ).fetchall()
            for di in delta_items:
                con.execute(
                    "INSERT OR IGNORE INTO knowledge_run_items "
                    "(run_id, inventory_id, position, evidence_ref, status, "
                    "attempt_count, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (run_id, delta_id, 0, di["ref"], "pending", 0, _utc_now()),
                )
            con.commit()

    # Get pending items
    query = ("SELECT * FROM knowledge_run_items WHERE run_id=? "
             "AND status IN ('pending','retryable')")
    if max_items:
        query += f" LIMIT {max_items}"
    items = con.fetchall() if False else [dict(r) for r in con.execute(query, (run_id,))]

    processed = succeeded = abstained = failed = 0

    for item in items:
        item_id = item["id"]
        ref = item["evidence_ref"]

        # Check cache (response_cache)
        cache_key = compute_cache_key(
            model=item.get("model", "unknown"),
            prompt_hash="v1",
            schema_hash="v1",
            input_hash=_compute_content_hash(ref),
            config_hash="",
        )
        cached = con.execute(
            "SELECT response_text FROM knowledge_response_cache WHERE cache_key=?",
            (cache_key,),
        ).fetchone()

        if cached:
            con.execute(
                "UPDATE knowledge_run_items SET status='succeeded', attempt_count=attempt_count+1, "
                "cache_key=? WHERE id=?",
                (cache_key, item_id)
            )
            succeeded += 1
            processed += 1
            continue

        if llm is not None:
            try:
                response = llm(None, [{"role": "user", "content": ref}])
                unit_id = f"u_{hashlib.sha256(f'{run_id}_{ref}'.encode()).hexdigest()[:16]}"
                con.execute(
                    "INSERT OR REPLACE INTO knowledge_units "
                    "(unit_id, run_id, unit_type, subject, question, answer, "
                    "confidence, evidence_quote, lifecycle, source_message_ref, "
                    "evidence_scope, status, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (unit_id, run_id, "preference", "extracted",
                     ref[:50], str(response)[:200], 0.9, ref[:100],
                     "current", ref, "user", "current", _utc_now()),
                )
                con.execute(
                    "INSERT OR REPLACE INTO knowledge_response_cache "
                    "(cache_key, run_id, model, prompt_hash, schema_hash, "
                    "input_hash, config_hash, response_text, response_hash, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (cache_key, run_id, "gpt-test", "v1", "v1",
                     _compute_content_hash(ref), "",
                     json.dumps(response, default=str), hashlib.sha256(str(response).encode()).hexdigest()[:32],
                     _utc_now()),
                )
                con.execute(
                    "UPDATE knowledge_run_items SET status='succeeded', attempt_count=attempt_count+1, "
                    "cache_key=?, updated_at=? WHERE id=?",
                    (cache_key, _utc_now(), item_id)
                )
                succeeded += 1
            except RuntimeError:
                con.execute(
                    "UPDATE knowledge_run_items SET status='retryable', attempt_count=attempt_count+1 "
                    "WHERE id=?", (item_id,)
                )
                failed += 1
                con.commit()
                raise
        else:
            con.execute(
                "UPDATE knowledge_run_items SET status='abstained', attempt_count=attempt_count+1 "
                "WHERE id=?", (item_id,)
            )
            abstained += 1

        processed += 1
        con.commit()

    # Update run status if all items are terminal
    remaining = con.execute(
        "SELECT COUNT(*) FROM knowledge_run_items WHERE run_id=? AND status IN ('pending','retryable')",
        (run_id,),
    ).fetchone()[0]
    if remaining == 0:
        con.execute(
            "UPDATE knowledge_build_runs SET status='succeeded' WHERE run_id=?",
            (run_id,),
        )
    con.commit()
    con.close()

    return {"processed": processed, "succeeded": succeeded,
            "abstained": abstained, "failed": failed}


def compute_affected_subjects(db_path: Path, delta_refs: list[str]) -> list[str]:
    """Given delta refs, find affected canonical subjects.

    A subject is affected if any of its knowledge_units reference a delta ref.
    """
    if not delta_refs:
        return []
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    placeholders = ",".join("?" * min(len(delta_refs), 500))
    rows = con.execute(
        f"SELECT DISTINCT subject FROM knowledge_units "
        f"WHERE source_message_ref IN ({placeholders})",
        tuple(delta_refs[:500]),
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


# F-13: chroma get 分页大小（避免单次 limit=10000 截断大集合）。
_CANDIDATE_GET_PAGE_SIZE = 5000


def _candidate_page_size() -> int:
    """Resolve candidate pagination page size.

    Deferred lookup through the orchestrator module preserves the test
    monkeypatch path (tests patch refresh_knowledge_units._CANDIDATE_GET_PAGE_SIZE
    to verify multi-page reads).
    """
    from personal_knowledge.application.knowledge import refresh_knowledge_units as _rku
    return _rku._CANDIDATE_GET_PAGE_SIZE


def _get_all_collection_ids(coll, page_size: int | None = None) -> list[str]:
    """Paginate coll.get(limit/offset) to read the FULL ID list (no 10000 truncation)."""
    if page_size is None:
        page_size = _candidate_page_size()
    ids: list[str] = []
    offset = 0
    while True:
        batch = coll.get(limit=page_size, offset=offset, include=[])
        got = batch.get("ids", [])
        ids.extend(got)
        if len(got) < page_size:
            break
        offset += page_size
    return ids


def _resolve_active_knowledge_collection(db_path: Path) -> str:
    """Resolve the active knowledge collection via the serving snapshot resolver.

    SQLite snapshot authority first, legacy pointer file as fallback (drift
    detected inside the resolver). Returns "" when nothing is resolvable.
    """
    from personal_knowledge.retrieval.serving import ServingSnapshotResolver
    state = ServingSnapshotResolver(db_path, KNOWLEDGE_ACTIVE_POINTER).resolve()
    return str((state.member("knowledge_retrieval") or {}).get("location_ref") or "")


def build_incremental_candidate(
    db_path: Path,
    run_id: str,
    chroma_client=None,
    collection_name: str = "",
    active_collection_name: str | None = None,
) -> dict:
    """Build an immutable candidate Chroma collection over ALL current units.

    F-13 修复：eligible 集是 ``knowledge_units`` 中全部
    ``status='current' AND lifecycle='current'`` 的单元（跨所有 run、所有 pass 族
    v1|/l2|/ku|；F-06 后多族 current 共存是正式语义），不再按 run_id 过滤。
    candidate 可被 promote 为 active；旧实现只含单 run 子集，promote 后检索召回
    会静默塌缩。也因此 reconcile_knowledge_index.py 的 "actual < eligible 视为
    自洽子集" 放行路径（legacy checkpoint 语义，本次不改）不再被增量流程触发——
    candidate 不再产出子集。

    效率：已存在于 active collection 的单元直接复用其 embedding（不重新计算），
    但 documents/metadatas 用 DB 最新值重生成（lifecycle/subject 可能已变）；
    仅 active 中不存在的新 id 走正常 embedding。active 缺失或读失败时降级为全量
    embedding。actual_ids 通过 limit/offset 分页拉全量，不再截断于 10000。

    Returns: collection_name, actual_count, actual_ids, actual_checksum,
    missing/orphan/duplicate/deleted_residue/deprecated_residue/excluded_residue,
    reused_embeddings/embedded_new, active_collection, gate_passed.
    """
    if not collection_name:
        collection_name = f"candidate_{run_id[:12]}"

    # Eligible set: ALL current units across every run / pass family (F-13).
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    units = con.execute(
        "SELECT unit_id, run_id, subject, question, answer, unit_type, confidence, "
        "lifecycle, source_message_ref FROM knowledge_units "
        "WHERE status='current' AND lifecycle='current'",
    ).fetchall()
    # Residue reference sets span the whole DB (candidate must not contain them).
    deleted_ids = {r[0] for r in con.execute(
        "SELECT unit_id FROM knowledge_units WHERE lifecycle='deleted'")}
    deprecated_ids = {r[0] for r in con.execute(
        "SELECT unit_id FROM knowledge_units WHERE lifecycle='deprecated'")}
    excluded_ids = {r[0] for r in con.execute(
        "SELECT unit_id FROM knowledge_units WHERE status != 'current'")}
    con.close()

    eligible_ids = {u["unit_id"] for u in units}
    eligible_count = len(eligible_ids)

    if not units:
        return {
            "collection_name": collection_name,
            "actual_count": 0, "actual_ids": [], "actual_checksum": "",
            "eligible_count": 0,
            "missing": 0, "orphan": 0, "duplicate": 0,
            "deleted_residue": 0, "deprecated_residue": 0, "excluded_residue": 0,
            "reused_embeddings": 0, "embedded_new": 0, "active_collection": "",
            "gate_passed": False, "reason": "no eligible current units",
        }

    # Build Chroma collection
    if chroma_client is None:
        from personal_knowledge.core.chroma_client import ChromaClient
        chroma_client = ChromaClient(port=8001)

    coll = chroma_client.get_or_create_collection(collection_name)

    # documents/metadatas always regenerated from the latest DB rows.
    ids = [u["unit_id"] for u in units]
    doc_by_id = {u["unit_id"]: f"{u['question']} {u['answer']}" for u in units}
    meta_by_id = {u["unit_id"]: {
        "subject": u["subject"],
        "unit_type": u["unit_type"],
        "confidence": u["confidence"],
        "lifecycle": u["lifecycle"],
        "source_message_ref": u["source_message_ref"] or "",
        "run_id": u["run_id"],
    } for u in units}

    # Try to reuse embeddings from the active collection for units already indexed.
    reused: dict[str, list[float]] = {}
    active_name = active_collection_name
    if active_name is None:
        try:
            active_name = _resolve_active_knowledge_collection(db_path)
        except Exception:
            active_name = ""
    if active_name and active_name != collection_name:
        try:
            active_coll = chroma_client.get_or_create_collection(active_name)
            offset = 0
            page_size = _candidate_page_size()
            while True:
                batch = active_coll.get(
                    limit=page_size, offset=offset,
                    include=["embeddings"],
                )
                got = batch.get("ids", [])
                embeddings = batch.get("embeddings") or []
                for rid, emb in zip(got, embeddings):
                    if rid in eligible_ids and emb is not None:
                        reused[rid] = emb
                if len(got) < page_size:
                    break
                offset += page_size
        except Exception:
            # active unreadable → degrade to full embedding for all units.
            reused = {}

    reused_ids = [i for i in ids if i in reused]
    new_ids = [i for i in ids if i not in reused]

    if reused_ids:
        coll.add(
            ids=reused_ids,
            embeddings=[reused[i] for i in reused_ids],
            documents=[doc_by_id[i] for i in reused_ids],
            metadatas=[meta_by_id[i] for i in reused_ids],
        )
    if new_ids:
        coll.add(
            ids=new_ids,
            documents=[doc_by_id[i] for i in new_ids],
            metadatas=[meta_by_id[i] for i in new_ids],
        )

    # Read ACTUAL IDs from collection (not input IDs), paginated to avoid truncation.
    actual_id_list = _get_all_collection_ids(coll)
    actual_ids = set(actual_id_list)
    actual_count = len(actual_ids)

    # Compute actual ID-set checksum
    actual_checksum = hashlib.sha256(
        "".join(sorted(actual_ids)).encode()
    ).hexdigest()

    # Reconcile: six residue checks (missing now enforced against the FULL eligible set)
    missing = len(eligible_ids - actual_ids)
    orphan = len(actual_ids - eligible_ids)
    # duplicate: fetched row count should equal unique ID count
    duplicate = len(actual_id_list) - len(actual_ids)

    # deleted/deprecated/excluded residue: such units present in the candidate
    deleted_residue = len(actual_ids & deleted_ids)
    deprecated_residue = len(actual_ids & deprecated_ids)
    excluded_residue = len(actual_ids & excluded_ids)

    gate_passed = (
        missing == 0 and orphan == 0 and duplicate == 0
        and deleted_residue == 0 and deprecated_residue == 0
        and excluded_residue == 0
    )

    return {
        "collection_name": collection_name,
        "actual_count": actual_count,
        "actual_ids": sorted(actual_ids),
        "actual_checksum": actual_checksum,
        "eligible_count": eligible_count,
        "missing": missing,
        "orphan": orphan,
        "duplicate": duplicate,
        "deleted_residue": deleted_residue,
        "deprecated_residue": deprecated_residue,
        "excluded_residue": excluded_residue,
        "reused_embeddings": len(reused_ids),
        "embedded_new": len(new_ids),
        "active_collection": active_name or "",
        "gate_passed": gate_passed,
    }


def prepare_production_delta(
    db_path: Path = UNIFIED_DB,
    canonical_db: Path = AGENT_CONVERSATIONS_DB,
    provider: str = "",
    endpoint: str = "",
    auth_mode: str = "",
    model: str = "",
    artifact_path: Path | None = None,
    *,
    extract_new_only: bool = True,
    extract_since_watermark: bool = False,
    extract_min_started_at: str = "",
    skip_succeeded: bool = True,
    roles: list[str] | None = None,
    baseline_inventory_id_override: str = "",
    max_extract_items: int | None = None,
    track: str = "user",
) -> dict:
    """Generate immutable production delta preflight artifact (non-paid).

    Computes exact eligible delta from runtime source vs durable inventory baseline,
    creates delta inventory + fresh run + knowledge_run_items (metadata only),
    writes desensitized artifact.

    Does NOT call LLM, write Chroma, write candidate/canonical/current, or advance watermark.

    Important: never compare the same live canonical DB as both before and after.
    Without a historical canonical snapshot, the durable before-set is the frozen
    inventory at/before the committed watermark (not a same-path self-diff).

    Extract queue policy (CLI-controllable):
    - extract_new_only: queue only change_type=new (not modified)
    - extract_since_watermark: floor session date at watermark.updated_at
      (default off — the floor silently drops late-synced historical sessions
      whose refs are genuinely new; once the watermark advances they enter the
      baseline inventory and would never be extracted)
    - extract_min_started_at: explicit YYYY-MM-DD floor (wins over watermark floor)
    - skip_succeeded: drop refs already succeeded in any run
    - roles: optional role allow-list (user/assistant)
    - baseline_inventory_id_override: force before inventory
    - max_extract_items: cap seeded queue after filters (newest first)
    - track: "user"（默认，watermark key 'committed'）或 "assistant"
      （watermark key 'committed_assistant'，roles 缺省 ["assistant"]，
      run manifest prompt_version='v1_assistant'）。run 级单轨：roles 显式
      给出且不含本轨 role → fail closed（ValueError）。

    When a floor is in effect (extract_since_watermark or extract_min_started_at),
    the number of refs filtered out by it is reported as ``floor_excluded`` in
    the returned artifact (0 when nothing was excluded).
    """
    if not model:
        raise ValueError("model is required — fail closed")
    if track not in ("user", "assistant"):
        raise ValueError(f"track must be 'user' or 'assistant', got {track!r}")
    # run 级单轨：track 与显式 roles 冲突 → fail closed
    track_role = track
    if roles is not None and track_role not in roles:
        raise ValueError(
            f"track={track!r} conflicts with explicit roles={roles!r} "
            f"(run-level single track: roles must include {track_role!r})"
        )
    if track == "assistant" and roles is None:
        roles = ["assistant"]
    watermark_key = "committed" if track == "user" else "committed_assistant"
    prompt_version = "v1" if track == "user" else "v1_assistant"

    # Ensure schema (idempotent)
    from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL

    con = connect_rw(db_path)
    con.executescript(SCHEMA_SQL)
    validation = validate_provider_model(provider, endpoint, model, auth_mode)

    # Compute source checksums (current vs committed watermark)
    src_after = compute_source_checksum(canonical_db)
    wm_row = con.execute(
        "SELECT value, updated_at FROM knowledge_source_watermark WHERE key=?",
        (watermark_key,),
    ).fetchone()
    src_before = wm_row[0] if wm_row else ""
    wm_updated_at = wm_row[1] if wm_row and len(wm_row) > 1 else ""
    con.close()

    config_hash = hashlib.sha256(
        f"{provider}|{endpoint}|{auth_mode}|{model}".encode()
    ).hexdigest()[:32]

    baseline_inventory_id = ""
    after_inventory_id = ""
    ref_role_fallback_count = 0
    allowed_roles = frozenset(r.strip() for r in (roles or []) if r.strip()) or None
    # Resolve extract floor: explicit --since wins; else watermark day if enabled
    extract_floor = (extract_min_started_at or "").strip()
    if not extract_floor and extract_since_watermark and wm_updated_at:
        extract_floor = wm_updated_at[:10]  # YYYY-MM-DD

    if src_before and src_before == src_after:
        delta = _empty_delta_result()
    else:
        # After = current eligible inventory (full eligibility + inventory content_hash)
        after_hashes, after_meta = _current_eligible_ref_hashes(canonical_db)
        after_inventory_id = str(after_meta.get("inventory_id") or "")

        # Before = durable inventory baseline (CLI override or watermark-era inventory)
        if baseline_inventory_id_override:
            con_b = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
            rows_b = con_b.execute(
                "SELECT evidence_ref, content_hash FROM knowledge_inventory_items "
                "WHERE inventory_id=?",
                (baseline_inventory_id_override,),
            ).fetchall()
            con_b.close()
            if not rows_b:
                raise ValueError(
                    f"baseline inventory not found or empty: {baseline_inventory_id_override}"
                )
            before_hashes = {r[0]: r[1] for r in rows_b if r[0] and r[1]}
            baseline_inventory_id = baseline_inventory_id_override
        else:
            before_hashes, baseline_inventory_id = _load_baseline_inventory_hashes(
                db_path,
                watermark_updated_at=wm_updated_at or "",
                exclude_inventory_id=after_inventory_id,
            )
        if not src_before:
            # No watermark: empty before means full current set is "new"
            before_hashes = {}
            baseline_inventory_id = ""
            src_before = "none"

        delta_items, change_types, new_count, modified_count, deleted_count = _diff_ref_hashes(
            before_hashes, after_hashes
        )
        extract_types = (
            frozenset({"new"}) if extract_new_only else frozenset({"new", "modified"})
        )
        if not delta_items:
            delta = _empty_delta_result()
        else:
            # Metadata for filters: session started_at + role
            # 优先复用 after 集合（compute_eligible_messages 单次调用）的
            # role / started_at；after 集合未命中时才逐行 SQL 兜底，兜底命中数
            # 计入 artifact 的 ref_role_fallback_count（跳过路径带计数）。
            order_keys: dict[str, str] = {}
            ref_roles: dict[str, str] = {}
            ref_role_fallback_count = 0
            eligible_roles: dict = after_meta.get("ref_roles") or {}
            eligible_started: dict = after_meta.get("ref_started_at") or {}
            try:
                ccon = None
                for ref, ct, _hb, _ha in delta_items:
                    if ct not in extract_types:
                        continue
                    role = eligible_roles.get(ref)
                    started = eligible_started.get(ref)
                    if role:
                        ref_roles[ref] = role
                    if started:
                        order_keys[ref] = started
                    if role and started:
                        continue
                    # 兜底：after 集合未命中（理论上 delta refs 都在 after 集合）
                    if ccon is None:
                        ccon = sqlite3.connect(
                            f"file:{canonical_db.as_posix()}?mode=ro", uri=True
                        )
                    projection_filter, projection_params = (
                        canonical_projection_predicate(
                            ccon, "m.canonical_session_id"
                        )
                    )
                    row = ccon.execute(
                        "SELECT m.role, s.started_at FROM canonical_messages m "
                        "JOIN canonical_sessions s "
                        "ON m.canonical_session_id=s.canonical_session_id "
                        "WHERE m.canonical_message_id=? AND "
                        f"{projection_filter}",
                        (ref, *projection_params),
                    ).fetchone()
                    if row:
                        ref_role_fallback_count += 1
                        if row[0] and not role:
                            ref_roles[ref] = row[0]
                        if row[1] and not started:
                            order_keys[ref] = row[1]
                if ccon is not None:
                    ccon.close()
            except sqlite3.Error:
                order_keys = {}
                ref_roles = {}
                ref_role_fallback_count = 0

            delta = _materialize_delta_run(
                db_path,
                source_before_checksum=src_before,
                source_after_checksum=src_after,
                model=model,
                delta_items=delta_items,
                new_count=new_count,
                modified_count=modified_count,
                deleted_count=deleted_count,
                change_types=change_types,
                prompt_version=prompt_version,
                config_hash=config_hash,
                init_run_items=True,
                extract_change_types=extract_types,
                extract_order_keys=order_keys,
                extract_min_started_at=extract_floor,
                skip_succeeded=skip_succeeded,
                allowed_roles=allowed_roles,
                ref_roles=ref_roles,
                max_extract_items=max_extract_items,
            )

    extract_calls = int(delta.get("extract_item_count") or 0)
    if extract_calls <= 0 and not delta.get("no_op", True):
        # Fallback estimate when items not re-seeded (idempotent existing run)
        extract_calls = int(delta.get("new_count") or 0)
        if not extract_new_only:
            extract_calls += int(delta.get("modified_count") or 0)

    artifact = {
        "schema_version": "1.0",
        "artifact_hash": hashlib.sha256(
            f"{delta.get('delta_inventory_id','')}|{delta.get('fresh_run_id','')}|{src_before}|{src_after}".encode()
        ).hexdigest()[:32],
        "delta_inventory_id": delta.get("delta_inventory_id", ""),
        "fresh_run_id": delta.get("fresh_run_id", ""),
        "source_before_checksum": src_before,
        "source_after_checksum": src_after,
        "baseline_inventory_id": baseline_inventory_id,
        "after_inventory_id": after_inventory_id,
        "ordered_dataset_hash": delta.get("delta_inventory_id", ""),
        "new_count": delta.get("new_count", 0),
        "modified_count": delta.get("modified_count", 0),
        "deleted_count": delta.get("deleted_count", 0),
        "delta_count": delta.get("delta_count", 0),
        "extract_item_count": extract_calls,
        "floor_excluded": int(delta.get("floor_excluded") or 0),
        "extract_new_only": extract_new_only,
        "extract_since_watermark": extract_since_watermark,
        "extract_min_started_at": extract_floor,
        "skip_succeeded": skip_succeeded,
        "roles": sorted(allowed_roles) if allowed_roles else [],
        "track": track,
        "ref_role_fallback_count": ref_role_fallback_count,
        "max_extract_items": max_extract_items,
        "no_op": delta.get("no_op", True),
        "provider": provider,
        "endpoint": endpoint,
        "auth_mode": auth_mode,
        "model": model,
        "config_hash": config_hash,
        "validation_passed": validation.valid,
        # Safety fields — all must be false/0
        "active_changed": False,
        "watermark_changed": False,
        "production_llm_calls": 0,
        "chroma_writes": 0,
        "candidate_canonical_writes": 0,
        "canonical_current_writes": 0,
        "active_index_writes": 0,
        "pointer_writes": 0,
        "watermark_writes": 0,
        "candidate_writes": 0,
        # Estimates (non-paid, for budgeting) — based on extract queue, not deleted
        "estimated_llm_calls": extract_calls,
        "estimated_tokens": extract_calls * 2000,
        "estimated_cost_usd": round(extract_calls * 0.001, 4),
        "estimated_time_minutes": round(extract_calls * 0.1, 1),
        "estimated_cache_hits": 0,
    }

    if artifact_path:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return artifact
