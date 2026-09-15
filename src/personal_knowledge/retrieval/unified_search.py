"""统一检索层 facade — 重新导出拆分后的子模块公开 API。

原 3221 行的 unified_search.py 已按职责拆成:
  _constants.py        共享常量(含路径常量真相源)+ sys.path bootstrap
  _db_utils.py         跨 concern 的私有 DB helper
  semantic_search.py   hybrid 语义检索 + 知识索引状态
  google_assertions.py Google light 断言
  events_query.py      事件精确查询 + data-access 契约
  memory.py            记忆层(memory_items + relations + graph)
  merge_cluster.py     merge layer + 向量聚类

本文件保留为 facade,所有 backend.X 调用者(api_server / mcp_server / tools)
零改动。_cli() 和 __main__ 保留以支持 python -m ...unified_search 入口。

路径常量(UNIFIED_DB 等)通过 _constants 模块引用,测试 monkeypatch
_constants 模块即可全局重定向 DB。
"""
from __future__ import annotations

# 路径常量 re-export(读取用;patch 请改 _constants 模块)
from personal_knowledge.retrieval._constants import (  # noqa: F401
    ROOT, UNIFIED_DB, GOOGLE_DB, AGENT_CONVERSATIONS_DB, DB_DIR,
)
# 分页/字段/检索旋钮常量
from personal_knowledge.retrieval._constants import (  # noqa: F401
    DEFAULT_MEMORY_GRAPH_LIMIT, MAX_MEMORY_GRAPH_LIMIT,
    DEFAULT_RELATION_REVIEW_LIMIT, MAX_RELATION_REVIEW_LIMIT,
    RELATION_REVIEW_STATUSES, DEFAULT_DATA_LIMIT, MAX_DATA_LIMIT, MAX_EXPORT_LIMIT,
    DEFAULT_EVENT_FIELDS, EVENT_FIELD_SQL, AGGREGATE_GROUP_SQL,
    FALLBACK_POLICIES, DEFAULT_FALLBACK_POLICY,
)
# 公开 API
from personal_knowledge.retrieval.semantic_search import (  # noqa: F401
    search_semantic, search_knowledge_units, get_knowledge_status,
)
from personal_knowledge.retrieval.google_assertions import (  # noqa: F401
    get_google_structure_status, list_google_light_assertions, get_google_light_assertion,
)
from personal_knowledge.retrieval.events_query import (  # noqa: F401
    query_events, get_event_detail, stats, list_categories,
    list_events_contract, get_event_by_id_contract, export_events_contract,
    export_all_contract, export_query_contract, aggregate_contract, timeline_contract,
)
from personal_knowledge.retrieval.memory import (  # noqa: F401
    list_memories_contract, get_memory_by_id_contract, list_relations_contract,
    get_memory_profile, get_memory_relations, get_memory_by_subject, get_memory_neighbors,
    get_memory_graph_contract, get_memory_relation_review_contract, data_quality_report_contract,
)
from personal_knowledge.retrieval.merge_cluster import (  # noqa: F401
    merge_stats, cluster,
)
# 内部符号(tools 和测试直接 import/monkeypatch,重新导出以保持兼容):
from personal_knowledge.retrieval.semantic_search import (  # noqa: F401
    _search_dialogue_canonical_messages,
    _resolve_fallback_policy,
    _read_knowledge_active_collection,
    _semantic_search,
)


def _cli() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(
        description="统一检索层 CLI: 知识混合语义检索 + 精确查询 + 记忆/知识状态",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 语义检索(wiki-first + layered/legacy fallback；读 wiki/cards 再 active knowledge index)
  python unified_search.py semantic "PPT 排版怎么做" --top-k 3
  python unified_search.py semantic "数据库调试" --source Agent

  # 知识索引状态(active collection / unit_count)
  python unified_search.py knowledge
  python unified_search.py knowledge --json

  # 语义检索 + 去重(合并层折叠重复命中；仅影响 raw 侧展示)
  python unified_search.py semantic "PPT" --top-k 8 --dedup

  # 精确查询(结构化过滤)
  python unified_search.py query --source GPT --month 2025-03
  python unified_search.py query --category 编程 --keyword 报错 --limit 10
  python unified_search.py query --source Agent --dedup --limit 30

  # 单条详情
  python unified_search.py detail <event_id>

  # 统计概览(含 knowledge 块)
  python unified_search.py stats
  python unified_search.py merge-stats        # 合并层压缩报告

  # 长期记忆对象
  python unified_search.py memory
  python unified_search.py memory --type tooling
  python unified_search.py memory --subject Codex
  python unified_search.py memory --subject Codex --neighbors 2

  # 向量库聚类/去重(管道加工,即时计算,不依赖合并层)
  python unified_search.py cluster --source Agent --threshold 0.92
  python unified_search.py cluster --threshold 0.88 --min-cluster-size 3 --json

  # JSON 输出(便于其他程序消费)—— --json 跟在子命令后
  python unified_search.py semantic "PPT" --json
  python unified_search.py knowledge --json
  python unified_search.py stats --json
  python unified_search.py merge-stats --json
  python unified_search.py cluster --json --limit 500    # 调试用小样本
        """,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("semantic", help="语义检索(wiki-first + layered/legacy fallback)")
    ps.add_argument("query")
    ps.add_argument("--top-k", type=int, default=5)
    ps.add_argument("--source", default=None)
    ps.add_argument(
        "--fallback-policy",
        choices=["legacy", "layered"],
        default=None,
        help="Hybrid fallback: legacy=KU+personal_events; layered=KU→dialogue→Google. "
             "Default: env PERSONAL_DATA_FALLBACK_POLICY or layered",
    )
    ps.add_argument("--dedup", action="store_true",
                    help="按合并层折叠重复命中(L1/L2 同簇只留代表,附 merged_count)")
    ps.add_argument(
        "--current-only",
        action="store_true",
        help="仅返回当前值 unit（现行默认行为，flag 用于显式声明）",
    )
    ps.add_argument("--json", action="store_true", help="输出 JSON(默认人类可读)")

    pk = sub.add_parser("knowledge", help="知识索引状态(active pointer / unit_count)")
    pk.add_argument(
        "--no-chroma",
        action="store_true",
        help="不探测 Chroma，仅读 pointer + SQLite 版本行",
    )
    pk.add_argument("--json", action="store_true", help="输出 JSON(默认人类可读)")

    pq = sub.add_parser("query", help="精确查询(结构化过滤)")
    pq.add_argument("--source", default=None)
    pq.add_argument("--month", default=None, help="如 2025-03 或 2025")
    pq.add_argument("--category", default=None, help="category_v2 子串")
    pq.add_argument("--keyword", default=None, help="title+content 子串")
    pq.add_argument("--limit", type=int, default=20)
    pq.add_argument("--dedup", action="store_true",
                    help="按合并层折叠(L1/L2 同簇只留代表,附 merged_count)")
    pq.add_argument("--json", action="store_true", help="输出 JSON(默认人类可读)")

    pd = sub.add_parser("detail", help="单条事件详情")
    pd.add_argument("event_id")
    pd.add_argument("--json", action="store_true", help="输出 JSON(默认人类可读)")

    pst = sub.add_parser("stats", help="数据库+向量库+知识索引统计")
    pst.add_argument("--json", action="store_true", help="输出 JSON(默认人类可读)")

    pms = sub.add_parser("merge-stats", help="合并层压缩报告(L1/L2 去重情况)")
    pms.add_argument("--json", action="store_true", help="输出 JSON(默认人类可读)")

    pm = sub.add_parser("memory", help="长期记忆对象查询")
    pm.add_argument("--type", dest="memory_type", default=None,
                    help="过滤记忆类型: tooling/preference/capability/fact/project/habit")
    pm.add_argument("--subject", default=None, help="按主体查详情,如 Codex")
    pm.add_argument("--neighbors", type=int, default=0, help="同时返回 N 跳邻居(1-4)")
    pm.add_argument("--limit", type=int, default=50, help="概览模式返回上限")
    pm.add_argument("--json", action="store_true", help="输出 JSON(默认人类可读)")

    pc = sub.add_parser(
        "cluster", help="向量库相似度聚类/去重(管道加工)"
    )
    pc.add_argument("--source", default=None, help="过滤数据源,不传=全库")
    pc.add_argument(
        "--threshold", type=float, default=0.92,
        help="相似度阈值(0-1,越大越严格,默认 0.92 抓几乎重复)",
    )
    pc.add_argument(
        "--min-cluster-size", type=int, default=2,
        help="只保留 size>=N 的簇(默认 2;孤立点单列不展开)",
    )
    pc.add_argument(
        "--limit", type=int, default=None,
        help="最多处理多少条(默认全部;调试可设小值)",
    )
    pc.add_argument(
        "--members", action="store_true",
        help="人类可读模式下展示每个簇的成员 id(默认只展示代表+数量)",
    )
    pc.add_argument("--json", action="store_true", help="输出 JSON(默认人类可读)")

    args = p.parse_args()

    if args.cmd == "semantic":
        ku_result = search_knowledge_units(
            args.query,
            top_k=args.top_k,
            source=args.source,
            fallback_policy=getattr(args, "fallback_policy", None),
            current_only=True,
        )
        data = ku_result.get("results", [])
        # 在 json 模式下输出 route/versions + results
        if args.json:
            print(json.dumps(ku_result, ensure_ascii=False, indent=2, default=str))
            return
    elif args.cmd == "knowledge":
        data = get_knowledge_status(probe_chroma=not args.no_chroma)
    elif args.cmd == "query":
        data = query_events(
            source=args.source, month=args.month,
            category=args.category, keyword=args.keyword, limit=args.limit,
            dedup=args.dedup,
        )
    elif args.cmd == "detail":
        data = get_event_detail(args.event_id)
        if data is None:
            print(f"未找到 event_id={args.event_id}")
            return
    elif args.cmd == "stats":
        data = stats()
    elif args.cmd == "merge-stats":
        data = merge_stats()
    elif args.cmd == "memory":
        if args.subject:
            detail = get_memory_by_subject(args.subject)
            if detail is None:
                print(f"未找到 memory subject={args.subject}")
                return
            if args.neighbors:
                detail["neighbors"] = get_memory_neighbors(args.subject, args.neighbors)
            data = detail
        else:
            data = get_memory_profile(memory_type=args.memory_type, limit=args.limit)
    elif args.cmd == "cluster":
        data = cluster(
            source=args.source, threshold=args.threshold,
            min_cluster_size=args.min_cluster_size, limit=args.limit,
        )

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return

    # 人类可读输出
    if args.cmd == "semantic":
        route = ku_result.get("route", "")
        versions = ku_result.get("versions") or {}
        print(f"检索路由: {route or '(n/a)'}  fallback_policy={ku_result.get('fallback_policy', '')}")
        if versions:
            print(
                f"知识索引: {versions.get('index_version') or versions.get('collection') or ''} "
                f"build={versions.get('build_id', '')} units={versions.get('unit_count', '')}"
            )
        if not data:
            print("无匹配结果")
            return
        for i, r in enumerate(data, 1):
            mc = r.get("merged_count")
            tail = f"  (折叠 {mc} 条)" if mc and mc > 1 else ""
            # 兼容 knowledge_unit 和 event 两种结果格式
            unit = r.get("retrieval_unit", "")
            subj = r.get("subject", r.get("title", ""))
            content = r.get("answer", r.get("content", ""))
            src = r.get("source", r.get("collection", ""))
            print(f"\n#{i} [score={r.get('score',0)}] [{src}] {(subj or '(无标题)')[:50]}{tail}")
            print(
                f"   来源库: {r.get('collection','')}  单元: {unit}  原因: {r.get('rank_reason','')}"
            )
            if r.get("event_time"):
                print(f"   时间: {r['event_time']}  分类: {r.get('category_v2','')}")
            c = (content or "")[:200]
            print(f"   内容: {c}{'…' if len(content or '')>200 else ''}")
        print(f"\n共 {len(data)} 条")
    elif args.cmd == "knowledge":
        print(f"知识索引 available: {data.get('available')}")
        print(f"active collection: {data.get('active_collection') or '(none)'}")
        print(f"unit_count: {data.get('unit_count')}")
        print(f"canonical_current: {data.get('canonical_current_count')}")
        print(f"route_policy: {data.get('route_policy')}")
        print(f"fallback_policy: {data.get('fallback_policy')}")
        ssot = data.get("ssot") or {}
        if ssot:
            print(
                f"ssot: dialogue={ssot.get('dialogue')} knowledge={ssot.get('knowledge')} "
                f"non_dialogue_raw={ssot.get('non_dialogue_raw')}"
            )
        gs = data.get("google_structure") or {}
        if gs:
            print(
                f"google_structure: activities={gs.get('activities')} "
                f"normalized={gs.get('normalized_events')} "
                f"assertions={gs.get('light_assertions')} "
                f"by_type={gs.get('assertions_by_type')}"
            )
        print(f"chroma: {'ok' if data.get('chroma_available') else data.get('chroma_error', 'n/a')}")
        ver = data.get("version") or {}
        if ver:
            print(
                f"version: status={ver.get('status')} build={ver.get('build_id')} "
                f"db_units={ver.get('unit_count')} activated={ver.get('activated_at')}"
            )
        routes = data.get("semantic_routes") or {}
        if routes:
            print("semantic routes:")
            for k, v in routes.items():
                print(f"  {k}: {v}")
    elif args.cmd == "query":
        if not data:
            print("无匹配结果")
            return
        for r in data:
            mc = r.get("merged_count")
            tail = f" ×{mc}" if mc and mc > 1 else ""
            print(f"[{r['source']}] {r['event_time']} | {(r.get('title') or '')[:40]} | {r.get('category_v2','')}{tail}")
        print(f"\n共 {len(data)} 条(上限 {args.limit})" + ("(已去重)" if args.dedup else ""))
    elif args.cmd == "detail":
        for k, v in data.items():
            val = str(v) if v is not None else ""
            if len(val) > 300:
                val = val[:300] + "…"
            print(f"{k}: {val}")
    elif args.cmd == "stats":
        print(f"总事件: {data['total_events']:,}")
        print(f"活跃月份: {data['active_months']}")
        print("按源分布:")
        for s, n in data.get("by_source", {}).items():
            print(f"  {s}: {n:,}")
        if data.get("vector_available"):
            print(f"向量库: {data['vector_count']:,} 条 (personal_events)")
            if data.get("conversation_turns_available"):
                print(f"turn 叙述: {data['conversation_turns_count']:,} 条 (conversation_turns)")
        else:
            print(f"向量库: 不可用({data.get('vector_error','')})")
        ku = data.get("knowledge") or {}
        if ku:
            print(
                f"知识索引: {'available' if ku.get('available') else 'unavailable'} "
                f"collection={ku.get('active_collection') or '(none)'} "
                f"units={ku.get('unit_count')} "
                f"policy={ku.get('route_policy')}"
            )
    elif args.cmd == "merge-stats":
        if not data.get("available"):
            print(data.get("hint", "合并层未构建"))
            return
        print(f"输入事件: {data['n_input']:,}  →  等效事件: {data['effective_events']:,}"
              f"  (压缩 {data['compression']:.1%})")
        print(f"L1 真重复: {data['l1_clusters']} 簇 / {data['l1_events']} 条")
        print(f"L2 同主题: {data['l2_clusters']} 簇 / {data['l2_events']} 条")
        print(f"L3 结构保护: {data['structural_clusters']} 簇 / {data['structural_events']} 条")
        th = data["thresholds"]
        print(f"阈值: L1={th['l1_cos']}/J{th['l1_jac']}/SJ{th['l1_sem_jac']}/DR{th['l1_distinct']} L2={th['l2_cos']}")
        if data.get("top_l1_clusters"):
            print("\nL1 Top 簇:")
            for c in data["top_l1_clusters"]:
                print(f"  size={c['member_count']} '{(c['title'] or '')[:45]}'")
        if data.get("top_l2_clusters"):
            print("\nL2 Top 簇:")
            for c in data["top_l2_clusters"]:
                print(f"  size={c['member_count']} {c['summary']}")
    elif args.cmd == "memory":
        if args.subject:
            memory = data["memory"]
            print(f"[{memory['memory_type']}/{memory['memory_subtype']}] {memory['subject']}")
            print(f"置信度: {memory.get('confidence')}  证据数: {memory.get('evidence_count')}")
            print(f"描述: {memory.get('description')}")
            if data.get("relations"):
                print("\n关系:")
                for r in data["relations"][:20]:
                    print(f"  {r['from_subject']} --{r['relation']}({r['strength']})--> {r['to_subject']}")
            if data.get("evidence_summary"):
                print("\n证据摘要:")
                for row in data["evidence_summary"][:5]:
                    print(
                        f"  {row.get('source','?')} {str(row.get('event_time',''))[:19]} "
                        f"{str(row.get('title') or '(无标题)')[:60]}"
                    )
            if data.get("neighbors"):
                print("\n邻居:")
                for level in data["neighbors"].get("levels", []):
                    names = ", ".join(f"{n['subject']}[{n['memory_type']}]" for n in level.get("nodes", []))
                    print(f"  {level['hop']}跳: {names or '(无)'}")
        else:
            if not data.get("available"):
                print(data.get("hint", "记忆层未构建"))
                return
            print(f"记忆总数: {data['total']}")
            print("按类型:")
            for t, n in data["by_type"].items():
                print(f"  {t}: {n}")
            print("\n明细:")
            for item in data["items"]:
                print(f"[{item['memory_type']}/{item['memory_subtype']}] {item['subject']} "
                      f"(证据 {item['evidence_count']}, 置信 {item['confidence']})")
                print(f"  {item['description'][:160]}")
    elif args.cmd == "cluster":
        print(f"输入事件: {data['n_input']:,}")
        print(f"保留(代表): {data['n_kept']:,}  合并掉: {data['n_merged']:,}"
              f"  压缩率: {data['compression']:.1%}")
        print(f"簇数: {data['n_clusters']}  孤立点: {data['n_singletons']}"
              f"  (阈值={data['threshold']}, 最小簇={data['min_cluster_size']})")
        if data["clusters"]:
            print(f"\nTop 簇(按 size 降序):")
            for c in data["clusters"]:
                print(f"  #{c['id']} size={c['size']} mean_sim={c['mean_similarity']}"
                      f"  代表: {(c['representative_title'] or '(无标题)')[:50]}")
                if args.members:
                    for mid in c["member_ids"]:
                        print(f"      - {mid}")
        if not data["clusters"]:
            print("\n(无达到 min-cluster-size 的簇,试试降低 --threshold)")


if __name__ == "__main__":
    _cli()
