from __future__ import annotations

import argparse
from pathlib import Path
import yaml


ROOT = Path(__file__).resolve().parents[3]


def check(root: Path = ROOT) -> list[str]:
    manifest = yaml.safe_load((root / "governance/stable_modules.yaml").read_text(encoding="utf-8"))
    required = manifest["required_sections"]
    errors: list[str] = []
    for module in manifest["modules"]:
        directory = root / module["path"]
        readme = directory / "README.md"
        if not directory.is_dir():
            errors.append(f"missing stable module: {module['path']}")
            continue
        if not readme.is_file():
            errors.append(f"missing README: {module['path']}")
            continue
        text = readme.read_text(encoding="utf-8")
        for section in required:
            if f"## {section}" not in text:
                errors.append(f"{module['path']}: missing section {section}")
        if module["owner"] not in text or module["status"] not in text:
            errors.append(f"{module['path']}: owner/status mismatch")
    root_readme = (root / "README.md").read_text(encoding="utf-8")
    for required_link in ("integration/README.md", ".planning/ROADMAP.md", "docs/architecture/repository-zones.md"):
        if required_link not in root_readme:
            errors.append(f"root README missing navigation: {required_link}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    errors = check(args.root.resolve())
    if errors:
        print("\n".join(f"ERROR {item}" for item in errors))
        return 1
    print("PASS stable_modules coverage=100%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

