# Personal Knowledge System Wiki

## 这个 Wiki 是什么

一份给开发者和运维人员读的项目说明书。不讲概念，讲实际怎么跑、怎么查、怎么改。

> **不要和"个人主题 Wiki"混淆：** 本 `docs/wiki/` 是开发/运维文档，解释系统怎么跑、怎么排障。
> 仓库里还规划了另一个概念上的"Wiki"——**Personal Knowledge Wiki Projection**（面向个人主题、目标、决策的只读投影页面），
> 见 `.planning/future-milestones/v1.5-personal-knowledge-wiki-projection/SPEC.md`。
> 截至本次审计，v1.5 仍是 `candidate_not_activated`（未激活候选），代码库里**没有**任何 `wiki`/`topic_page`/`backlink` 路由或实现。
> 本页所有内容仅描述当前这份静态开发/运维文档，与 v1.5 候选规格无关。

## 快速索引

| 如果我想… | 去这里 |
|----------|--------|
| 快速理解这个项目是干什么的 | [项目总览](01-overview.md) |
| 理解整体架构、路线图演进和分类系统 | [架构总览与路线图](11-architecture-roadmap.md) |
| 部署/启动/日常操作 | [CLI 命令速查](09-cli-reference.md) |
| 理解搜索为什么不灵 | [搜索机制](05-search-mechanism.md) |
| 理解数据是怎么处理的 | [数据加工管线](04-data-pipeline.md) |
| 理解合并层压缩做了什么 | [数据结构化与压缩](06-merge-compression.md) |
| 哪些东西不能碰、为什么 | [数据治理](08-data-governance.md) |
| 目录/文件太多不知道从哪看起 | [目录结构](02-directory-structure.md) |
| 想加新 API / 新命令 | [工具层架构](03-tool-layer.md) |
| 检索结果不准，怀疑是门禁拦了 | [门禁体系](07-gate-system.md) |
| 常见问题和踩坑记录 | [常见问题](10-faq.md) |

## 快速入口

```powershell
# 日常操作
pk-sync conversations --write        # 同步本地对话
pk-ku inspect                        # 查看是否有新的知识增量
pk-ku doctor                         # 系统健康检查

# 搜索
rag-search "Python 调试" --top-k 5

# 启动服务群
pwsh -NoProfile -ExecutionPolicy Bypass -File apps\personal_data_chatgpt\scripts\start-services.ps1

# 合规审计
python -m personal_knowledge.governance.preflight --ci
```
