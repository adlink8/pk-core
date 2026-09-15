"""共享工具函数。

从 build_integrated_system.py 和 build_deep_profiles.py 提取的纯函数,
消除两处重复定义。所有统合脚本统一从这里 import,保证行为一致。

设计原则:
- 纯函数,无副作用,可被任何脚本复用
- 不依赖项目内其他模块,只依赖标准库
- 保持与原实现完全一致的行为(向后兼容)
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from urllib.parse import urlparse


def sha256_text(text: str) -> str:
    """对文本做 SHA-256,用于生成确定性 event_id / entity_id / link_id。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def norm(value: object) -> str:
    """空白归一化:去首尾空白,内部多空白压成单空格。None -> 空串。"""
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def short(value: object, limit: int = 2000) -> str:
    """norm 后截断到 limit 字符,避免超长文本撑爆 SQLite/CSV。"""
    return norm(value)[:limit]


def event_id(source: str, source_table: str, source_id: object) -> str:
    """由 (source, source_table, source_id) 派生确定性事件 ID。"""
    return sha256_text(f"{source}|{source_table}|{source_id}")


def entity_id(entity_type: str, name: str) -> str:
    """由 (entity_type, 归一化小写名) 派生确定性实体 ID。"""
    return sha256_text(f"{entity_type}|{norm(name).lower()}")


def extract_domain(url: str) -> str:
    """从 URL 抽取 netloc(小写),空 URL 返回空串。"""
    if not url:
        return ""
    parsed = urlparse(url)
    return parsed.netloc.lower()


def extract_tools(text: str, tool_names: list[str]) -> list[str]:
    """在 text 中扫描 tool_names,返回命中的工具名(去重排序)。"""
    low = text.lower()
    found = []
    for tool in tool_names:
        if tool.lower() in low:
            found.append(tool)
    return sorted(set(found))


def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    """UTF-8-SIG 写 CSV(Excel 可直接打开中文不乱码)。

    fieldnames 为 None 时取 rows 第一条的所有 key(保持插入顺序)。
    空行列表也会创建文件头(若给了 fieldnames)。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_json(path: Path, data: object) -> None:
    """UTF-8 写 JSON,ensure_ascii=False 保留中文。"""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ensure_dirs(paths: list[Path]) -> None:
    """批量创建目录(已存在则跳过)。"""
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
