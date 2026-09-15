# 常见问题

> 本页收集项目使用中反复出现的问题，来自实际开发和运维经验。

---

## 搜索相关

### Q：搜不到结果，为什么？

看 `telemetry.layers`：

- 所有层 `attempted=false` → **embedding 失败**。检查模型路径、Chroma 服务是否运行
- `knowledge_unit` 有 `hits=0` 但其他层有 → **知识索引没相关内容**。可能是还没 promote 或没有匹配的知识单元
- 所有层 `hits=0` → 确实没有匹配数据
- `knowledge_unit` 有 `abstained=N` → **门禁拦截了 N 条**。查 `support_reason_codes` 看具体原因

### Q：知识索引有数据但搜不到？

可能是门禁拦截了。常见 reason_code：

| reason_code | 含义 | 怎么修 |
|-------------|------|--------|
| `lifecycle_not_current` | 知识单元已标记 superseded/deprecated | 检查生命周期状态，可能需要重新 promote |
| `evidence_missing` | 证据引用找不到 | 可能是重建后 evidence DB 不一致，重跑证据链 |
| `query_candidate_ungrounded` | 查询词和知识单元无词汇重叠 | 知识单元内容太短或查询太偏，属于正常行为 |

### Q：MCP 搜索和 REST 搜索结果不一样？

MCP 内部是通过 `http://127.0.0.1:8000/search/semantic` 调 REST API 的。如果 REST API 没启动或版本不一致，MCP 会报错或返回旧结果。

排查：
```powershell
curl http://127.0.0.1:8000/health        # REST 是否正常
curl http://127.0.0.1:8789/health        # MCP 是否正常
```

### Q：查询向量的维度是多少？和向量库的维度一致吗？

**都是 512 维。** 嵌入模型是 `bge-small-zh-v1.5`，写入和查询用同一个模型。

之前在 wiki 里错误标注过 768d，实际代码里确认：

```python
# local_embed.py:35
EMBED_DIM = 512  # bge-small-zh-v1.5 维度
```

---

## 数据与合并

### Q：我的压缩率 40%+，正常吗？

首先确认一下看到的"压缩率"是哪个数值：

```
merge_stats 输出:
  n_input:            7,606    ← 输入总数
  l1_events:             63   ← L1 真重复（8个簇）
  l2_events:          1,565   ← L2 同主题（462个簇）
  structural_events:  3,492   ← L3 结构保护（5个超大簇）
  solo_events:        2,486   ← L3 独立事件
  ─────────────────────────────────
  effective_events:   6,448
  compression:       15.2%    ← 这才是压缩率
```

你可能把 **L3 结构保护占比（3492/7606 = 45.9%）** 当成了压缩率。这个 45.9% 不是"被压缩了"，而是"被保护不合并"。

**压缩率不是越高越好：**

| 场景 | 压缩率 | 问题 |
|------|--------|------|
| L1 占比 > 20% | 高 | 上游写入有 bug，同一数据被重复写入 |
| L2 阈值太松（如 0.80） | 虚高 | 把"Python"和"JavaScript"也合并了 |
| 无结构保护 | 虚高 | 200 条日志强行合并，代表无代表性 |

### Q：去重用的是余弦相似度还是 SQLite 文本比较？

**两者结合，分两个阶段：**

**阶段 A：构建合并层（偶尔跑一次）**——两者都用

```
余弦矩阵（≥0.97）→ 候选池      ← 从 Chroma 拉 512d 向量
    ↓
4-gram Jaccard（≥0.80）         ← SQLite 读原始文本
    ↓
语义骨架 Jaccard（≥0.75）       ← 去数字/路径/UUID 后比
    ↓
内容区分度（<0.5）              ← 防结构相似假阳性
```

**阶段 B：搜索时去重（每次查询）**——只用 SQLite

```sql
-- 直接查预计算好的合并表，毫秒级
SELECT mm.event_id, mc.representative_id
FROM merge_members mm JOIN merge_clusters mc ON mc.cluster_id = mm.cluster_id
WHERE mm.event_id IN (...)
```

**为什么不分家：**

| 只用余弦 | 只用 Jaccard | 两者结合 |
|---------|-------------|---------|
| "umath-42.csv"和"umath-99.csv"余弦≥0.99 ✅但内容不同，误判 ❌ | "用换页功能做PPT"和"字体统一留白" Jaccard 低，语义相同但漏判 ❌ | 余弦管的宽（语义近似都抓到），Jaccard 管的严（只有真重复才过）✅ |

---

## 知识单元管线

### Q：extract 时 LLM 调用了很多次，结果 yield 很低

检查 `pk-ku extract-gate` 的输出：

- `api_completion` 失败多 → 检查 LLM 端点和配额
- `privacy` 失败多 → 查询中包含敏感信息请求，被隐私门禁拦截
- `schema` 失败多 → LLM 返回格式不符合 Pydantic schema

### Q：全量提取和增量提取什么区别

| | 增量（日常） | 全量（罕见） |
|--|-----------|-----------|
| 入口 | `pk-ku extract --run ir_*` | 需要 `PK_KU_ALLOW_NON_INCREMENTAL_RUN=1` |
| 范围 | 仅上次 watermark 之后的新内容 | 全部历史对话 |
| 费用 | 少 | 大量的 LLM 费用 |
| 什么时候用 | 每天 | 首次搭建、迁移恢复 |

### Q：`pk-ku prepare` 说 no_op 但 `pk-ku inspect` 显示有变化

**这是已知缺陷。** 当 watermark 存在时，`prepare` 可能读到和上次相同的 canonical DB，导致 content delta 为空。此时：

```powershell
# ❌ 错误做法：跑全量提取
build_knowledge_inventory --write + build_knowledge_units_prod --start

# ✅ 正确做法：报告缺陷，等待修复
# 不要自己绕过去
```

---

## 概念辨析

### Q：推荐系统、人脸识别也算 RAG 吗？

**不算。** RAG 有严格的三步定义：

```
Retrieval（检索） → Augmentation（增强） → Generation（生成）
```

| 场景 | 有 R？ | 有 A？ | 有 G？ | 算 RAG？ |
|------|:-----:|:-----:|:-----:|:--------:|
| 抖音推荐流 | ✅ | ❌ | ❌ | ❌ 纯向量搜索 |
| 人脸识别 | ✅ | ❌ | ❌ | ❌ 纯向量匹配 |
| **本项目 rag-search** | ✅ | ✅（拼结果） | ❌（等调用方处理）| 半 RAG ⚠️ |
| **Copilot 代码补全** | ✅ | ✅ | ✅ | **✅ RAG** |
| 银行反欺诈 | ✅ | ❌ | ❌ | ❌ 规则引擎 |

**一句话：向量搜索是找东西，RAG 是找到后让 LLM 基于它写东西。**

### Q：向量库是 LLM 之后才出现的吗？

**不是。** 核心算法远早于 LLM：

| 年代 | 技术 |
|------|------|
| 1960s | 向量空间模型（Salton 的 SMART 系统） |
| 1990s | LSA / SVD 降维 |
| 2011 | Spotify Annoy（ANN 搜索库） |
| 2016 | Meta FAISS（GPU 加速向量搜索） |
| 2017 | Milvus 第一个开源向量数据库 |
| 2018 | BERT 让文本 embedding 真正理解语义 |
| 2019+ | Pinecone、Weaviate、Qdrant 等独立产品出现 |
| 2022+ | LLM + 向量库 = RAG 成为主流模式 |

**"向量数据库"作为独立产品品类是新东西，但向量搜索本身不新。**

### Q：turn（对话轮次）是怎么定义的？

**turn 边界来自原始数据（AgentsView）的 `turn_id`，系统不创造不修改。**

一个 turn = 一次 user → assistant 的完整交换周期：

```
User: "帮我调试这个 Python 报错"      ← turn 开始
  → Assistant: "贴一下报错信息"
  → Tool: python test.py → Traceback
  → Assistant: "是循环导入问题"       ← turn 结束
```

**关键规则：**
- 同一个 `turn_id` 内的重复消息（同 role 同内容）会被去重
- 不同 `turn_id` 的相同文本各自保留（跨 turn 不去重）
- 没有 `turn_id` 的消息归入 "prologue"（开场序言）

### Q：项目到底在做什么？用一句话说清楚

**把你的对话记录变成可搜索的知识库，再基于这些知识做决策辅助。全本地运行，数据不出机器。**

具体到每天的流程：
```powershell
pk-sync conversations --write    # ① 拉取新对话
pk-ku inspect                    # ② 检查增量
pk-ku extract --run ir_xxxx      # ③ 提取知识单元（只有这一步花钱）
rag-search "Python 调试"         # ④ 随时搜索
```
