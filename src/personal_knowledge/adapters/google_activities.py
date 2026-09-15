"""Google activities 样例 adapter。

只做旁路样例,不接管现有 pipeline。
目标是验证 canonical record contract 可落地。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from personal_knowledge.core.common import norm, sha256_text, short  # noqa: E402
from personal_knowledge.adapters.base import CanonicalRecord, SourceAdapter  # noqa: E402


ROOT = _THIS_DIR.parents[2]
GOOGLE_DB = ROOT / "Google" / "structured" / "db" / "google_data.sqlite"


class GoogleActivitiesAdapter(SourceAdapter):
    source_type = "google.activities"

    def __init__(self, db_path: Path = GOOGLE_DB) -> None:
        self.db_path = db_path

    def iter_records(self, limit: int | None = None):
        """从 Google activities 表映射成 canonical record。"""
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        sql = (
            "SELECT id, service, event_at, action, category, title_or_query, "
            "channel_or_source, domain, url, raw_excerpt, source_dataset, created_at "
            "FROM activities ORDER BY event_at DESC, id DESC"
        )
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        try:
            for row in con.execute(sql, params):
                title = norm(row["title_or_query"] or row["action"] or row["service"] or "")
                content = short(row["raw_excerpt"] or title, 4000)
                source_id = f"activities:{row['id']}"
                source_path = f"sqlite://{self.db_path.as_posix()}#{source_id}"
                yield CanonicalRecord(
                    source_type=self.source_type,
                    source_id=source_id,
                    title=title,
                    content=content,
                    created_at=str(row["event_at"] or row["created_at"] or ""),
                    updated_at=str(row["created_at"] or row["event_at"] or ""),
                    metadata={
                        "service": row["service"],
                        "action": row["action"],
                        "category": row["category"],
                        "channel_or_source": row["channel_or_source"],
                        "domain": row["domain"],
                        "url": row["url"],
                        "source_dataset": row["source_dataset"],
                    },
                    source_path=source_path,
                    source_hash=sha256_text(
                        "|".join(
                            [
                                self.source_type,
                                source_id,
                                str(row["event_at"] or ""),
                                title,
                                content,
                            ]
                        )
                    ),
                )
        finally:
            con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="输出 canonical Google activity 样例。")
    parser.add_argument("--limit", type=int, default=3, help="样例条数")
    args = parser.parse_args()

    adapter = GoogleActivitiesAdapter()
    rows = [record.to_dict() for record in adapter.iter_records(limit=args.limit)]
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
