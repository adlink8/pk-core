"""MCP tool schema 定义与 profile 过滤。

从 services/mcp_server.py 拆出：所有工具名常量 + ALL_TOOLS 内联 JSON Schema
（原文件第 122-694 行）。对外契约与拆分前完全一致，仅移动位置。

对外符号：
    CORE_TOOL_NAMES / FULL_ONLY_TOOL_NAMES / ALL_TOOLS
    _mcp_profile / active_tools / TOOLS
"""

from __future__ import annotations

import os

import mcp.types as types  # noqa: E402

# core: KU-first 默认面；full: 含历史兼容别名与时间线
CORE_TOOL_NAMES = frozenset({
    "search_semantic",
    "search_semantic_cards",
    "stats",
    "knowledge_status",
    "list_google_assertions",
    "get_google_assertion",
    "get_memory_profile",
    "get_memory_by_subject",
    "data_list_events",
    "data_list_memories",
    "data_list_relations",
    "data_aggregate",
    "data_export",
    "data_get_event_by_id",
    "data_get_memory_by_id",
    "data_quality_report",
    "personal_state_current",
    "personal_state_history",
    "personal_changes_recent",
    "personal_state_explain",
    "decision_recommendations_list",
    "decision_recommendations_get",
    "decision_recommendation_history",
    "decision_recommendation_outcomes",
    "decision_recommendation_effectiveness",
    "proactive_inbox",
    "proactive_digest",
    "proactive_candidate_get",
    "proactive_candidate_explain",
    "proactive_controls_status",
    "proactive_metrics",
    "external_context_list",
    "external_context_get",
    "external_context_explain",
    "decision_analysis_list",
    "decision_analysis_get",
    "decision_analysis_explain",
    "project_pilot_list",
    "project_pilot_get",
    "project_pilot_explain",
    "recommendation_calibration_list",
    "recommendation_calibration_get",
    "recommendation_calibration_explain",
    "agent_session_prepare",
    "agent_session_confirm",
    "agent_session_preview",
    "agent_session_generate",
    "agent_session_publish",
    "agent_session_decide",
    "agent_session_preregister",
    "agent_session_action_start",
    "agent_session_action_complete",
    "agent_session_observe",
    "agent_session_calibrate",
    "agent_session_resume",
    "agent_session_explain",
})

FULL_ONLY_TOOL_NAMES = frozenset({
    "query_events",
    "get_event_detail",
    "list_categories",
    "data_timeline",
})

ALL_TOOLS = [
    types.Tool(
        name="search_semantic",
        description=(
            "语义检索(wiki-first + layered fallback)。"
            "先查主题 Wiki 页与会话卡,再查 active 知识单元,再回落对话与 raw 事件。"
            "适合'我大概记得做过类似的事'这类模糊查询。"
            "返回 route、versions 与结果列表;结果可能是 wiki_page、semantic_card、knowledge_unit 或 event。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "自然语言查询,如 'PPT 排版怎么做'、'上次怎么调试数据库的'",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回条数(默认 5)",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
                "source": {
                    "type": "string",
                    "description": "过滤 raw 事件数据源:Google / GPT / Agent。不传则全源(不影响知识层)",
                    "enum": ["Google", "GPT", "Agent"],
                },
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="search_semantic_cards",
        description=(
            "语义检索 MVP 会话卡(var/db/semantic_mvp_v3.sqlite 只读,不写库)。"
            "对 173 张会话卡与其 active 事实做关键词打分检索(purpose/summary_md/fact,"
            "ASCII 标识符或中文 2-gram),返回 session_id、purpose、score 与命中事实。"
            "与 search_semantic(知识单元/原始事件)互补,适合'哪次会话谈过 X'这类定位。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "关键词查询,如 'AI-Memory'、'Dockerfile 代理'",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回条数(默认 8)",
                    "default": 8,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="query_events",
        description=(
            "精确查询事件(按结构化条件过滤 sqlite)。"
            "适合'列出 2025 年 3 月所有 Agent 事件'这类结构化过滤。"
            "所有参数都是可选的 AND 过滤。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "数据源:Google / GPT / Agent",
                    "enum": ["Google", "GPT", "Agent"],
                },
                "month": {
                    "type": "string",
                    "description": "月份前缀,如 '2025-03' 或 '2025'",
                },
                "category": {
                    "type": "string",
                    "description": "category_v2 子串匹配,如 '编程'、'调试'",
                },
                "keyword": {
                    "type": "string",
                    "description": "title + content_rich 子串匹配",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回条数(默认 50,上限 200)",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
        },
    ),
    types.Tool(
        name="get_event_detail",
        description=(
            "按 event_id 取单条事件全字段(含增强内容 content_rich)。"
            "用于'点开看详情'。通常先用 search_semantic / query_events 拿到 event_id,"
            "再用本工具读完整内容。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "事件 ID(search_semantic / query_events 返回的 event_id 字段)",
                },
            },
            "required": ["event_id"],
        },
    ),
    types.Tool(
        name="stats",
        description=(
            "数据库 + 向量库 + 知识索引的统计概览。建议 AI 在回答前先调一次,"
            "了解数据总量、按源分布、向量库可用性与 active 知识 collection,建立全局认知。"
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="knowledge_status",
        description=(
            "知识单元索引只读状态: active collection 名、unit_count、canonical_current、"
            "route_policy、以及 CLI/REST/MCP 语义入口对应关系。"
            "对齐 GET /knowledge。不执行 promote/rollback。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "probe_chroma": {
                    "type": "boolean",
                    "description": "是否探测 Chroma 实际条数(默认 true)",
                    "default": True,
                },
            },
        },
    ),
    types.Tool(
        name="list_google_assertions",
        description=(
            "只读列出 Google 轻量聚合断言(interest_topic/service/channel/domain)。"
            "这是 aggregate_ok 信号，不是 dialogue knowledge unit，不是 personal_fact。"
            "对齐 GET /google/assertions。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "assertion_type": {
                    "type": "string",
                    "description": "可选过滤: interest_topic|frequent_service|frequent_channel|domain_affinity",
                },
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
            },
        },
    ),
    types.Tool(
        name="get_google_assertion",
        description=(
            "只读获取单条 Google 轻量断言。not_knowledge_unit=true；"
            "evidence 为 g| 前缀事件引用。对齐 GET /google/assertions/<id>。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "assertion_id": {
                    "type": "string",
                    "description": "断言 ID（gla|...）",
                },
            },
            "required": ["assertion_id"],
        },
    ),
    types.Tool(
        name="list_categories",
        description=(
            "列出所有 category_v2 分类及其事件数(降序)。"
            "帮 AI 知道有哪些分类维度可用于 query_events 的 category 过滤。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "可选,只看某个数据源的分类",
                    "enum": ["Google", "GPT", "Agent"],
                },
            },
        },
    ),
    types.Tool(
        name="get_memory_profile",
        description=(
            "获取用户长期记忆概览。"
            "适合在回答前了解用户的工具偏好、能力、项目、事实、习惯和内容偏好。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_type": {
                    "type": "string",
                    "description": "可选过滤: tooling / preference / capability / fact / project / habit",
                    "enum": ["tooling", "preference", "capability", "fact", "project", "habit"],
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条明细(默认 50)",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
        },
    ),
    types.Tool(
        name="get_memory_by_subject",
        description=(
            "按主体查询长期记忆详情和图谱关系,如 Codex、Python、GSD项目管理。"
            "可选返回 N 跳邻居,用于理解相关工具/能力/项目之间的关系。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "记忆主体,如 Codex",
                },
                "neighbors": {
                    "type": "integer",
                    "description": "可选:返回 N 跳邻居(0=不返回,默认 0)",
                    "default": 0,
                    "minimum": 0,
                    "maximum": 4,
                },
            },
            "required": ["subject"],
        },
    ),
    types.Tool(
        name="data_list_events",
        description="Data.list_events: 对齐 GET /data/events,分页浏览事件,支持来源/服务/分类/时间/关键词/字段/order。",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
                "source": {"type": "string", "enum": ["Google", "GPT", "Agent"]},
                "service": {"type": "string"},
                "category": {"type": "string"},
                "category_v2": {"type": "string", "description": "category 的 REST 别名"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
                "keyword": {"type": "string"},
                "fields": {"type": "string", "description": "逗号分隔字段;默认不返回 content/content_rich"},
                "order": {"type": "string", "enum": ["desc", "asc"], "default": "desc"},
            },
        },
    ),
    types.Tool(
        name="data_list_memories",
        description="Data.list_memories: 对齐 GET /data/memories,分页浏览长期记忆。",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
                "memory_type": {"type": "string"},
                "memory_subtype": {"type": "string"},
                "subject_like": {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="data_list_relations",
        description="Data.list_relations: 对齐 GET /data/relations,分页浏览规则关系;传 status 时浏览 LLM judgment 关系。",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
                "relation_type": {"type": "string"},
                "subject": {"type": "string"},
                "from_memory_id": {"type": "string"},
                "to_memory_id": {"type": "string"},
                "status": {"type": "string", "enum": ["review", "accepted", "rejected", "all"], "default": "all"},
            },
        },
    ),
    types.Tool(
        name="data_aggregate",
        description="Data.aggregate: 对齐 GET /data/aggregate,按 month/source/service/category/memory_type/relation_type 计数。",
        inputSchema={
            "type": "object",
            "properties": {
                "group_by": {
                    "type": "string",
                    "default": "source",
                    "description": "Single group field. Kept for backward compatibility.",
                },
                "group_by_fields": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["month", "source", "service", "category", "memory_type", "relation_type"],
                    },
                    "description": "Preferred multi-field grouping, for example ['source', 'service']. Overrides group_by when provided.",
                },
                "metric": {"type": "string", "enum": ["count"], "default": "count"},
                "source": {"type": "string", "enum": ["Google", "GPT", "Agent"]},
                "service": {"type": "string"},
                "category": {"type": "string"},
                "category_v2": {"type": "string"},
                "keyword": {"type": "string"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
                "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
            },
        },
    ),
    types.Tool(
        name="data_timeline",
        description="Data.timeline: 对齐 GET /data/timeline,按 day/month/year 返回时间线;subject 会映射为关键词过滤。",
        inputSchema={
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "bucket": {"type": "string", "enum": ["day", "month", "year"], "default": "month"},
                "source": {"type": "string", "enum": ["Google", "GPT", "Agent"]},
                "service": {"type": "string"},
                "category": {"type": "string"},
                "category_v2": {"type": "string"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
                "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
            },
        },
    ),
    types.Tool(
        name="data_export",
        description=(
            "Data.export: 对齐 GET /data/export，有界导出事件为 JSONL/CSV/JSON。"
            "可选 query/keyword 过滤（合并原 data_export_all + data_export_query）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "可选关键词；映射到 keyword/export query"},
                "format": {"type": "string", "enum": ["jsonl", "csv", "json"], "default": "jsonl"},
                "limit": {"type": "integer", "default": 500, "minimum": 1, "maximum": 5000},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
                "source": {"type": "string", "enum": ["Google", "GPT", "Agent"]},
                "service": {"type": "string"},
                "category": {"type": "string"},
                "category_v2": {"type": "string"},
                "keyword": {"type": "string"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
                "fields": {"type": "string"},
                "order": {"type": "string", "enum": ["desc", "asc"], "default": "desc"},
            },
        },
    ),
    types.Tool(
        name="data_get_event_by_id",
        description="Data.get_event_by_id: 对齐 GET /data/event/<event_id>,按精确 event_id 读取事件。",
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "fields": {"type": "string"},
            },
            "required": ["event_id"],
        },
    ),
    types.Tool(
        name="data_get_memory_by_id",
        description="Data.get_memory_by_id: 对齐 GET /data/memory/<memory_id>,按精确 memory_id 读取长期记忆。",
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
            },
            "required": ["memory_id"],
        },
    ),
    types.Tool(
        name="data_quality_report",
        description="Data.data_quality_report: 对齐 GET /data/quality,返回重复、缺失字段、断链、LLM judgment 状态等只读质量报告。",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="personal_state_current",
        description="读取一个快照/运行绑定的当前目标、约束和观察；默认仅元数据。",
        inputSchema={
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string"},
                "run_id": {"type": "string"},
                "as_of": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 100},
            },
        },
    ),
    types.Tool(
        name="personal_state_history",
        description="读取一个快照内的个人状态形成历史；仅返回引用、checksum 和不确定性。",
        inputSchema={
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string"},
                "run_id": {"type": "string"},
                "as_of": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 100},
            },
        },
    ),
    types.Tool(
        name="personal_changes_recent",
        description="读取有界时间窗内的个人状态变化；不产生建议或动作。",
        inputSchema={
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string"},
                "run_id": {"type": "string"},
                "as_of": {"type": "string"},
                "window_start": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 100},
            },
        },
    ),
    types.Tool(
        name="personal_state_explain",
        description="解释一个明确状态键的形成路径和证据状态；不返回私有正文。",
        inputSchema={
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string"},
                "run_id": {"type": "string"},
                "as_of": {"type": "string"},
                "assertion_kind": {"type": "string", "enum": ["goal", "constraint", "observation", "state"]},
                "subject": {"type": "string"},
                "domain": {"type": "string"},
                "scope": {"type": "string"},
                "predicate": {"type": "string"},
            },
            "required": ["assertion_kind", "subject", "domain", "scope", "predicate"],
        },
    ),
    types.Tool(
        name="decision_recommendations_list",
        description="读取有界推荐元数据列表；不确认、不执行、不写入。",
        inputSchema={"type": "object", "properties": {
            "domain": {"type": "string"},
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 100},
        }},
    ),
    types.Tool(
        name="decision_recommendations_get",
        description="读取一条checksum验证后的推荐元数据。",
        inputSchema={"type": "object", "properties": {
            "recommendation_id": {"type": "string"},
        }, "required": ["recommendation_id"]},
    ),
    types.Tool(
        name="decision_recommendation_history",
        description="读取genesis-rooted决策历史；仅元数据。",
        inputSchema={"type": "object", "properties": {
            "recommendation_id": {"type": "string"},
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 100},
        }, "required": ["recommendation_id"]},
    ),
    types.Tool(
        name="decision_recommendation_outcomes",
        description="读取非因果结果观察元数据。",
        inputSchema={"type": "object", "properties": {
            "recommendation_id": {"type": "string"},
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 100},
        }, "required": ["recommendation_id"]},
    ),
    types.Tool(
        name="decision_recommendation_effectiveness",
        description="读取观测性效果评估元数据；causal_claim恒为false。",
        inputSchema={"type": "object", "properties": {
            "recommendation_id": {"type": "string"},
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 100},
        }, "required": ["recommendation_id"]},
    ),
    types.Tool(name="proactive_inbox", description="读取校验后的主动情报收件箱；无发送或写入。", inputSchema={"type":"object","properties":{"domain":{"type":"string"},"limit":{"type":"integer","default":50,"minimum":1,"maximum":100}}}),
    types.Tool(name="proactive_digest", description="读取元数据摘要提案；无调度或通知。", inputSchema={"type":"object","properties":{"domain":{"type":"string"},"limit":{"type":"integer","default":50,"minimum":1,"maximum":100}}}),
    types.Tool(name="proactive_candidate_get", description="读取单个候选元数据。", inputSchema={"type":"object","properties":{"candidate_id":{"type":"string"}},"required":["candidate_id"]}),
    types.Tool(name="proactive_candidate_explain", description="解释候选的理由、不确定性和校验链。", inputSchema={"type":"object","properties":{"candidate_id":{"type":"string"}},"required":["candidate_id"]}),
    types.Tool(name="proactive_controls_status", description="读取用户控制投影及不可变历史。", inputSchema={"type":"object","properties":{"candidate_id":{"type":"string"},"as_of":{"type":"string"}},"required":["candidate_id"]}),
    types.Tool(name="proactive_metrics", description="读取主动情报可观测性元数据；外部动作恒为零。", inputSchema={"type":"object","properties":{}}),
    types.Tool(name="external_context_list", description="列出校验后的外部来源、active snapshot 与受控事实；只读。", inputSchema={"type":"object","properties":{"limit":{"type":"integer","default":50,"minimum":1,"maximum":100}}}),
    types.Tool(name="external_context_get", description="读取一个外部 source、fact 或 snapshot；只读。", inputSchema={"type":"object","properties":{"resource_type":{"type":"string","enum":["source","fact","snapshot"]},"resource_id":{"type":"string"}},"required":["resource_type"]}),
    types.Tool(name="external_context_explain", description="解释一个外部资源的 lineage、限制和下钻入口；只读。", inputSchema={"type":"object","properties":{"resource_type":{"type":"string","enum":["source","fact","snapshot"]},"resource_id":{"type":"string"}},"required":["resource_type"]}),
    types.Tool(name="decision_analysis_list", description="列出结构化决策分析 run 元数据；不返回 provider 正文。", inputSchema={"type":"object","properties":{"limit":{"type":"integer","default":50,"minimum":1,"maximum":100}}}),
    types.Tool(name="decision_analysis_get", description="读取一个校验后的决策分析 candidate、claims 与 evidence refs。", inputSchema={"type":"object","properties":{"run_id":{"type":"string"}},"required":["run_id"]}),
    types.Tool(name="decision_analysis_explain", description="解释一个决策分析 run 的证据、限制和非权威边界。", inputSchema={"type":"object","properties":{"run_id":{"type":"string"}},"required":["run_id"]}),
    types.Tool(name="project_pilot_list", description="列出低风险 project pilot cases；无外部动作。", inputSchema={"type":"object","properties":{"limit":{"type":"integer","default":50,"minimum":1,"maximum":100}}}),
    types.Tool(name="project_pilot_get", description="读取一个 pilot case、recommendation 与 protocol。", inputSchema={"type":"object","properties":{"case_id":{"type":"string"}},"required":["case_id"]}),
    types.Tool(name="project_pilot_explain", description="解释 pilot history、controls 和 outcome；系统动作恒为零。", inputSchema={"type":"object","properties":{"case_id":{"type":"string"},"as_of":{"type":"string"}},"required":["case_id"]}),
    types.Tool(name="recommendation_calibration_list", description="列出 calibration protocols；只读。", inputSchema={"type":"object","properties":{"limit":{"type":"integer","default":50,"minimum":1,"maximum":100}}}),
    types.Tool(name="recommendation_calibration_get", description="读取 calibration protocol、arms、measurements 与 verdict。", inputSchema={"type":"object","properties":{"protocol_id":{"type":"string"}},"required":["protocol_id"]}),
    types.Tool(name="recommendation_calibration_explain", description="解释 calibration 限制；保持 causal_claim=false 且不可自动 promotion。", inputSchema={"type":"object","properties":{"protocol_id":{"type":"string"}},"required":["protocol_id"]}),
    types.Tool(name="agent_session_prepare", description="准备一个低风险 project 决策会话预览；此步骤不写入。", inputSchema={"type":"object","additionalProperties":False,"properties":{"goal":{"type":"string"},"constraints":{"type":"array","items":{"type":"string"}},"weights":{"type":"object","additionalProperties":{"type":"number"}},"actor_identity_hash":{"type":"string"},"domain":{"type":"string","enum":["project"]},"risk_budget":{"type":"string","enum":["low"]},"region":{"type":"string"},"max_external_age_seconds":{"type":"integer"},"now":{"type":"string"}},"required":["goal","constraints","weights","actor_identity_hash"]}),
    types.Tool(name="agent_session_confirm", description="用户明确确认绑定预览后，由服务内部签发短期令牌并完成本地不可变写入。", inputSchema={"type":"object","additionalProperties":False,"properties":{"preview":{"type":"object"},"confirmed":{"type":"boolean","const":True},"idempotency_key":{"type":"string"},"now":{"type":"string"}},"required":["preview","confirmed","idempotency_key","now"]}),
    types.Tool(name="agent_session_preview", description="预览会话下一次状态转换；此步骤不写入。", inputSchema={"type":"object","additionalProperties":False,"properties":{"session_id":{"type":"string"},"transition":{"type":"string","enum":["generate","publish","decide","preregister","action_start","action_complete","observe","calibrate"]},"payload":{"type":"object"},"actor_identity_hash":{"type":"string"},"expected_sequence":{"type":"integer"},"now":{"type":"string"}},"required":["session_id","transition","payload","actor_identity_hash","expected_sequence"]}),
    *[
        types.Tool(name=f"agent_session_{operation}", description=f"用户明确确认后执行 {operation} 本地受控转换；令牌由服务内部签发，幂等且不执行开放世界动作。", inputSchema={"type":"object","additionalProperties":False,"properties":{"preview":{"type":"object"},"confirmed":{"type":"boolean","const":True},"idempotency_key":{"type":"string"},"now":{"type":"string"}},"required":["preview","confirmed","idempotency_key","now"]})
        for operation in ("generate", "publish", "decide", "preregister", "action_start", "action_complete", "observe", "calibrate")
    ],
    types.Tool(name="agent_session_resume", description="恢复并校验一个会话的当前状态。", inputSchema={"type":"object","additionalProperties":False,"properties":{"session_id":{"type":"string"},"now":{"type":"string"}},"required":["session_id"]}),
    types.Tool(name="agent_session_explain", description="解释会话链、下一步和安全限制。", inputSchema={"type":"object","additionalProperties":False,"properties":{"session_id":{"type":"string"},"now":{"type":"string"}},"required":["session_id"]}),
]


def _mcp_profile() -> str:
    raw = (os.environ.get("PERSONAL_DATA_MCP_PROFILE") or "core").strip().lower()
    return raw if raw in {"core", "full"} else "core"


def active_tools() -> list[types.Tool]:
    """按 profile 过滤对外暴露的 tools。"""
    profile = _mcp_profile()
    if profile == "full":
        return list(ALL_TOOLS)
    return [t for t in ALL_TOOLS if t.name in CORE_TOOL_NAMES]


# 兼容旧测试与 import 名
TOOLS = active_tools()
