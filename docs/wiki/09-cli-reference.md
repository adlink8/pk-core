# CLI 命令速查

> **用法约定：** 所有命令在项目根目录执行。`^` 是 PowerShell 换行符。

---

## pk-sync：同步对话

```powershell
# 日常（先看 dry-run，确认无误后 --write）
pk-sync conversations                     # 预览：有哪些新对话
pk-sync conversations --write             # 实际写入规范库

# 其他数据源
pk-sync turns                             # 预览 Turn 向量重建
pk-sync turns --write                     # 写入
pk-sync google                            # 预览 Google 事件
pk-sync google --write                    # 写入
pk-sync status --json                     # 查看版本/水印状态
```

---

## pk-ku：知识单元全流程

### 日常增量流程（8 步）

```powershell
# Step 1: 检查增量（免费）
pk-ku inspect
# 看 source_changed 和 new_refs_count
# new_refs_count > 0 才继续，否则停

# Step 2: 准备提取清单（免费）
pk-ku prepare --model gemini-3.5-flash --provider vertex_google ^
  --endpoint "https://aiplatform.googleapis.com" --auth-mode gcloud

# Step 3: 付费提取
pk-ku extract --run ir_<run_id> --max-items 50 --workers 4

# Step 4: 提取门禁
pk-ku extract-gate --run ir_<run_id> --min-yield 0.7

# Step 5: 规范化 + 发布
pk-ku canonical --run ir_<run_id> --write
pk-ku publish --run ir_<run_id> --write

# Step 6: 候选向量索引
pk-ku vector --write

# Step 7: 金丝雀评估 + 严格门禁
pk-ku canary --candidate-override <collection> --report canary.json
pk-ku canary --report canary.json --label-with-llm
pk-ku canary --report canary.json --strict

# Step 8: 晋升 + 水印
pk-ku promote --collection <name> --require-eval-pass ^
  --eval-summary <path> --eval-gate <path>
pk-ku watermark --advance --from-canonical --write
```

### 生命周期管理

```powershell
pk-ku reconcile --dry-run --max-subjects 50          # 预览生命周期对齐
pk-ku reconcile --subject "Python" --dry-run         # 只看某个主题
pk-ku reconcile --write --i-know --max-subjects 20   # 写入（需 --i-know）
pk-ku history --subject "Python"                     # 查询成长线
```

### 运维

```powershell
pk-ku doctor                          # 健康检查
pk-ku doctor --json --skip-ports      # JSON 格式输出
pk-ku workflow                        # 展示当前工作流状态
pk-ku watermark                        # 查看水印
pk-ku promote --list                   # 列出可晋升的候选索引
```

---

## rag-search：搜索

### 语义检索

```powershell
rag-search "Python 调试"                      # 默认 top_k=5
rag-search "数据库优化" --top-k 3              # 指定返回条数
rag-search "PPT 排版" --source Agent           # 过滤数据源
rag-search "测试" --top-k 8 --dedup            # 合并层去重
```

### 精确查询

```powershell
rag-search query --source GPT --month 2025-03                  # 按月+源
rag-search query --category 编程 --keyword 报错 --limit 10      # 分类+关键词
rag-search query --source Agent --dedup --limit 30             # 精确+去重
```

### 详情与统计

```powershell
rag-search detail <event_id>                  # 单条事件详情
rag-search stats                              # 统计概览
rag-search knowledge                          # 知识索引状态
rag-search merge-stats                        # 合并层报告
rag-search memory                             # 记忆概览
rag-search memory --type tooling              # 过滤记忆类型
rag-search memory --subject Codex             # 按主体查记忆
rag-search memory --subject Codex --neighbors 2  # 记忆+图谱
rag-search cluster --source Agent --threshold 0.92  # 向量库聚类
```

---

## rag-api：启动 REST 服务

```powershell
rag-api                                       # 默认 127.0.0.1:8000
rag-api --host 0.0.0.0 --port 8080            # 改地址端口
```

---

## rag-mcp：启动 MCP 服务

```powershell
rag-mcp                                       # stdio 模式

# 环境变量控制
$env:PERSONAL_DATA_MCP_PROFILE = "full"       # 暴露全部工具（默认 core）
$env:PERSONAL_DATA_EMBED_DEVICE = "cpu"       # MCP 用 CPU 嵌入
$env:PERSONAL_DATA_FALLBACK_POLICY = "legacy" # 使用旧版回落策略
```

---

## rag-dashboard：启动仪表盘

```powershell
rag-dashboard                                 # 打开 http://localhost:8501
```

---

## 一键启动所有服务

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File ^
  apps\personal_data_chatgpt\scripts\start-services.ps1
```

启动后检查：

```powershell
curl http://127.0.0.1:8000/health             # REST API 健康
curl http://127.0.0.1:8789/health             # MCP 健康
pk-ku doctor --skip-ports                     # 系统体检
```

---

## 治理

```powershell
# 合规检查（CI 用）
python -m personal_knowledge.governance.preflight --ci

# 治理专项测试
python -m pytest tests/governance/ -q
```

---

## 环境变量速查

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `PERSONAL_DATA_MCP_PROFILE` | `core` | MCP 暴露工具集：`core` / `full` |
| `PERSONAL_DATA_EMBED_DEVICE` | `cuda` | 嵌入模型设备：`cuda` / `cpu` |
| `PERSONAL_DATA_FALLBACK_POLICY` | `layered` | 搜索回落策略：`layered` / `legacy` |
| `PERSONAL_DATA_ALLOW_LEGACY_PAD` | `1` | layered 模式是否允许 legacy 填充 |
| `PERSONAL_DATA_EMBED_MODEL_PATH` | （自动） | 嵌入模型路径 |
| `PK_ALLOW_LEGACY_PIPELINE` | `0` | 是否允许退役管线 |
| `PK_KU_ALLOW_FULL_INVENTORY_START` | `0` | 是否允许全量提取 |
| `PK_KU_ALLOW_NON_INCREMENTAL_RUN` | `0` | 是否允许非增量 run |
