"""示例 3:把个人数据注入 RAG 流程(无 Agent,纯上下文增强)。

适用:Dify / FastGPT / Coze / 自建 RAG 等"用户提问 → 检索 → 喂给 LLM"的流程。
      与示例 1/2 的区别:这里不做 function-calling,而是在提问时"静默"检索相关历史,
      把结果拼进 prompt。简单、可控、任何 LLM 都能用。

两种 RAG 增强策略,可叠加:

A. 长期画像(静态注入)
   把 person_profile.md 作为 system prompt 的一部分,每轮对话都带。
   ——让 AI"知道你是谁",适合做长期记忆/个性化。

B. 相关事件(动态检索)
   用用户当前问题做语义检索,把 top-K 相关历史事件拼进 user prompt。
   ——让 AI"记得你做过什么",适合"我之前怎么处理 X 来着"这类回忆型问题。

本示例用本地 HTTP API(api_server.py)取数据,这样 RAG 平台不用装 Python 依赖,
任何能发 HTTP 的环境(包括 Dify 的"自定义工具")都能跑。也可直接 import unified_search。

运行前:先启动 API:python integration/scripts/api_server.py
运行:  python examples/rag_inject.py "上次怎么调试 Docker 的"
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

API_BASE = os.environ.get("PERSONAL_API", "http://127.0.0.1:8000")


def _http_get(path: str) -> dict:
    with urllib.request.urlopen(f"{API_BASE}{path}", timeout=60) as r:
        return json.loads(r.read())


def _http_post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# === 策略 A:长期画像(静态) ============================================
def get_profile_text() -> str:
    """取 AI 长期上下文文档。注入 system prompt 用。"""
    data = _http_get("/profile")
    return data["data"]["profile"]


# === 策略 B:相关事件(动态检索) ========================================
def retrieve_context(query: str, top_k: int = 5, source: str | None = None) -> str:
    """用当前问题检索历史,拼成上下文文本。注入 user prompt 用。"""
    body = {"query": query, "top_k": top_k}
    if source:
        body["source"] = source
    data = _http_post("/search/semantic", body)
    events = data["data"]
    if not events:
        return "(无相关历史记录)"

    lines = [f"以下是该用户与「{query}」最相关的 {len(events)} 条历史记录:"]
    for i, e in enumerate(events, 1):
        lines.append(
            f"\n[{i}] {e.get('source','')} · {e.get('event_time','')[:10]} "
            f"· {e.get('category_v2','')}\n"
            f"{(e.get('content') or '')[:500]}"
        )
    return "\n".join(lines)


# === 组装一个增强 prompt(给任意 LLM 用)================================
def build_enhanced_prompt(user_question: str, top_k: int = 5) -> list[dict]:
    """返回 messages 列表,已注入长期画像 + 相关事件。可直接喂 openai/任意 SDK。"""
    profile = get_profile_text()
    context = retrieve_context(user_question, top_k=top_k)
    return [
        {
            "role": "system",
            "content": (
                "你是个人的 AI 助手。下面是该用户的长期画像,回答时结合:\n\n"
                f"{profile}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"参考我下面的相关历史记录,回答我的问题。\n\n"
                f"【相关历史】\n{context}\n\n"
                f"【我的问题】{user_question}"
            ),
        },
    ]


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or "我之前怎么处理 Docker 调试问题的?"
    print(f"用户: {question}\n")

    msgs = build_enhanced_prompt(question, top_k=3)
    print("=== 注入 system 的长期画像(节选)===")
    print(msgs[0]["content"][:300] + "...\n")
    print("=== 注入 user 的相关历史(节选)===")
    print(msgs[1]["content"][:600] + "...\n")
    print("(把上面 msgs 直接喂给任意 LLM 即可得到个性化回答)")

    # 如装了 openai,可直接跑:
    try:
        from openai import OpenAI
        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o-mini", messages=msgs  # type: ignore[arg-type]
        )
        print(f"\nAI: {resp.choices[0].message.content}")
    except Exception as e:
        print(f"\n(未自动调用 LLM: {e})")
