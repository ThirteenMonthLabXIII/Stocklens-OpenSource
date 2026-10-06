from __future__ import annotations

"""Block promotion to a Qt-default release until the parity contract is proven."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "stock_tool" / "config" / "frontend_parity_contract.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.config.frontend_contract import FRONTEND_CONTRACT_SCHEMA_VERSION


REQUIRED_FEATURE_FIELDS = {
    "feature_id",
    "source",
    "required_for_default",
    "status",
    "qt_action_ids",
    "core_services",
    "observable_results",
    "automated_tests",
    "manual_cases",
    "owner",
    "alignment_evidence",
}
REQUIRED_DEFAULT_FEATURE_IDS = {
    "home.dashboard", "research.stock_analysis", "screening.parse_and_execute", "screening.closed_loop", "screening.strategy_management", "screening.history_compare", "screening.candidate_lens_agents", "watchlist.management", "alerts.price_monitoring", "tracking.task_review", "performance.review", "performance.evaluation", "data.health", "data.pit", "data.fetch_logs", "settings.ai_key", "settings.storage", "settings.tushare", "acceptance.center", "export.center", "startup.default_ui", "packaging.qt",
}


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdefABCDEF" for char in text)


def _validate_promotion_record(record: dict[str, Any]) -> list[str]:
    """Validate the minimum evidence shape emitted by promote_v3_rc0.py.

    The contract hash proves that the referenced file is the one that was
    promoted; it does not, by itself, prove that an arbitrary JSON file is a
    valid accepted RC0 record.  Keep this check local to the strict Gate so
    pre-RC0 and exploratory contract checks remain usable.
    """

    failures: list[str] = []
    if record.get("schema_version") != 2:
        failures.append("RC0 转正记录 schema_version 必须为 2")
    if record.get("conclusion") != "accepted":
        failures.append("RC0 转正记录 conclusion 必须为 accepted")
    candidate = record.get("rc0")
    if not isinstance(candidate, dict):
        failures.append("RC0 转正记录缺少 rc0 候选绑定")
    else:
        for field in (
            "candidate_id",
            "git_commit",
            "installer_sha256",
            "commercial_zip_sha256",
            "source_zip_sha256",
        ):
            value = candidate.get(field)
            if field.endswith("_sha256"):
                if not _is_sha256(value):
                    failures.append(f"RC0 转正记录 {field} 必须为 SHA256")
            elif not isinstance(value, str) or not value.strip():
                failures.append(f"RC0 转正记录缺少候选字段：{field}")
    for field in ("rc0_manifest_sha256", "p7_report_sha256"):
        if not _is_sha256(record.get(field)):
            failures.append(f"RC0 转正记录 {field} 必须为 SHA256")

    devices = record.get("device_matrix")
    if not isinstance(devices, list) or not devices:
        failures.append("RC0 转正记录缺少设备矩阵")
    else:
        for index, device in enumerate(devices):
            if not isinstance(device, dict) or not all(
                isinstance(device.get(field), str) and device[field].strip()
                for field in ("device_id", "windows_version", "os_label")
            ):
                failures.append(f"RC0 转正记录设备矩阵第 {index + 1} 项格式错误")

    fingerprints = record.get("screenshot_fingerprints")
    if not isinstance(fingerprints, list):
        failures.append("RC0 转正记录 screenshot_fingerprints 必须为列表")
    compatibility = record.get("compatibility")
    win10 = compatibility.get("win10_22h2") if isinstance(compatibility, dict) else None
    if not isinstance(win10, dict):
        failures.append("RC0 转正记录缺少 Win10 兼容性结论")
    elif win10.get("officially_supported") is not False or not isinstance(win10.get("verified_passed"), bool):
        failures.append("RC0 转正记录 Win10 兼容性结论格式错误")
    return failures


def validate_contract(payload: dict[str, Any], *, strict: bool) -> list[str]:
    failures: list[str] = []
    if payload.get("schema_version") != FRONTEND_CONTRACT_SCHEMA_VERSION:
        failures.append(f"schema_version 必须为 {FRONTEND_CONTRACT_SCHEMA_VERSION}")
    allowed_statuses = set(payload.get("status_values", []))
    expected = set(payload.get("default_required_feature_ids", []))
    if expected != REQUIRED_DEFAULT_FEATURE_IDS:
        failures.append(
            "default_required_feature_ids 被篡改或不完整："
            f"缺少={sorted(REQUIRED_DEFAULT_FEATURE_IDS - expected)} 多余={sorted(expected - REQUIRED_DEFAULT_FEATURE_IDS)}"
        )
    registry = payload.get("test_registry")
    if not isinstance(registry, dict) or not registry:
        failures.append("test_registry 不能为空")
        registry = {}
    features = payload.get("features")
    if not isinstance(features, list) or not features:
        return failures + ["features 不能为空"]

    promotion_policy = payload.get("promotion_policy")
    if not isinstance(promotion_policy, dict) or promotion_policy.get("schema_version") != 1:
        failures.append(
            f"schema {FRONTEND_CONTRACT_SCHEMA_VERSION} 前端合同必须声明 promotion_policy schema_version=1"
        )
    promotion_record = payload.get("promotion_record")
    if strict:
        if not isinstance(promotion_record, dict):
            failures.append("严格前端合同缺少 RC0 转正记录")
        else:
            record_path = ROOT / str(promotion_record.get("path") or "")
            expected_sha = str(promotion_record.get("sha256") or "")
            try:
                record_inside_root = record_path.resolve().is_relative_to(ROOT.resolve())
            except (OSError, RuntimeError, ValueError):
                record_inside_root = False
            if not record_inside_root:
                failures.append("严格前端合同 RC0 转正记录路径必须位于仓库内")
            elif not record_path.is_file():
                failures.append("严格前端合同引用的 RC0 转正记录不存在")
            elif not expected_sha:
                failures.append("严格前端合同缺少 RC0 转正记录 SHA256")
            else:
                import hashlib

                actual_sha = hashlib.sha256(record_path.read_bytes()).hexdigest()
                if actual_sha != expected_sha:
                    failures.append("严格前端合同 RC0 转正记录 SHA256 不一致")
                else:
                    try:
                        record_payload = json.loads(record_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                        failures.append(f"严格前端合同 RC0 转正记录 JSON 无法读取：{exc}")
                    else:
                        if not isinstance(record_payload, dict):
                            failures.append("严格前端合同 RC0 转正记录必须为对象")
                        else:
                            failures.extend(_validate_promotion_record(record_payload))

    ids: set[str] = set()
    required_ids: set[str] = set()
    for item in features:
        if not isinstance(item, dict):
            failures.append("feature 必须为对象")
            continue
        feature_id = item.get("feature_id")
        if not isinstance(feature_id, str) or not feature_id:
            failures.append("feature_id 必须为非空字符串")
            continue
        if feature_id in ids:
            failures.append(f"feature_id 重复：{feature_id}")
        ids.add(feature_id)
        missing = sorted(REQUIRED_FEATURE_FIELDS - set(item))
        if missing:
            failures.append(f"{feature_id} 缺少字段：{', '.join(missing)}")
            continue
        if item["status"] not in allowed_statuses:
            failures.append(f"{feature_id} 的 status 非法：{item['status']}")
        if not isinstance(item["required_for_default"], bool):
            failures.append(f"{feature_id} 的 required_for_default 必须为布尔值")
        if not all(isinstance(item[field], list) for field in ("qt_action_ids", "core_services", "observable_results", "automated_tests", "manual_cases")):
            failures.append(f"{feature_id} 的列表字段类型错误")
        if not all(isinstance(item[field], str) and item[field].strip() for field in ("source", "owner", "alignment_evidence")):
            failures.append(f"{feature_id} 的说明字段不能为空")
        unknown_tests = sorted(set(item["automated_tests"]) - set(registry))
        if unknown_tests:
            failures.append(f"{feature_id} 引用了未登记测试：{', '.join(unknown_tests)}")
        if item["required_for_default"]:
            required_ids.add(feature_id)
            if not item["automated_tests"]:
                failures.append(f"{feature_id} 是默认必需能力但没有自动化测试")
            if strict and item["status"] != "已对齐":
                failures.append(f"{feature_id} 尚未已对齐：{item['status']}")
            if strict:
                evidence = item.get("promotion_evidence")
                if not isinstance(evidence, dict):
                    failures.append(f"{feature_id} 缺少转正证据")
                else:
                    if not isinstance(promotion_record, dict) or evidence.get("record_path") != promotion_record.get("path") or evidence.get("record_sha256") != promotion_record.get("sha256"):
                        failures.append(f"{feature_id} 未引用当前 RC0 转正记录")
                    if evidence.get("automated_tests") != item["automated_tests"]:
                        failures.append(f"{feature_id} 转正证据未完整引用自动化测试")

    if required_ids != expected:
        failures.append(
            "required_for_default 与 default_required_feature_ids 不一致："
            f"缺少={sorted(expected - required_ids)} 多余={sorted(required_ids - expected)}"
        )
    if expected - ids:
        failures.append(f"default_required_feature_ids 包含不存在能力：{sorted(expected - ids)}")
    for test_id, test in registry.items():
        command = test.get("command") if isinstance(test, dict) else None
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            failures.append(f"测试 {test_id} 缺少可执行 command")
    return failures


def run_declared_tests(payload: dict[str, Any]) -> list[str]:
    registry = payload["test_registry"]
    test_ids = sorted({test_id for feature in payload["features"] if feature["required_for_default"] for test_id in feature["automated_tests"]})
    failures: list[str] = []
    for test_id in test_ids:
        command = [sys.executable, *registry[test_id]["command"]]
        completed = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = completed.stdout or ""
        if completed.returncode != 0:
            failures.append(f"{test_id} 失败（exit={completed.returncode}）：\n{output}")
        elif "[SKIP]" in output:
            failures.append(f"{test_id} 出现 SKIP，严格转正测试不允许跳过：\n{output}")
        else:
            print(f"[PASS] {test_id}")
    return failures


def main() -> int:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    failures = validate_contract(payload, strict=True)
    if not failures:
        failures.extend(run_declared_tests(payload))
    if failures:
        print("[BLOCKED] Qt 默认前端转正闸门未通过：")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("[OK] Qt 默认前端转正闸门通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
