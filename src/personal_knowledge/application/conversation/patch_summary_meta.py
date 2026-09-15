"""Wave 8.3.2 后处理:给 conversation_summaries.json 补 model/prompt_version meta。

重跑 v2/v3 用的代码在 meta 字段补全之前启动,所以产物里缺这两个字段。
本脚本读取产物,给每个 session.meta 补上 model 和 prompt_version,原地写回。

用法:
  python patch_summary_meta.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
TARGET = ROOT / "integration" / "analysis" / "ai_context" / "conversation_summaries.json"

# 本次重跑的配置(从环境变量回溯不到,写死为本次实际用的值)
MODEL = "gpt-5.4"
PROMPT_VERSION = "v2"


def main() -> int:
    if not TARGET.exists():
        print(f"[error] 产物不存在: {TARGET.name}", file=sys.stderr)
        return 1
    summaries = json.loads(TARGET.read_text(encoding="utf-8"))
    patched = 0
    already = 0
    for s in summaries:
        meta = s.setdefault("meta", {})
        if "model" not in meta or "prompt_version" not in meta:
            meta["model"] = MODEL
            meta["prompt_version"] = PROMPT_VERSION
            patched += 1
        else:
            already += 1
    TARGET.write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[patch] 共 {len(summaries)} session | 补全 {patched} | 已有 {already}")
    print(f"  model={MODEL}, prompt_version={PROMPT_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
