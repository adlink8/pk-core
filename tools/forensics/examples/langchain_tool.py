"""示例 2:把个人数据接进 LangChain(作为 Agent tool)。

适用:已经在用 LangChain / LangGraph 的项目,想给 Agent 加一个"翻用户历史"的能力。

思路:用 @tool 装饰器把 unified_search 包成 LangChain Tool,
      然后挂进 create_react_agent。模型走 ReAct 循环,自己决定何时检索。

运行前:
    pip install langchain langchain-openai langgraph
    set OPENAI_API_KEY=sk-...

运行:
    python examples/langchain_tool.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS = _THIS_DIR.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import personal_knowledge.retrieval.unified_search as us  # noqa: E402

try:
    from langchain_core.tools import tool  # noqa: E402
    from langchain_openai import ChatOpenAI  # noqa: E402
    from langgraph.prebuilt import create_react_agent  # noqa: E402
except ImportError:
    sys.exit("请先安装: pip install langchain langchain-openai langgraph")


# === 把检索能力包成 LangChain Tool =====================================
# docstring 是给模型看的 —— 写清楚"什么时候用",模型才会正确路由。

@tool
def search_semantic(query: str, top_k: int = 5, source: str = "") -> list[dict]:
    """语义检索用户的历史事件(模糊召回)。当用户说"我之前好像做过X"时用。
    query: 自然语言; source: 可选 Google/GPT/Agent; 返回按相关度排序的事件。"""
    return us.search_semantic(query, top_k=top_k, source=source or None)


@tool
def query_events(source: str = "", month: str = "", category: str = "",
                 keyword: str = "", limit: int = 20) -> list[dict]:
    """按结构化条件精确过滤用户事件。当用户说"列出某月某分类的事件"时用。
    所有参数可选,空字符串表示不过滤。"""
    return us.query_events(
        source=source or None, month=month or None,
        category=category or None, keyword=keyword or None, limit=limit,
    )


def build_agent(model: str = "gpt-4o-mini"):
    """构建一个带个人数据检索能力的 ReAct Agent。"""
    llm = ChatOpenAI(model=model)
    return create_react_agent(llm, [search_semantic, query_events])


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or "我最近做过哪些和数据库调试有关的事?"
    agent = build_agent()
    print(f"用户: {question}\n")
    result = agent.invoke({"messages": [("user", question)]})
    # 最后一条 message 是最终回答
    print(f"\nAI: {result['messages'][-1].content}")
