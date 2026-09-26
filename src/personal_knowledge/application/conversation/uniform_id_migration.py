"""Uniform, origin-derived canonical ids — one scheme for the whole authority DB.

Why this module exists
======================

The authority DB grew three id regimes for the same logical data:

* ``v2|cs|`` / ``v2|cm|`` / ``v2|cte|`` — the adapter (event-store) projection,
  ids hashed from internal ``ce_*`` ids.
* ``cs|`` / ``cm|`` / ``cte|`` — the AgentsView chain, message/tool ids hashed
  from AgentsView's *rowid*. AgentsView re-imported its database, so rowids were
  reallocated: the same id now denotes a different message depending on which
  track wrote the row (measured 2026-09-23: 165 message + 874 tool ids collided
  across tracks, 24 sessions affected).
* ``cm|legacy|<session>:<event_index>`` — legacy raw refs.

Consequence: one native session can occupy two canonical rows with different
ids (179 codex pairs), and an id's meaning depends on who wrote it. That is the
"different states in one table" this migration removes.

The rule (one formula, whole DB)
================================

Every canonical row is keyed by where it physically comes from::

    session:  cs|<family>|<native_session_id>
    message:  cm|<family>|<native_session_id>|<address at origin>
    tool:     ct|<family>|<native_session_id>|<address at origin>

``<address at origin>`` is the message's own address inside its origin, in a
per-family precedence (see ``NATIVE_ID_FIRST_FAMILIES``):

1. for families whose adapters record a reliable per-message native id (claude
   ``uuid``, zcode ``part_…``, mimo/opencode ``msg_…``, …) the native message
   id is the address and the native locator (``rollout-….jsonl#L1011``,
   ``agent-….jsonl#L1``) is only the fallback. The locator embeds the
   mirror-relative path of the artifact it was staged from, so when one native
   session is collected through N different mirror paths the same message used
   to get N different addresses (measured 2026-09-24: ~770 duplicate rows out
   of 4,007 in one canonical session); the native id is mirror-path-free and
   collapses those copies back onto one row;
2. for families whose native id is NOT reliably unique (measured 2026-09-23:
   the codex adapter records the literal ``agent_message`` as the native id of
   446 message events in one session, so id-first would collapse them) the
   locator is the address and the native id is only the fallback — codex, grok
   and chatgpt stay locator-first;
3. for rows whose only surviving origin is the AgentsView snapshot (no native
   root on disk: chatgpt, vscode-copilot, gemini, qoder, …, plus any session the
   adapter never captured): the legacy raw ref (``legacy:<file>:<index>``) for
   legacy rows, else the message's position in that snapshot (``av<ordinal>``
   for messages, ``av<call_index>`` for tools, with a deterministic ``#n``
   suffix when a session repeats a call_index).

The *form* of the address varies per origin because the origins differ; the
*rule* does not. What the rule guarantees: one row per (session, message), the
id re-derivable from the origin alone, and a later re-import of the same native
file producing the *same* ids — so it updates rows instead of duplicating them.

Merge semantics
===============

Sessions are keyed by ``(family, native_session_id)``; all tracks' rows for one
native session are merged. Adapter rows own the id (they carry the native
address). A snapshot row is dropped only when its ``(role, content_hash,
content_length, timestamp)`` is already present among the adapter rows for that
session (multiset semantics, so repeated identical turns are handled); every
dropped row is still mapped in ``id_migration_map`` to the surviving row that
covered it. Snapshot rows with no adapter counterpart are kept and addressed
from the snapshot — measured 2026-09-23, for sessions present in both tracks the
snapshot holds content the adapter does not (grok sessions where the adapter
projected zero messages; codex sessions with hundreds of snapshot-only
messages) and vice versa, so dropping either side loses data.

Known trade-off: tool rows carry no content, so tool "already present" is
decided on ``(source_kind, tool_name, content_length, timestamp, category,
status)``. Two genuinely different tool results agreeing on all six fields would
be merged; the run report counts the exposed sessions.

Second known trade-off (P1-20): ``_sanitize`` maps ``|`` to ``/`` because ids
are ``|``-joined, so two different raw keys (one containing ``|``, one
containing ``/``) fold onto the same id. The formula is frozen in 610k+ stored
ids and must not change; both the migration planner and the live projection
therefore only *count* such collisions (``stats['id_collisions']`` /
``CompatibilityProjectionReport.sanitized_id_collisions``) so a silent
overwrite becomes an observable, auditable event.

Safety
======

``plan_migration`` is pure and read-only. ``verify_plan`` must report no
problems before ``apply_plan`` may run: every old id mapped exactly once, no
content multiset lost per session, no duplicate new ids, every relation endpoint
resolvable. ``apply_plan`` swaps the canonical tables inside one transaction;
take a file-level backup first (``backup_authority_db``). ``id_migration_map``
records old→new for every row so downstream indexes can be re-anchored by join
instead of re-derivation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Event kinds the compatibility projection turns into canonical rows.
MESSAGE_KINDS = frozenset(
    {"user_message", "assistant_message", "developer_message", "system_message"}
)
TOOL_KINDS = frozenset({"tool_call", "tool_result"})

# AgentsView agent label -> adapter family (aliases resolve to their owner).
ADAPTER_FAMILY_BY_AGENTSVIEW: dict[str, str] = {
    "codex": "codex", "claude": "claude", "qoder": "qoder", "zcode": "zcode",
    "workbuddy": "workbuddy", "kimi": "kimi", "kimi-work": "kimi-work",
    "grok": "grok", "chatgpt": "chatgpt", "copilot": "copilot",
    "vscode-copilot": "copilot", "mimocode": "mimo", "mimo": "mimo",
    "opencode": "opencode", "antigravity": "antigravity", "gemini": "gemini",
    "cursor": "cursor", "pi": "pi",
}

SESSION_COLUMNS = (
    "canonical_session_id", "primary_source", "agent", "started_at", "ended_at",
    "message_count", "user_message_count", "file_hash", "parent_canonical_id",
    "relationship_type", "cwd", "git_branch", "model", "evidence_eligible",
    "evidence_scope", "merged", "lifecycle", "superseded_by_canonical_id",
)
MESSAGE_COLUMNS = (
    "canonical_message_id", "canonical_session_id", "source",
    "source_message_ref", "ordinal", "role", "content", "content_length",
    "timestamp", "model", "is_system", "is_sidechain", "content_hash",
    "evidence_scope",
)
TOOL_COLUMNS = (
    "canonical_tool_id", "canonical_session_id", "source", "source_kind",
    "tool_name", "category", "status", "call_index", "subagent_session_id",
    "content_length", "timestamp",
)
LINK_COLUMNS = (
    "link_id", "canonical_session_id", "source", "source_session_id",
    "source_raw_file", "match_method", "match_confidence",
)

INDEX_DDL = (
    "CREATE INDEX idx_cm_session ON canonical_messages (canonical_session_id, ordinal)",
    "CREATE INDEX idx_cm_role ON canonical_messages (role)",
    "CREATE INDEX idx_cs_agent ON canonical_sessions (agent)",
    "CREATE INDEX idx_cs_evidence ON canonical_sessions (evidence_eligible)",
    "CREATE INDEX idx_cte_session ON canonical_tool_events (canonical_session_id)",
    "CREATE INDEX idx_ssl_canonical ON session_source_links (canonical_session_id)",
    "CREATE INDEX idx_ssl_source ON session_source_links (source, source_session_id)",
)

LEGACY_REF_RE = re.compile(r"^legacy:(.+):(\d+)$")


def _note_id_collision(raw_by_id: dict, new_id: str, raw_key: tuple,
                       stats: Counter) -> None:
    """P1-20: 观测 ``_sanitize`` 把不同原始键折叠到同一 canonical id 的碰撞。

    id 以 ``|`` 连接，``_sanitize`` 因此把部件里的 ``|`` 替换成 ``/``——含
    ``|`` 与含 ``/`` 的两个不同原始键会得到同一个 id。该公式已固化在 61 万+
    存量 id 里，绝不能改（改 = 全库重编 id），碰撞无法消除、只能观测：每个
    落到已占用 id 上的"新"原始键计 1 次，写入 ``stats['id_collisions']``；
    同一原始键的合法重现（同址重捕获）不计。规划路径上 verify_plan 本就会对
    plan 内重复 id fail-closed，这个计数让报告能直接说明重复的成因。
    """
    raws = raw_by_id.setdefault(new_id, set())
    if raws and raw_key not in raws:
        stats["id_collisions"] += 1
    raws.add(raw_key)

# Families whose adapters record a reliable per-message native id (a client
# uuid unique within the native session): for these the native id is the
# message address and the locator (which embeds the mirror-relative path) is
# only the fallback, so collecting one native session through several mirror
# paths reproduces the same cm id instead of duplicating every message.
# Deliberately absent: codex (literal 'agent_message' shared by 446 message
# events of one session — id-first would collapse them), grok and chatgpt
# (no reliable native message id; keep the locator-first behavior), cursor
# (native message ids optional/unverified — conservative default applies).
NATIVE_ID_FIRST_FAMILIES: frozenset[str] = frozenset({
    "claude", "qoder", "gemini", "copilot", "workbuddy", "kimi", "kimi-work",
    "zcode", "mimo", "opencode", "antigravity", "pi",
})


# --------------------------------------------------------------------------
# id construction
# --------------------------------------------------------------------------

def _sanitize(part: object) -> str:
    """Ids are '|'-joined; a part containing '|' would forge extra fields."""
    return str(part).replace("|", "/").replace("\n", " ").replace("\r", " ").strip()


def make_session_id(family: str, native_key: str) -> str:
    return f"cs|{_sanitize(family)}|{_sanitize(native_key)}"


def make_message_id(family: str, native_key: str, address: str) -> str:
    return f"cm|{_sanitize(family)}|{_sanitize(native_key)}|{_sanitize(address)}"


def make_tool_id(family: str, native_key: str, address: str) -> str:
    return f"ct|{_sanitize(family)}|{_sanitize(native_key)}|{_sanitize(address)}"


def _norm_hash(prefix: str, *parts: object) -> str:
    """The legacy hash formula, reproduced to read old ids back."""
    payload = "|".join(str(p) for p in parts)
    return f"{prefix}|{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


def v2_session_hash(ce_session_id: str) -> str:
    return _norm_hash("v2|cs", ce_session_id)


def v2_message_hash(event_id: str) -> str:
    return _norm_hash("v2|cm", event_id)


def v2_tool_hash(event_id: str) -> str:
    return _norm_hash("v2|cte", event_id)


# --------------------------------------------------------------------------
# plan structures
# --------------------------------------------------------------------------

class MigrationPlan:
    """Pure data: the complete post-migration state plus provenance maps."""

    def __init__(self) -> None:
        self.sessions: list[dict] = []
        self.messages: list[dict] = []   # 'content' excluded (joined at apply)
        self.tools: list[dict] = []
        self.links: list[dict] = []
        self.relations: list[dict] = []
        self.origins: list[tuple] = []   # (session, kind, ref)
        self.id_map: list[tuple] = []    # (table_name, old_id, new_id)
        self.adapter_addresses: dict[str, str] = {}  # old adapter id -> address
        self.adapter_new_ids: set[str] = set()       # new ids from adapter rows
        self.own_row_ids: set[str] = set()           # old ids that kept own row
        self.policy: list[tuple] = []    # (family, entity, address_form, rows)
        self.stats: dict = {}
        # P1-20: canonical id -> set(raw keys)，碰撞检测的临时账本（不进报告）。
        self.raw_keys_by_id: dict[str, set] = {}

    def report(self) -> dict:
        return {
            "stats": self.stats,
            "counts": {
                "sessions": len(self.sessions),
                "messages": len(self.messages),
                "tools": len(self.tools),
                "links": len(self.links),
                "relations": len(self.relations),
                "id_map": len(self.id_map),
            },
            "policy": [list(p) for p in self.policy],
        }


class _Group:
    """Everything the migration knows about one native session."""

    def __init__(self, family: str, native_key: str) -> None:
        self.family = family
        self.native_key = native_key
        self.v1_sessions: list[dict] = []
        self.v1_links: list[dict] = []
        self.v1_messages: list[dict] = []
        self.v1_tools: list[dict] = []
        self.v2_csids: list[str] = []
        self.v2_sessions: list[dict] = []
        self.v2_messages: list[dict] = []
        self.v2_tools: list[dict] = []

    @property
    def new_session_id(self) -> str:
        return make_session_id(self.family, self.native_key)


# --------------------------------------------------------------------------
# planning (read-only)
# --------------------------------------------------------------------------

def plan_migration(con: sqlite3.Connection) -> MigrationPlan:
    """Compute the full post-migration state. Never writes to ``con``."""
    plan = MigrationPlan()
    stats: Counter = Counter()
    forms: Counter = Counter()

    gen_rank = _generation_ranks(con)
    nat_of_ce, ceids_of_nat = _load_ce_sessions(con)
    stats["ce_sessions"] = len(nat_of_ce)

    v2_csids = {
        r[0] for r in con.execute(
            "SELECT canonical_session_id FROM canonical_sessions "
            "WHERE canonical_session_id LIKE 'v2|%'")
    }
    ce_of_v2: dict[str, str] = {}
    for ce_sid in _flatten(ceids_of_nat):
        h = v2_session_hash(ce_sid)
        if h in v2_csids:
            ce_of_v2[h] = ce_sid
    stats["v2_canonical_sessions"] = len(v2_csids)
    stats["v2_sessions_resolved"] = len(ce_of_v2)

    ev_attr = _load_event_addresses(con, set(ce_of_v2.values()))
    stats["event_addresses"] = len(ev_attr)
    gen_of_v2_csid = {
        csid: gen_rank.get(_generation_of(con, ce_sid), 99)
        for csid, ce_sid in ce_of_v2.items()
    }

    groups: dict[tuple[str, str], _Group] = {}

    def group_for(family: str, native: str) -> _Group:
        return groups.setdefault((family, native), _Group(family, native))

    for csid, ce_sid in ce_of_v2.items():
        fam, nk = nat_of_ce[ce_sid]
        group_for(fam, nk).v2_csids.append(csid)

    v1_family_of = _load_v1_families(con)
    for row in _fetch(con, SESSION_COLUMNS, "canonical_sessions",
                      "WHERE canonical_session_id NOT LIKE 'v2|%'"):
        fam, nk = v1_family_of.get(row["canonical_session_id"], (None, None))
        if fam is None:
            fam, nk = "legacy", row["canonical_session_id"]
            stats["v1_sessions_without_link"] += 1
        group_for(fam, nk).v1_sessions.append(row)
        stats["v1_sessions"] += 1

    new_session_of_old: dict[str, str] = {}
    for (fam, nk), g in groups.items():
        new_csid = g.new_session_id
        _note_id_collision(plan.raw_keys_by_id, new_csid, (fam, nk), stats)
        for row in g.v1_sessions:
            new_session_of_old[row["canonical_session_id"]] = new_csid
        for csid in g.v2_csids:
            new_session_of_old[csid] = new_csid

        if g.v2_csids:
            marks = ",".join("?" * len(g.v2_csids))
            in_clause = f"WHERE canonical_session_id IN ({marks})"
            g.v2_sessions = _fetch(con, SESSION_COLUMNS, "canonical_sessions",
                                   in_clause, g.v2_csids)
            g.v2_messages = _fetch(con, MESSAGE_COLUMNS, "canonical_messages",
                                   in_clause, g.v2_csids)
            g.v2_tools = _fetch(con, TOOL_COLUMNS, "canonical_tool_events",
                                in_clause, g.v2_csids)
        for row in g.v1_sessions:
            csid = row["canonical_session_id"]
            g.v1_messages.extend(_fetch(
                con, MESSAGE_COLUMNS, "canonical_messages",
                "WHERE canonical_session_id=?", (csid,)))
            g.v1_tools.extend(_fetch(
                con, TOOL_COLUMNS, "canonical_tool_events",
                "WHERE canonical_session_id=?", (csid,)))
            g.v1_links.extend(_fetch(
                con, LINK_COLUMNS, "session_source_links",
                "WHERE canonical_session_id=?", (csid,)))

        msgs, msg_map = _merge_messages(g, ev_attr, gen_of_v2_csid, stats,
                                        forms, plan)
        tools, tool_map = _merge_tools(g, ev_attr, stats, forms, plan)
        plan.messages.extend(msgs)
        plan.tools.extend(tools)
        plan.id_map.extend(("canonical_messages", o, n)
                           for o, n in msg_map.items())
        plan.id_map.extend(("canonical_tool_events", o, n)
                           for o, n in tool_map.items())

        plan.sessions.append(_merge_session(g, new_msgs=msgs))
        for link in g.v1_links:
            plan.links.append({
                "link_id": _norm_hash("link", new_csid, link["source"],
                                      link["source_session_id"]),
                "canonical_session_id": new_csid,
                "source": link["source"],
                "source_session_id": link["source_session_id"],
                "source_raw_file": link["source_raw_file"],
                "match_method": link["match_method"],
                "match_confidence": link["match_confidence"],
            })
            plan.origins.append((new_csid, link["source"],
                                 link["source_session_id"]))
        if g.v2_csids:
            plan.origins.append((new_csid, "adapter", nk))
        if g.v1_sessions and not g.v2_csids:
            stats["groups_snapshot_only"] += 1
        elif g.v2_csids and not g.v1_sessions:
            stats["groups_adapter_only"] += 1
        else:
            stats["groups_merged"] += 1

    _repoint_relations(con, plan, new_session_of_old, stats)
    plan.id_map.extend(("canonical_sessions", o, n)
                       for o, n in new_session_of_old.items())
    stats["sessions_before"] = len(v2_csids) + stats["v1_sessions"]
    stats["sessions_after"] = len(plan.sessions)
    stats["messages_after"] = len(plan.messages)
    stats["tools_after"] = len(plan.tools)
    plan.policy = _address_policy(forms)
    plan.stats = dict(stats)
    return plan


def _flatten(ceids_of_nat: dict) -> list[str]:
    out: list[str] = []
    for sids in ceids_of_nat.values():
        out.extend(sids)
    return out


def _fetch(con, columns, table, where="", params=()) -> list[dict]:
    sql = f"SELECT {','.join(columns)} FROM {table} {where}"
    return [dict(zip(columns, r)) for r in con.execute(sql, params)]


def _generation_of(con, ce_sid: str):
    row = con.execute("SELECT generation_id FROM ce_sessions WHERE session_id=?",
                      (ce_sid,)).fetchone()
    return row[0] if row else None


def _generation_ranks(con) -> dict[str, int]:
    """Lower rank = preferred when one event exists in several generations."""
    ranks: dict[str, int] = {}
    try:
        rows = con.execute(
            "SELECT generation_id, active FROM ce_generation_authority "
            "ORDER BY active DESC, updated_at DESC").fetchall()
    except sqlite3.Error:
        return ranks
    for i, (gid, active) in enumerate(rows):
        ranks[gid] = 0 if active else i + 1
    return ranks


def _load_ce_sessions(con):
    nat_of_ce: dict[str, tuple[str, str]] = {}
    ceids_of_nat: dict[tuple[str, str], list[str]] = defaultdict(list)
    for sid, fam, nsid in con.execute(
        "SELECT session_id, family, native_session_id FROM ce_sessions ORDER BY rowid"
    ):
        family = (fam or "unknown").strip().lower()
        native = (nsid or "").strip() or f"ce:{sid}"
        nat_of_ce[sid] = (family, native)
        ceids_of_nat[(family, native)].append(sid)
    return nat_of_ce, ceids_of_nat


def _load_event_addresses(con, ce_sids: set[str]) -> dict:
    """canonical id (v2 hash) -> (native_event_id, native_locator)."""
    con.execute("CREATE TEMP TABLE IF NOT EXISTS proj_ce (session_id TEXT PRIMARY KEY)")
    con.execute("DELETE FROM proj_ce")
    con.executemany("INSERT OR IGNORE INTO proj_ce VALUES (?)",
                    [(s,) for s in ce_sids])
    attr: dict[str, tuple] = {}
    for eid, kind, nid, loc in con.execute(
        "SELECT e.event_id, e.kind, e.native_event_id, e.native_locator "
        "FROM ce_events e JOIN proj_ce p ON p.session_id = e.session_id"
    ):
        if kind in MESSAGE_KINDS:
            attr[v2_message_hash(eid)] = (nid, loc)
        elif kind in TOOL_KINDS:
            attr[v2_tool_hash(eid)] = (nid, loc)
    con.execute("DELETE FROM proj_ce")
    return attr


def _load_v1_families(con) -> dict[str, tuple[str, str]]:
    """v1 canonical session -> (adapter family, native session key)."""
    agent_of = {
        r[0]: r[1] for r in con.execute(
            "SELECT canonical_session_id, agent FROM canonical_sessions "
            "WHERE canonical_session_id NOT LIKE 'v2|%'")
    }
    out: dict[str, tuple[str, str]] = {}
    for csid, source_sid in con.execute(
        "SELECT canonical_session_id, source_session_id FROM session_source_links "
        "WHERE source='agentsview'"
    ):
        raw = (source_sid or "").strip()
        native, prefix = raw, None
        if ":" in raw:
            prefix, rest = raw.split(":", 1)
            if prefix in ADAPTER_FAMILY_BY_AGENTSVIEW:
                native = rest
        agent = (agent_of.get(csid) or "").strip().lower()
        family = ADAPTER_FAMILY_BY_AGENTSVIEW.get(agent)
        if family is None and prefix:
            family = ADAPTER_FAMILY_BY_AGENTSVIEW.get(prefix.lower())
        if family is None:
            family = agent or "unknown"
        out[csid] = (family, native)
    return out


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------

def _content_key(row: dict) -> tuple:
    return (row.get("role"), row.get("content_hash"), row.get("content_length"),
            row.get("timestamp"))


def _tool_key(row: dict) -> tuple:
    return (row.get("source_kind"), row.get("tool_name"), row.get("content_length"),
            row.get("timestamp"), row.get("category"), row.get("status"))


def adapter_address(native_event_id: str | None,
                    native_locator: str | None,
                    family: str = "") -> str:
    """The origin address of an adapter event, per family (see module doc).

    For families in ``NATIVE_ID_FIRST_FAMILIES`` the native message id is the
    address and the locator is the fallback: the locator embeds the
    mirror-relative path of the artifact a capture was staged from, so id-first
    is what keeps a re-capture through a different mirror path on the same cm
    id. For every other family (codex records the literal string
    ``agent_message`` as the native id of 446 message events in a single
    session, measured 2026-09-23) the locator stays the address and the native
    id the fallback.

    ``family`` defaults to ``""`` — an unknown family keeps the historical
    locator-first rule, so older call sites that do not pass a family keep
    their behavior. Tools are addressed by the locator everywhere: the decided
    scope of this fix is message ids only.
    """
    nid = (native_event_id or "").strip()
    loc = (native_locator or "").strip()
    if (family or "").strip().lower() in NATIVE_ID_FIRST_FAMILIES:
        return nid or loc
    return loc or nid


def _adapter_address(canonical_id: str, row: dict, ev_attr: dict,
                     stats: Counter, family: str = "") -> tuple[str, str]:
    """-> (address, form) for an adapter row (keeps the form for reporting).

    Same per-family rule as :func:`adapter_address`: messages of
    native-id-first families are addressed by their native id so the migrated
    id matches what the live projection derives for a re-capture; every other
    family (and every tool row) keeps the locator-first rule.
    """
    attr = ev_attr.get(canonical_id)
    nid_first = (family or "").strip().lower() in NATIVE_ID_FIRST_FAMILIES
    if attr:
        nid, loc = str(attr[0] or "").strip(), str(attr[1] or "").strip()
        if nid_first and nid:
            return nid, "native_event_id"
        if loc:
            return loc, "native_locator"
        if nid:
            return nid, "native_event_id"
    ref = (row.get("source_message_ref") or "").strip()
    if ref:
        stats["address_fallback_source_ref"] += 1
        return ref, "source_message_ref"
    stats["address_fallback_hash"] += 1
    return canonical_id, "canonical_id_hash"


def _snapshot_message_address(row: dict, stats: Counter) -> tuple[str, str]:
    if row.get("source") == "legacy":
        m = LEGACY_REF_RE.match((row.get("source_message_ref") or "").strip())
        if m:
            return f"legacy:{m.group(1)}:{m.group(2)}", "legacy_ref"
    ordinal = row.get("ordinal")
    if ordinal is None:
        stats["snapshot_message_without_ordinal"] += 1
        ordinal = 0
    return f"av{ordinal}", "av_ordinal"


def _snapshot_tool_address(row: dict, occurrence: int) -> tuple[str, str]:
    base = f"av{row.get('call_index')}"
    if occurrence > 1:
        return f"{base}#{occurrence}", "av_call_index_occ"
    return base, "av_call_index"


def _merge_messages(g: _Group, ev_attr: dict, gen_of_v2_csid: dict,
                    stats: Counter, forms: Counter,
                    plan: "MigrationPlan"):
    """Adapter rows own native addresses; uncovered snapshot rows survive."""
    fam = g.family
    by_addr: dict[str, list[dict]] = defaultdict(list)
    for row in g.v2_messages:
        addr, form = _adapter_address(row["canonical_message_id"], row,
                                      ev_attr, stats, fam)
        forms[(fam, "message", form)] += 1
        by_addr[addr].append(row)
    chosen: list[tuple[str, dict]] = []
    for addr, rows in by_addr.items():
        if len(rows) > 1:
            stats["adapter_duplicate_addresses"] += len(rows) - 1
        rows.sort(key=lambda r: (
            gen_of_v2_csid.get(r["canonical_session_id"], 99),
            -(r.get("content_length") or 0),
            r.get("content_hash") is None,
            r["canonical_message_id"],
        ))
        chosen.append((addr, rows[0]))

    chosen_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for addr, row in chosen:
        chosen_by_key[_content_key(row)].append((addr, row))
    for rows in chosen_by_key.values():
        rows.sort(key=lambda t: t[1]["canonical_message_id"])

    snap_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in g.v1_messages:
        snap_by_key[_content_key(row)].append(row)
    for rows in snap_by_key.values():
        rows.sort(key=lambda r: (r.get("ordinal") or 0,
                                 r["canonical_message_id"]))

    survivors: list[dict] = []
    cover_pairs: dict[str, str] = {}
    for key, snaps in snap_by_key.items():
        adapters = chosen_by_key.get(key, [])
        for i, snap in enumerate(snaps):
            if i < len(adapters):
                addr, winner = adapters[i]
                cover_pairs[snap["canonical_message_id"]] = \
                    make_message_id(fam, g.native_key, addr)
                stats["snapshot_messages_dropped_covered"] += 1
            else:
                survivors.append(snap)

    new_rows: list[dict] = []
    old_to_new: dict[str, str] = {}
    for addr, rows in by_addr.items():
        new_id = make_message_id(fam, g.native_key, addr)
        _note_id_collision(plan.raw_keys_by_id, new_id,
                           (fam, g.native_key, addr), stats)
        for row in rows:
            old_to_new[row["canonical_message_id"]] = new_id
            plan.adapter_addresses[row["canonical_message_id"]] = addr
            plan.adapter_new_ids.add(new_id)
            plan.own_row_ids.add(row["canonical_message_id"])
        new_rows.append(_message_row(g.new_session_id, new_id, rows[0]))
    # Survivor id collisions: two v1 snapshot copies of the same native session
    # (the merged db holds more than one v1 row per native id since the 09-23
    # publish) both carry ``av<N>`` ordinals starting at 0, so both copies'
    # av1 map to the same target id. Two cases:
    #   * same content tuple  -> a true captured duplicate; collapse to one
    #     row and count it (``snapshot_survivor_duplicates_dropped``);
    #   * diverged content at the same ordinal (AgentsView re-captured an
    #     edited session) -> BOTH survive: the later copy's address gets a
    #     ``#dup-<n>`` suffix, mirroring the tools-side occurrence rule.
    # Nothing else is dropped: this pass is a renumbering, not a cleanup.
    _CONTENT_TUPLE = ("role", "content_hash", "content_length", "timestamp")

    def _tuple_of(row: dict) -> tuple:
        return tuple(row.get(c) for c in _CONTENT_TUPLE)

    survivor_by_id: dict[str, dict] = {}
    dropped_duplicates = 0
    for row in survivors:
        addr, form = _snapshot_message_address(row, stats)
        forms[(fam, "message", form)] += 1
        new_id = make_message_id(fam, g.native_key, addr)
        prior = survivor_by_id.get(new_id)
        if prior is not None:
            if _tuple_of(prior) == _tuple_of(row):
                old_to_new[row["canonical_message_id"]] = new_id
                dropped_duplicates += 1
                continue
            serial = 2
            while True:
                disambiguated = make_message_id(
                    fam, g.native_key, f"{addr}#dup-{serial}")
                if disambiguated not in survivor_by_id:
                    new_id = disambiguated
                    break
                serial += 1
        survivor_by_id[new_id] = row
        old_to_new[row["canonical_message_id"]] = new_id
        plan.own_row_ids.add(row["canonical_message_id"])
    if dropped_duplicates:
        stats["snapshot_survivor_duplicates_dropped"] += dropped_duplicates
    for new_id in sorted(survivor_by_id):
        new_rows.append(
            _message_row(g.new_session_id, new_id, survivor_by_id[new_id])
        )
    old_to_new.update(cover_pairs)
    return new_rows, old_to_new


def _message_row(session_id: str, new_id: str, row: dict) -> dict:
    return {
        "canonical_message_id": new_id,
        "canonical_session_id": session_id,
        "source": row.get("source"),
        "source_message_ref": row.get("source_message_ref"),
        "ordinal": 0,  # assigned per session after merge
        "role": row.get("role"),
        "content_length": row.get("content_length"),
        "timestamp": row.get("timestamp"),
        "model": row.get("model"),
        "is_system": row.get("is_system") or 0,
        "is_sidechain": row.get("is_sidechain") or 0,
        "content_hash": row.get("content_hash"),
        "evidence_scope": row.get("evidence_scope") or "user",
        "_old_id": row["canonical_message_id"],
    }


def _merge_tools(g: _Group, ev_attr: dict, stats: Counter, forms: Counter,
                 plan: "MigrationPlan"):
    fam = g.family
    by_addr: dict[str, list[dict]] = defaultdict(list)
    for row in g.v2_tools:
        addr, form = _adapter_address(row["canonical_tool_id"], row, ev_attr,
                                      stats)
        forms[(fam, "tool", form)] += 1
        by_addr[addr].append(row)
    chosen: list[tuple[str, dict]] = []
    for addr, rows in by_addr.items():
        if len(rows) > 1:
            stats["adapter_duplicate_addresses"] += len(rows) - 1
        rows.sort(key=lambda r: (-(r.get("content_length") or 0),
                                 r.get("tool_name") is None,
                                 r["canonical_tool_id"]))
        chosen.append((addr, rows[0]))

    v1_key_counts = Counter(_tool_key(r) for r in g.v1_tools)
    if any(c > 1 for c in v1_key_counts.values()):
        stats["tool_key_ambiguous_sessions"] += 1

    chosen_by_key: dict[tuple, list[tuple[str, dict]]] = defaultdict(list)
    for addr, row in chosen:
        chosen_by_key[_tool_key(row)].append((addr, row))
    for rows in chosen_by_key.values():
        rows.sort(key=lambda t: t[1]["canonical_tool_id"])

    snap_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in g.v1_tools:
        snap_by_key[_tool_key(row)].append(row)
    for rows in snap_by_key.values():
        rows.sort(key=lambda r: (r.get("call_index") if r.get("call_index")
                                 is not None else 0,
                                 r.get("timestamp") or "",
                                 r.get("tool_name") or "",
                                 r.get("content_length") or 0,
                                 r["canonical_tool_id"]))

    survivors: list[dict] = []
    cover_pairs: dict[str, str] = {}
    for key, snaps in snap_by_key.items():
        adapters = chosen_by_key.get(key, [])
        for i, snap in enumerate(snaps):
            if i < len(adapters):
                addr, _winner = adapters[i]
                cover_pairs[snap["canonical_tool_id"]] = \
                    make_tool_id(fam, g.native_key, addr)
                stats["snapshot_tools_dropped_covered"] += 1
            else:
                survivors.append(snap)

    new_rows: list[dict] = []
    old_to_new: dict[str, str] = {}
    for addr, rows in by_addr.items():
        new_id = make_tool_id(fam, g.native_key, addr)
        _note_id_collision(plan.raw_keys_by_id, new_id,
                           (fam, g.native_key, addr), stats)
        for row in rows:
            old_to_new[row["canonical_tool_id"]] = new_id
            plan.adapter_addresses[row["canonical_tool_id"]] = addr
            plan.adapter_new_ids.add(new_id)
            plan.own_row_ids.add(row["canonical_tool_id"])
        new_rows.append(_tool_row(g.new_session_id, new_id, rows[0]))
    survivors.sort(key=lambda r: (r.get("call_index") if r.get("call_index")
                                  is not None else 0,
                                  r.get("timestamp") or "",
                                  r.get("tool_name") or "",
                                  r["canonical_tool_id"]))
    call_counts: Counter = Counter()
    for row in survivors:
        call_counts[row.get("call_index")] += 1
        addr, form = _snapshot_tool_address(row, call_counts[row.get("call_index")])
        forms[(fam, "tool", form)] += 1
        new_id = make_tool_id(fam, g.native_key, addr)
        old_to_new[row["canonical_tool_id"]] = new_id
        plan.own_row_ids.add(row["canonical_tool_id"])
        new_rows.append(_tool_row(g.new_session_id, new_id, row))
    old_to_new.update(cover_pairs)
    return new_rows, old_to_new


def _tool_row(session_id: str, new_id: str, row: dict) -> dict:
    return {
        "canonical_tool_id": new_id,
        "canonical_session_id": session_id,
        "source": row.get("source"),
        "source_kind": row.get("source_kind"),
        "tool_name": row.get("tool_name"),
        "category": row.get("category"),
        "status": row.get("status"),
        "call_index": row.get("call_index"),
        "subagent_session_id": row.get("subagent_session_id"),
        "content_length": row.get("content_length"),
        "timestamp": row.get("timestamp"),
        "_old_id": row["canonical_tool_id"],
    }


def _merge_session(g: _Group, new_msgs: list[dict]) -> dict:
    contributing = list(g.v1_sessions) + list(g.v2_sessions)
    starts = [r.get("started_at") for r in contributing if r.get("started_at")]
    ends = [r.get("ended_at") for r in contributing if r.get("ended_at")]
    eligible = 1
    for r in g.v1_sessions:
        if r.get("evidence_eligible") == 0:
            eligible = 0
    v1 = g.v1_sessions[0] if g.v1_sessions else {}
    return {
        "canonical_session_id": g.new_session_id,
        "primary_source": "agentsview" if g.v1_sessions else "legacy",
        "agent": g.family,
        "started_at": min(starts) if starts else None,
        "ended_at": max(ends) if ends else None,
        "message_count": len(new_msgs),
        "user_message_count": sum(1 for m in new_msgs if m["role"] == "user"),
        "file_hash": v1.get("file_hash"),
        "parent_canonical_id": None,
        "relationship_type": v1.get("relationship_type"),
        "cwd": v1.get("cwd"),
        "git_branch": v1.get("git_branch"),
        "model": v1.get("model"),
        "evidence_eligible": eligible,
        "evidence_scope": v1.get("evidence_scope") or "user",
        "merged": 0,
        "lifecycle": "active",
        "superseded_by_canonical_id": None,
    }


def _repoint_relations(con, plan: MigrationPlan, new_session_of_old: dict,
                       stats: Counter) -> None:
    seen: set[tuple] = set()
    for parent, child, rtype in con.execute(
        "SELECT parent_canonical_id, child_canonical_id, relationship_type "
        "FROM session_relations"
    ):
        np_, nc = new_session_of_old.get(parent), new_session_of_old.get(child)
        if not np_ or not nc:
            stats["relations_dropped_unresolved"] += 1
            continue
        sig = (np_, nc, rtype)
        if sig in seen:
            stats["relations_collapsed"] += 1
            continue
        seen.add(sig)
        plan.relations.append({
            "relation_id": _norm_hash("rel", np_, nc),
            "parent_canonical_id": np_,
            "child_canonical_id": nc,
            "relationship_type": rtype,
        })
    stats["relations_after"] = len(plan.relations)


def _address_policy(forms: Counter) -> list[tuple]:
    return [(fam, entity, form, n)
            for (fam, entity, form), n in sorted(forms.items())]


def _assign_ordinals(plan: MigrationPlan) -> None:
    """Dense 1-based ordinals per session, chronological (address as tiebreak)."""
    by_sid: dict[str, list[dict]] = defaultdict(list)
    for row in plan.messages:
        by_sid[row["canonical_session_id"]].append(row)
    for rows in by_sid.values():
        rows.sort(key=lambda r: (r.get("timestamp") or "~",
                                 r["canonical_message_id"]))
        for i, row in enumerate(rows, start=1):
            row["ordinal"] = i


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def verify_plan(con: sqlite3.Connection, plan: MigrationPlan) -> list[str]:
    """Return a list of problems; empty means the plan is safe to apply."""
    problems: list[str] = []

    for name, rows, key in (
        ("sessions", plan.sessions, "canonical_session_id"),
        ("messages", plan.messages, "canonical_message_id"),
        ("tools", plan.tools, "canonical_tool_id"),
    ):
        ids = [r[key] for r in rows]
        dupes = [i for i, c in Counter(ids).items() if c > 1]
        if dupes:
            problems.append(f"duplicate new {name} ids: {len(dupes)} "
                            f"(e.g. {dupes[:3]})")

    targets = {
        "canonical_sessions": {r["canonical_session_id"] for r in plan.sessions},
        "canonical_messages": {r["canonical_message_id"] for r in plan.messages},
        "canonical_tool_events": {r["canonical_tool_id"] for r in plan.tools},
    }
    map_counts: Counter = Counter()
    bad_targets: Counter = Counter()
    for table, old, new in plan.id_map:
        map_counts[(table, old)] += 1
        if new not in targets[table]:
            bad_targets[table] += 1
    for (table, old), c in map_counts.items():
        if c > 1:
            problems.append(f"old {table} id mapped {c} times: {old}")
    for table, c in bad_targets.items():
        problems.append(f"{c} {table} id_map entries point at missing new ids")

    old_counts = {
        "canonical_sessions": con.execute(
            "SELECT count(*) FROM canonical_sessions").fetchone()[0],
        "canonical_messages": con.execute(
            "SELECT count(*) FROM canonical_messages").fetchone()[0],
        "canonical_tool_events": con.execute(
            "SELECT count(*) FROM canonical_tool_events").fetchone()[0],
    }
    for table, n in old_counts.items():
        mapped = sum(c for (t, _), c in map_counts.items() if t == table)
        if mapped != n:
            problems.append(f"{table}: {n} old rows but {mapped} id_map entries")

    session_of_old = {o: n for t, o, n in plan.id_map if t == "canonical_sessions"}

    problems.extend(_coverage_problems(con, plan, session_of_old))

    for rel in plan.relations:
        if rel["parent_canonical_id"] not in targets["canonical_sessions"]:
            problems.append(f"relation parent missing: {rel['relation_id']}")
        if rel["child_canonical_id"] not in targets["canonical_sessions"]:
            problems.append(f"relation child missing: {rel['relation_id']}")
    for link in plan.links:
        if link["canonical_session_id"] not in targets["canonical_sessions"]:
            problems.append(f"link points at missing session: {link['link_id']}")
    for csid, _kind, _ref in plan.origins:
        if csid not in targets["canonical_sessions"]:
            problems.append(f"origin points at missing session: {csid}")
    return problems


def _address_of_new_id(new_id: str) -> str:
    """The address embedded in a new message/tool id (cm|family|native|addr)."""
    return new_id.split("|", 3)[3]


def _coverage_problems(con: sqlite3.Connection, plan: MigrationPlan,
                       session_of_old: dict[str, str]) -> list[str]:
    """Two independent gates, one per track.

    Adapter track (address gate): every distinct origin address that existed
    before must have exactly one surviving row. One address can appear in
    several event generations with different captured content — the newest
    capture wins, which is intended, so content is not compared here.

    Snapshot track (content gate): a snapshot row may be dropped only because an
    adapter row already carries the same content in that session, so every
    content key the snapshot held must still be present after the merge.
    """
    problems: list[str] = []
    specs = (
        ("canonical_messages", "canonical_message_id",
         ("role", "content_hash", "content_length", "timestamp"),
         plan.messages, _content_key),
        ("canonical_tool_events", "canonical_tool_id",
         ("source_kind", "tool_name", "content_length", "timestamp",
          "category", "status"),
         plan.tools, _tool_key),
    )
    for table, id_col, key_cols, rows, key_fn in specs:
        select = (f"SELECT canonical_session_id, {id_col}, "
                  + ", ".join(key_cols) + f" FROM {table}")
        old_adapter_addr: dict[str, set] = defaultdict(set)
        snapshot_keys: dict[str, Counter] = defaultdict(Counter)
        for row in con.execute(select):
            csid, old_id, key = row[0], row[1], tuple(row[2:])
            new_sid = session_of_old.get(csid)
            if new_sid is None:
                problems.append(f"{table} session {csid} has no new session")
                continue
            if csid.startswith("v2|"):
                addr = plan.adapter_addresses.get(old_id)
                if addr is None:
                    problems.append(f"{table} row {old_id} has no adapter address")
                    continue
                old_adapter_addr[new_sid].add(_sanitize(addr))
            else:
                snapshot_keys[new_sid][key] += 1

        new_keys: dict[str, Counter] = defaultdict(Counter)
        new_addr: dict[str, Counter] = defaultdict(Counter)
        for row in rows:
            sid = row["canonical_session_id"]
            new_keys[sid][key_fn(row)] += 1
            if row[id_col] in plan.adapter_new_ids:
                new_addr[sid][_address_of_new_id(row[id_col])] += 1

        for sid, addrs in old_adapter_addr.items():
            have = new_addr.get(sid, Counter())
            missing = {a for a in addrs if have.get(a, 0) == 0}
            if missing:
                problems.append(
                    f"session {sid}: {len(missing)} adapter {table} addresses "
                    f"lost (e.g. {sorted(missing)[:2]})")
        for sid, keys in snapshot_keys.items():
            have = new_keys.get(sid, Counter())
            for key in keys:
                if have.get(key, 0) == 0:
                    problems.append(
                        f"session {sid}: snapshot {table} content lost ({key})")
    return problems


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def backup_authority_db(db_path: Path, backup_path: Path) -> Path:
    """Single rolling file-level backup of the authority DB."""
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, backup_path)
    return backup_path


def apply_plan(con: sqlite3.Connection, plan: MigrationPlan) -> None:
    """Swap the canonical tables in one transaction. Assumes verify_plan passed."""
    con.execute("BEGIN IMMEDIATE")
    try:
        _stage(con, plan)
        live = ("canonical_messages", "canonical_tool_events",
                "canonical_sessions", "session_source_links", "session_relations")
        for table in live:
            ddl = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()[0]
            con.execute(f"ALTER TABLE {table} RENAME TO {table}_pre")
            con.execute(ddl)
        con.execute("INSERT INTO canonical_sessions SELECT "
                    + ",".join(SESSION_COLUMNS) + " FROM stage_sessions")
        con.execute(
            "INSERT INTO canonical_messages ("
            + ",".join(c for c in MESSAGE_COLUMNS if c != "content") + ", content) "
            "SELECT s.new_id, s.canonical_session_id, s.source, s.source_message_ref,"
            " s.ordinal, s.role, s.content_length, s.timestamp, s.model,"
            " s.is_system, s.is_sidechain, s.content_hash, s.evidence_scope,"
            " o.content "
            "FROM stage_messages s JOIN canonical_messages_pre o "
            "ON o.canonical_message_id = s.old_id")
        con.execute("INSERT INTO canonical_tool_events SELECT "
                    + ",".join(TOOL_COLUMNS) + " FROM stage_tools")
        con.execute(
            "INSERT INTO session_source_links (link_id, canonical_session_id, source,"
            " source_session_id, source_raw_file, match_method, match_confidence)"
            " SELECT link_id, canonical_session_id, source, source_session_id,"
            " source_raw_file, match_method, match_confidence FROM stage_links")
        con.execute(
            "INSERT INTO session_relations (relation_id, parent_canonical_id,"
            " child_canonical_id, relationship_type) SELECT relation_id,"
            " parent_canonical_id, child_canonical_id, relationship_type"
            " FROM stage_relations")
        # Drop the pre-migration tables before recreating indexes: a renamed
        # table keeps its indexes (and their names), so idx_cm_session would
        # still be taken by canonical_messages_pre.
        for table in live:
            con.execute(f"DROP TABLE {table}_pre")
        for ddl in INDEX_DDL:
            con.execute(ddl)
        _repair_renamed_references(con)
        _write_bookkeeping(con, plan)
        con.commit()
    except Exception:
        con.rollback()
        raise


def _repair_renamed_references(con: sqlite3.Connection) -> None:
    """Undo the REFERENCES rewrite that ``ALTER TABLE … RENAME`` performs.

    SQLite rewrites the ``REFERENCES canonical_sessions`` clause of every other
    table when ``canonical_sessions`` is renamed (to ``…_pre`` above). Once the
    ``_pre`` tables are dropped those clauses point at a table that no longer
    exists: the data is intact, but ``PRAGMA foreign_key_check`` reports one
    violation per row (measured 586,725) and any connection that enables
    ``PRAGMA foreign_keys`` breaks. Patch the stored DDL back and bump the
    schema version so other connections reparse it.
    """
    referencing = (
        "canonical_messages", "canonical_tool_events", "session_source_links",
    )
    ddl_of = {
        table: (con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone() or [None])[0]
        for table in referencing
    }
    if not any(ddl and "canonical_sessions_pre" in ddl
               for ddl in ddl_of.values()):
        return
    version = con.execute("PRAGMA schema_version").fetchone()[0]
    con.execute("PRAGMA writable_schema=ON")
    try:
        for table in referencing:
            con.execute(
                "UPDATE sqlite_master SET sql=replace(sql, ?, ?) "
                "WHERE type='table' AND name=?",
                ("canonical_sessions_pre", "canonical_sessions", table),
            )
        con.execute(f"PRAGMA schema_version={version + 1}")
    finally:
        con.execute("PRAGMA writable_schema=OFF")


def _stage(con: sqlite3.Connection, plan: MigrationPlan) -> None:
    for name in ("stage_sessions", "stage_messages", "stage_tools",
                 "stage_links", "stage_relations", "stage_map"):
        con.execute(f"DROP TABLE IF EXISTS {name}")
    con.execute("CREATE TEMP TABLE stage_sessions ("
                + ",".join(SESSION_COLUMNS) + ")")
    con.executemany(
        f"INSERT INTO stage_sessions VALUES ({','.join('?' * len(SESSION_COLUMNS))})",
        [tuple(r[c] for c in SESSION_COLUMNS) for r in plan.sessions])

    con.execute(
        "CREATE TEMP TABLE stage_messages (old_id TEXT, new_id TEXT,"
        " canonical_session_id TEXT, source TEXT, source_message_ref TEXT,"
        " ordinal INTEGER, role TEXT, content_length INTEGER, timestamp TEXT,"
        " model TEXT, is_system INTEGER, is_sidechain INTEGER, content_hash TEXT,"
        " evidence_scope TEXT)")
    con.executemany(
        "INSERT INTO stage_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r["_old_id"], r["canonical_message_id"], r["canonical_session_id"],
          r["source"], r["source_message_ref"], r["ordinal"], r["role"],
          r["content_length"], r["timestamp"], r["model"], r["is_system"],
          r["is_sidechain"], r["content_hash"], r["evidence_scope"])
         for r in plan.messages])

    con.execute("CREATE TEMP TABLE stage_tools ("
                + "old_id TEXT, " + ",".join(TOOL_COLUMNS) + ")")
    con.executemany(
        f"INSERT INTO stage_tools VALUES ({','.join(['?'] * (len(TOOL_COLUMNS) + 1))})",
        [(r["_old_id"],) + tuple(r[c] for c in TOOL_COLUMNS)
         for r in plan.tools])

    con.execute("CREATE TEMP TABLE stage_links ("
                + ",".join(LINK_COLUMNS) + ")")
    con.executemany(
        f"INSERT INTO stage_links VALUES ({','.join('?' * len(LINK_COLUMNS))})",
        [tuple(r[c] for c in LINK_COLUMNS) for r in plan.links])

    con.execute("CREATE TEMP TABLE stage_relations (relation_id TEXT PRIMARY KEY,"
                " parent_canonical_id TEXT, child_canonical_id TEXT,"
                " relationship_type TEXT)")
    con.executemany(
        "INSERT OR IGNORE INTO stage_relations VALUES (?,?,?,?)",
        [(r["relation_id"], r["parent_canonical_id"], r["child_canonical_id"],
          r["relationship_type"]) for r in plan.relations])

    con.execute("CREATE TEMP TABLE stage_map (table_name TEXT, old_id TEXT,"
                " new_id TEXT)")
    con.executemany("INSERT INTO stage_map VALUES (?,?,?)", plan.id_map)


def _write_bookkeeping(con: sqlite3.Connection, plan: MigrationPlan) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS id_migration_map ("
                " table_name TEXT NOT NULL, old_id TEXT NOT NULL,"
                " new_id TEXT NOT NULL, PRIMARY KEY (table_name, old_id))")
    con.execute("DELETE FROM id_migration_map")
    con.execute("INSERT INTO id_migration_map SELECT * FROM stage_map")
    con.execute("CREATE TABLE IF NOT EXISTS id_address_policy ("
                " family TEXT NOT NULL, entity TEXT NOT NULL,"
                " address_form TEXT NOT NULL, rows INTEGER NOT NULL,"
                " PRIMARY KEY (family, entity, address_form))")
    con.execute("DELETE FROM id_address_policy")
    con.executemany("INSERT INTO id_address_policy VALUES (?,?,?,?)", plan.policy)
    con.execute("CREATE TABLE IF NOT EXISTS canonical_session_origins ("
                " canonical_session_id TEXT NOT NULL, origin_kind TEXT NOT NULL,"
                " origin_ref TEXT, PRIMARY KEY (canonical_session_id, origin_kind,"
                " origin_ref))")
    con.execute("DELETE FROM canonical_session_origins")
    con.executemany("INSERT OR IGNORE INTO canonical_session_origins VALUES (?,?,?)",
                    plan.origins)
    for name in ("stage_sessions", "stage_messages", "stage_tools",
                 "stage_links", "stage_relations", "stage_map"):
        con.execute(f"DROP TABLE IF EXISTS {name}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _connect(db_path: Path, writable: bool) -> sqlite3.Connection:
    if writable:
        con = sqlite3.connect(db_path)
    else:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, type=Path,
                        help="authority DB (agent_conversations.sqlite)")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan + verify only; never writes")
    parser.add_argument("--apply", action="store_true",
                        help="verify, back up, then swap the canonical tables")
    parser.add_argument("--backup", type=Path, default=None,
                        help="backup path used by --apply")
    parser.add_argument("--report", type=Path, default=None,
                        help="write the plan report as JSON")
    args = parser.parse_args(argv)
    if args.dry_run == args.apply:
        parser.error("choose exactly one of --dry-run / --apply")

    con = _connect(args.db, writable=False)
    plan = plan_migration(con)
    _assign_ordinals(plan)
    problems = verify_plan(con, plan)
    con.close()

    report = plan.report()
    report["verify_problems"] = problems
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    print(json.dumps(report["counts"], ensure_ascii=False))
    print(json.dumps(report["stats"], ensure_ascii=False, sort_keys=True))
    if problems:
        print(f"VERIFY FAILED: {len(problems)} problem(s)", file=sys.stderr)
        for p in problems[:20]:
            print(f"  - {p}", file=sys.stderr)
        return 2
    print("verify: OK")

    if args.apply:
        backup = args.backup or args.db.with_suffix(".sqlite.bak-uniform-ids")
        backup_authority_db(args.db, backup)
        print(f"backup: {backup}")
        con = _connect(args.db, writable=True)
        try:
            apply_plan(con, plan)
        finally:
            con.close()
        con = _connect(args.db, writable=False)
        after = {
            "sessions": con.execute(
                "SELECT count(*) FROM canonical_sessions").fetchone()[0],
            "messages": con.execute(
                "SELECT count(*) FROM canonical_messages").fetchone()[0],
            "tools": con.execute(
                "SELECT count(*) FROM canonical_tool_events").fetchone()[0],
        }
        con.close()
        print(f"applied: {json.dumps(after, ensure_ascii=False)}")
        for key, expected in (("sessions", len(plan.sessions)),
                              ("messages", len(plan.messages)),
                              ("tools", len(plan.tools))):
            if after[key] != expected:
                print(f"POST-CHECK FAILED: {key} {after[key]} != {expected}",
                      file=sys.stderr)
                return 3
        print("post-check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
