"""Plan 61-06 Task 1 RED contract: metadata-only committed delta publication (HARNESS-05).

The producer entry is the real post-commit seam inside ``pk-sync conversations
--write`` (``personal_knowledge.application.sync``), NOT a test helper and never
``harness_reflection`` or a Candidate helper. The Kernel-side producer endpoint,
dispatcher replay and cursor contract live in the Node test
``apps/personal_intelligence_kernel/test/conversation-delta-reflection.test.mjs``.

Sentinel fixtures prove the published ``conversation.delta.committed`` event is
metadata-only: canonical checksum, committed watermark/publication version,
source/scope binding, occurred time and idempotency key. No conversation body,
prompt, credential, SQL statement or secret ever reaches the Journal
(``pi_kernel_events``), a consumer checkpoint, or the dispatcher callback.

Implementation target (Plan 61-06 Task 2):
    personal_knowledge/application/sync.py
      publish_conversation_delta_committed(
        *, canonical_checksum, source_checksum=None, watermark=None,
           publication_version=None, source="pk-sync", scope="agent.conversation",
           idempotency_key, occurred_at=None, committed=True,
           endpoint=None, internal_capability=None) -> dict
    called by _cmd_conversations() only after _record_conversation_versions()
    (strictly post-commit, checksum == committed watermark); every dry-run,
    uncommitted, missing or mismatched pre-commit state publishes nothing.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.contract.test_pi_kernel_host import _Server  # noqa: E402
from personal_knowledge.application.sync import _cmd_conversations  # noqa: E402

try:  # RED until Plan 61-06 Task 2 adds the post-commit publisher hook.
    from personal_knowledge.application.sync import publish_conversation_delta_committed  # noqa: F401
    _PUBLISHER_AVAILABLE = True
    _PUBLISHER_IMPORT_ERROR = None
except (ImportError, AttributeError) as exc:  # expected RED: publisher not implemented yet
    _PUBLISHER_AVAILABLE = False
    _PUBLISHER_IMPORT_ERROR = exc


def _require_publisher() -> None:
    """Fail each publisher test with a clear RED signal until the seam exists."""
    if not _PUBLISHER_AVAILABLE:
        pytest.fail(
            "RED: personal_knowledge.application.sync.publish_conversation_delta_committed "
            f"missing (expected for 61-06 Task 1 RED): {_PUBLISHER_IMPORT_ERROR}",
            pytrace=False,
        )


DELTA_TYPE = "conversation.delta.committed"
INTERNAL_CAPABILITY = "test-conversation-delta-capability"


@pytest.fixture(autouse=True)
def _isolate_delta_outbox(tmp_path, monkeypatch):
    """Publisher integration tests must never write the live operational ledger."""
    monkeypatch.setenv("PK_CONVERSATION_DELTA_OUTBOX", str(tmp_path / "conversation_delta_outbox.sqlite"))

# Sentinel private values. If any reaches the published event, the Journal, a
# checkpoint or a callback payload the test fails closed, exactly like the
# Kernel-side privacy walker.
SENTINELS = (
    "PRIVATE_CONVERSATION_BODY_SENTINEL_4a1f2b",
    "PRIVATE_PROMPT_SENTINEL_9f3a1c",
    "PRIVATE_CREDENTIAL_SENTINEL_8a4c2d",
    "PRIVATE_SECRET_SENTINEL_1b5e7c",
    "SELECT * FROM messages WHERE body LIKE '%PRIVATE_SQL_SENTINEL_2c6d8e%'",
)
FORBIDDEN_KEYS = (
    "body",
    "content",
    "prompt",
    "completion",
    "payload",
    "inline_payload",
    "input",
    "output",
    "result",
    "credential",
    "secret",
    "token",
    "password",
    "path",
    "sql",
    "query",
    "statement",
)


def _walk_private(node, path, errors):
    if node is None:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in FORBIDDEN_KEYS:
                errors.append(f"forbidden key {key!r} at {path}")
            _walk_private(value, f"{path}.{key}", errors)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk_private(value, f"{path}[{index}]", errors)
    elif isinstance(node, str):
        for sentinel in SENTINELS:
            if sentinel in node:
                errors.append(f"sentinel leaked at {path}")


def _assert_metadata_only(value) -> None:
    errors: list[str] = []
    _walk_private(value, "event", errors)
    assert not errors, "delta event leaked private data: " + "; ".join(errors)


def _canonical_fixture(tmp_path: Path) -> Path:
    """A redacted temporary canonical store whose rows carry sentinel private values.

    The publisher must only ever derive a content checksum from this file; the
    sentinel bodies/prompt/credential/SQL must never appear in any event.
    """
    store = tmp_path / "canonical" / "agent_conversations.sqlite"
    store.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(store)
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id TEXT, role TEXT, content TEXT)"
    )
    con.executemany(
        "INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)",
        [
            ("c1", "user", SENTINELS[1]),
            ("c1", "assistant", SENTINELS[0]),
            ("c2", "system", f"credential={SENTINELS[2]} secret={SENTINELS[3]}"),
            ("c2", "tool", SENTINELS[4]),
        ],
    )
    con.commit()
    con.close()
    return store


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _delta_rows(server: _Server) -> list[dict]:
    """Read the append-only EventJournal (the Journal) for committed delta events."""
    con = sqlite3.connect(f"file:{server.database.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in con.execute(
                "SELECT sequence, event_id, event_type, event_json, canonical_checksum "
                "FROM pi_kernel_events WHERE event_type = ? ORDER BY sequence",
                (DELTA_TYPE,),
            )
        ]
    finally:
        con.close()


@pytest.fixture
def kernel_server(tmp_path: Path):
    """Real Kernel server (temp EventJournal) wired with the internal capability."""
    previous = os.environ.get("PI_KERNEL_INTERNAL_CAPABILITY")
    os.environ["PI_KERNEL_INTERNAL_CAPABILITY"] = INTERNAL_CAPABILITY
    server = _Server(tmp_path)
    yield server
    server.stop()
    if previous is None:
        os.environ.pop("PI_KERNEL_INTERNAL_CAPABILITY", None)
    else:
        os.environ["PI_KERNEL_INTERNAL_CAPABILITY"] = previous


def _committed_publish_args(canonical_checksum: str, source_checksum: str, **overrides) -> dict:
    args = {
        "canonical_checksum": canonical_checksum,
        "source_checksum": source_checksum,
        "watermark": canonical_checksum,
        "publication_version": "2026-08-09T09:00:00.000Z#1",
        "source": "pk-sync",
        "scope": "agent.conversation",
        "idempotency_key": "pi-idem-python-delta-001",
        "occurred_at": "2026-08-09T09:00:00.000Z",
        "committed": True,
    }
    args.update(overrides)
    return args


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_publisher_is_wired_into_the_production_post_commit_command():
    """The seam is the real `pk-sync conversations --write` post-commit path."""
    _require_publisher()
    source = inspect.getsource(_cmd_conversations)
    assert "publish_conversation_delta_committed(" in source, (
        "RED: _cmd_conversations must call the publisher after commit (expected for 61-06 Task 1)"
    )
    assert source.index("publish_conversation_delta_committed(") > source.index("_record_conversation_versions"), (
        "publisher must run after canonical records/watermark are committed"
    )


def test_committed_publish_reaches_the_journal_as_one_metadata_only_delta(kernel_server, tmp_path):
    """A committed canonical checksum/watermark publishes exactly one event."""
    _require_publisher()
    canonical = _canonical_fixture(tmp_path)
    checksum = hashlib.sha256(canonical.read_bytes()).hexdigest()
    source_checksum = _sha256("agentsview:sessions.db:fixture-v1")
    result = publish_conversation_delta_committed(
        endpoint=f"http://127.0.0.1:{kernel_server.port}",
        internal_capability=INTERNAL_CAPABILITY,
        **_committed_publish_args(checksum, source_checksum),
    )
    assert result.get("published") is True
    assert result.get("status") == "appended"
    assert result.get("event_type") == DELTA_TYPE
    assert result.get("event_id", "").startswith("pi_evt_")
    assert isinstance(result.get("sequence"), int)
    _assert_metadata_only(result)

    rows = _delta_rows(kernel_server)
    assert len(rows) == 1, "one committed sync emits exactly one delta event"
    event = json.loads(rows[0]["event_json"])
    assert event["type"] == DELTA_TYPE
    assert event["payload_ref"]["checksum"] == checksum, "AgentView->canonical binding carries the committed checksum"
    assert source_checksum in event["snapshot"], "source->AgentView binding is present in the snapshot"
    assert checksum in event["payload_ref"]["ref"], "payload ref binds the committed watermark"
    assert "2026-08-09T09:00:00.000Z#1" in event["payload_ref"]["ref"], "payload ref binds the publication version"
    assert rows[0]["canonical_checksum"] == checksum
    _assert_metadata_only(rows[0])


def test_exact_retry_returns_the_same_event_and_sequence(kernel_server, tmp_path):
    """An exact retry replays the same Journal event/sequence without a second delta."""
    _require_publisher()
    canonical = _canonical_fixture(tmp_path)
    checksum = hashlib.sha256(canonical.read_bytes()).hexdigest()
    source_checksum = _sha256("agentsview:sessions.db:fixture-v1")
    args = dict(
        endpoint=f"http://127.0.0.1:{kernel_server.port}",
        internal_capability=INTERNAL_CAPABILITY,
        **_committed_publish_args(checksum, source_checksum),
    )
    first = publish_conversation_delta_committed(**args)
    retry = publish_conversation_delta_committed(**args)
    assert first.get("published") is True and retry.get("published") is True
    assert retry.get("replay") is True
    assert retry.get("event_id") == first.get("event_id")
    assert retry.get("sequence") == first.get("sequence")
    assert len(_delta_rows(kernel_server)) == 1, "exact retry must not append a second delta"


def test_dry_run_uncommitted_missing_and_mismatched_publish_nothing(kernel_server, tmp_path):
    """Every non-committed or mismatched pre-commit state emits no event."""
    _require_publisher()
    canonical = _canonical_fixture(tmp_path)
    checksum = hashlib.sha256(canonical.read_bytes()).hexdigest()
    source_checksum = _sha256("agentsview:sessions.db:fixture-v1")
    cases = [
        ("dry-run/uncommitted", dict(committed=False)),
        ("missing canonical checksum", dict(canonical_checksum="")),
        ("missing watermark", dict(watermark="")),
        ("mismatched watermark", dict(watermark=_sha256("different:watermark"))),
    ]
    for label, overrides in cases:
        args = _committed_publish_args(checksum, source_checksum)
        args.update(overrides)
        result = publish_conversation_delta_committed(
            endpoint=f"http://127.0.0.1:{kernel_server.port}",
            internal_capability=INTERNAL_CAPABILITY,
            **args,
        )
        assert result.get("published") is False, f"{label} must publish nothing"
        assert result.get("reason"), f"{label} must state a fail-closed reason"
        _assert_metadata_only(result)
    assert _delta_rows(kernel_server) == [], "no uncommitted/mismatched trigger may reach the Journal"


def test_sentinels_never_reach_journal_checkpoint_or_callback(kernel_server, tmp_path):
    """No conversation body/prompt/credential/SQL reaches the Journal payload."""
    _require_publisher()
    canonical = _canonical_fixture(tmp_path)
    checksum = hashlib.sha256(canonical.read_bytes()).hexdigest()
    source_checksum = _sha256("agentsview:sessions.db:fixture-v1")
    result = publish_conversation_delta_committed(
        endpoint=f"http://127.0.0.1:{kernel_server.port}",
        internal_capability=INTERNAL_CAPABILITY,
        **_committed_publish_args(checksum, source_checksum),
    )
    _assert_metadata_only(result)
    rows = _delta_rows(kernel_server)
    assert len(rows) == 1
    for row in rows:
        _assert_metadata_only(row)  # event_json + canonical_checksum carry no sentinel/private key
    raw_journal = kernel_server.database.read_bytes()
    for sentinel in SENTINELS:
        assert sentinel.encode() not in raw_journal, "sentinel reached the EventJournal file"


def test_publisher_signature_accepts_only_metadata_never_bodies():
    """The publisher seam can never be handed a body, prompt, credential or SQL."""
    _require_publisher()
    parameters = inspect.signature(publish_conversation_delta_committed).parameters
    names = set(parameters)
    assert "canonical_checksum" in names and "idempotency_key" in names, (
        "publisher must bind the canonical checksum and idempotency key"
    )
    forbidden = {"body", "content", "prompt", "completion", "credential", "secret", "sql", "statement", "token", "password", "path"}
    assert not (names & forbidden), f"publisher must never accept {sorted(names & forbidden)}"
    assert parameters["canonical_checksum"].kind is inspect.Parameter.KEYWORD_ONLY, "publisher args are keyword-only metadata"


# ---------------------------------------------------------------------------
# Plan 61-07 Task 1 RED contract: dispatcher-bound reflection staging
# (HARNESS-05 / T-61-REFLECT-01 / T-61-CAND-01 / T-61-LEAK-03)
#
# Reflection starts ONLY through the Plan 61-06 dispatcher binding: the
# `conversation.reflection.stage` PiDomainGateway provider receives the
# dispatcher-authenticated event_id/canonical_checksum/watermark/source/
# snapshot/two-freshness/rule_version/task/idempotency/capability binding. A
# direct Python/public/model call, a foreign/stale/mixed binding or a divergent
# replay is rejected. `reflection_key` binds event_id + canonical_checksum +
# watermark + rule_version and is recomputed before any inference; exact replay
# is duplicate-safe and never overwrites. The staged Candidate keeps immutable
# Evidence, a reproducible Observation and `provenance_class: inference`
# separate, with a valid interval, confidence/uncertainty, support/conflict refs
# and a metadata-only receipt -- never a canonical fact, a body or a secret.
#
# Implementation target (Plan 61-07 Task 2):
#     src/personal_knowledge/application/conversation/harness_reflection.py
#       STAGE_OUTCOMES        -> frozenset({"staged","duplicate","rejected","failed"})
#       REFLECTION_KEY_FIELDS -> ("event_id","canonical_checksum","watermark","rule_version")
#       HarnessReflectionAdapter(db_path) with .stage(**dispatcher_binding)
#       ReflectionStageError(code, detail)
#
# Running this against the current tree MUST FAIL: the adapter module is missing
# and the gateway provider is not registered. Every failure points at the
# missing implementation, never at a syntax error.
# ---------------------------------------------------------------------------

from personal_knowledge.services.pi_domain_gateway import (  # noqa: E402
    OPERATIONS as PI_DOMAIN_OPERATIONS,
    PiDomainGateway,
)

try:  # RED until Plan 61-07 Task 2 creates the adapter module.
    from personal_knowledge.application.conversation.harness_reflection import (  # noqa: F401
        HarnessReflectionAdapter,
        REFLECTION_KEY_FIELDS,
        ReflectionStageError,
        STAGE_OUTCOMES,
    )
    _REFLECTION_AVAILABLE = True
    _REFLECTION_IMPORT_ERROR = None
except (ImportError, AttributeError) as exc:  # expected RED: adapter not implemented yet
    _REFLECTION_AVAILABLE = False
    _REFLECTION_IMPORT_ERROR = exc


REFLECTION_RULE_VERSION = "conversation-reflection-v1"
REFLECTION_STAGE_OPERATION = "conversation.reflection.stage"


def _require_reflection() -> None:
    """Fail each staging test with a clear RED signal until the adapter exists."""
    if not _REFLECTION_AVAILABLE:
        pytest.fail(
            "RED: personal_knowledge.application.conversation.harness_reflection "
            f"missing (expected for 61-07 Task 1 RED): {_REFLECTION_IMPORT_ERROR}",
            pytrace=False,
        )


def _reflection_key(binding: dict) -> str:
    """Deterministic stable-key contract: event_id + canonical_checksum +
    watermark + rule_version, recomputed before any inference."""
    return hashlib.sha256(json.dumps(
        {
            "event_id": binding["event_id"],
            "canonical_checksum": binding["canonical_checksum"],
            "watermark": binding["watermark"],
            "rule_version": binding["rule_version"],
        },
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _freshness(*, statuses: tuple[str, str] = ("current", "current")) -> dict:
    """Two independently truthful freshness legs (Plan 61-05 shape)."""
    legs = {}
    for name, status in zip(("source_to_agentsview", "agentsview_to_canonical"), statuses):
        legs[name] = {
            "leg": name,
            "status": status,
            "watermark": "2026-08-09T07:00:00Z" if status != "missing_watermark" else None,
            "observed_at": "2026-08-09T08:00:00Z",
            "backlog": 0,
            "limitation": f"{status}: fixture leg",
        }
    return legs


def _dispatcher_metadata(**overrides) -> dict:
    """Metadata the 61-06 dispatcher hands to its guarded staging seam plus the
    gateway-enriched source/snapshot/freshness/task bindings."""
    canonical = _sha256("canonical:agent.conversation:fixture-v1")
    metadata = {
        "event_id": "pi_evt_" + _sha256("delta:fixture:001"),
        "canonical_checksum": canonical,
        "watermark": canonical,
        "rule_version": REFLECTION_RULE_VERSION,
        "source": "pk-sync",
        "snapshot": "agentsview@" + _sha256("agentsview:sessions.db:fixture-v1"),
        "scope": "agent.conversation",
        "publication_version": "2026-08-09T09:00:00.000Z#1",
        "occurred_at": "2026-08-09T09:00:00.000Z",
        "freshness": _freshness(),
        "task_id": "task-conversation-reflection",
        "idempotency_key": "pi-idem-reflection-stage-001",
        "binding": {"scope": "agent.conversation", "role": "reflection-consumer"},
    }
    metadata.update(overrides)
    return metadata


def _stage(db_path: Path, metadata: dict) -> dict:
    _require_reflection()
    adapter = HarnessReflectionAdapter(db_path=db_path)
    return adapter.stage(**metadata)


def test_gateway_registers_only_the_dispatcher_bound_stage_provider():
    """The reflection entry is the named gateway provider, not a helper."""
    assert REFLECTION_STAGE_OPERATION in PI_DOMAIN_OPERATIONS, (
        "RED: PiDomainGateway must register conversation.reflection.stage "
        "(expected for 61-07 Task 1 RED)"
    )
    spec = PI_DOMAIN_OPERATIONS[REFLECTION_STAGE_OPERATION]
    assert spec["kind"] == "guarded_write", "reflection staging is a guarded write"
    required = {
        "event_id", "canonical_checksum", "watermark", "source", "snapshot",
        "freshness", "rule_version", "scope", "publication_version", "occurred_at",
        "task_id", "idempotency_key", "binding",
    }
    missing = sorted(required - set(spec["allowed"]))
    assert not missing, f"RED: stage provider must accept the dispatcher binding metadata: missing {missing}"
    forbidden = {"body", "content", "prompt", "completion", "credential", "secret",
                 "sql", "statement", "token", "password", "path"}
    assert not (set(spec["allowed"]) & forbidden), "stage provider must never accept private payload fields"


def test_gateway_stage_rejects_without_capability():
    """The gateway enforces the loopback capability before any staging work."""
    if REFLECTION_STAGE_OPERATION not in PI_DOMAIN_OPERATIONS:
        pytest.fail(
            "RED: PiDomainGateway must register conversation.reflection.stage before "
            "capability gating can be enforced (expected for 61-07 Task 1 RED)",
            pytrace=False,
        )
    gateway = PiDomainGateway()
    result = gateway.invoke(REFLECTION_STAGE_OPERATION, _dispatcher_metadata(), capability=None)
    assert result.get("ok") is False
    assert result.get("error", {}).get("code") == "capability_invalid"


def test_reflection_key_binds_the_four_dispatcher_fields_exactly(tmp_path):
    """Stable key = event_id + canonical_checksum + watermark + rule_version."""
    _require_reflection()
    assert REFLECTION_KEY_FIELDS == ("event_id", "canonical_checksum", "watermark", "rule_version")
    assert STAGE_OUTCOMES == {"staged", "duplicate", "rejected", "failed"}
    metadata = _dispatcher_metadata()
    result = _stage(tmp_path / "reflection.sqlite", metadata)
    assert result["status"] == "staged"
    assert result["reflection_key"] == _reflection_key(metadata), (
        "reflection_key must be recomputed deterministically from the dispatcher fields"
    )


def test_staging_starts_from_a_real_committed_delta_dispatcher_binding(kernel_server, tmp_path):
    """The adapter consumes metadata produced by the real 61-06 post-commit path."""
    _require_reflection()
    canonical = _canonical_fixture(tmp_path)
    checksum = hashlib.sha256(canonical.read_bytes()).hexdigest()
    source_checksum = _sha256("agentsview:sessions.db:fixture-v1")
    published = publish_conversation_delta_committed(
        endpoint=f"http://127.0.0.1:{kernel_server.port}",
        internal_capability=INTERNAL_CAPABILITY,
        **_committed_publish_args(checksum, source_checksum),
    )
    assert published.get("published") is True
    rows = _delta_rows(kernel_server)
    assert len(rows) == 1, "one committed sync emits exactly one delta"
    event = json.loads(rows[0]["event_json"])
    metadata = _dispatcher_metadata(
        event_id=rows[0]["event_id"],
        canonical_checksum=event["payload_ref"]["checksum"],
        watermark=event["payload_ref"]["ref"].split("@")[1].split("#")[0],
        source=event["source"],
        snapshot=event["snapshot"],
        scope=event["correlation_id"].removeprefix("scope:"),
        publication_version=event["payload_ref"]["ref"].split("#", 1)[1],
        occurred_at=event["occurred_at"],
        idempotency_key="pi-idem-reflection-from-journal-001",
    )
    result = _stage(tmp_path / "reflection.sqlite", metadata)
    assert result["status"] == "staged"
    assert result["reflection_key"] == _reflection_key(metadata)
    assert result["candidate_id"] and result["candidate_checksum"]
    _assert_metadata_only(result)


def test_exact_replay_is_duplicate_safe_and_never_overwrites(tmp_path):
    """The same reflection_key replays as duplicate and cannot overwrite."""
    _require_reflection()
    db = tmp_path / "reflection.sqlite"
    metadata = _dispatcher_metadata()
    first = _stage(db, metadata)
    assert first["status"] == "staged"
    replay = _stage(db, metadata)
    assert replay["status"] == "duplicate", "exact replay must be duplicate-safe"
    assert replay["candidate_id"] == first["candidate_id"], "duplicate must not mint a new candidate"
    assert replay["candidate_checksum"] == first["candidate_checksum"], "duplicate must not overwrite"
    assert replay["reflection_key"] == first["reflection_key"]


def test_divergent_replay_identity_is_rejected(tmp_path):
    """A different checksum/watermark under the same replay identity fails closed."""
    _require_reflection()
    db = tmp_path / "reflection.sqlite"
    first = _stage(db, _dispatcher_metadata())
    assert first["status"] == "staged"
    divergent = _dispatcher_metadata(
        canonical_checksum=_sha256("different:canonical"),
        watermark=_sha256("different:canonical"),
    )
    result = _stage(db, divergent)
    assert result["status"] == "rejected", "divergent identity must fail closed"
    assert result.get("candidate_id") is None, "divergent identity must never produce a candidate"


def test_mixed_watermark_checksum_binding_is_rejected(tmp_path):
    """A committed delta binds watermark == canonical checksum (61-06 rule)."""
    _require_reflection()
    mixed = _dispatcher_metadata(watermark=_sha256("different:watermark"))
    result = _stage(tmp_path / "reflection.sqlite", mixed)
    assert result["status"] == "rejected", "committed watermark must equal the canonical checksum"
    assert result.get("candidate_id") is None


def test_direct_python_public_or_model_trigger_is_rejected(tmp_path):
    """No direct/public/model entry and no missing dispatcher binding stages."""
    _require_reflection()
    db = tmp_path / "reflection.sqlite"
    cases = [
        ("public renderer source", {"source": "renderer"}),
        ("model wake source", {"source": "model.wake"}),
        ("missing event id", {"event_id": ""}),
        ("missing canonical checksum", {"canonical_checksum": ""}),
        ("missing watermark", {"watermark": ""}),
        ("missing rule version", {"rule_version": ""}),
        ("missing task id", {"task_id": ""}),
        ("missing binding", {"binding": {}}),
        ("missing idempotency key", {"idempotency_key": ""}),
    ]
    for label, overrides in cases:
        result = _stage(db, _dispatcher_metadata(**overrides))
        assert result["status"] == "rejected", f"{label} must be rejected"
        assert result.get("candidate_id") is None, f"{label} must not produce a candidate"


def test_foreign_stale_or_missing_freshness_binding_is_rejected(tmp_path):
    """Foreign snapshots and unproven freshness legs never stage a Candidate."""
    _require_reflection()
    db = tmp_path / "reflection.sqlite"
    foreign = _dispatcher_metadata(snapshot="snapshot:renderer:not-an-agentsview-binding")
    assert _stage(db, foreign)["status"] == "rejected", "a foreign/non-agentsview snapshot must be rejected"
    stale = _dispatcher_metadata(freshness=_freshness(statuses=("current", "stale")))
    assert _stage(db, stale)["status"] == "rejected", "a stale freshness leg must never stage"
    unknown = _dispatcher_metadata(freshness=_freshness(statuses=("unknown", "current")))
    assert _stage(db, unknown)["status"] == "rejected", "an unknown freshness leg must never stage"
    missing_leg = _dispatcher_metadata(
        freshness={"source_to_agentsview": _freshness()["source_to_agentsview"]}
    )
    assert _stage(db, missing_leg)["status"] == "rejected", "a missing freshness leg must be rejected"


def test_staged_candidate_keeps_evidence_observation_and_inference_separate(tmp_path):
    """Immutable Evidence, reproducible Observation and inference Candidate are distinct."""
    _require_reflection()
    result = _stage(tmp_path / "reflection.sqlite", _dispatcher_metadata())
    assert result["status"] == "staged"
    candidate = result["candidate"]
    evidence = candidate["evidence"]
    assert isinstance(evidence, tuple) and evidence, "candidate must bind immutable Evidence refs"
    for ref in evidence:
        for field in ("ref", "checksum", "privacy_class", "serving_role", "artifact_version_id"):
            assert ref.get(field), f"Evidence record must carry {field}"
        assert not ({key.lower() for key in ref} & {"body", "content", "prompt", "completion"}), \
            "Evidence must never carry a body"
    observation = candidate["observation"]
    assert observation.get("provenance_class") == "observation"
    assert observation.get("observation_checksum"), "Observation must be reproducible"
    assert observation.get("observed_at")
    assert candidate["provenance_class"] == "inference", "the Candidate is an inference, never a fact"
    assert candidate["status"] == "candidate", "staged content stays a Candidate, not a canonical fact"
    assert candidate["valid_from"] <= candidate["valid_to"], "the Candidate needs a valid interval"
    assert 0.0 <= float(candidate["confidence"]) <= 1.0
    assert candidate["uncertainty"], "every inference Candidate names its uncertainty"
    assert candidate["support_refs"] and candidate["conflict_refs"] is not None
    assert candidate["receipt"]["receipt_id"] and candidate["receipt"]["receipt_checksum"]
    _assert_metadata_only(candidate)


def test_two_freshness_legs_are_validated_and_retained(tmp_path):
    """Both freshness legs stay typed in the staged binding; none collapses to current."""
    _require_reflection()
    result = _stage(tmp_path / "reflection.sqlite", _dispatcher_metadata())
    assert result["status"] == "staged"
    legs = result["candidate"]["freshness"]
    assert set(legs) == {"source_to_agentsview", "agentsview_to_canonical"}
    for leg in legs.values():
        assert leg["leg"] in {"source_to_agentsview", "agentsview_to_canonical"}
        assert leg["status"] == "current"
        assert leg["watermark"] and leg["observed_at"]


def test_stage_receipt_is_metadata_only_no_body_or_secret(tmp_path):
    """The staged receipt preserves no private body, prompt, credential or SQL."""
    _require_reflection()
    result = _stage(tmp_path / "reflection.sqlite", _dispatcher_metadata())
    assert result["status"] == "staged"
    _assert_metadata_only(result)
    _assert_metadata_only(result["receipt"])
    text = json.dumps(result)
    for sentinel in SENTINELS:
        assert sentinel not in text, "staged receipt leaked a private sentinel"


def test_staging_never_writes_canonical_promotion_pointer_or_permission_state(tmp_path):
    """Candidate staging cannot mutate canonical/promotion/authority state."""
    _require_reflection()
    authority = tmp_path / "authority"
    authority.mkdir()
    files = {
        "canonical.sqlite": b"canonical-bytes",
        "active_pointer.txt": b"active-pointer-bytes",
        "watermark.json": b"{}",
        "permissions.json": b"{}",
        "values.json": b"{}",
    }
    for name, content in files.items():
        (authority / name).write_bytes(content)

    def fingerprints() -> dict:
        return {name: hashlib.sha256((authority / name).read_bytes()).hexdigest() for name in files}

    before = fingerprints()
    _stage(tmp_path / "reflection.sqlite", _dispatcher_metadata())
    assert fingerprints() == before, (
        "staging a Candidate must never mutate canonical/promotion/watermark/"
        "active-pointer/permission/value state"
    )
