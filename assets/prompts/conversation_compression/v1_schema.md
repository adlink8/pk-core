# Output Schema — v1

**版本**: v1
**创建**: 2026-06-27
**配套**: v1_main.md

---

## JSON Schema

LLM 必须输出**单个 JSON 对象**(不要 markdown 代码块包裹,不要前后多余文字),结构如下:

```json
{
  "turn_no": 1,
  "context_brief": "一段 200-1200 字的中文叙述,讲清这个 turn:用户问了什么/想做什么→助手怎么推进→用了什么工具→得出什么结论。必须保留主干、分支和关键细节(路径/命令/错误栈/阈值)。可直接拼到后续 AI 的 prompt 里。",
  "main_topic": "不超过 20 字的中文话题标签,如 'MQTT 线性回归 TypeError 排查'",
  "key_details": [
    "逐字保留的细节 1,如:错误类型 TypeError: 'float' object is not subscriptable",
    "逐字保留的细节 2,如:关键函数名 client.loop_forever()",
    "逐字保留的细节 3,如:建议命令 python mqtt_client.py(用终端而非 Code Runner)"
  ],
  "branches": [
    "分支 1:中途出现的子问题/报错/替代方案,如:用户先试了 Code Runner,进程瞬间结束",
    "分支 2:助手给出的两种修法(payload 取下标 vs 校验类型)"
  ],
  "conclusion": "助手的最终结论或建议边界,如:建议先 print 原始消息确认格式再修;不确定 Code Runner 是否吞了异常",
  "preference_vs_oneoff": "判断这段里有没有'稳定偏好' vs '一次性指令',格式:'稳定偏好:无;一次性指令:用终端运行 mqtt scripts'。两者都没有就写'无'",
  "source_refs": ["raw_file:line_no", "raw_file:line_no"]
}
```

---

## 字段说明

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `turn_no` | int | ✓ | turn 序号(从 1 开始) |
| `context_brief` | string | ✓ | **核心产物**。高密度叙述,可直接注入后续 AI。长度按原 turn 长度分级(见 v1_main.md 压缩率目标) |
| `main_topic` | string | ✓ | ≤20 字话题标签,用于检索聚合 |
| `key_details` | string[] | ✓ | 逐字保留的细节清单(路径/命令/错误/函数名/阈值)。空 turn 至少留 1 条 |
| `branches` | string[] | ✓ | 分支因果(子问题/报错/替代方案)。无分支写 `[]` |
| `conclusion` | string | ✓ | 助手结论 + 不确定边界。无明确结论写"无明显结论" |
| `preference_vs_oneoff` | string | ✓ | 稳定偏好 vs 一次性指令的区分判断(本项目核心难点) |
| `source_refs` | string[] | ✓ | 直接回填输入的 source_refs,不得编造 |

---

## 解析容错(scripts侧)

LLM 输出可能不严格,`evaluate_conversation_prompt.py` 必须做以下容错:

1. 去掉可能的 markdown 代码块包裹(```json ... ```)。
2. 如果输出有前后多余文字,提取第一个 `{` 到最后一个 `}` 之间的内容。
3. 字段缺失时:`context_brief` 缺失直接判该样本失败;其他字段缺失用默认值(`[]`/`""`)但扣分。
4. `source_refs` 与输入不符(数量或内容)直接判 faithfulness=1。

---

## 反面示例(本项目不要的输出)

```json
{
  "context_brief": "用户问了PPT的事"
}
```
↑ 过度压缩,丢失所有细节和因果。

```json
{
  "topic": "用户对PPT制作有明确排版偏好"
}
```
↑ mem0 风格离散 claim,且把一次性任务误判为偏好。

```json
{
  "key_details": ["用户想要好看的PPT", "助手说能做"]
}
```
↑ 细节被改写成空话,丢失了"排版/动画/截图设置"等原始措辞。

---

## 变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1 | 2026-06-27 | 初版。强制 context_brief 叙述形态 + key_details 逐字保留 + preference_vs_oneoff 区分判断。 |
