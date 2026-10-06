from __future__ import annotations

"""P2 阶段闸门：只证明 P2 已完成，绝不放行默认 Qt 转正。"""

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "stock_tool" / "config" / "frontend_parity_contract.json"
P2_FEATURES = {
    "home.dashboard",
    "research.stock_analysis",
    "screening.parse_and_execute",
    "screening.closed_loop",
    "screening.strategy_management",
    "screening.history_compare",
    "watchlist.management",
}
P2_TESTS = (
    "tools/test_frontend_p1_gate.py",
    "tools/test_frontend_consistency.py",
    "tools/test_qt_screening_interactions.py",
    "tools/test_qt_p2_acceptance.py",
)


def validate_contract(contract: dict) -> list[str]:
    failures: list[str] = []
    registry = contract.get("test_registry", {})
    features = {item.get("feature_id"): item for item in contract.get("features", []) if isinstance(item, dict)}
    for feature_id in P2_FEATURES:
        feature = features.get(feature_id)
        if feature is None:
            failures.append(f"P2 合同缺少能力：{feature_id}")
            continue
        if feature.get("status") != "部分对齐":
            failures.append(f"{feature_id} 在 P2 完成后仍必须保持‘部分对齐’")
    for test_id in ("qt.p2.acceptance", "frontend.p2.gate"):
        if test_id not in registry:
            failures.append(f"P2 合同未登记测试：{test_id}")
    for path in P2_TESTS:
        if not (ROOT / path).is_file():
            failures.append(f"P2 测试文件缺失：{path}")
    return failures


def run_tests() -> list[str]:
    failures: list[str] = []
    for path in P2_TESTS:
        completed = subprocess.run([sys.executable, path], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = completed.stdout or ""
        if completed.returncode:
            failures.append(f"{path} 失败：\n{output}")
        elif "[SKIP]" in output:
            failures.append(f"{path} 出现 SKIP：\n{output}")
        else:
            print(f"[PASS] {path}")
    return failures


def main() -> int:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    failures = validate_contract(contract)
    if not failures:
        failures = run_tests()
    if failures:
        print("[BLOCKED] P2 Qt 研究、筛选与策略闭环闸门未通过：")
        print("\n".join(f"- {item}" for item in failures))
        return 1
    print("[OK] P2 闸门通过；默认 Qt 转正仍由 test_frontend_default_gate.py 阻断。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
