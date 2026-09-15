# Knowledge Unit Merger v1 — Pydantic Schema

```python
class MergeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    should_merge: bool
    reason: str = Field(min_length=4, max_length=500)
    merged_subject: str = Field(default="", max_length=200)
    merged_answer: str = Field(default="", max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
```

## Canonical ID 生成

```python
canonical_unit_id = "cu|" + sha256(f"{normalized_subject}|{unit_type}|{answer_hash}"[:32])
```

## Merge 规则

1. 先按 subject + unit_type + evidence_scope + temporal_compatible 做 deterministic buckets
2. 同 bucket 内用 question+answer embedding 相似度提案
3. 0.85+ 相似度 → LLM merge proposal
4. merge 后 confidence 取 members 最小值
5. conflict → review，不自动 current
6. 保留 member links + merge reason + supersedes/version lineage
