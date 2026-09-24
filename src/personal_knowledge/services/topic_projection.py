"""Projection service over the Personal Knowledge Wiki.

The stable contract primitives (``TopicKey``, envelope format, reason codes)
live in ``personal_knowledge.wiki.topic_key`` and are re-exported here so
historical import sites and tests keep working.  This service has no
authority, database, provider, HTTP, or index dependencies beyond the wiki
package's public interfaces; the dependency direction is ``services -> wiki``
only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Callable, Mapping

from personal_knowledge.wiki.topic_key import (
    TopicKey,
    TopicProjectionError,
    WIKI_OPERATIONS,
    WIKI_REASON_CODES,
    WIKI_SCHEMA_VERSION,
    make_wiki_envelope,
    opaque_topic_id,
    parse_topic_key,
    safe_reason_code,
)
from personal_knowledge.wiki.derived_store import ProjectionDependency
from personal_knowledge.wiki.read_router import WikiReadRouter


def _canonical_checksum(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _reader_invoke(reader: Any, operation: str, **params: Any) -> dict[str, Any]:
    if hasattr(reader, "invoke"):
        return reader.invoke(operation, **params)
    if callable(reader):
        return reader(operation, **params)
    raise RuntimeError("authority reader unavailable")


def _result_data(result: Any) -> tuple[bool, dict[str, Any] | None, str | None]:
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        error = result.get("error") if isinstance(result, Mapping) else None
        code = error.get("code") if isinstance(error, Mapping) else None
        return False, None, str(code or "authority_unavailable")
    data = result.get("data")
    if not isinstance(data, Mapping):
        return True, {}, None
    normalized = dict(data)
    for field in ("snapshot", "run"):
        if field in result and field not in normalized:
            normalized[field] = result[field]
    return True, normalized, None


def _safe_evidence_refs(item: Mapping[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    raw = item.get("evidence_status") or item.get("evidence_refs") or ()
    refs: list[dict[str, Any]] = []
    for row in raw:
        if isinstance(row, str):
            refs.append({"ref": row})
        elif isinstance(row, Mapping) and row.get("ref"):
            refs.append({
                key: row.get(key)
                for key in (
                    "ref", "artifact_type", "serving_role", "artifact_version_id",
                    "privacy_class", "status",
                )
                if row.get(key) is not None
            })
        if len(refs) >= limit:
            break
    return refs


def _safe_text_list(value: Any, limit: int = 8) -> list[str]:
    """Normalize authority uncertainty without turning one string into characters."""
    if isinstance(value, str):
        return [value[:500]] if value else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item)[:500] for item in value if isinstance(item, (str, int, float))][:limit]


def _authority_ref(
    *, authority_id: str, record_type: str, record_id: Any,
    snapshot_id: Any = None, checksum: Any = None,
) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "authority_id": authority_id,
            "record_type": record_type,
            "record_id": str(record_id) if record_id is not None else None,
            "snapshot_id": str(snapshot_id) if snapshot_id is not None else None,
            "checksum": str(checksum) if checksum is not None else None,
        }.items()
        if value is not None
    }


def _safe_personal_claim(item: Mapping[str, Any], snapshot_id: Any) -> dict[str, Any]:
    key = item.get("key") if isinstance(item.get("key"), Mapping) else {}
    record_id = item.get("current_assertion_id")
    return {
        "claim_type": "current" if item.get("status") == "current" else "observation",
        "key": {
            field: key.get(field)
            for field in ("assertion_kind", "subject", "domain", "scope", "predicate")
        },
        "status": item.get("status"),
        "assertion_type": item.get("assertion_type"),
        "provenance_class": item.get("provenance_class"),
        "confidence": item.get("confidence"),
        "uncertainty": _safe_text_list(item.get("uncertainty")),
        "authority_ref": _authority_ref(
            authority_id="a.personal_state",
            record_type="assertion",
            record_id=record_id,
            snapshot_id=snapshot_id,
            checksum=item.get("current_value_checksum"),
        ),
        "evidence_refs": _safe_evidence_refs(item),
    }


def _safe_decision_claim(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "claim_type": "recommendation",
        "recommendation_id": item.get("recommendation_id"),
        "domain": item.get("domain"),
        "scope": item.get("scope"),
        "recommendation_kind": item.get("recommendation_kind"),
        "horizon": item.get("horizon"),
        "confidence": item.get("confidence"),
        "confirmation_state": item.get("confirmation_state"),
        "action_state": item.get("action_state"),
        "uncertainty": _safe_text_list(item.get("uncertainty")),
        "authority_ref": _authority_ref(
            authority_id="a.decision_feedback",
            record_type="recommendation",
            record_id=item.get("recommendation_id"),
            snapshot_id=item.get("snapshot_id"),
            checksum=item.get("recommendation_checksum"),
        ),
        "evidence_refs": _safe_evidence_refs({"evidence_refs": item.get("support") or ()}),
    }


class _LatestCommittedPersonalReader:
    """Read the latest committed Personal State run without publishing anything.

    The active serving snapshot can move ahead of the last committed Personal
    State analysis run.  In that case the normal authority reader reports
    ``run_missing`` for the active snapshot even though a valid historical run
    exists.  Wiki may use that run only as an explicitly stale projection.
    """

    def __init__(self, db_path: Any, delegate: Any) -> None:
        self.db_path = db_path
        self.delegate = delegate

    def invoke(self, operation: str, **params: Any) -> dict[str, Any]:
        result = self.delegate.invoke(operation, **params)
        if operation != "state.current" or result.get("ok") is True:
            return result
        error = result.get("error") if isinstance(result, Mapping) else None
        code = error.get("code") if isinstance(error, Mapping) else None
        if code != "run_missing":
            return result
        latest = self._latest_committed_run()
        if latest is None:
            return result
        fallback = self.delegate.invoke(operation, **{**params, "run_id": latest["run_id"]})
        if fallback.get("ok") is not True or not isinstance(fallback.get("data"), Mapping):
            return fallback
        selected_snapshot = (fallback.get("snapshot") or {}).get("snapshot_id")
        active_snapshot = latest.get("active_snapshot_id")
        status = "stale" if active_snapshot and active_snapshot != selected_snapshot else "partial"
        reason = "serving_snapshot_changed" if status == "stale" else "authority_binding_missing"
        data = dict(fallback["data"])
        data["_wiki_authority_status"] = status
        data["_wiki_reason_code"] = reason
        data["items"] = [
            {**item, "_wiki_authority_status": status}
            for item in data.get("items", ())
            if isinstance(item, Mapping)
        ]
        return {**fallback, "data": data}

    def _latest_committed_run(self) -> dict[str, str] | None:
        import sqlite3

        try:
            con = sqlite3.connect(
                f"file:{self.db_path.resolve().as_posix()}?mode=ro", uri=True
            )
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            row = con.execute(
                "SELECT r.run_id,r.snapshot_id,a.active_snapshot_id "
                "FROM personal_state_runs r "
                "JOIN personal_state_publications p ON p.run_id=r.run_id "
                "LEFT JOIN serving_authority a ON a.singleton_id=1 "
                "WHERE r.status='committed' "
                "ORDER BY p.publication_sequence DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row is not None else None
        except (OSError, sqlite3.Error):
            return None
        finally:
            try:
                con.close()
            except UnboundLocalError:
                pass


class TopicProjectionService:
    """Deterministic, read-only projection over existing authority readers.

    Readers are injectable so contract tests can use synthetic fixtures.  The
    default readers are imported lazily and use the existing read services;
    this class never writes authority data, invokes providers, or performs
    semantic retrieval.
    """

    def __init__(
        self,
        *,
        personal_reader: Any | None = None,
        decision_reader: Any | None = None,
        external_reader: Any | None = None,
        read_router: Any | None = None,
        materializer: Any | None = None,
        page_reader: Any | None = None,
        now: Callable[[], str] | None = None,
        limit: int = 100,
    ) -> None:
        self.personal_reader = personal_reader or self._default_personal_reader()
        self.decision_reader = decision_reader or self._default_decision_reader()
        self.external_reader = external_reader or self._default_external_reader()
        self.materializer = materializer
        self.page_reader = page_reader
        if read_router is None:
            self.read_router = WikiReadRouter(topic_service=self)
        else:
            self.read_router = read_router
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        self.limit = max(1, min(int(limit), 100))

    @staticmethod
    def _default_personal_reader() -> Any:
        from personal_knowledge.core.project_paths import UNIFIED_DB
        from personal_knowledge.intelligence.service import IntelligenceService
        return _LatestCommittedPersonalReader(UNIFIED_DB, IntelligenceService(UNIFIED_DB))

    @staticmethod
    def _default_decision_reader() -> Any:
        from personal_knowledge.core.project_paths import UNIFIED_DB
        from personal_knowledge.intelligence.decision.service import DecisionFeedbackService
        return DecisionFeedbackService(UNIFIED_DB)

    @staticmethod
    def _default_external_reader() -> Any:
        from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService
        return DecisionIntelligenceReadService()

    @staticmethod
    def _failure_code(code: str | None) -> str:
        if code in {"snapshot_missing", "snapshot_not_validated", "run_missing", "database_missing"}:
            return "authority_binding_missing"
        if code in {"snapshot_hash_mismatch", "recommendation_binding_mismatch", "typed_record_checksum_mismatch"}:
            return "snapshot_mismatch"
        if code in {"privacy_sealed", "privacy_denied"}:
            return "privacy_sealed"
        return "authority_unavailable"

    def _read(self, reader: Any, operation: str, **params: Any) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            ok, data, error = _result_data(_reader_invoke(reader, operation, **params))
        except Exception:  # noqa: BLE001 — public response must use safe code only
            return "error", None, "authority_unavailable"
        if not ok:
            return "error", None, self._failure_code(error)
        if isinstance(data, Mapping):
            authority_status = data.get("_wiki_authority_status")
            if authority_status in {"stale", "partial"}:
                return str(authority_status), data, None
        return ("empty" if not data else "ok"), data, None

    @staticmethod
    def _topic_from_key(raw: Any) -> TopicKey:
        if not isinstance(raw, str):
            raise TopicProjectionError("invalid_topic_key")
        return parse_topic_key(raw)

    def _personal_data(self) -> tuple[str, dict[str, Any] | None, str | None]:
        return self._read(self.personal_reader, "state.current", limit=self.limit)

    def _decision_data(self) -> tuple[str, dict[str, Any] | None, str | None]:
        return self._read(self.decision_reader, "recommendations.list", limit=self.limit)

    def _external_data(self) -> tuple[str, dict[str, Any] | None, str | None]:
        return self._read(self.external_reader, "external.list", limit=self.limit)

    def _current_dependencies(
        self, key: TopicKey, personal: Mapping[str, Any] | None,
        decision: Mapping[str, Any] | None, external: Mapping[str, Any] | None,
    ) -> tuple[Any, ...]:
        dependencies: list[ProjectionDependency] = []
        personal_snapshot = self._snapshot_id(personal)
        if key.topic_type != "decision" and personal_snapshot:
            dependencies.append(ProjectionDependency("personal", personal_snapshot, expected_version=str((personal or {}).get("run", {}).get("run_id") or ""), expected_checksum=str((personal or {}).get("run", {}).get("run_checksum") or ""), order_key=f"personal:{personal_snapshot}"))
        for item in (decision or {}).get("items", ()):
            if isinstance(item, Mapping) and self._decision_matches(key, item):
                dependencies.append(ProjectionDependency("decision", str(item.get("recommendation_id")), expected_version=str(item.get("current_sequence") or ""), expected_checksum=str(item.get("recommendation_checksum") or ""), order_key=f"decision:{item.get('recommendation_id')}"))
        external_snapshot = self._snapshot_id(external)
        if external_snapshot:
            dependencies.append(ProjectionDependency("external", external_snapshot, expected_version=None, expected_checksum=str((external or {}).get("snapshot", {}).get("snapshot_hash") or ""), essential=False, order_key=f"external:{external_snapshot}"))
        return tuple(dependencies)

    def _materialization_verdict(self, key: TopicKey, personal: Mapping[str, Any] | None, decision: Mapping[str, Any] | None, external: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if self.materializer is None:
            return None
        try:
            return self.materializer.validate_latest(key, self._current_dependencies(key, personal, decision, external))
        except Exception:  # noqa: BLE001 — derived-store problems are typed as unavailable
            return {"status": "unavailable", "reason_codes": ("authority_unavailable",), "version": None}

    def materialize_topic(self, *, topic_key: str, materializer: Any | None = None) -> Any:
        """Explicit local-only materialization entry point; never called by GET."""
        target = materializer or self.materializer
        if target is None:
            raise ValueError("materializer_not_configured")
        key = parse_topic_key(topic_key)
        personal_status, personal, personal_error = self._personal_data()
        _, decision, decision_error = self._decision_data()
        _, external, external_error = self._external_data()
        if (key.topic_type == "decision" and decision_error) or (key.topic_type != "decision" and personal_error):
            raise ValueError("authority_unavailable")
        decision_items = [item for item in (decision or {}).get("items", ()) if isinstance(item, Mapping) and self._decision_matches(key, item)]
        bindings = {
            "personal": self._snapshot_id(personal) if key.topic_type != "decision" else None,
            "external": self._snapshot_id(external),
            "decision": decision_items[0].get("snapshot_id") if decision_items else None,
        }
        reasons: tuple[str, ...] = ()
        if external_error:
            reasons = ("authority_unavailable",)
        elif key.topic_type != "decision" and personal_status == "stale":
            reasons = ("serving_snapshot_changed",)
        elif key.topic_type != "decision" and personal_status == "partial":
            reasons = ("authority_unavailable",)
        materialized_status = "partial" if external_error else (
            "stale" if key.topic_type != "decision" and personal_status == "stale" else (
                "partial" if key.topic_type != "decision" and personal_status == "partial" else "fresh"
            )
        )
        return target.materialize(
            key,
            snapshot_bindings=bindings,
            dependencies=self._current_dependencies(key, personal, decision, external),
            source_refs={"authority_ids": ["a.personal_state", "a.decision_feedback", "a.external_context"]},
            freshness_status=materialized_status,
            reason_codes=reasons,
        )

    @staticmethod
    def _snapshot_id(data: Mapping[str, Any] | None) -> str | None:
        if not data:
            return None
        snapshot = data.get("snapshot")
        if isinstance(snapshot, Mapping):
            return str(snapshot.get("snapshot_id")) if snapshot.get("snapshot_id") else None
        return str(data.get("snapshot_id")) if data.get("snapshot_id") else None

    @staticmethod
    def _personal_matches(key: TopicKey, item: Mapping[str, Any]) -> bool:
        raw = item.get("key") if isinstance(item.get("key"), Mapping) else {}
        if key.topic_type == "project":
            return raw.get("domain") == "project" and raw.get("scope") == key.parts[0]
        return (
            raw.get("assertion_kind") == "goal"
            and raw.get("domain") == key.parts[0]
            and raw.get("scope") == key.parts[1]
            and raw.get("predicate") == key.parts[2]
        )

    @staticmethod
    def _decision_matches(key: TopicKey, item: Mapping[str, Any]) -> bool:
        if key.topic_type == "decision":
            return item.get("recommendation_id") == key.parts[0]
        return (
            item.get("domain") == key.parts[0] if key.topic_type == "goal"
            else item.get("domain") == "project" and item.get("scope") == key.parts[0]
        )

    def _keys(self) -> tuple[list[tuple[TopicKey, dict[str, Any]]], dict[str, Any], dict[str, Any]]:
        personal_status, personal, personal_error = self._personal_data()
        decision_status, decision, decision_error = self._decision_data()
        rows: dict[str, tuple[TopicKey, dict[str, Any]]] = {}
        for item in (personal or {}).get("items", ()):
            if not isinstance(item, Mapping):
                continue
            raw = item.get("key") if isinstance(item.get("key"), Mapping) else {}
            try:
                if raw.get("domain") == "project" and raw.get("scope"):
                    key = TopicKey("project", (str(raw["scope"]),))
                    rows[key.canonical] = (key, item)
                if raw.get("assertion_kind") == "goal" and all(raw.get(field) for field in ("domain", "scope", "predicate")):
                    key = TopicKey("goal", (str(raw["domain"]), str(raw["scope"]), str(raw["predicate"])))
                    rows[key.canonical] = (key, item)
            except TopicProjectionError:
                continue
        for item in (decision or {}).get("items", ()):
            if not isinstance(item, Mapping) or not item.get("recommendation_id"):
                continue
            key = TopicKey("decision", (str(item["recommendation_id"]),))
            rows[key.canonical] = (key, item)
        authority = {
            "personal": personal_status if personal_error is None else "error",
            "decision": decision_status if decision_error is None else "error",
        }
        errors = {name: code for name, code in (("personal", personal_error), ("decision", decision_error)) if code}
        return sorted(rows.values(), key=lambda pair: pair[0].canonical), authority, errors

    def _envelope_error(self, operation: str, code: str, *, limitations: list[str] | None = None) -> dict[str, Any]:
        return make_wiki_envelope(
            operation, ok=False, partial=code == "projection_partial",
            limitations=limitations or ["Wiki 只读投影暂时不可用"], error=safe_reason_code(code),
            freshness={"state": "unavailable", "generated_at": self.now()},
            authorities={}, status="unavailable",
        )

    def topic_list(self, *, limit: int = 50, cursor: str | None = None, **_: Any) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return self._envelope_error("topic.list", "invalid_topic_key", limitations=["limit 无效"])
        rows, authorities, errors = self._keys()
        personal_status, personal_data, personal_error = self._personal_data()
        decision_status, decision_data, decision_error = self._decision_data()
        external_status, external, external_error = self._external_data()
        if external_error:
            errors = {**errors, "external": external_error}
            authorities["external"] = "error"
        else:
            authorities["external"] = external_status
        # NEW: wiki-first directory — consolidated subject pages from the store
        # are listed alongside authority-derived topics when a page reader is
        # configured.  Fail-safe: any store problem just leaves them out.
        subject_items: list[dict[str, Any]] = []
        if self.page_reader is not None:
            try:
                for page in self.page_reader.directory():
                    body = None
                    try:
                        from personal_knowledge.wiki.page_reader import parse_page_body
                        body = parse_page_body(page)
                    except Exception:  # noqa: BLE001 — malformed pages are skipped
                        body = None
                    if body is None:
                        continue
                    topic = body.get("topic") or {}
                    subject_items.append({
                        "topic_id": page.topic_id,
                        "topic_type": "subject",
                        "canonical_key": topic.get("canonical_key") or f"subject:{body.get('subject', '')}",
                        "display_label": topic.get("display_label") or f"subject:{body.get('subject', '')}",
                        "authority": "ok",
                        "snapshot_id": None,
                        "freshness": page.freshness_status if page.freshness_status in {"stale", "partial", "unavailable"} else "fresh",
                    })
            except Exception:  # noqa: BLE001 — directory failures degrade gracefully
                subject_items = []
        if errors and not rows and not subject_items:
            return self._envelope_error("topic.list", "authority_unavailable")
        start = 0
        if cursor:
            ids = [opaque_topic_id(key) for key, _ in rows] + [item["topic_id"] for item in subject_items]
            if cursor not in ids:
                return self._envelope_error("topic.list", "invalid_topic_key", limitations=["游标无效"])
            start = ids.index(cursor) + 1
        selected = rows[start:start + limit]
        items: list[dict[str, Any]] = []
        for key, source in selected:
            snapshot = source.get("snapshot_id") if key.topic_type == "decision" else None
            materialization = self._materialization_verdict(
                key,
                personal_data if key.topic_type != "decision" else None,
                decision_data,
                external,
            )
            material_state = (
                str(materialization.get("status"))
                if materialization
                else str(source.get("_wiki_authority_status") or ("partial" if errors else "fresh"))
            )
            items.append({
                "topic_id": opaque_topic_id(key),
                "topic_type": key.topic_type,
                "canonical_key": key.canonical,
                "display_label": f"{key.topic_type}:{' · '.join(key.parts)}",
                "authority": "ok",
                "snapshot_id": snapshot,
                "freshness": material_state,
            })
        merged = items + subject_items
        next_cursor = opaque_topic_id(selected[-1][0]) if len(selected) == limit and start + limit < len(rows) else None
        if next_cursor is None and subject_items and len(merged) > limit and start + limit >= len(rows):
            if limit <= len(subject_items):
                next_cursor = subject_items[min(limit, len(subject_items)) - 1]["topic_id"]
        data = {"items": merged, "total_available": len(rows) + len(subject_items), "limit": limit, "next_cursor": next_cursor}
        checksum = _canonical_checksum({"operation": "topic.list", "data": data, "authorities": authorities, "errors": errors})
        generated = self.now()
        list_partial = bool(errors) or any(item["freshness"] != "fresh" for item in merged)
        return make_wiki_envelope(
            "topic.list", ok=True, data=data, generated_at=generated,
            snapshot_bindings={}, freshness={"state": "partial" if list_partial else "fresh", "generated_at": generated},
            authorities=authorities, partial=list_partial, limitations=["部分 authority 或 derived projection 不可用"] if list_partial else [],
            projection_checksum=checksum, status="partial" if list_partial else "fresh",
        )

    def _resolve(self, *, topic_type: str | None, topic_id: str | None, topic_key: str | None) -> tuple[TopicKey | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, str | None]:
        if topic_key:
            try:
                key = self._topic_from_key(topic_key)
            except TopicProjectionError as exc:
                return None, None, None, None, exc.reason_code
            # Subject topics are Phase 4 consolidated pages: they have no
            # personal/decision authority backing and resolve via the page
            # store only (never via read-time authority compute).
            if key.topic_type == "subject":
                if topic_type and topic_type != key.topic_type:
                    return None, None, None, None, "unsupported_topic_type"
                if self._page_exists(key):
                    return key, None, None, None, None
                return None, None, None, None, "topic_not_found"
            personal_status, personal, personal_error = self._personal_data()
            decision_status, decision, decision_error = self._decision_data()
            if topic_type and topic_type != key.topic_type:
                return None, None, None, None, "unsupported_topic_type"
            personal_item = next((item for item in (personal or {}).get("items", ()) if isinstance(item, Mapping) and self._personal_matches(key, item)), None)
            decision_item = next((item for item in (decision or {}).get("items", ()) if isinstance(item, Mapping) and self._decision_matches(key, item)), None)
            if key.topic_type == "decision" and decision_item is None and decision_error is None:
                return key, None, decision, personal, "topic_not_found"
            if key.topic_type != "decision" and personal_item is None and personal_error is None:
                return key, None, decision, personal, "topic_not_found"
            essential_error = decision_error if key.topic_type == "decision" else personal_error
            if essential_error:
                return key, personal_item, decision, personal, "projection_partial"
            return key, personal_item or decision_item, decision, personal, None
        if not topic_id or not topic_type:
            return None, None, None, None, "invalid_topic_key"
        if topic_type == "subject":
            if self._page_exists_by_id(topic_id):
                return self._subject_key_from_page(topic_id) or (None, None, None, None, "topic_not_found")
            return None, None, None, None, "topic_not_found"
        rows, _, _ = self._keys()
        for key, source in rows:
            if opaque_topic_id(key) == topic_id:
                if key.topic_type != topic_type:
                    return None, None, None, None, "unsupported_topic_type"
                return self._resolve(topic_type=topic_type, topic_id=None, topic_key=key.canonical)
        return None, None, None, None, "topic_not_found"

    def _page_exists(self, key: TopicKey) -> bool:
        if self.page_reader is None:
            return False
        topic_id = self._page_topic_id(key)
        return self.page_reader.latest(topic_id) is not None

    def _page_exists_by_id(self, topic_id: str) -> bool:
        if self.page_reader is None:
            return False
        return self.page_reader.latest(topic_id) is not None

    def _subject_key_from_page(self, topic_id: str) -> tuple[TopicKey | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, str | None] | None:
        result = self.page_reader.latest_body(topic_id) if self.page_reader is not None else None
        if result is None:
            return None
        _, body = result
        canonical = (body.get("topic") or {}).get("canonical_key")
        try:
            key = self._topic_from_key(canonical)
        except (TopicProjectionError, TypeError):
            return None
        return key, None, None, None, None

    @staticmethod
    def _page_topic_id(key: TopicKey) -> str:
        if key.topic_type == "subject":
            from personal_knowledge.wiki.page_reader import subject_topic_id
            return subject_topic_id(key.parts[0])
        return opaque_topic_id(key)

    def topic_get(self, *, topic_type: str | None = None, topic_id: str | None = None, topic_key: str | None = None, **_: Any) -> dict[str, Any]:
        key, source, decision, personal, error = self._resolve(topic_type=topic_type, topic_id=topic_id, topic_key=topic_key)
        if error:
            return self._envelope_error("topic.get", error)
        assert key is not None
        # NEW: page-first read.  A consolidated page (when present, valid and
        # not corrupted) is served directly; missing or corrupt pages fall back
        # to the read-time compute path below.  Stale pages are served with a
        # freshness_status annotation (wiki-first; subject pages have no
        # authority backing to compute from anyway).
        page_envelope = self._page_first_get(key)
        if page_envelope is not None:
            return page_envelope
        personal_status, personal_data, personal_error = self._personal_data()
        decision_status, decision_data, decision_error = self._decision_data()
        external_status, external_data, external_error = self._external_data()
        personal_snapshot = self._snapshot_id(personal_data)
        external_snapshot = self._snapshot_id(external_data)
        claims = {name: [] for name in ("current", "observations", "inferences", "recommendations", "historical", "conflicts", "decision_feedback")}
        matches = [item for item in (personal_data or {}).get("items", ()) if isinstance(item, Mapping) and self._personal_matches(key, item)]
        for item in matches:
            claim = _safe_personal_claim(item, personal_snapshot)
            status = str(item.get("status") or "")
            bucket = "conflicts" if status == "conflict" else "historical" if status in {"expired", "superseded", "stale"} else "current" if status == "current" else "observations"
            claims[bucket].append(claim)
        decision_items = [item for item in (decision_data or {}).get("items", ()) if isinstance(item, Mapping) and self._decision_matches(key, item)]
        for item in decision_items:
            claims["recommendations"].append(_safe_decision_claim(item))
        limitations: list[str] = ["Decision 结果保留 non-causal 限制；Wiki 不提供确认或行动写入。"]
        if external_error:
            limitations.append("External authority 不可用，未将其降级为 Personal Fact。")
        elif external_data:
            for fact in (external_data.get("facts") or ())[:8]:
                if isinstance(fact, Mapping):
                    claims_external = {
                        "claim_type": "external",
                        "resource_id": fact.get("fact_id") or fact.get("source_id"),
                        "source": fact.get("source_id") or fact.get("source"),
                        "status": fact.get("status") or fact.get("lifecycle"),
                        "authority_ref": _authority_ref(authority_id="a.external_context", record_type="fact", record_id=fact.get("fact_id"), snapshot_id=external_snapshot, checksum=fact.get("fact_checksum")),
                    }
                    claims.setdefault("external", []).append(claims_external)
        if decision_items and key.topic_type == "decision":
            rid = key.parts[0]
            for op, bucket in (("recommendations.history", "decision_feedback"), ("recommendations.outcomes", "decision_feedback"), ("recommendations.effectiveness", "decision_feedback")):
                status, data, code = self._read(self.decision_reader, op, recommendation_id=rid, limit=50)
                if code:
                    limitations.append(f"Decision feedback {op} 不可用。")
                    continue
                for row in (data or {}).get("items", ()) if isinstance(data, Mapping) else ():
                    if isinstance(row, Mapping):
                        claims[bucket].append({
                            "claim_type": "historical_feedback",
                            "record_type": row.get("event_type") or row.get("record_type"),
                            "authority_ref": _authority_ref(authority_id="a.decision_feedback", record_type="feedback", record_id=row.get("event_id") or row.get("outcome_id") or row.get("assessment_id"), snapshot_id=decision_items[0].get("snapshot_id"), checksum=row.get("payload_checksum")),
                            "causal_claim": False,
                        })
        if key.topic_type == "decision":
            authorities = {
                "decision": "error" if decision_error else decision_status,
                "external": "error" if external_error else external_status,
            }
            errors = [decision_error, external_error]
            if personal_error:
                limitations.append("Personal State authority 不可用；Decision 页面未将其当作必需事实区块。")
            elif personal_status in {"stale", "partial"}:
                limitations.append("Personal State 使用旧 committed run；未将其作为当前 Decision 事实。")
        else:
            authorities = {
                "personal": "error" if personal_error else personal_status,
                "decision": "error" if decision_error else decision_status,
                "external": "error" if external_error else external_status,
            }
            errors = [personal_error, decision_error, external_error]
        relevant_statuses = (
            (decision_status, external_status)
            if key.topic_type == "decision"
            else (personal_status, decision_status, external_status)
        )
        relevant_errors = (
            (decision_error, external_error)
            if key.topic_type == "decision"
            else tuple(errors)
        )
        partial = any(relevant_errors) or any(status in {"partial", "stale"} for status in relevant_statuses)
        state = "unavailable" if all(relevant_errors) else (
            "stale" if any(status == "stale" for status in relevant_statuses) else "partial" if partial else "fresh"
        )
        if key.topic_type != "decision" and personal_status == "stale":
            limitations.append("Personal State serving snapshot 已变化；页面保留旧 run，但不标记为当前 fresh。")
        data = {
            "topic": {"topic_id": opaque_topic_id(key), "topic_type": key.topic_type, "canonical_key": key.canonical, "display_label": f"{key.topic_type}:{' · '.join(key.parts)}"},
            "claims": claims,
            "evidence_refs": [ref for claim_list in claims.values() for claim in claim_list if isinstance(claim, Mapping) for ref in claim.get("evidence_refs", ())][:16],
        }
        generated = self.now()
        bindings = {"personal": personal_snapshot, "external": external_snapshot, "decision": decision_items[0].get("snapshot_id") if decision_items else None}
        projection_checksum = _canonical_checksum({"data": data, "snapshot_bindings": bindings, "authorities": authorities, "state": state})
        materialization = self._materialization_verdict(key, personal_data, decision_data, external_data)
        if materialization is not None:
            material_state = str(materialization.get("status") or "unavailable")
            material_reasons = list(materialization.get("reason_codes") or ())
            limitations.extend(f"Wiki derived projection: {reason}" for reason in material_reasons)
            if material_state == "missing":
                return make_wiki_envelope(
                    "topic.get", ok=False, data=None, generated_at=generated,
                    snapshot_bindings=bindings, freshness={"state": "missing", "generated_at": generated},
                    authorities=authorities, partial=False, limitations=limitations,
                    error="projection_record_missing", projection_checksum=None, status="missing",
                )
            if material_state in {"stale", "partial", "unavailable"}:
                state = material_state
                projection_checksum = materialization.get("version").projection_checksum if materialization.get("version") else projection_checksum
        return make_wiki_envelope(
            "topic.get", ok=True, data=data, generated_at=generated,
            snapshot_bindings=bindings, freshness={"state": state, "generated_at": generated},
            authorities=authorities, partial=partial, limitations=limitations,
            projection_checksum=projection_checksum, status=state,
        )

    def _page_first_get(self, key: TopicKey) -> dict[str, Any] | None:
        """Serve a consolidated page body when the store has a valid one.

        Returns ``None`` when the page reader is not configured, the page is
        missing, or the stored body fails structural/checksum validation — the
        caller then falls back to the existing read-time compute path.  Stale
        pages (stored freshness_status is stale/partial) are still served but
        carry the freshness_status annotation, so the wiki remains the primary
        read surface.
        """
        if self.page_reader is None:
            return None
        topic_id = self._page_topic_id(key)
        result = self.page_reader.latest_body(topic_id)
        if result is None:
            return None
        page, body = result
        state = page.freshness_status if page.freshness_status in {"stale", "partial", "unavailable"} else "fresh"
        generated = page.generated_at or self.now()
        data = {
            "topic": body.get("topic") or {"topic_id": topic_id, "topic_type": key.topic_type, "canonical_key": key.canonical, "display_label": f"{key.topic_type}:{' · '.join(key.parts)}"},
            "subject": body.get("subject"),
            "aggregation": body.get("aggregation"),
            "claims": body.get("claims"),
            "evidence_refs": body.get("evidence_refs", []),
        }
        limitations = ["Wiki 页面由知识单元统合生成；对应底层工具可穿透访问。"] if state == "fresh" else ["Wiki 页面 freshness 为 stale/partial；底层 KU 已变化，建议重新统合。"]
        return make_wiki_envelope(
            "topic.get", ok=True, data=data, generated_at=generated,
            snapshot_bindings=dict(page.snapshot_bindings),
            freshness={"state": state, "generated_at": generated},
            authorities={"wiki": "ok"}, partial=state != "fresh",
            limitations=limitations, projection_checksum=page.page_checksum, status=state,
        )

    def topic_backlinks(self, *, topic_type: str | None = None, topic_id: str | None = None, topic_key: str | None = None, **_: Any) -> dict[str, Any]:
        key, source, decision, personal, error = self._resolve(topic_type=topic_type, topic_id=topic_id, topic_key=topic_key)
        if error:
            return self._envelope_error("topic.backlinks", error)
        assert key is not None
        links: list[dict[str, Any]] = []
        personal_data = personal or {}
        personal_snapshot = self._snapshot_id(personal_data)
        for item in personal_data.get("items", ()):
            if isinstance(item, Mapping) and self._personal_matches(key, item):
                links.append({
                    "relation_type": "assertion_matches_topic",
                    "join_basis": "exact canonical state key",
                    "source": _authority_ref(authority_id="a.personal_state", record_type="assertion", record_id=item.get("current_assertion_id"), snapshot_id=personal_snapshot, checksum=item.get("current_value_checksum")),
                })
        for item in (decision or {}).get("items", ()):
            if isinstance(item, Mapping) and self._decision_matches(key, item):
                rid = item.get("recommendation_id")
                links.append({
                    "relation_type": "recommendation_targets_topic",
                    "join_basis": "exact domain/scope or recommendation ID",
                    "source": _authority_ref(authority_id="a.decision_feedback", record_type="recommendation", record_id=rid, snapshot_id=item.get("snapshot_id"), checksum=item.get("recommendation_checksum")),
                })
                if key.topic_type == "decision":
                    for op, record_type, id_key in (("recommendations.history", "feedback_event", "event_id"), ("recommendations.outcomes", "outcome", "outcome_id"), ("recommendations.effectiveness", "effectiveness", "assessment_id")):
                        _, rows, code = self._read(self.decision_reader, op, recommendation_id=str(rid), limit=50)
                        if code:
                            continue
                        for row in (rows or {}).get("items", ()) if isinstance(rows, Mapping) else ():
                            if isinstance(row, Mapping) and row.get(id_key):
                                links.append({
                                    "relation_type": "decision_feedback_for_recommendation",
                                    "join_basis": "exact recommendation_id",
                                    "source": _authority_ref(authority_id="a.decision_feedback", record_type=record_type, record_id=row.get(id_key), snapshot_id=item.get("snapshot_id"), checksum=row.get("payload_checksum")),
                                })
        links.sort(key=lambda row: (row["relation_type"], str(row["source"].get("record_id"))))
        data = {"topic": {"topic_id": opaque_topic_id(key), "topic_type": key.topic_type, "canonical_key": key.canonical}, "links": links}
        generated = self.now()
        checksum = _canonical_checksum(data)
        personal_status, _, personal_error = self._personal_data()
        decision_status, _, decision_error = self._decision_data()
        backlink_state = (
            "stale" if "stale" in {personal_status, decision_status}
            else "partial" if personal_error or decision_error or "partial" in {personal_status, decision_status}
            else "fresh"
        )
        backlink_authorities = {
            "personal": "error" if personal_error else personal_status,
            "decision": "error" if decision_error else decision_status,
        }
        return make_wiki_envelope(
            "topic.backlinks", ok=True, data=data, generated_at=generated,
            snapshot_bindings={"personal": personal_snapshot, "decision": (source or {}).get("snapshot_id") if isinstance(source, Mapping) else None},
            freshness={"state": backlink_state, "generated_at": generated}, authorities=backlink_authorities,
            partial=backlink_state != "fresh", limitations=["Personal State serving snapshot 已变化。"] if backlink_state == "stale" else [],
            projection_checksum=checksum, status=backlink_state,
        )

    def topic_resolve(self, *, topic_key: str | None = None, query: str | None = None, **_: Any) -> dict[str, Any]:
        if self.read_router is None:
            return self._envelope_error("topic.resolve", "authority_unavailable")
        try:
            return self.read_router.resolve(topic_key=topic_key, query=query)
        except Exception:  # noqa: BLE001 — router errors are typed at the boundary
            return self._envelope_error("topic.resolve", "authority_unavailable")

    def invoke(self, operation: str, **params: Any) -> dict[str, Any]:
        handlers = {"topic.list": self.topic_list, "topic.get": self.topic_get, "topic.backlinks": self.topic_backlinks, "topic.resolve": self.topic_resolve}
        handler = handlers.get(operation)
        if handler is None:
            return {
                "schema_version": WIKI_SCHEMA_VERSION,
                "operation": operation,
                "ok": False,
                "status": "unavailable",
                "error": "unsupported_topic_type",
                "data": None,
            }
        try:
            return handler(**params)
        except TopicProjectionError as exc:
            return self._envelope_error(operation, exc.reason_code)
        except Exception:  # noqa: BLE001 — fail closed without leaking authority details
            return self._envelope_error(operation, "authority_unavailable")


__all__ = [
    "TopicKey",
    "TopicProjectionError",
    "TopicProjectionService",
    "WIKI_OPERATIONS",
    "WIKI_REASON_CODES",
    "WIKI_SCHEMA_VERSION",
    "make_wiki_envelope",
    "opaque_topic_id",
    "parse_topic_key",
    "safe_reason_code",
]
