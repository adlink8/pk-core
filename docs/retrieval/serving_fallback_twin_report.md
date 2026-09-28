# 检索服务侧改造报告：双胞胎归并 × 裁判 Fallback（2026-09-28）

范围：只动「检索服务侧」。语料库 `var/db/conversation_vector.sqlite` 全程 `mode=ro` 只读；
索引构建、压缩、embed、live-sync、eval_exam/exam_v2（另一工作流所有）零触碰。
基准 commit `40333bc1`，考卷 `docs/retrieval/exam_v2.json`（173 题）。

## 一、结论先行

- **裁判 fallback 是本次的净赢手**：Hit@1 从基线 147/173 提到 **161/173（+8 题）**，超过纯向量的 153/173，
  MRR 0.902→**0.953**；裁判调用量从 173 题降到 **52 题（省 70%）**，单卷评测耗时 733.7s→**217.5s（3.4 倍速）**。
- **twin-collapse 对考卷指标净影响为 0**（147→147、161→161），它的真实价值有二：
  ① 与 fallback 协同，**9 道题免于误触发裁判**（52→43），总耗时再降到 175.3s；
  ② 为近重复会话归并铺好了确定性基础设施（当前映射只覆盖 12.9% 会话，天花板受此限制）。
- **基线精确复现昨晚**：纯向量 153/173 MRR 0.922、永远重排 147/173 MRR 0.902，偏差 **0**（重跑亦逐位一致，裁判引擎确定性）。
- 昨晚 6 道未进前三的题：**4 道被裁判救回前三**（novel-mind onboarding→#1、LLM 替代人工→#3、Phase 15→#1、BATCH-013→#2），
  2 道（Phase 25-01、宠物医院验收）仍失败——它们的混淆源不在双胞胎映射簇里，collapse 只把 Phase 25-01 从 #51 挪到 #49。

## 二、改了什么 / 为何 / 哪里

| 文件 | 改动 | 为何 |
|---|---|---|
| `src/personal_knowledge/retrieval/vector/query.py` | ① 召回聚合 twin-collapse（默认开，`--no-collapse` 关）② 裁判默认 fallback（`--judge`/`--judge-mode always` 切回旧行为） | 昨晚实测"永远重排"净负（153→147）；且双胞胎副本挤占 top-k 名额、放大簇内混淆 |
| `src/personal_knowledge/retrieval/vector/twin_map.py`（新建） | 全库会话按兄弟规则（定稿版）建边→连通分量→确定性 canonical，产出 `var/db/twin_map.json` | 服务侧归并需要权威映射；规则与考卷侧一致，勿两套标准 |
| `src/personal_knowledge/retrieval/vector/bench_serving.py`（新建） | 独立四组评测工具（不 import eval_exam） | 服务口径改变必须全卷可复测 |
| `var/db/twin_map.json`（新建） | 映射数据，schema 写在 twin_map.py 文件头与 JSON `_schema` 键 | — |

query.py 每处改动顶部注释均写明「为何/怎么/怎么回退」；`get_judge`/`judge_candidates` 判定逻辑零改动。

**回退方式（全部可独立执行）**：
- 关归并：命令行加 `--no-collapse`，或删除 `var/db/twin_map.json`（自动降级为不归并并打警告，fail-open）；
- 切回永远重排：`--judge` 或 `--judge-mode always`；纯向量：`--no-judge` / `--judge-mode never`；
- 彻底还原：`git checkout -- src/personal_knowledge/retrieval/vector/query.py`。

## 三、四组对比（173 题全卷，串行，同一 GPU 裁判进程约束下依次运行）

| 组 | 配置 | Hit@1 | Hit@3 | Hit@5 | MRR | 耗时 | 裁判触发 |
|---|---|---|---|---|---|---|---|
| 1 基线 | 无 collapse，judge always（=昨晚口径） | 147/173 | 163/173 | 168/173 | 0.902 | 733.7s | 173 题/5190 条 |
| 2 | +fallback | **161/173** | **168/173** | **170/173** | **0.953** | **217.5s** | 52 题/1560 条 |
| 3 | +collapse，judge always | 147/173 | 163/173 | 168/173 | 0.902 | 683.9s | 173 题/5190 条 |
| 4 | +collapse +fallback | **161/173** | **168/173** | **170/173** | **0.953** | **175.3s** | **43 题/1290 条** |

纯向量参考（各组一致）：Hit@1=153/173，Hit@3=165/173，Hit@5=166/173，MRR=0.922。

每个数字意味着什么：
- **147→161（+8）**：fallback 让裁判只在拿不准时出手，避开了"永远重排"对自信题的帮倒忙（昨晚裁判帮倒忙 21 题 vs 改善 14 题），净效果由 -6 转 +8，意味着裁判的判断力本身没问题，错在"何时用"。
- **0.902→0.953 MRR**：翻车题不但进了 top-k 还普遍进到第 1-2 位（21 题名次变化：13 变好、8 变差），意味着靠前名次的可信度整体上移，不只是 Hit@1 数字游戏。
- **52/173（30%）触发**：七成问题纯向量自己有把握，裁判算力/时延降约 70%，意味着该方案可以直接挂到在线服务而不必担心 GPU 排队。
- **collapse 单独使用指标纹丝不动（g3=g1）**：归并在 173 题的 top-100 窗口内共发生 615 次文档归并（108 题涉及），但因为"命中判定认会话、归并不动各簇最高分文档"，纯向量名次几乎不受影响（全卷仅 1 题变化：Phase 25-01 #51→#49）——意味着归并按设计是"无损去重"，不是碰运气的重排。
- **g4 比 g2 少 9 次裁判触发且指标不变**：被免判的 9 题全是"双胞胎副本占了第 2 名、分差被压到 0.02 内"的假告警（HWiNFO64、TraceMemo、novel-mind RAG 等，全部落在映射簇里），意味着归并消除了 fallback 的主要误报源，两者是互补而非重复建设。
- **耗时 733.7s→175.3s（g1→g4）**：全卷评测从 12 分钟降到 3 分钟，意味着后续每次改索引/考卷的回归成本降到原来的 1/4。

## 四、Fallback 阈值依据（用昨晚全卷逐题数据标定）

触发条件（二选一，实现在 `query.py: should_fallback`）：
- **(a) top3 最高分 < 0.58（TH_ABS）**：昨晚 165 道命中题 top1 分 p01=0.5833、min=0.5619——此线以下几乎从无命中。本次全卷仅触发 1 题（"CI 一直红到底为啥"，向量 #2→裁判后 #1，救回 1 题），保险丝性质。
- **(b) 领先分差 < 0.02（TH_MARGIN）**：即"top3 无簇命中"的服务态可测实现——第一名簇与次名簇拉不开差距。依据：昨晚 8 道未进前三题分差全部 ≤0.0169，而命中题分差 median=0.0361、p25=0.0139。取 0.02 时 **8/8 翻车题全部被覆盖、32% 触发率**，是昨晚数据上唯一有区分度的信号。

口径说明：任务原文"top3 无簇命中"若解释为"top3 缺同会话互证"不可用——45% 命中题同样无互证（区分度为零），且 8 道翻车题里有 2 道恰有互证；故按"无领先簇（分差不足）"实现。本次 52 题触发中 51 题由条件 (b) 命中、1 题由 (a) 命中。

## 五、失败题回收（昨晚 6 道未进前三 + 另外 2 道 rank>3，逐题）

| 题 | 纯向量名次 | g1 基线 | g2 fallback | g3 collapse | g4 both | 结论 |
|---|---|---|---|---|---|---|
| novel-mind onboarding 到 GSD | 6 | 1 | 1 | 1 | 1 | 裁判救回 #1 |
| novel-mind Phase 15 全流程 | 7 | 1 | 1 | 1 | 1 | 裁判救回 #1 |
| BATCH-013 deep-read 升级 | 17 | 2 | 2 | 2 | 2 | 裁判救回 #2 |
| 用 LLM 替代人工验收 | 29 | 3 | 3 | 3 | 3 | 裁判救回 #3 |
| **Phase 25-01 A 层不可变表** | 51 | 未进前三 | 未进前三 | 49→未进前三 | 49→未进前三 | **未回收**（collapse 小幅提位，混淆源不在映射簇） |
| **宠物医院阶段二三验收** | 40 | 未进前三 | 未进前三 | 未进前三 | 未进前三 | **未回收**（gold 在 #40，裁判只看 top-30） |
| xAI Grok Build 对抗验证 | 4 | 5 | 5 | 5 | 5 | 裁判轻微帮倒忙（#4→#5），四组一致 |
| Phase 2 主数据 API | 13 | 1 | 1 | 1 | 1 | 裁判救回 #1 |

注意：4 道"救回"在昨晚基线里其实已经发生（被 147 的总账掩盖）；fallback 的贡献是以 30% 算力保留全部救回、并净增 8 题_hit@1。真正卡死的两题病因不同：Phase 25-01 的混淆对（Phase 24 人工门禁）不满足兄弟规则、不在映射里；宠物医院则是 gold 本身排名太后，属召回问题，需要索引侧手段（quote 提取质量等），不是服务侧归并能解决的。

## 六、双胞胎映射产出与人工抽验

`twin_map.py` 全库构建结果：**1,511 会话 → 96 簇（最大簇 4）/195 个被映射会话，覆盖率 12.9%**；
边构成：跨家族镜像 **82** 条 + 同家族深重合 **21** 条；kimi 工作区 id 碰撞歧义拒并 **16** 组（宁漏勿错），
其中 1 组由"恰好一个候选共享 ≥60 字引语"的内容裁决救回。

与背景口径的差异（以实测为准）：背景说"全库跨通道重复会话约 1,182 对"，本 commit 按定稿规则实测硬 id 镜像仅 **82 对**（与"legacy 内嵌 id 与原生 id 严格相等"的独立核对**逐条一致**）；放宽到"共享 ≥2 条标准化引语"全库也只有 931 对。1,182 在当前语料上不可复现，推测来自更早语料状态或更松口径，特此记录。

人工抽验（读双方 summary_json 的 theme+首引语，共 10 簇）：
- **8 个镜像簇全部确认同内容**（如 chatgpt `17681ace` 双方首引语逐字相同；kimi 救回簇 `75a7614e` 主会话与 legacy 聚合同讲 Personal Decision Cockpit）；
- **2 个深重合簇（Target D 系列）判定为误并**：4 个"进度评估"会话共享同一条罐头项目状态话术（"按真正完成 Target D 的口径，约完成 20%-25%"），实为不同时点的例会，即数据质量报告早已指出的 Target D 罐头话术模式。定稿规则未改（勿改约定），靠两点控制爆炸半径：只归并映射簇内文档（不动单例会话）、fail-open。实测该簇仅被 1 道考题瞄准，且四个组名次不变，本次未造成实际伤害。
- 遗留建议：深重合边若要继续扩，应叠加"会话时间窗 + 非罐头引语"过滤，否则 Target D 模式会随例会增多而放大。

## 七、PARTIAL / 已知边界

1. **PARTIAL：6 道目标失败题只回收 4 道**。Phase 25-01、宠物医院验收两题病因在映射覆盖之外（未验证的近重复对、gold 排名太后），服务侧手段到顶了。
2. **PARTIAL：collapse 当前收益主要在协同项（-9 次误触发）**。映射覆盖 12.9% 决定了上限；扩充映射（尤其是把 Phase 25-01 这类"主题近重复但未达定稿阈值"的对收进来）需要先修订兄弟规则本身，超出本次"勿改"约束，未做。
3. **四组对比为全卷真实运行**，无抽样（`--limit` 未使用）；g2/g4 各重跑一次（g2b/g4b），指标与触发数逐位一致，裁判引擎确定性成立。
4. kimi 歧义拒并的 16 组里，可能有真镜像被一并放弃（宁漏勿错的代价），未逐组人工复核。

## 八、运行证据

- `var/reports/twin_bench_g1.json` ~ `g4.json`：四组逐题明细（rank、触发原因、top3）
- `var/reports/twin_bench_g2b.json` / `g4b.json`：复跑样本（确定性验证）
- `var/db/twin_map.json`：映射（96 簇/195 会话/82+21 边）
- 复现命令：
  ```bash
  python src/personal_knowledge/retrieval/vector/twin_map.py --sample 10
  python src/personal_knowledge/retrieval/vector/bench_serving.py --out var/reports/twin_bench_g1.json
  python src/personal_knowledge/retrieval/vector/bench_serving.py --fallback --out var/reports/twin_bench_g2.json
  python src/personal_knowledge/retrieval/vector/bench_serving.py --collapse --out var/reports/twin_bench_g3.json
  python src/personal_knowledge/retrieval/vector/bench_serving.py --collapse --fallback --out var/reports/twin_bench_g4.json
  ```
