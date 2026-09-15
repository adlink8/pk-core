# Evaluation Rubric — v1

**版本**: v1
**创建**: 2026-06-27
**配套**: v1_main.md / v1_schema.md
**用途**: 评测 `evaluate_conversation_prompt.py` 的 LLM 压缩输出质量。

---

## 评分总览

7 个维度,每维 1-5 分。`gate_passed` 由阈值逻辑判定(见末尾)。

| # | 维度 | 权重 | 核心问题 |
| --- | --- | --- | --- |
| 1 | trunk_preservation | 高 | 主干(用户诉求 + 助手推进路径)是否完整 |
| 2 | branch_preservation | 高 | 分支(子问题/报错/替代方案)是否保留 |
| 3 | detail_retention | 高 | 路径/命令/错误栈/函数名/阈值等细节是否逐字保留 |
| 4 | compression_ratio | 中 | 是否足够短但不过度压缩 |
| 5 | retrieval_usefulness | 高 | 摘要能否支持后续检索判断 |
| 6 | faithfulness | **关键** | 是否引入原文没有的结论 |
| 7 | context_brief_quality | 中 | 短上下文是否可直接注入后续 AI |

---

## 评分标准(每维 1-5 分)

### 1. trunk_preservation(主干完整性)
- **5**:用户诉求 + 助手整体推进路径完整,能看出"先做了什么、再做了什么、最终结论"。
- **4**:主干完整,个别环节略简但可推断。
- **3**:主干基本在,但某个关键推进步骤缺失。
- **2**:只能看出大方向,推进路径断裂。
- **1**:主干丢失,看不出这个 turn 在解决什么问题。

### 2. branch_preservation(分支保留)
- **5**:所有分支(子问题/报错/替代方案/被否定的思路)都保留,且与主干关联清晰。
- **4**:主要分支保留,次要分支省略但合理。
- **3**:只保留 1 个分支,其他丢失。
- **2**:分支几乎全丢,只剩线性主干。
- **1**:无分支记录(原 turn 明明有多分支时)。
- **注**:原 turn 确实无分支(如纯短问答)时,此项默认 5(不扣分),但需在评语标注"原 turn 无分支"。

### 3. detail_retention(细节逐字保留)★
- **5**:路径/命令/错误栈关键行/函数名/阈值/版本号全部逐字保留,无改写。
- **4**:大部分细节在,个别细节被概括(如端口 22 写成"默认端口")。
- **3**:关键细节部分丢失(如只留命令名不留参数)。
- **2**:细节大量丢失或被改写成空话。
- **1**:几乎无细节,只剩泛泛叙述。
- **注**:对比基准 = 评测样本集里 `turn_text` 的原文。

### 4. compression_ratio(压缩率)
- **5**:压缩到目标区间(见 v1_main.md),既不啰嗦也不过度压缩。
- **4**:略长或略短,但不影响可读。
- **3**:偏长(冗余)或偏短(信息密度仍可)。
- **2**:严重过短(丢失可读性)或严重过长(没压缩)。
- **1**:基本没压缩(与原文等长)或压成一句话。
- **计算**:`ratio = len(context_brief) / len(turn_text)`。
  - 目标区间:短对话(<500字) ratio 0.6-0.85;中等(500-2000) 0.4-0.7;
    长对话(2000-5000) 0.3-0.5;超长(>5000) 0.2-0.4。
  - 超出区间酌情扣分。

### 5. retrieval_usefulness(检索可用性)
- **5**:看到 context_brief 就能判断"这个 turn 是否与我的查询相关",且 main_topic 准确。
- **4**:可判断相关性,topic 略宽泛。
- **3**:勉强能判断,但需要细读。
- **2**:摘要太泛,无法区分这个 turn 和其他 turn。
- **1**:摘要空洞,检索时会被噪音淹没。

### 6. faithfulness(忠实度)★★★ **关键维度**
- **5**:完全忠于原文,无任何引入、无推断、无"常识补充"。
- **4**:忠于原文,个别措辞润色但不改变含义。
- **3**:有轻微推断但标注了"可能/推测"。
- **2**:引入原文没有的结论或因果,未标注。
- **1**:严重幻觉(编造路径/命令/结论)。
- **★ gate 硬门槛:此项 <4 直接判该样本 gate 失败,不论其他维度多高。**

### 7. context_brief_quality(可注入性)
- **5**:context_brief 是连贯叙述,可直接拼到后续 AI 的 prompt 里,无需改写。
- **4**:基本可用,个别句子需润色。
- **3**:能看懂,但结构松散,注入前需整理。
- **2**:更像 bullet list 堆砌,不像叙述。
- **1**:形态错误(如离散 claim、表格),完全不可直接注入。

---

## preference_vs_oneoff 专项检查(本项目核心难点)

除 7 维评分外,对每个样本额外检查 `preference_vs_oneoff` 字段:

- **正确**:能区分稳定偏好 vs 一次性指令,标注准确。
- **错误(严重)**:把一次性指令误判为稳定偏好(如"重构PPT"→"用户喜欢重构PPT")。
- **错误(轻微)**:区分逻辑在但标注措辞含糊。

误判为偏好直接扣 branch_preservation 和 faithfulness 各 1 分,并在评语标注 `oneoff_as_preference=true`。

---

## gate 判定逻辑(evaluate_conversation_prompt.py 实现)

```python
# 单样本 gate
sample_gate_passed = (
    avg_score >= 4.0                          # 平均分 >= 4/5
    and scores["faithfulness"] >= 4           # 忠实度硬门槛
    and not oneoff_as_preference              # 未把一次性任务误判为偏好
    and len(source_refs_matching) >= 1        # 至少 1 个 source_refs 与输入匹配
)

# 整体 gate(全样本集)
overall_gate_passed = (
    pass_rate >= 0.85                         # >= 85% 样本通过单样本 gate
    and avg_of_all_faithfulness >= 4.0        # 全样本忠实度均值 >= 4
    and source_refs_coverage >= 0.9           # >= 90% 样本有匹配的 source_refs
)
```

**输出**:评测报告必须显式输出 `gate_passed: true/false` + 失败原因清单。未通过时scripts退出码非 0。

---

## 评分方式

PLAN 要求"人工/半自动评分"。实现策略:

1. **自动可判维度**(`evaluate_conversation_prompt.py` 直接计算):
   - `compression_ratio`:按 ratio 公式。
   - `faithfulness` 的 source_refs 部分:比对输入输出。
   - `preference_vs_oneoff` 的 oneoff_as_preference:关键字启发式 + LLM 二次判断。
2. **需 LLM-as-judge 的维度**:用一个独立的 judge prompt(比 v1_main 更严格的评分提示词)对 `context_brief` vs `turn_text` 打分。
3. **人工复核**:评测报告输出后,人工抽查 2-3 个样本确认 judge 是否合理。

---

## 变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1 | 2026-06-27 | 初版。7 维评分 + gate 阈值 + preference_vs_oneoff 专项检查。faithfulness 设为硬门槛。 |
