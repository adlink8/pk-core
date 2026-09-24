"""registry 是唯一的族级 seam：家族清单、别名、fail-closed、无泛化兜底。

生产代码只经 registry 消费适配器（``v2_sync`` / ``live_sync`` /
``live_native_shadow`` / ``native_inventory`` 全部只用 ``adapt_for`` /
``capability_for`` / ``detect_family`` / ``select_adapter`` / ``resolve_family``），
没有任何调用方直接摸家族模块。所以产品契约在这里，不在模块函数上。
"""

from __future__ import annotations

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.adapters.conversation_sources.contracts import (
    PROBE_CONTENT_HASH,
    SourceArtifact,
)
from tests.contract.conversation_sources.support import artifacts

# 全量家族键。新增客户端必须同时在这里登记并补一个模块测试文件，
# 否则这个断言先红 —— 这就是「新家族不许悄悄溜进 registry」的闸门。
FAMILIES = (
    "antigravity",
    "chatgpt",
    "claude",
    "codex",
    "copilot",
    "cursor",
    "gemini",
    "grok",
    "kimi",
    "kimi-work",
    "mimo",
    "opencode",
    "pi",
    "qoder",
    "workbuddy",
    "zcode",
)

# 别名 -> 属主家族。别名不另立契约，只是家族名的另一个入口。
ALIASES = {"vscode-copilot": "copilot"}

# 一个模块服务多个家族时，它们必须共享同一个 adapter_version：
# 版本号是模块级的，一次修复只升一个模块。
SHARED_VERSION_GROUPS = (
    ("claude", "qoder"),
    ("workbuddy", "kimi", "kimi-work"),
    ("mimo", "opencode"),
)


def test_known_families_is_exactly_the_registered_clients():
    assert registry.known_families() == tuple(sorted((*FAMILIES, *ALIASES)))
    assert len(FAMILIES) == 16


@pytest.mark.parametrize("alias,owner", sorted(ALIASES.items()))
def test_alias_resolves_to_its_owning_family(alias: str, owner: str) -> None:
    assert registry.resolve_family(alias) == owner
    # 别名拿到的 capability 是属主家的，不另起一份。
    assert registry.capability_for(alias) == registry.capability_for(owner)


def test_unknown_family_fails_closed() -> None:
    with pytest.raises(KeyError):
        registry.resolve_family("not-a-client")
    with pytest.raises(KeyError):
        registry.capability_for("not-a-client")


@pytest.mark.parametrize("family", FAMILIES)
def test_every_family_publishes_a_versioned_capability(family: str) -> None:
    capability = registry.capability_for(family)
    assert capability.family == family
    assert capability.adapter_version
    assert capability.contract_version
    # digest 是版本门的输入：同样的能力必须同样，改了版本必须变。
    assert capability.digest() == registry.capability_for(family).digest()


def test_every_family_speaks_one_contract_generation() -> None:
    generations = {
        registry.capability_for(family).contract_version for family in FAMILIES
    }
    assert len(generations) == 1, generations


@pytest.mark.parametrize("group", SHARED_VERSION_GROUPS, ids=lambda g: "+".join(g))
def test_families_sharing_a_module_share_one_adapter_version(group) -> None:
    """同一个适配器模块服务多家客户端：一次修复只升一个版本号。"""
    versions = {registry.capability_for(family).adapter_version for family in group}
    assert len(versions) == 1, f"{group} drifted: {versions}"


def test_select_adapter_has_no_generic_fallback(tmp_path) -> None:
    """读不懂的产物必须返回 None，不允许落到某个泛化解析器。"""
    junk = tmp_path / "junk.jsonl"
    junk.write_text('{"type": "totally-unknown-wire"}\n', encoding="utf-8")
    artifact = SourceArtifact(
        artifact_id="art-junk",
        family="",
        source_kind="file",
        content_hash=PROBE_CONTENT_HASH,
        capture_method="fixture",
        relative_path="junk.jsonl",
        byte_size=junk.stat().st_size,
    )
    assert registry.select_adapter(artifact, artifact_root=tmp_path) is None


def test_select_adapter_routes_a_real_wire_to_its_owning_family(tmp_path) -> None:
    """认得出来的产物路由给拥有它的家族，而不是第一个敢认它的解析器。"""
    rollout = artifacts.jsonl([{
        "timestamp": "2026-03-29T10:23:20.293Z",
        "ordinal": 0,
        "type": "session_meta",
        "payload": {
            "session_id": "s-route",
            "id": "s-route",
            "timestamp": "2026-03-29T10:22:48.025Z",
            "cwd": "C:\\合成",
            "originator": "codex_cli_rs",
        },
    }])
    artifact, root = artifacts.file_artifact(
        tmp_path, "codex.route", "rollout-route.jsonl", rollout, family="codex",
    )
    assert registry.select_adapter(artifact, artifact_root=root) == "codex"


def test_adaptive_entry_points_accept_the_same_signature(tmp_path) -> None:
    """``adapt_for`` / ``detect_family`` 是三个家族签名形态的统一入口。"""
    rollout = artifacts.jsonl([{
        "type": "session_meta",
        "payload": {"id": "s-shape"},
    }])
    artifact, root = artifacts.file_artifact(
        tmp_path, "codex.shape", "rollout-shape.jsonl", rollout, family="codex",
    )
    assert registry.detect_family("codex", artifact, artifact_root=root) is True
    result = registry.adapt_for("codex", artifacts.single(artifact), artifact_root=root)
    assert result.family == "codex"
