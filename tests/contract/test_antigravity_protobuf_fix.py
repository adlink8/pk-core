# Antigravity live-store step_payload contract.
# Covers the payload classes the live steps.step_payload column can hold:
#   a) a UTF-8 JSON payload -> mapped to typed events (user/assistant/tool/usage);
#   b) well-formed binary protobuf (no .proto schema) -> decoded from the
#      protobuf wire format, which is self-describing. The full field mapping
#      recovered from real stores is covered by
#      test_antigravity_protobuf_decode.py;
#   c) binary bytes that are NOT well-formed protobuf -> preserved by reference
#      as UNKNOWN_NATIVE with an explicit step_payload -> preserved_by_reference
#      field disposition and content_availability = unavailable -- never invented.
# Real-artifact recon (C:/Users/li/.gemini/antigravity/conversations/*.db) shows
# every step carries step_format = 0 with binary protobuf payloads beginning in
# protobuf varint framing (0x08 field 1 / 0x2a field 5), never JSON text.
#
# _PROTOBUF_PAYLOAD below is deliberately *truncated* -- it declares a
# length-delimited field longer than the buffer -- so it exercises path (c) and
# stands in for a payload whose wire format cannot be trusted.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources import antigravity
from personal_knowledge.adapters.conversation_sources.contracts import (
    SourceArtifact,
    SourceArtifactSet,
)
from personal_knowledge.core.conversation_events import (
    EventKind,
    FidelityDimension,
    FidelityLevel,
)


_LIVE_SCHEMA = """
CREATE TABLE trajectory_meta (
    trajectory_id text, cascade_id text, trajectory_type integer, source integer,
    PRIMARY KEY (trajectory_id)
);
CREATE TABLE steps (
    idx integer, step_type integer NOT NULL DEFAULT 0, status integer NOT NULL DEFAULT 0,
    has_subtrajectory numeric NOT NULL DEFAULT false, metadata blob,
    error_details blob, permissions blob, task_details blob, render_info blob,
    step_payload blob, step_format integer NOT NULL DEFAULT 0, PRIMARY KEY (idx)
);
CREATE TABLE parent_references (idx integer, data blob, PRIMARY KEY (idx));
"""

# A realistic protobuf Step header: varint fields then a length-delimited
# field 5 carrying a subtree. Binary, not decodable without a .proto schema.
_PROTOBUF_PAYLOAD = bytes([0x08, 0x0E, 0x20, 0x03, 0x2A, 0x98, 0x01, 0x0A, 0x22])


def _make_artifact(root: Path, name: str = "live.db") -> SourceArtifact:
    return SourceArtifact(
        artifact_id=name, family="antigravity", source_kind="sqlite",
        content_hash="h", capture_method="sqlite", relative_path=name, byte_size=1,
    )


def _live_db(tmp_path: Path, *, payloads, format_values=None) -> Path:
    db = tmp_path / "live.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(_LIVE_SCHEMA)
        con.execute("INSERT INTO trajectory_meta VALUES ('t1','c1',1,0)")
        for idx, payload in enumerate(payloads):
            fmt = (format_values[idx] if format_values else 0)
            con.execute(
                "INSERT INTO steps (idx, step_type, status, step_payload, step_format) "
                "VALUES (?,0,0,?,?)",
                (idx, payload, fmt),
            )
        con.commit()
    finally:
        con.close()
    return db


def _adapt(db: Path):
    artifact = _make_artifact(db.parent)
    return antigravity.adapt(
        SourceArtifactSet(artifacts=(artifact,)), artifact_root=db.parent
    )


# ---------------------------------------------------------------- protobuf path

class TestProtobufPayloadPreservedByReference:
    def test_protobuf_yields_unknown_native_without_crash(self, tmp_path):
        db = _live_db(tmp_path, payloads=[_PROTOBUF_PAYLOAD, _PROTOBUF_PAYLOAD])
        result = _adapt(db)
        assert result.family == "antigravity"
        unknown = [e for e in result.events if e.kind is EventKind.UNKNOWN_NATIVE]
        assert len(unknown) == 2
        for ev in unknown:
            assert ev.ordinal is not None
            assert ev.summary and "step_type=" in ev.summary

    def test_protobuf_has_explicit_step_payload_field_disposition(self, tmp_path):
        db = _live_db(tmp_path, payloads=[_PROTOBUF_PAYLOAD])
        result = _adapt(db)
        events_with_disp = [e for e in result.events if e.field_dispositions]
        assert len(events_with_disp) == 1
        disp = events_with_disp[0].field_dispositions[0]
        assert disp.field_name == "step_payload"
        assert disp.disposition.value == "preserved_by_reference"
        assert "protobuf" in disp.reason.lower()

    def test_content_availability_unavailable_on_protobuf_step(self, tmp_path):
        db = _live_db(tmp_path, payloads=[_PROTOBUF_PAYLOAD])
        result = _adapt(db)
        events_with_disp = [e for e in result.events if e.field_dispositions]
        ev = events_with_disp[0]
        assert (
            ev.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY)
            is FidelityLevel.UNAVAILABLE
        )

    def test_no_n_by_m_step_duplication(self, tmp_path):
        db = _live_db(tmp_path, payloads=[_PROTOBUF_PAYLOAD] * 38)
        result = _adapt(db)
        unknown = [e for e in result.events if e.kind is EventKind.UNKNOWN_NATIVE]
        assert len(unknown) == 38
        ids = [e.event_id for e in result.events]
        assert len(ids) == len(set(ids))

    def test_warning_states_protobuf_reason(self, tmp_path):
        db = _live_db(tmp_path, payloads=[_PROTOBUF_PAYLOAD])
        result = _adapt(db)
        joined = " ".join(result.warnings)
        assert "protobuf" in joined
        assert "schema" in joined

    def test_capability_declares_wire_format_decoding(self):
        # Adapter 1.2.x replaced "unavailable_when_protobuf_without_schema"
        # with real wire-format decoding; only malformed payloads still fall
        # back to preserve-by-reference.
        caps = antigravity.capability().capabilities
        assert "wire_format" in caps["content_availability"]
        assert antigravity.ADAPTER_VERSION == "1.2.1"


# ------------------------------------------------------------------ JSON path

class TestJsonPayloadClassified:
    def test_json_payload_maps_user_and_assistant(self, tmp_path):
        payload = json.dumps(
            {"phase": "a", "items": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]}
        ).encode("utf-8")
        db = _live_db(tmp_path, payloads=[payload])
        result = _adapt(db)
        kinds = {e.kind for e in result.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        user = next(e for e in result.events if e.kind is EventKind.USER_MESSAGE)
        assert user.content == "hello"
        assert user.summary is None
        assistant = next(e for e in result.events if e.kind is EventKind.ASSISTANT_MESSAGE)
        assert assistant.content == "hi there"

    def test_json_with_usage_yields_usage_event(self, tmp_path):
        payload = json.dumps(
            {"items": [{
                "role": "assistant", "content": "a",
                "usage": {"input_tokens": 12, "output_tokens": 5},
            }]}
        ).encode("utf-8")
        db = _live_db(tmp_path, payloads=[payload])
        result = _adapt(db)
        usage = [e for e in result.events if e.kind is EventKind.USAGE]
        assert len(usage) == 1
        assert "input_tokens=12" in usage[0].summary

    def test_json_list_payload(self, tmp_path):
        payload = json.dumps([
            {"role": "user", "content": "q1"},
            {"role": "tool", "content": "tool out"},
        ]).encode("utf-8")
        db = _live_db(tmp_path, payloads=[payload])
        result = _adapt(db)
        kinds = {e.kind for e in result.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.TOOL_CALL in kinds

    def test_json_unrecognized_role_becomes_unknown(self, tmp_path):
        payload = json.dumps({"role": "weird_thing", "content": "x"}).encode("utf-8")
        db = _live_db(tmp_path, payloads=[payload])
        result = _adapt(db)
        unknown = [e for e in result.events if e.kind is EventKind.UNKNOWN_NATIVE]
        assert len(unknown) == 1

    def test_json_warning_reported(self, tmp_path):
        payload = json.dumps({"role": "user", "content": "hi"}).encode("utf-8")
        db = _live_db(tmp_path, payloads=[payload])
        result = _adapt(db)
        assert any("JSON" in w for w in result.warnings)
