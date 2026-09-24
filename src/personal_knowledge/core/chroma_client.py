"""轻量 chroma REST 客户端(基于 requests)。

绕开 chromadb 官方客户端的 httpx 兼容性问题(httpx 访问本地 chroma
返回 502,requests 正常)。封装我们需要的最小操作集:

- list_collections()        列出所有 collection
- get_or_create_collection() 创建/获取 collection(返回 collection 对象)
- collection.add()          批量写入向量
- collection.query()        向量检索
- collection.count()        计数
- collection.delete()       删除 collection

设计:
- 只依赖 requests(标准库级别,Python 3.14 完全兼容)
- 不依赖 chromadb 包(避免 starlette/protobuf 冲突)
- collection 用 id 操作(chroma v2 要求)
- 所有请求带超时,失败抛 ChromaError

用法:
    from personal_knowledge.core.chroma_client import ChromaClient
    c = ChromaClient(host="localhost", port=8001)
    coll = c.get_or_create_collection("personal_events")
    coll.add(ids=[...], embeddings=[...], documents=[...], metadatas=[...])
    results = coll.query(query_embeddings=[...], n_results=5)
"""

from __future__ import annotations

from typing import Any

import requests


class ChromaError(Exception):
    """chroma REST API 错误。"""


class Collection:
    """单个 collection 的操作句柄。"""

    def __init__(self, client: "ChromaClient", coll_id: str, name: str, dimension: int | None = None):
        self._client = client
        self.id = coll_id
        self.name = name
        self.dimension = dimension

    def _base(self) -> str:
        return f"{self._client._base}/collections/{self.id}"

    def add(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str] | None = None,
        metadatas: list[dict] | None = None,
        timeout: int = 120,
    ) -> None:
        """批量写入向量。ids/embeddings 必填,documents/metadatas 可选。"""
        payload: dict[str, Any] = {"ids": ids, "embeddings": embeddings}
        if documents is not None:
            payload["documents"] = documents
        if metadatas is not None:
            payload["metadatas"] = metadatas
        r = self._client._session.post(f"{self._base()}/add", json=payload, timeout=timeout)
        if r.status_code not in (200, 201):
            raise ChromaError(f"add failed: {r.status_code} {r.text[:200]}")

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str] | None = None,
        metadatas: list[dict] | None = None,
        timeout: int = 120,
    ) -> None:
        """批量 upsert(存在则更新,不存在则插入)。"""
        payload: dict[str, Any] = {"ids": ids, "embeddings": embeddings}
        if documents is not None:
            payload["documents"] = documents
        if metadatas is not None:
            payload["metadatas"] = metadatas
        r = self._client._session.post(f"{self._base()}/upsert", json=payload, timeout=timeout)
        if r.status_code not in (200, 201):
            raise ChromaError(f"upsert failed: {r.status_code} {r.text[:200]}")

    def query(
        self,
        query_embeddings: list[list[float]] | None = None,
        n_results: int = 10,
        where: dict | None = None,
        include: list[str] | None = None,
        timeout: int = 60,
    ) -> dict:
        """向量检索。返回 {ids, distances, documents, metadatas}。

        query_embeddings: 查询向量列表(每个是一维 float list)
        n_results: 每个查询返回的邻居数
        where: 元数据过滤,如 {"source": "Agent"}
        include: 返回哪些字段,默认 ["metadatas","documents","distances"]
        """
        if include is None:
            include = ["metadatas", "documents", "distances"]
        payload: dict[str, Any] = {
            "query_embeddings": query_embeddings,
            "n_results": n_results,
            "include": include,
        }
        if where is not None:
            payload["where"] = where
        r = self._client._session.post(f"{self._base()}/query", json=payload, timeout=timeout)
        if r.status_code != 200:
            raise ChromaError(f"query failed: {r.status_code} {r.text[:200]}")
        return r.json()

    def get(
        self,
        ids: list[str] | None = None,
        where: dict | None = None,
        limit: int | None = None,
        offset: int = 0,
        include: list[str] | None = None,
        timeout: int = 60,
    ) -> dict:
        """按条件获取向量(非相似度检索)。"""
        if include is None:
            include = ["documents", "metadatas"]
        payload: dict[str, Any] = {"include": include, "offset": offset}
        if ids is not None:
            payload["ids"] = ids
        if where is not None:
            payload["where"] = where
        if limit is not None:
            payload["limit"] = limit
        r = self._client._session.post(f"{self._base()}/get", json=payload, timeout=timeout)
        if r.status_code != 200:
            raise ChromaError(f"get failed: {r.status_code} {r.text[:200]}")
        return r.json()

    def count(self, timeout: int = 30) -> int:
        """返回 collection 中向量数。"""
        r = self._client._session.get(f"{self._base()}/count", timeout=timeout)
        if r.status_code != 200:
            raise ChromaError(f"count failed: {r.status_code} {r.text[:200]}")
        return int(r.json())

    def delete_collection(self, timeout: int = 30) -> None:
        """删除整个 collection。已不存在(404)视为成功。
        注意:chromadb v1.4 的 v2 API DELETE 端点必须用 collection name,
        用 id 会返回 404(尽管 list 返回的是 id)。"""
        r = self._client._session.delete(f"{self._client._base}/collections/{self.name}", timeout=timeout)
        # 200/204 = 删除成功;404 = 已不存在(幂等视为成功)
        if r.status_code not in (200, 204, 404):
            raise ChromaError(f"delete_collection failed: {r.status_code} {r.text[:200]}")


class ChromaClient:
    """chroma 服务客户端(REST API v2,基于 requests)。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8001, tenant: str = "default_tenant", database: str = "default_database"):
        self.host = host
        self.port = port
        self.tenant = tenant
        self.database = database
        self._base = f"http://{host}:{port}/api/v2/tenants/{tenant}/databases/{database}"
        self._session = requests.Session()
        # Chroma 是本机服务。禁用系统代理环境，避免 localhost 首次请求
        # 被代理探测拖慢约 20 秒或返回 502。
        self._session.trust_env = False
        self._session.headers.update({"Content-Type": "application/json"})

    def heartbeat(self, timeout: int = 10) -> int:
        """健康检查,返回纳秒心跳。"""
        r = self._session.get(f"http://{self.host}:{self.port}/api/v2/heartbeat", timeout=timeout)
        if r.status_code != 200:
            raise ChromaError(f"heartbeat failed: {r.status_code}")
        return int(r.json().get("nanosecond heartbeat", 0))

    def list_collections(self, timeout: int = 30) -> list[dict]:
        """列出所有 collection。返回 [{name, id, dimension}, ...]。"""
        try:
            r = self._session.get(f"{self._base}/collections?limit=1000", timeout=timeout)
        except requests.RequestException as exc:
            raise ChromaError("list_collections unavailable") from exc
        if r.status_code != 200:
            raise ChromaError(f"list_collections failed: {r.status_code} {r.text[:200]}")
        return r.json()

    def get_or_create_collection(
        self,
        name: str,
        metadata: dict | None = None,
        timeout: int = 30,
    ) -> Collection:
        """获取或创建 collection。metadata 默认 {hnsw:space: cosine}。

        幂等:同名已存在则返回现有(不报错)。
        """
        # Chroma 对“已存在 collection 的 create 请求”可能等待较久才返回
        # 冲突。查询路径会频繁调用本方法，因此先读取现有 collection，
        # 只有确实不存在时才发创建请求。
        existing = self._find_collection_by_name(name)
        if existing:
            return Collection(self, existing["id"], existing["name"], existing.get("dimension"))

        if metadata is None:
            metadata = {"hnsw:space": "cosine"}
        payload = {"name": name, "metadata": metadata}
        r = self._session.post(f"{self._base}/collections", json=payload, timeout=timeout)
        if r.status_code in (200, 201):
            data = r.json()
            return Collection(self, data["id"], data["name"], data.get("dimension"))
        # 可能是已存在(409 或 400),尝试 get by name
        if r.status_code in (400, 409):
            existing = self._find_collection_by_name(name)
            if existing:
                return Collection(self, existing["id"], existing["name"], existing.get("dimension"))
        raise ChromaError(f"get_or_create_collection failed: {r.status_code} {r.text[:300]}")

    def _find_collection_by_name(self, name: str) -> dict | None:
        for coll in self.list_collections():
            if coll.get("name") == name:
                return coll
        return None

    def delete_collection_by_name(self, name: str, timeout: int = 30) -> bool:
        """按名删除 collection。不存在返回 False。"""
        coll = self._find_collection_by_name(name)
        if not coll:
            return False
        c = Collection(self, coll["id"], coll["name"])
        c.delete_collection(timeout=timeout)
        return True
