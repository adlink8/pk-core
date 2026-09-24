"""/data/* /google/* /categories /memory* /event/* /profile 数据域处理器 + POST /search/*。

从 api_server.py 的 do_GET data 段(google assertions / categories / data CRUD 聚合导出 /
memory / memory graph / event / profile)与 do_POST /search/semantic、/search/query 段
原样抽出,行为等价:状态码、信封结构、错误封包格式与现状完全一致。

依赖 api_server.backend 延迟解析(运行时取值),测试对 api_server.backend 的
monkeypatch(test_knowledge_distribution_contracts / test_cockpit_transport_security)生效。
"""

from __future__ import annotations

from urllib.parse import unquote


def handle_get(handler, ctx) -> None:
    import personal_knowledge.services.api_server as api_server

    path = ctx["path"]
    qs = ctx["qs"]
    backend = api_server.backend

    if path == "/google/assertions":
        data = backend.list_google_light_assertions(
            assertion_type=qs.get("type") or qs.get("assertion_type"),
            limit=int(qs.get("limit", 50)),
            offset=int(qs.get("offset", 0)),
        )
        handler._send(api_server._ok(data))
        return

    if path.startswith("/google/assertions/"):
        # IDs contain '|' (e.g. gla|interest_topic|…); must unquote %7C
        aid = unquote(path[len("/google/assertions/") :].strip("/"))
        if not aid:
            body, code = api_server._err("assertion_id required", 400)
            handler._send(body, code)
            return
        item = backend.get_google_light_assertion(aid)
        if item is None:
            body, code = api_server._err(f"assertion not found: {aid}", 404)
            handler._send(body, code)
            return
        handler._send(api_server._ok(item))
        return

    if path == "/categories":
        rows = backend.list_categories(qs.get("source"))
        handler._send(api_server._ok(rows))
        return

    if path == "/data/events":
        data = backend.list_events_contract(
            **api_server._data_filters(qs),
            fields=qs.get("fields"),
            limit=int(qs.get("limit", backend.DEFAULT_DATA_LIMIT)),
            offset=int(qs.get("offset", 0)),
            order=qs.get("order", "desc"),
        )
        handler._send(api_server._contract(data))
        return

    if path == "/data/memories":
        data = backend.list_memories_contract(
            memory_type=qs.get("type") or qs.get("memory_type"),
            memory_subtype=qs.get("subtype") or qs.get("memory_subtype"),
            subject=qs.get("subject") or qs.get("subject_like"),
            limit=int(qs.get("limit", backend.DEFAULT_DATA_LIMIT)),
            offset=int(qs.get("offset", 0)),
        )
        handler._send(api_server._contract(data))
        return

    if path == "/data/relations":
        data = backend.list_relations_contract(
            relation=qs.get("relation") or qs.get("relation_type"),
            from_memory_id=qs.get("from_memory_id"),
            to_memory_id=qs.get("to_memory_id"),
            subject=qs.get("subject"),
            status=qs.get("status"),
            limit=int(qs.get("limit", backend.DEFAULT_DATA_LIMIT)),
            offset=int(qs.get("offset", 0)),
        )
        handler._send(api_server._contract(data))
        return

    if path == "/data/aggregate":
        data = backend.aggregate_contract(
            group_by=qs.get("group_by", "source"),
            **api_server._data_filters(qs),
            limit=int(qs.get("limit", backend.DEFAULT_DATA_LIMIT)),
        )
        handler._send(api_server._contract(data))
        return

    if path == "/data/timeline":
        filters = api_server._data_filters(qs)
        if qs.get("subject") and not filters.get("keyword"):
            filters["keyword"] = qs.get("subject")
        data = backend.timeline_contract(
            interval=qs.get("interval") or qs.get("bucket") or "month",
            **filters,
            limit=int(qs.get("limit", backend.DEFAULT_DATA_LIMIT)),
        )
        handler._send(api_server._contract(data))
        return

    if path == "/data/export":
        filters = api_server._data_filters(qs)
        if qs.get("query") and not filters.get("keyword"):
            filters["keyword"] = qs.get("query")
        data = backend.export_events_contract(
            export_format=qs.get("format", "jsonl"),
            **filters,
            fields=qs.get("fields"),
            limit=int(qs.get("limit", backend.MAX_EXPORT_LIMIT)),
            offset=int(qs.get("offset", 0)),
            order=qs.get("order", "desc"),
        )
        handler._send(api_server._contract(data))
        return

    if path == "/data/quality":
        handler._send(api_server._contract(backend.data_quality_report_contract()))
        return

    if path.startswith("/data/event/"):
        event_id = unquote(path[len("/data/event/"):])
        if not event_id:
            body, code = api_server._err("缺少 event_id", 400)
            handler._send(body, code)
            return
        data = backend.get_event_by_id_contract(event_id, fields=qs.get("fields"))
        handler._send(api_server._contract(data), 200 if data.get("found") else 404)
        return

    if path.startswith("/data/memory/"):
        memory_id = unquote(path[len("/data/memory/"):])
        if not memory_id:
            body, code = api_server._err("缺少 memory_id", 400)
            handler._send(body, code)
            return
        # default true; accept false/0/no/off
        include_evidence = True
        if "include_evidence" in qs:
            include_evidence = api_server._truthy(qs.get("include_evidence"))
        data = backend.get_memory_by_id_contract(
            memory_id, include_evidence=include_evidence
        )
        handler._send(api_server._contract(data), 200 if data.get("found") else 404)
        return

    if path == "/memory":
        rows = backend.get_memory_profile(
            memory_type=qs.get("type"),
            limit=int(qs.get("limit", 200)),
        )
        handler._send(api_server._ok(rows))
        return

    if path == "/memory/graph":
        data = backend.get_memory_graph_contract(
            subject=qs.get("subject"),
            hops=int(qs.get("hops", 1)),
            include_llm=api_server._truthy(qs.get("include_llm")),
            limit=int(qs.get("limit", backend.DEFAULT_MEMORY_GRAPH_LIMIT)),
        )
        handler._send(api_server._contract(data))
        return

    if path == "/memory/relation-review":
        data = backend.get_memory_relation_review_contract(
            limit=int(qs.get("limit", backend.DEFAULT_RELATION_REVIEW_LIMIT)),
            status=qs.get("status"),
        )
        handler._send(api_server._contract(data))
        return

    if path.startswith("/memory/"):
        subject = unquote(path[len("/memory/"):])
        if not subject:
            body, code = api_server._err("缺少 memory subject", 400)
            handler._send(body, code)
            return
        data = backend.get_memory_by_subject(subject)
        if data is None:
            body, code = api_server._err(f"未找到 memory subject={subject}", 404)
            handler._send(body, code)
            return
        if qs.get("neighbors"):
            data["neighbors"] = backend.get_memory_neighbors(
                subject, int(qs.get("neighbors", 2))
            )
        handler._send(api_server._ok(data))
        return

    if path == "/profile":
        if not api_server.PROFILE_MD.exists():
            body, code = api_server._err("AI 上下文文档未生成,请先跑 build_context_doc.py", 404)
            handler._send(body, code)
            return
        text = api_server.PROFILE_MD.read_text(encoding="utf-8")
        handler._send(api_server._ok({"profile": text, "path": str(api_server.PROFILE_MD)}))
        return

    if path.startswith("/event/"):
        event_id = path[len("/event/"):]
        if not event_id:
            body, code = api_server._err("缺少 event_id", 400)
            handler._send(body, code)
            return
        data = backend.get_event_detail(event_id)
        if data is None:
            body, code = api_server._err(f"未找到 event_id={event_id}", 404)
            handler._send(body, code)
        else:
            handler._send(api_server._ok(data))
        return


def handle_search_semantic(handler, ctx) -> None:
    import personal_knowledge.services.api_server as api_server

    body = ctx["body"]
    query = body.get("query", "").strip()
    if not query:
        b, c = api_server._err("缺少 query 参数")
        handler._send(b, c)
        return
    # knowledge-first + raw fallback；可选 collection_override 仅用于 canary/评测
    result = api_server.backend.search_knowledge_units(
        query=query,
        top_k=int(body.get("top_k", 5)),
        source=body.get("source"),
        include_evidence=bool(body.get("include_evidence", False)),
        collection_override=(body.get("collection") or body.get("collection_override") or None),
    )
    handler._send(api_server._ok(result))


def handle_search_cards(handler, ctx) -> None:
    import personal_knowledge.retrieval.semantic_cards as semantic_cards
    import personal_knowledge.services.api_server as api_server

    body = ctx["body"]
    # 详情模式：body 传 session_id 时返回单张完整会话卡 + active facts（含 evidence_refs）
    session_id = str(body.get("session_id") or "").strip()
    if session_id:
        card = semantic_cards.get_card(session_id)
        if card is None:
            b, c = api_server._err(f"未找到 session_id={session_id}", 404)
            handler._send(b, c)
            return
        handler._send(api_server._ok(card))
        return
    query = str(body.get("query") or "").strip()
    if not query:
        b, c = api_server._err("缺少 query 或 session_id 参数")
        handler._send(b, c)
        return
    limit = int(body.get("limit", body.get("top_k", 8)) or 8)
    results = semantic_cards.search_cards(query, limit=limit)
    handler._send(api_server._ok(results))


def handle_search_query(handler, ctx) -> None:
    import personal_knowledge.services.api_server as api_server

    body = ctx["body"]
    results = api_server.backend.query_events(
        source=body.get("source"),
        month=body.get("month"),
        category=body.get("category"),
        keyword=body.get("keyword"),
        limit=int(body.get("limit", 50)),
    )
    handler._send(api_server._ok(results))
