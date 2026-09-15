# Memory Mechanism Judge v1 Schema

## JSON Schema

```json
{
  "type": "object",
  "required": [
    "mechanism_step",
    "old_method",
    "new_method",
    "keep_from_old",
    "keep_from_new",
    "merged_method",
    "delete_or_deprecate",
    "required_tables",
    "required_prompts",
    "required_human_review",
    "evidence_refs",
    "source_files",
    "reason",
    "risk_flags",
    "prompt_version",
    "model",
    "temperature",
    "llm_status"
  ],
  "properties": {
    "mechanism_step": {
      "type": "string",
      "enum": [
        "input_selection",
        "unitization",
        "compression",
        "candidate_generation",
        "semantic_judgment",
        "evidence_gate",
        "storage_boundary",
        "promotion_policy",
        "decomplexity"
      ]
    },
    "old_method": {"type": "string"},
    "new_method": {"type": "string"},
    "keep_from_old": {"type": "string"},
    "keep_from_new": {"type": "string"},
    "merged_method": {"type": "string"},
    "delete_or_deprecate": {"type": "array", "items": {"type": "string"}},
    "required_tables": {"type": "array", "items": {"type": "string"}},
    "required_prompts": {"type": "array", "items": {"type": "string"}},
    "required_human_review": {"type": "boolean"},
    "evidence_refs": {"type": "array", "items": {"type": "string"}},
    "source_files": {"type": "array", "items": {"type": "string"}},
    "reason": {"type": "string"},
    "risk_flags": {"type": "array", "items": {"type": "string"}},
    "prompt_version": {"type": "string"},
    "model": {"type": "string"},
    "temperature": {"type": "number"},
    "llm_status": {"type": "string"}
  },
  "additionalProperties": false
}
```
