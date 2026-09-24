"""Where the live engine writes: the library, not a shadow store.

The engine used to inherit its target from ``--v2-db``, the Phase 62 shadow
database whose entire purpose was that v2 output never touched the authority.
That reason died with the manual-activation gate; what remained was an engine
filling a database nothing reads while the authority stayed frozen. These cases
pin the target to the authoritative store and keep the mirror where it belongs.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from personal_knowledge.application.conversation.watch import live_targets
from personal_knowledge.application.sync import build_parser
from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB

#: The Phase 62 shadow store. Spelled as a literal (not imported) so the
#: expectation cannot drift with the code under test.
SHADOW_DB = Path("data") / "staging" / "v2" / "agent_conversations_v2.sqlite"
#: The collected corpus — client-file snapshots plus the blob store beside them.
MIRROR_ROOT = Path("data") / "staging" / "v2" / "native"


def _live_args(argv: list[str]) -> argparse.Namespace:
    """Parse real CLI argv, so the flags' own defaults are what is exercised."""

    return build_parser().parse_args(["conversations", *argv])


def test_default_target_is_the_authoritative_store() -> None:
    mirror_root, db = live_targets(_live_args(["--live-status"]))

    assert db == AGENT_CONVERSATIONS_DB
    assert db != SHADOW_DB
    # Path shape, independent of the constant: the authority is a library db,
    # never anything under the staging tree.
    assert db.name == "agent_conversations.sqlite"
    assert "staging" not in db.parts
    assert mirror_root == MIRROR_ROOT


def test_explicit_db_wins() -> None:
    mirror_root, db = live_targets(
        _live_args(["--live-status", "--live-db", "tmp/other.sqlite"])
    )

    assert db == Path("tmp/other.sqlite")
    assert mirror_root == MIRROR_ROOT


def test_explicit_mirror_does_not_move_the_target() -> None:
    mirror_root, db = live_targets(
        _live_args(["--live-status", "--live-mirror", "tmp/mirror"])
    )

    assert mirror_root == Path("tmp/mirror")
    assert db == AGENT_CONVERSATIONS_DB
