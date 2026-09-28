# exam_v3 对比报告（2026-09-28，考卷侧工作流）

**结论**：v3 = 173 题兄弟补标 + 11 道新题型，共 184 题，审计 0 ERROR。同环境纯向量口径：v2 基线 153/173（MRR 0.922）→ v3 single 档 156/179（MRR 0.914）；补标对 173 题的 rank 改动为 **0，翻正 0 题**；新题里 B 类时间限定 4/4 全部进 Hit@3，A 类聚合 partial-recall@5 均值 0.530。扩题目标 30 实产 11，**PARTIAL**（原因见下）。

## 1. 基线与工具改动验证

| 项 | 数字 | 说明 |
|---|---|---|
| v2 基线复现（`--no-judge`） | Hit@1=153/173，Hit@3=165，Hit@5=166，MRR=0.922 | 与昨晚数字逐位一致，证明 eval_exam.py 加开关后默认向量行为零漂移 |
| v3 single 档 | Hit@1=156/179 (87.2%)，Hit@3=170，Hit@5=171，MRR=0.914 | 新增 6 道 single 新题贡献 H@1 +3、H@5 +5 |
| v3 aggregate 档 | n=5，partial-recall@5 均值=0.530，全覆盖 0/5 | 单独汇报，未混入 single 指标 |
| 垃圾块 | 3/179 题 top5 含系统上下文垃圾 (1%) | 与 v2 持平 |

eval_exam.py 改动（最小 diff，注释已写为何/怎么）：`--no-judge` 跳过 `import query` 与全部 GPU 调用；`type=aggregate` 题按 partial recall@5（top5 覆盖 answer_set 的不同会话数/answer_set 大小）单独记分，无 type 字段默认 single 行为不变。

## 2. 兄弟补标（Task 1）

- 定稿规则全量扫描 1511 会话：173 题共命中 **8 处**候选 append（id 规则 6、content 规则 2），上限 gold+3、与 v2 已有兄弟去重后写入。
- 规则实现反测：v2 已标注的 73 对兄弟中仅 20 对满足定稿规则；抽查 5 对不满足者，其标准化引语共享数=0——证实 v2 旧标签本就是草案判据（主题相似）所标，与定稿规则无关。按"不得修改既有前缀"约束，v2 旧标签原样保留。
- **人工核验：8 处全检（不足抽检 10，如实全查）**，逐对读双方 theme/asks/quotes：
  - **保留 3**：题47 L1/L2 查重（legacy 镜像 asks 直含"交叉查重"，答案等价）、题78 Phase 61 反思闭环（同家族内容 twin，asks 逐字一致、3 条引语共享）、题155 cockpit 前端（kimi main 摘要 did 含 Phase 36-40 实现）。
  - **剔除 5**：题46、题155+agent-0、题157+main、题157+agent-10（kimi 上游 uuid 碰撞：legacy 整包导入与分支片段共享 session uuid，被 append 方摘要不含题面答案，且 cockpit 高频词有制造假命中风险）；题172（Target D 罐头话术簇：共享 3 条进度核算引语但不含题眼 Phase 27 F-01 控制排序修复）。
- **规则参数未改，改用题面级核验剔除**。理由：5 处误连与 2 处正连同簇同信号（同 uuid/同引语），任何会话对级参数都无法同时分开两者，只有"被 append 会话摘要是否含题面答案"这一题面级判据可分。定稿规则在会话簇层面命中全部正确，误连发生在题面等价层。全程留痕于 `var/reports/exam_v3_brother_log.json`（verdict+理由）。
- **效果量化：补标后 173 题 rank 变化 0 处，v2 的 8 道 miss（向量#4/6/7/13/17/29/40/51）翻正 0 题**。原因：8 道 miss 均不在补标题上；3 处保留 append 的目标题原本已命中且排名不变。昨晚 v2 的兄弟标注已覆盖可检索的兄弟，定稿规则的增量本就很小。

## 3. 新题型扩题（Task 3）：11/30，PARTIAL

依据全部写入 `docs/retrieval/exam_v3_pool.json`（每题素材会话、did 摘录证据、时间核查记录，可复现）。逐会话核对 theme/asks/did 后定稿：

| 类型 | 产出 | 纯向量成绩 |
|---|---|---|
| A 跨会话聚合（type=aggregate） | 5 题：多服务器部署史 / 思必驰全套准备 / watchdog 三连 / novel-mind CI 门禁 / petcare 服务器线 | partial-recall@5 = 0.40(2/5)、0.50(3/6)、0.67(2/3)、0.75(3/4)、0.33(2/6)，均值 0.530，全覆盖 0/5 |
| B 时间限定 | 4 题：07-12 GSD 06-03、06-06 后端审计、09-07 智联/BOSS 双版简历、07-17 CONCERNS 体检 | **4/4 全进 Hit@3**（3 道 rank1 + 1 道 rank2） |
| C 多来源多答案（type=single） | 2 题：内网远程访问方案、英语学习规划 | C1 rank=2 命中；C2 rank=7 未进前三 |

- 时间真实性：B 类时间全部取自 `var/db/conversation_fts.sqlite` 的 `sessions_meta.started_at`（权威源，覆盖向量库 1511 会话 100%），并做了全语料窗口唯一性核查（如 09-07 当天简历相关仅 2 场、07-12 当天 frozen fixture 仅 1 场），核查记录在 pool 的 time_check 字段。无编造时间。
- 去重：11 题的 gold 会话与 v2 既有 answer_set 解析集（278 会话）程序化校验零重叠；主题与 v2 173 题不撞。
- **PARTIAL 声明**：目标 30（A/B/C 各≤10）实产 11。缺口主因：C 类"同主题不同会话各自完整回答"在语料中稀缺——asks 相似度全量挖掘（295 对候选）命中的几乎全是跨通道镜像对和 GSD 罐头话术簇（恰为已知坑，不能当多来源）；A/B 其余候选因与 v2 主题撞车（简历/C盘/novel-mind 分析线）或时间窗不唯一被否。质量优先，宁缺毋滥。
- 扩题中发现并规避的坑：`019f1340` 前缀下有两个不同会话（远程部署验证 / deploy 脚本健壮性修复），新题 answer_set 全部使用完整 canonical_session_id 消歧；06-29 zcode"宠物医院开发调试"会话无部署内容，从 A5 剔除。

## 4. 审计（Task 4）

`exam_audit.py` 在 v3 上：条目 185 = meta 1 + 题目 184；前缀 321 个，零匹配 0、歧义 0、agent 家族不符 0；重复题 0；**ERROR 0 / WARN 0（退出码 0）**。本文件为所有权内小改：新增 type 字段合法性校验（只允许 aggregate/缺省，拼错会被 eval 当 single 混入）与 aggregate answer_set 规模 3-6 校验，均带注释。

## 5. 改动文件清单

| 文件 | 动作 |
|---|---|
| `src/personal_knowledge/retrieval/vector/eval_exam.py` | 修改：--no-judge、aggregate 题型（默认行为与昨晚一致，已实测） |
| `src/personal_knowledge/retrieval/vector/exam_audit.py` | 修改：type/aggregate 校验（注释标明） |
| `docs/retrieval/exam_v3.json` | 新建：meta + 184 题 |
| `docs/retrieval/exam_v3_pool.json` | 新建：11 题依据 |
| `docs/retrieval/exam_v3_report.md` | 新建：本报告 |
| `var/reports/exam_v3_brother_scan.py` | 新建：定稿规则扫描（含核验裁定表，可复现） |
| `var/reports/exam_v3_brother_log.json` | 新建：8 命中 + 5 剔除留痕 |
| `var/reports/exam_v3_base173.json`、`exam_v3_covered.json`、`exam_v3_build_new.py` | 新建：补标中间产物与新题构建（可复现） |
| `var/reports/exam_v3_v2baseline_rerun.json`、`exam_v3_vec.json`、`exam_v3_audit.json` | 新建：真实运行输出 |

未触碰：query.py、twin_map.py、bench_serving.py、exam_v2.json、var/db/ 既有文件；未执行任何 git commit/push；Jev 裁判未运行（GPU 让给并行工作流）。

## 6. PARTIAL / 诚实声明

1. **扩题 11/30，PARTIAL**——C 类素材结构性稀缺（镜像对与罐头簇污染），已说明；不凑数。
2. **补标翻正 0 题**——任务预期"昨晚因兄弟未标而 miss 的题翻正几个"，实测为 0：8 道 miss 均无兄弟可补；补标对现有 173 题 rank 零改动。这意味着 v2 的可检索兄弟昨晚已标尽，本轮增量只在标签完备性而非检索指标。
3. 抽验 10 处的要求以"8 处全检"完成（命中总数只有 8）。
4. 规则参数未按"误连即改参数"字面执行：分析表明簇内信号不可分，改用题面级剔除并留痕；若坚持参数化，需给规则引入题面上下文，超出"定稿勿改"边界。
