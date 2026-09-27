# 测评数据质量检测报告（2026-09-28 夜间）

范围：只做「测评/检测」相关。全程未改任何核心管线代码（live-sync、压缩、嵌入、索引构建零触碰）；
`var/db/conversation_vector.sqlite` 全程 `mode=ro` 只读。

## 一、检测工具（新增，可复用）

`src/personal_knowledge/retrieval/vector/exam_audit.py`

```bash
python src/personal_knowledge/retrieval/vector/exam_audit.py --exam docs/retrieval/exam_v2.json --json var/reports/exam_v2_audit.json
```

检查 7 项：结构完整性 / 重复题 / answer_set 前缀有效性 / 前缀歧义（一个前缀匹配多会话会虚高命中）/
agent 家族一致 / 家族覆盖缺口 / 原话层系统上下文污染率。有 ERROR 退出码 1，可直接挂 CI。

## 二、v1 考卷审计结论：底子干净

88 条 = 1 条 meta 头 + 87 题（此前"88 vs 87"疑点即 meta，非坏题）。
重复题 0；181 个 answer_set 前缀全部有效且唯一；agent 字段与前缀家族零不符。**v1 题目今晚零改动**（保证基线可比）。

## 三、发现 1：覆盖严重偏科（已通过扩题修复）

| 家族 | v1 题数 | 语料会话 | v2 题数 |
|---|---|---|---|
| legacy | 24 | 653 | 46 |
| codex | 25 | 417 | 40 |
| grok | 11 | 149 | 21 |
| zcode | 15 | 66 | 19 |
| chatgpt | 3 | 80 | **11** |
| workbuddy | 3 | 71 | **11** |
| kimi | 2 | 30 | **7** |
| copilot/mimo/antigravity/qoder | 各 1 | 13/13/8/6 | 5/5/4/1* |
| claude / gemini / cursor | 0 | 2/1/1 | **各 1** |
| opencode（原生通道） | 0 | 1 | 0（其内容已由 legacy 通道题目覆盖） |

*qoder 原生会话与 legacy 通道同一对话合并为一题（见发现 2）。

## 四、发现 2：跨通道双胞胎会话（全库 1,182 对，本夜只做检测与标注，未做合并）

同一对话经 legacy 聚合通道与原生通道各入库一份，如 `cs|chatgpt|66dbe177-…` 与
`cs|legacy|cs/chatgpt/chatgpt:66dbe177-…`。对策（仅限考卷侧）：

- **兄弟规则（最终版）**：跨家族且 id 十六进制片段（≥10 位）互含 → 镜像双胞胎；同家族需标准化引语共享 ≥3 条且最长 ≥60 字（深重合）。答案集上限 gold+3。
- 中间版本教训：纯"id 互含"会把 kimi 同工作区所有 agent 轮次并进来（上游把同一 uuid 发给不同任务）；纯"共享引语"会把 Target D 模板化罐头话术簇（9/16 个"双胞胎"）误连。两组都已排除并人工抽验。
- v2 新题共并入 15 处兄弟（12 组 chatgpt↔legacy、kimi 聚合镜像 2 题、antigravity 1、codex 深重合 1）。
- **遗留建议（需另行批准，属核心管线）**：索引层做双胞胎归并或召回去重，可省 top-k 名额。

## 五、发现 3：原话层污染 29%（量化取证，未动核心代码）

6,190 条原话块中 **1,810 条（29%）以 `<system-reminder>`/`<user_info>` 等系统上下文开头，波及 190 个会话**。
v1 详报 Q01 的 top3 里第 2、3 名全是这种垃圾块（score 0.6189），实证污染在挤占召回名额。
修复需改 quote 提取/索引管线（embed_index/full_run 一侧），按今晚红线**未动**，仅新增检测指标：
eval 跑分时输出「top5 含垃圾块的题目占比」，见 v2 报告。

## 六、发现 4：kimi 上游 id 碰撞（检测记录，未修）

kimi 会话 id 形如 `wd_<workspace>:agent-N:session_<uuid>`，同一 workspace 的不同任务共享同一 session uuid
（如 `064a8fb3b6d0` 工作区 agent-3=ruff 修 lint、agent-6=路由持久化，uuid 相同）。按 id 找兄弟会连环误并，
这也是兄弟规则最终版要求"同家族必须过内容验证"的原因。**上游 id 生成缺陷，建议后续在摄入侧登记。**

## 七、明细文件

- `var/reports/exam_v1_audit.json` / `exam_v2_audit.json`：审计明细
- `var/reports/twin_map_strong.json`：强双胞胎图（283 节点）
- `docs/retrieval/exam_v2_pool.json`：扩题候选池（90 条，分层采样，可据此逐题核对新题依据）
- `var/reports/exam_v2_new.json`：86 道新题的中间稿（组卷前单答案版本）
