---
project: Personal Decision Intelligence System
milestone: v1.5 preplanned
status: not_active
---

# v1.5 Project Delta: Personal Knowledge Wiki Projection

## Core value

Give the user a fast, stable way to understand a recurring personal topic without repeatedly asking a model to reconstruct it from raw conversations.

## Non-negotiable architecture

```text
Canonical / KU / Personal State / External / Decision Authority
  -> deterministic, snapshot-bound Wiki projection
  -> directory and topic pages
  -> evidence drill-down
```

Deleting a Wiki projection must never destroy or change upstream facts, and rebuilding it must not require an LLM. A projected page and its summary must never be promoted back into KU, Chroma or any evidence authority.

## Scope

P0 topic types are only Project, Goal and Decision, with explicit stable keys. Fact, Observation, Inference, Forecast, Recommendation and Historical/Conflict stay visibly distinct. Page generation is read-only and deterministic.

## Out of scope

No static developer-wiki migration, general entity graph, freeform editor, LLM narrative, external crawling, provider call, external action, automatic promotion, fact editing or topic generation for every KU/entity.

