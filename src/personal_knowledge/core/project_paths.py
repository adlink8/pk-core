"""统一项目路径常量。

从 __file__ 派生，不依赖运行目录 (Path.cwd())。
所有脚本应从这里 import，而不是各自用 parents[N] 魔法数字。

Phase 20：目标树为 data/、var/、archive/。迁移窗口内优先已存在的
新路径，否则回落到 legacy integration/Agent/Google 路径（config fallback）。
"""
from __future__ import annotations
from pathlib import Path


def _prefer(*candidates: Path) -> Path:
    """Return the first existing candidate, else the preferred (first) target path."""
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


# 项目根（src/personal_knowledge/core/project_paths.py → parents[3] 是项目根）
# parents[0]=core  parents[1]=personal_knowledge  parents[2]=src  parents[3]=root
ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
PACKAGE_DIR = SRC_DIR / "personal_knowledge"

# Phase 20 target roots
DATA_DIR = ROOT / "data"
VAR_DIR = ROOT / "var"
ARCHIVE_DIR = ROOT / "archive"
DATA_RAW = DATA_DIR / "raw"
DATA_STAGING = DATA_DIR / "staging"
DATA_CANONICAL = DATA_DIR / "canonical"
DATA_IMPORTS = DATA_DIR / "imports"
VAR_DB = VAR_DIR / "db"
VAR_RUNTIME = VAR_DIR / "runtime"
VAR_REPORTS = VAR_DIR / "reports"
VAR_LOGS = VAR_DIR / "logs"
VAR_CACHE = VAR_DIR / "cache"
VAR_TMP = VAR_DIR / "tmp"

INTEGRATION_DIR = ROOT / "integration"
# Prefer Phase 20 var layout when present; keep legacy as fallback.
DB_DIR = _prefer(VAR_DB, INTEGRATION_DIR / "db")
SCRIPTS_DIR = INTEGRATION_DIR / "scripts"
ANALYSIS_DIR = _prefer(VAR_REPORTS / "analysis", INTEGRATION_DIR / "analysis")
AI_CONTEXT_DIR = ANALYSIS_DIR / "ai_context"
def _knowledge_eval_dir() -> Path:
    """Prefer the directory that actually holds private frozen suites when present."""
    assets = ROOT / "assets" / "evals" / "knowledge_units"
    legacy = INTEGRATION_DIR / "evals" / "knowledge_units"
    for candidate in (legacy, assets):
        if (candidate / "frozen_test_queries.private.jsonl").exists():
            return candidate
    return _prefer(assets, legacy)


KNOWLEDGE_EVAL_DIR = _knowledge_eval_dir()
KNOWLEDGE_ACTIVE_POINTER = DB_DIR / "knowledge_index_active.txt"
# 阶段一画像/报表（profile、capability、memory_report 等）— 与 ai_context 分离
STAGE1_PROFILE_DIR = ANALYSIS_DIR / "stage1_profile"

# 常用数据库路径
UNIFIED_DB = DB_DIR / "personal_system.sqlite"
EXTERNAL_CONTEXT_DB = VAR_DB / "external_context.sqlite"
CONV_GRAPH_DB = DB_DIR / "conversation_graph.duckdb"

# Phase 4: wiki 最上层统合层的派生库（版本/依赖/失效 + 落盘页面正文）
WIKI_PROJECTION_DB = VAR_DB / "personal_wiki_projection.sqlite"

# 可回收归档（历史模块软归档，不删除；见 _recycle/README.md）
RECYCLE_DIR = _prefer(ARCHIVE_DIR / "quarantine" / "_recycle", ROOT / "_recycle")
RECYCLE_CLEANUP_20260712 = RECYCLE_DIR / "2026-07-12_structure_cleanup"

# 源数据库
# - Google：主树仍保留 structured/raw（可选导入源）
# - GPT：整模块已归档到 _recycle（路径指向归档，兼容旧脚本）
# - Agent：仅保留 structured/db（会话/知识证据主库）；raw/analysis 已归档
GOOGLE_DB = _prefer(
    DATA_CANONICAL / "google" / "structured" / "db" / "google_data.sqlite",
    ROOT / "Google" / "structured" / "db" / "google_data.sqlite",
)
GPT_DB = (
    RECYCLE_CLEANUP_20260712 / "GPT" / "structured" / "db" / "chatgpt_data.db"
)
AGENT_DB = _prefer(
    DATA_CANONICAL / "agent" / "structured" / "db" / "agent_data.sqlite",
    DATA_CANONICAL / "agent" / "db" / "agent_data.sqlite",
    ROOT / "Agent" / "structured" / "db" / "agent_data.sqlite",
)

SOURCE_DBS = {
    "Google": GOOGLE_DB,
    "GPT": GPT_DB,
    "Agent": AGENT_DB,
}

# Phase 13.5：AgentView 会话源（只读 live WAL，位于用户主目录）
# 这是 AgentView daemon 正在写入的库，任何连接都必须 mode=ro + query_only。
# Phase 20: protected-external — NEVER relocate.
AGENTSVIEW_DB = Path.home() / ".agentsview" / "sessions.db"

# Phase 13.5：AgentView 规范化产物（安全快照，由 adapter 原子发布）
AGENT_STRUCTURED_DB_DIR = _prefer(
    DATA_CANONICAL / "agent" / "structured" / "db",
    DATA_CANONICAL / "agent" / "db",
    ROOT / "Agent" / "structured" / "db",
)
AGENTSVIEW_NORMALIZED_DB = AGENT_STRUCTURED_DB_DIR / "agentsview_normalized.sqlite"
AGENT_CONVERSATIONS_DB = AGENT_STRUCTURED_DB_DIR / "agent_conversations.sqlite"
