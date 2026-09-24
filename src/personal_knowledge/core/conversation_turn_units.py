"""Canonical conversation-turn vector unit extraction (OC-10).

``load_turn_units`` was relocated from
``application/conversation/build_conversation_vector_store.py`` so the
``evaluation`` layer can derive expected conversation_turns counts without
importing into ``application``.  The application vector-build module re-imports
it and re-exports for backwards compatibility.
"""
from __future__ import annotations

import json
from pathlib import Path

from personal_knowledge.core.project_paths import AI_CONTEXT_DIR, ROOT

SUMMARIES_JSON = AI_CONTEXT_DIR / "conversation_summaries.json"
MIN_NARRATIVE_LEN = 20  # turn 叙述最短长度,短于此跳过(无语义价值)


def load_turn_units() -> tuple[list[dict], int]:
    """从 conversation_summaries.json 抽取 turn 叙述作为向量单元。

    每个 turn 一个单元(含因果链),元数据带 session_id/turn_id/main_topic。
    返回 (units, skipped_short)。每条 unit:
      - id: "{session_id}#{turn_id or turn_no}"(幂等去重键)
      - text: turn narrative(用于 embedding)
      - metadata: session_id/turn_id/turn_no/main_topic/source/tools
    """
    if not SUMMARIES_JSON.exists():
        print(f"[error] 缺少 summary 产物: {SUMMARIES_JSON.relative_to(ROOT)}")
        print("        先运行: python -m personal_knowledge.application.conversation.summary --write")
        return [], 0

    data = json.loads(SUMMARIES_JSON.read_text(encoding="utf-8"))
    units: list[dict] = []
    skipped_short = 0

    for session in data:
        session_id = session["session_id"]
        main_topic = session.get("main_topic", "")
        source = session.get("meta", {}).get("source", "Agent")
        for turn_no, turn in enumerate(session.get("turn_summaries", []), 1):
            narrative = (turn.get("narrative") or "").strip()
            if len(narrative) < MIN_NARRATIVE_LEN:
                skipped_short += 1
                continue
            turn_id = turn.get("turn_id")
            # 幂等去重键:session_id + turn_id(无 turn_id 用 turn_no)
            unit_id = f"{session_id}#{turn_id or f't{turn_no}'}"
            # 拼接 main_topic + narrative(topic 提供检索锚点)
            text = f"{main_topic}。{narrative}" if main_topic else narrative
            units.append({
                "id": unit_id,
                "text": text,
                "metadata": {
                    "session_id": session_id,
                    "turn_id": turn_id or "",
                    "turn_no": turn_no,
                    "main_topic": main_topic[:100],  # chroma metadata 值有长度限制
                    "source": source,
                    "event_type": "conversation_turn",
                    "tools_used": ",".join(turn.get("tools_used", []))[:200],
                },
            })

    return units, skipped_short


__all__ = ["MIN_NARRATIVE_LEN", "SUMMARIES_JSON", "load_turn_units"]
