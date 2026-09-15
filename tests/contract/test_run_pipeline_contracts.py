"""P1: run_pipeline 步骤选择与 dry-run 解析契约。"""

from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import personal_knowledge.application.run_pipeline as rp  # noqa: E402


def _args(**kwargs) -> Namespace:
    base = dict(
        from_step=1,
        only_steps="",
        skip_steps="",
        dry_run=False,
        include_conversation_turns=False,
        agentsview=False,
        agentsview_write=False,
        agentsview_only=False,
        legacy_integrated=False,
    )
    base.update(kwargs)
    return Namespace(**base)


def test_steps_table_has_core_pipeline() -> None:
    nums = [s["num"] for s in rp.STEPS]
    assert nums[0] == 1
    assert 1 in nums and 12 in nums
    names = {s["name"] for s in rp.STEPS}
    assert "build_integrated_system" in names
    assert "build_vector_store" in names
    assert "enrich_unified_events" in names


def test_parse_step_list() -> None:
    assert rp.parse_step_list("") == set()
    assert rp.parse_step_list("3,4") == {3, 4}
    assert rp.parse_step_list(" 10 ") == {10}


def test_select_steps_default_excludes_13() -> None:
    selected = rp.select_steps(_args())
    nums = [s["num"] for s in selected]
    assert nums == [s["num"] for s in rp.STEPS if s["num"] != 13]
    assert 13 not in nums


def test_select_steps_from_and_skip() -> None:
    selected = rp.select_steps(_args(from_step=5, skip_steps="9,10"))
    nums = [s["num"] for s in selected]
    assert all(n >= 5 for n in nums)
    assert 9 not in nums and 10 not in nums


def test_select_steps_only() -> None:
    selected = rp.select_steps(_args(only_steps="3,4"))
    assert [s["num"] for s in selected] == [3, 4]


def test_select_steps_include_conversation_turns() -> None:
    selected = rp.select_steps(_args(include_conversation_turns=True))
    assert 13 in [s["num"] for s in selected]


def test_select_steps_only_13_without_flag() -> None:
    """--only 13 应能单独选中步骤 13。"""
    selected = rp.select_steps(_args(only_steps="13"))
    assert [s["num"] for s in selected] == [13]


def test_fmt_step_includes_name() -> None:
    step = rp.STEPS[0]
    text = rp.fmt_step(step)
    assert step["name"] in text
    assert str(step["num"]) in text


def test_dry_run_main_prints_without_executing(monkeypatch, capsys) -> None:
    called = {"run": 0}

    def boom(*_a, **_k):
        called["run"] += 1
        raise AssertionError("run_step should not be called in dry-run")

    monkeypatch.setattr(rp, "run_step", boom)
    monkeypatch.setattr(
        rp,
        "parse_args",
        # dry-run of retired steps still requires explicit --legacy-integrated
        lambda: _args(dry_run=True, only_steps="1,2", legacy_integrated=True),
    )
    rp.main()
    out = capsys.readouterr().out
    assert called["run"] == 0
    assert "build_integrated_system" in out or "步骤" in out or "1" in out
