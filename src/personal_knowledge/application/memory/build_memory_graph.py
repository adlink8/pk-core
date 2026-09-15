"""记忆图谱构建脚本(里程碑4:记忆间关系 + networkx 成图)。

把 194 条记忆对象连成图——孤立的节点变关系网络,这才是"知识图谱"。

5种跨类关系(基于真实数据设计,非乱连):
  1. uses_tool      capability → tooling      (能力用什么工具)
  2. relates_to_topic project/capability → preference (项目/能力关联什么主题)
  3. enables        fact → capability          (技术环境支撑什么能力)
  4. embodies       habit → fact/tooling       (习惯体现什么工作方式)
  5. same_subject   跨类同名记忆               (两个记忆说同一东西)

minor capability(142条)不入图——只出现1次,无连接价值,会让图稀疏。
图只放 core/occasional capability + 其他5类全部。

运行: python integration\\scripts\\build_memory_graph.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from personal_knowledge.core.common import write_json, ensure_dirs

import networkx as nx


# === 配置 ===
ROOT = Path(__file__).resolve().parents[4]
UNIFIED_DB = ROOT / "integration" / "db" / "personal_system.sqlite"
from personal_knowledge.core.project_paths import STAGE1_PROFILE_DIR as ANALYSIS_DIR  # stage1 reports

RULES_VERSION = "graph-v1"

# 入图时排除的 capability 子类型(minor 太碎不入图)
CAP_EXCLUDE = {"minor_capability"}

# === 主题映射规则(项目/能力 → 关注主题)===
# 基于数据语义,人工定义哪些项目/能力关联哪个 preference 主题
TOPIC_MAPPING = {
    # 项目 → 主题
    "Myproject": "开发技术栈",
    "Python": "开发技术栈",
    "Obsidian": "开发技术栈",
    "windows-powershell-escaping": "开发技术栈",
    "课程": "职业/求职",
    # 能力 → 主题
    "CLI工具搭建(cli-anything方法论)": "开发技术栈",
    "GSD项目管理": "开发技术栈",
    "find-skills": "开发技术栈",
    "storage-analyzer": "开发技术栈",
    "CLI Hub": "开发技术栈",
    "anki": "英语/语言学习",
}

# === 环境支撑能力映射(fact env → capability)===
ENABLES_MAPPING = {
    "Python": "CLI工具搭建(cli-anything方法论)",
    "GitHub": "GSD项目管理",
}

# === 习惯体现映射(habit → fact/tooling)===
EMBODIES_MAPPING = {
    "搜→问→做闭环": "执行优先协作",  # → fact
    "搜→问模式": "Codex",            # → tooling
}


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_items (
            memory_id TEXT PRIMARY KEY, memory_type TEXT NOT NULL,
            memory_subtype TEXT NOT NULL, subject TEXT NOT NULL,
            description TEXT NOT NULL, confidence REAL DEFAULT 0.5,
            evidence_count INTEGER DEFAULT 0, metadata TEXT, created_at TEXT NOT NULL,
            UNIQUE(memory_type, memory_subtype, subject)
        );
        CREATE TABLE IF NOT EXISTS memory_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_memory_id TEXT NOT NULL, to_memory_id TEXT NOT NULL,
            relation TEXT NOT NULL, strength REAL DEFAULT 1.0,
            FOREIGN KEY (from_memory_id) REFERENCES memory_items(memory_id) ON DELETE CASCADE,
            FOREIGN KEY (to_memory_id) REFERENCES memory_items(memory_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_memory_relations_from ON memory_relations(from_memory_id);
        """
    )
    con.commit()


def reset_relations(con: sqlite3.Connection) -> None:
    """幂等:清空旧关系。"""
    con.execute("DELETE FROM memory_relations")
    con.commit()


def load_memories(con: sqlite3.Connection) -> dict:
    """加载所有记忆(排除 minor capability)。返回 {memory_id: {...}}。"""
    memories = {}
    rows = con.execute(
        "SELECT memory_id, memory_type, memory_subtype, subject, "
        "description, confidence, metadata FROM memory_items"
    ).fetchall()
    for r in rows:
        # 排除 minor capability
        if r[1] == "capability" and r[2] in CAP_EXCLUDE:
            continue
        md = json.loads(r[6]) if r[6] else {}
        memories[r[0]] = {
            "memory_id": r[0], "memory_type": r[1], "memory_subtype": r[2],
            "subject": r[3], "description": r[4], "confidence": r[5],
            "metadata": md,
        }
    return memories


def build_subject_index(memories: dict) -> dict:
    """建 subject → [memory_id] 索引(用于同名查找)。"""
    idx = {}
    for mid, m in memories.items():
        key = m["subject"].lower().strip()
        idx.setdefault(key, []).append(mid)
    return idx


def build_relations(memories: dict) -> list:
    """建 5 种关系。返回 [(from_id, to_id, relation, strength), ...]。"""
    subj_idx = build_subject_index(memories)
    relations = []

    def find_by_subject(subject, mtype=None):
        """按 subject 找记忆,可选限定类型。"""
        key = subject.lower().strip()
        cands = subj_idx.get(key, [])
        if mtype:
            cands = [c for c in cands if memories[c]["memory_type"] == mtype]
        return cands

    # === 关系1: uses_tool (capability → tooling) ===
    # 从 capability metadata.top_services 连到 tooling
    for mid, m in memories.items():
        if m["memory_type"] != "capability":
            continue
        top_svcs = m["metadata"].get("top_services", {})
        for svc, cnt in top_svcs.items():
            # 找 tooling 里 subject 匹配的(支持 Documents:Codex → Codex 这种)
            tool_ids = find_by_subject(svc, "tooling")
            if not tool_ids:
                # 尝试部分匹配(Documents:Codex 包含 Codex)
                for t_mid, t_m in memories.items():
                    if t_m["memory_type"] == "tooling" and (
                        svc == t_m["subject"] or svc.endswith(":" + t_m["subject"])
                        or t_m["subject"] in svc
                    ):
                        tool_ids.append(t_mid)
            for tid in tool_ids:
                strength = min(1.0, cnt / 50)  # 次数越多边越强
                relations.append((mid, tid, "uses_tool", round(strength, 2)))

    # === 关系2: relates_to_topic (project/capability → preference) ===
    for mid, m in memories.items():
        if m["memory_type"] not in ("project", "capability"):
            continue
        topic = TOPIC_MAPPING.get(m["subject"])
        if not topic:
            continue
        pref_ids = find_by_subject(topic, "preference")
        for pid in pref_ids:
            relations.append((mid, pid, "relates_to_topic", 0.8))

    # === 关系3: enables (fact env → capability) ===
    for mid, m in memories.items():
        if m["memory_type"] != "fact" or m["memory_subtype"] != "env_signal":
            continue
        cap_subject = ENABLES_MAPPING.get(m["subject"])
        if not cap_subject:
            continue
        cap_ids = find_by_subject(cap_subject, "capability")
        for cid in cap_ids:
            relations.append((mid, cid, "enables", 0.7))

    # === 关系4: embodies (habit → fact/tooling) ===
    for mid, m in memories.items():
        if m["memory_type"] != "habit":
            continue
        target = EMBODIES_MAPPING.get(m["subject"])
        if not target:
            continue
        # 先找 fact,再找 tooling
        for mtype in ("fact", "tooling"):
            tids = find_by_subject(target, mtype)
            for tid in tids:
                relations.append((mid, tid, "embodies", 0.6))

    # === 关系5: same_subject (跨类同名) ===
    # 找 subject 相同但 type 不同的记忆对
    seen_pairs = set()
    for key, mids in subj_idx.items():
        if len(mids) < 2:
            continue
        # 同 subject 不同 type 的连边
        for i in range(len(mids)):
            for j in range(i + 1, len(mids)):
                a, b = mids[i], mids[j]
                if memories[a]["memory_type"] != memories[b]["memory_type"]:
                    pair = tuple(sorted([a, b]))
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        relations.append((a, b, "same_subject", 0.9))

    # 去重(同 from-to-relation 只留一条,取最大 strength)
    seen = {}
    for frm, to, rel, st in relations:
        key = (frm, to, rel)
        if key not in seen or st > seen[key]:
            seen[key] = st
    return [(frm, to, rel, st) for (frm, to, rel), st in seen.items()]


def save_relations(con, relations):
    for frm, to, rel, st in relations:
        con.execute(
            "INSERT INTO memory_relations (from_memory_id, to_memory_id, relation, strength) "
            "VALUES (?, ?, ?, ?)",
            (frm, to, rel, st)
        )
    con.commit()


def build_networkx(memories, relations):
    """导进 networkx 多重有向图。"""
    G = nx.MultiDiGraph()
    # 节点
    for mid, m in memories.items():
        G.add_node(mid, **{
            "memory_type": m["memory_type"],
            "memory_subtype": m["memory_subtype"],
            "subject": m["subject"],
            "label": m["subject"],
        })
    # 边
    for frm, to, rel, st in relations:
        G.add_edge(frm, to, relation=rel, strength=st)
    return G


def graph_stats(G, memories, relations):
    """算图统计。"""
    stats = {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "relations": len(relations),
        "by_type_nodes": {},
        "by_relation_edges": {},
        "isolated_nodes": 0,
        "weakly_connected_components": 0,
        "largest_component_size": 0,
        "density": round(nx.density(G), 4),
        "avg_degree": round(2 * G.number_of_edges() / max(G.number_of_nodes(), 1), 2),
    }
    # 节点按类型
    for mid, d in G.nodes(data=True):
        t = d.get("memory_type", "?")
        stats["by_type_nodes"][t] = stats["by_type_nodes"].get(t, 0) + 1
    # 边按关系
    for _, _, d in G.edges(data=True):
        r = d.get("relation", "?")
        stats["by_relation_edges"][r] = stats["by_relation_edges"].get(r, 0) + 1
    # 孤立节点
    stats["isolated_nodes"] = sum(1 for n in G.nodes() if G.degree(n) == 0)
    # 连通分量(弱连通,忽略方向)
    wcc = list(nx.weakly_connected_components(G))
    stats["weakly_connected_components"] = len(wcc)
    stats["largest_component_size"] = max((len(c) for c in wcc), default=0)
    return stats


def write_report(stats, relations, memories, analysis_dir):
    ensure_dirs([analysis_dir])
    lines = [
        "# 记忆图谱报告(里程碑4:成图)",
        "",
        f"- 构建时间: {stats['build_time']}",
        f"- 规则版本: {RULES_VERSION}",
        "",
        "## 图统计",
        "",
        f"| 指标 | 值 |",
        f"|------|-----|",
        f"| 节点数(入图记忆) | {stats['nodes']} |",
        f"| 边数(关系) | {stats['edges']} |",
        f"| 孤立节点(无连接) | {stats['isolated_nodes']} |",
        f"| 连通分量数 | {stats['weakly_connected_components']} |",
        f"| 最大连通分量大小 | {stats['largest_component_size']} |",
        f"| 图密度 | {stats['density']} |",
        f"| 平均度数 | {stats['avg_degree']} |",
        "",
        "## 节点按类型分布",
        "",
    ]
    for t, c in sorted(stats["by_type_nodes"].items(), key=lambda x: -x[1]):
        lines.append(f"- {t}: {c}")
    lines.append("\n## 边按关系类型分布\n")
    for r, c in sorted(stats["by_relation_edges"].items(), key=lambda x: -x[1]):
        lines.append(f"- {r}: {c}")

    # 展示一些有代表性的连接路径(图的"故事")
    lines.append("\n## 代表性连接(图的'故事')\n")
    # 找度数最高的几个节点(枢纽)
    import networkx as nx
    lines.append("### 枢纽节点(连接最多的记忆)\n")
    lines.append("| 记忆 | 类型 | 连接数 |")
    lines.append("|------|------|--------|")
    degrees = sorted(memories.keys(), key=lambda m: -_degree_in(relations, m))[:10]
    for mid in degrees:
        m = memories[mid]
        deg = _degree_in(relations, mid)
        if deg > 0:
            lines.append(f"| {m['subject']} | {m['memory_type']} | {deg} |")

    lines.append("\n---\n*里程碑4: 记忆间关系建立 + networkx 成图完成*")
    (analysis_dir / "graph_report.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(analysis_dir / "graph_report.json", stats)


def _degree_in(relations, mid):
    return sum(1 for r in relations if r[0] == mid or r[1] == mid)


def main():
    print("=" * 60)
    print("记忆图谱构建 build_memory_graph.py")
    print(f"  里程碑4: 关系建边 + networkx 成图 ({RULES_VERSION})")
    print("=" * 60)

    if not UNIFIED_DB.exists():
        print(f"\n[ERROR] 统合库不存在: {UNIFIED_DB}")
        sys.exit(1)

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    con = sqlite3.connect(UNIFIED_DB)
    try:
        print("\n[1/5] 确保表结构...")
        ensure_schema(con)
        print("    [OK] 已就绪")

        print("\n[2/5] 清空旧关系(幂等)...")
        reset_relations(con)
        print("    [OK] 已清空")

        print("\n[3/5] 加载记忆(minor capability 不入图)...")
        memories = load_memories(con)
        print(f"    [OK] {len(memories)} 条记忆入图(排除了 minor capability)")

        print("\n[4/5] 建 5 种跨类关系...")
        relations = build_relations(memories)
        print(f"    [OK] {len(relations)} 条关系")
        from collections import Counter
        rel_counter = Counter(r[2] for r in relations)
        for rel, c in rel_counter.most_common():
            print(f"      {rel}: {c}")

        save_relations(con, relations)
        print(f"    [OK] 已写入 memory_relations 表")

        print("\n[5/5] 导入 networkx + 图统计...")
        G = build_networkx(memories, relations)
        stats = graph_stats(G, memories, relations)
        stats["build_time"] = now
        print(f"    [OK] 节点 {stats['nodes']} / 边 {stats['edges']}")
        print(f"    [OK] 连通分量 {stats['weakly_connected_components']} / "
              f"最大 {stats['largest_component_size']}")
        print(f"    [OK] 孤立节点 {stats['isolated_nodes']} / 密度 {stats['density']}")

        write_report(stats, relations, memories, ANALYSIS_DIR)
        print(f"\n    [OK] {ANALYSIS_DIR / 'graph_report.md'}")
    finally:
        con.close()

    print("\n" + "=" * 60)
    print("里程碑4 完成。记忆已连成图谱。")
    print("下一步(里程碑5):图遍历查询 + 可视化")
    print("=" * 60)


if __name__ == "__main__":
    main()
