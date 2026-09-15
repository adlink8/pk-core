---
milestone: v1.0
status: complete
completed: 2026-07-12
phases: "01–16 (08 cancelled)"
---

# Milestone v1.0 Complete

## Goal achieved

把个人多源数字足迹建成：**可增量导入、可审计证据、可评测发布的知识单元 RAG**，并提供 CLI/REST/MCP 消费与 Google 轻量非对话层。

## Phase roll-up

| Phase | Status | Notes |
|-------|--------|-------|
| 01–07 | Complete | 导入、结构化、记忆/图谱实验、对话规范化 |
| 08 | **Cancelled** | 被 KU SSOT 取代（MEMX-01 wontfix） |
| 09–13.5 | Complete | 语义候选、关系、Apps/API、重构、AgentsView canonical |
| 14 | Complete | KU-01..08；active 30,012；增量 journal/watermark |
| 15 | Complete | 三层 SSOT、layered hybrid、telemetry、live holdout |
| 16 | Complete | normalized_events + light assertions lifecycle + RO API |

## Authoritative surfaces (v1.0)

| Layer | SSOT |
|-------|------|
| Dialogue | `agent_conversations.sqlite` (`cm|`) |
| Knowledge | `canonical_knowledge_units` + active KU Chroma |
| Non-dialogue | `personal_events` / Google `g|` + light assertions |

## Residual (not blocking v1.0)

- Holdout gold enrichment (paraphrase / Google PE ids)
- Real-source non-empty incremental paid extract when AgentsView changes
- Optional dual-pass extract (message + session window) — backlog only
- Test coverage gaps (non-main-path modules)

## Next milestone (optional)

See `ROADMAP.md` § Optional Next — quality & ops, not required for v1.0 ship.
