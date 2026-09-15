# Knowledge Unit Extractor v1 — Pydantic Schema

## ExtractionResult

```python
class KnowledgeUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_type: str = Field(..., description="preference/habit/personal_fact/project_decision/capability/tool_usage")
    subject: str = Field(min_length=1, max_length=200, description="主题")
    question: str = Field(min_length=4, max_length=500, description="该单元能回答的问题")
    answer: str = Field(min_length=4, max_length=2000, description="知识单元内容")
    confidence: float = Field(ge=0.0, le=1.0, description="置信度")
    evidence_quote: str = Field(min_length=1, description="用户原文片段")
    lifecycle: str = Field(default="current", description="current/deprecated/superseded/conflict")

    @field_validator("unit_type")
    @classmethod
    def valid_unit_type(cls, v: str) -> str:
        allowed = {"preference", "habit", "personal_fact", "project_decision", "capability", "tool_usage"}
        if v not in allowed:
            raise ValueError(f"unit_type must be one of {allowed}")
        return v

    @field_validator("evidence_quote")
    @classmethod
    def non_empty_evidence(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("evidence_quote must not be empty")
        return v.strip()


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    units: list[KnowledgeUnit] = Field(default_factory=list)
    abstain: bool = Field(default=False, description="无足够证据，拒绝抽取")
    abstain_reason: str = Field(default="", description="拒绝原因")
```

## 验证规则

1. `extra="forbid"` — 不允许任何 schema 外字段
2. `unit_type` 必须在 6 种允许值内
3. `evidence_quote` 非空（至少 1 字符）
4. `confidence` 在 [0.0, 1.0] 范围内
5. `question` 和 `answer` 有最小长度要求（4 字符）
6. 验证失败最多重试一次；仍失败则记录失败并计入 abort rate
