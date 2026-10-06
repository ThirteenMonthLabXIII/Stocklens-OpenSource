from __future__ import annotations

"""Protect the P1 shared-workflow scope without relaxing the default-Qt gate."""

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "stock_tool" / "config" / "frontend_parity_contract.json"
UI_FILES = (ROOT / "stock_tool" / "ui" / "qt_workbench.py",)
P1_FEATURES = {
    "screening.parse_and_execute": "ScreeningWorkflowService",
    "screening.closed_loop": "ScreeningWorkflowService",
    "alerts.price_monitoring": "AlertService",
    "tracking.task_review": "ReviewWorkflowService",
    "performance.review": "ReviewWorkflowService",
}
P1_TESTS = ("tools/test_alert_service.py", "tools/test_review_workflow.py", "tools/test_frontend_consistency.py")
FORBIDDEN_UI_WRITES = re.compile(
    r"self\.(?:cache|performance_store)\.(?:add_price_alert|update_price_alert|set_price_alert_enabled|delete_price_alert|"
    r"clear_alert_history|add_tracking_task|update_tracking_task|delete_tracking_task|"
    r"save_run_context|create_observation_samples_for_run|save_validated_professional_candidate|"
    r"activate_professional_formula_candidate)\s*\(",
)


def validate_contract(contract: dict) -> list[str]:
    failures: list[str] = []
    registry = contract.get("test_registry", {})
    features = {item.get("feature_id"): item for item in contract.get("features", []) if isinstance(item, dict)}
    for feature_id, service in P1_FEATURES.items():
        feature = features.get(feature_id)
        if feature is None:
            failures.append(f"P1 合同缺少能力：{feature_id}")
            continue
        if feature.get("status") != "部分对齐":
            failures.append(f"{feature_id} 在 P1 必须保持‘部分对齐’")
        if service not in feature.get("core_services", []):
            failures.append(f"{feature_id} 未登记唯一服务 {service}")
        test_ids = set(feature.get("automated_tests", []))
        if "frontend.p1.gate" not in test_ids:
            failures.append(f"{feature_id} 未登记 frontend.p1.gate")
        if any(test_id not in registry for test_id in test_ids):
            failures.append(f"{feature_id} 引用了未登记测试")
    for test_path in P1_TESTS:
        if not (ROOT / test_path).is_file():
            failures.append(f"P1 测试文件缺失：{test_path}")
    return failures


def validate_ui() -> list[str]:
    failures: list[str] = []
    for path in UI_FILES:
        source = path.read_text(encoding="utf-8")
        for match in FORBIDDEN_UI_WRITES.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            failures.append(f"{path.relative_to(ROOT)}:{line} UI 不得直接写共享工作流状态：{match.group(0)}")
    return failures


def run_p1_tests() -> list[str]:
    failures: list[str] = []
    for test_path in P1_TESTS:
        completed = subprocess.run([sys.executable, test_path], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = completed.stdout or ""
        if completed.returncode:
            failures.append(f"{test_path} 失败：\n{output}")
        elif "[SKIP]" in output:
            failures.append(f"{test_path} 出现 SKIP：\n{output}")
        else:
            print(f"[PASS] {test_path}")
    return failures


def main() -> int:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    failures = validate_contract(contract) + validate_ui()
    if not failures:
        failures = run_p1_tests()
    if failures:
        print("[BLOCKED] P1 共享工作流闸门未通过：")
        print("\n".join(f"- {item}" for item in failures))
        return 1
    print("[OK] P1 共享工作流闸门通过；默认 Qt 合同仍由 test_frontend_default_gate.py 管理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
