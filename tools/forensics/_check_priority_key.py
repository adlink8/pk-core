from pathlib import Path

t = Path("integration/scripts/_tools/build_generation_gap_analysis.py").read_text(encoding="utf-8")
for i, line in enumerate(t.splitlines(), 1):
    if "P0" in line or "P1" in line or "P2" in line or "P3" in line:
        if ":" in line and "id" not in line:
            # show key portion
            key = line.strip().split(":")[0].strip().strip('"').strip("'")
            print(i, repr(line.strip()[:80]), [hex(ord(c)) for c in key if key.isidentifier() or True][:20])
