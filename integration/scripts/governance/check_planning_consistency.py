from __future__ import annotations

import argparse
import re
from pathlib import Path
import yaml


ROOT = Path(__file__).resolve().parents[3]


def check(root: Path = ROOT) -> list[str]:
    policy = yaml.safe_load((root / "governance/policies/planning.yaml").read_text(encoding="utf-8"))
    state = (root / ".planning/STATE.md").read_text(encoding="utf-8")
    roadmap = (root / ".planning/ROADMAP.md").read_text(encoding="utf-8")
    errors: list[str] = []
    if policy["authoritative"] != ".planning" or policy["historical_read_only"] != ".gsd":
        errors.append("planning truth hierarchy is invalid")
    if "Phase 17 code complete" not in state or "human" not in state.lower():
        errors.append("STATE must preserve Phase 17 human checkpoints")
    if re.search(r"^- \[x\].*Phase 17", roadmap, re.IGNORECASE | re.MULTILINE):
        errors.append("ROADMAP incorrectly marks Phase 17 complete")
    if "17-01..04 planned" in roadmap:
        errors.append("ROADMAP drift: Phase 17 plans are implemented, not planned")
    if "Phase 17 规划中" in roadmap:
        errors.append("ROADMAP overview drift: Phase 17 is executing")
    if "18 | 0/6" in roadmap and "Plan: 1 of 6" in state:
        errors.append("ROADMAP drift: Phase 18 progress lags STATE")
    phase18_context = root / ".planning/phases/18-full-repository-governance/18-CONTEXT.md"
    phase18_complete = bool(
        re.search(r"^- \[x\].*Phase 18", roadmap, re.IGNORECASE | re.MULTILINE)
        or re.search(r"\|\s*18\s*\|\s*6/6\s*\|\s*\*\*Complete", roadmap, re.IGNORECASE)
    )
    if phase18_complete:
        if not phase18_context.exists():
            errors.append("Phase 18 complete but CONTEXT.md is missing")
        else:
            context = phase18_context.read_text(encoding="utf-8")
            frontmatter = context.split("---", 2)[1] if context.startswith("---") and context.count("---") >= 2 else ""
            if not re.search(r"^status:\s*complete\s*$", frontmatter, re.MULTILINE):
                errors.append("Phase 18 complete but CONTEXT status is not complete")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    errors = check(args.root.resolve())
    if errors:
        print("\n".join(f"DRIFT {item}" for item in errors))
        return 1
    print("PASS .planning authoritative; Phase 17 remains open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
