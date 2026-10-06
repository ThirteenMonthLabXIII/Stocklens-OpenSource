from __future__ import annotations

"""P4 gate: keep default-Qt protection while proving the P4 Qt surface."""

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "stock_tool" / "config" / "frontend_parity_contract.json"
P4_FEATURES = {"data.health", "data.pit", "data.fetch_logs", "settings.ai_key", "settings.storage", "settings.tushare", "acceptance.center", "export.center"}
P4_TESTS = ("tools/test_frontend_p3_gate.py", "tools/test_qt_p4_acceptance.py")


def validate_contract(contract: dict) -> list[str]:
    registry = contract.get("test_registry", {})
    features = {item.get("feature_id"): item for item in contract.get("features", []) if isinstance(item, dict)}
    failures: list[str] = []
    for feature_id in P4_FEATURES:
        feature = features.get(feature_id)
        if feature is None:
            failures.append(f"P4 合同缺少能力：{feature_id}")
        elif feature.get("status") != "部分对齐":
            failures.append(f"{feature_id} 在 P4 后必须保持‘部分对齐’，不得提前放行默认 Qt")
        elif "qt.p4.acceptance" not in feature.get("automated_tests", []):
            failures.append(f"{feature_id} 未登记 qt.p4.acceptance")
    if "frontend.p4.gate" not in registry or "qt.p4.acceptance" not in registry:
        failures.append("P4 测试未登记到功能合同")
    return failures


def main() -> int:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    failures = validate_contract(contract)
    for test_path in P4_TESTS if not failures else ():
        completed = subprocess.run([sys.executable, test_path], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = completed.stdout or ""
        if completed.returncode or "[SKIP]" in output:
            failures.append(f"{test_path}:\n{output}")
        else:
            print(f"[PASS] {test_path}")
    if failures:
        print("[BLOCKED] P4 设置、数据、导出与验收中心闸门未通过：\n" + "\n".join(f"- {item}" for item in failures))
        return 1
    print("[OK] P4 闸门通过；P5 启动闸门可继续执行，正式发布仍由 P7 控制。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
