"""Phase 14 Wave 4.2 rollback 入口。

用法::

    python rollback_knowledge_checkpoint.py --to previous
    python rollback_knowledge_checkpoint.py --to previous --dry-run
"""

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

from personal_knowledge.application.knowledge.promote_knowledge_index import rollback_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(rollback_main())
