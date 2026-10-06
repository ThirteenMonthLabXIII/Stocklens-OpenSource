from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    "tools/test_formula_governance.py",
    "tools/test_qt_performance_review.py",
    "tools/test_qt_shadow_governance.py",
    "tools/test_qt_end_to_end.py",
)


def main() -> int:
    failures = []
    for path in TESTS:
        completed = subprocess.run([sys.executable, path], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if completed.returncode or "[SKIP]" in (completed.stdout or ""):
            failures.append(f"{path}:\n{completed.stdout}")
        else:
            print(f"[PASS] {path}")
    if failures:
        print("[BLOCKED] P3 闸门未通过：\n" + "\n".join(failures))
        return 1
    print("[OK] P3 闸门通过；默认 Qt 转正仍由 P5 闸门控制。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
