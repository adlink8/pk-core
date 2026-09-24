"""证据派生置信度（PDA-41 deferred：替代 LLM 自报置信）。

背景：LLM 自报 confidence 95.2% ≥0.9（众数 0.95），无区分度；且检索
排序不消费 confidence（score=纯向量距离），它只在 CLI/MCP/UI 展示与
canonical min 聚合中出现——展示一个无信息量的高值是误导。

语义约定：confidence 只回答"这条 unit 的证据有多硬"，不回答"内容有多真"：
- 用户亲述（user 轨） > 助手声称（assistant 轨）/ 会话窗口（L2）
- 多证据互证（≥2 条 evidence 行，如 QA 联立 question-side ref） > 单证据
- 长 verbatim quote（≥20 字符） > 短 quote
- assistant 轨 D-03 信号：用户采纳 +0.05 / 用户纠正 -0.2（非硬 gate）

取值 0.4–0.95，永不满置信。确定性派生——同证据形态必得同值。
"""
from __future__ import annotations

BASE = 0.4
CAP = 0.95


def derive_confidence(
    *,
    evidence_count: int,
    evidence_scope: str,
    evidence_quote: str,
    confirmation_signal: str = "none",
) -> float:
    conf = BASE
    if evidence_count >= 1:
        conf += 0.2
    if evidence_count >= 2:
        conf += 0.15
    if evidence_scope == "user":
        conf += 0.15
    if len((evidence_quote or "").strip()) >= 20:
        conf += 0.1
    # D-03（仅 assistant 轨）：采纳 +0.05，纠正 -0.2
    if confirmation_signal == "adopted":
        conf += 0.05
    elif confirmation_signal == "corrected":
        conf -= 0.2
    return round(max(0.0, min(CAP, conf)), 2)
