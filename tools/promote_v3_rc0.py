from __future__ import annotations

"""Promote an accepted RC0 P7 record into the committed RC1 contract inputs."""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.record_v3_p7_manual_acceptance import _session_candidate, validate_p7_report
from tools.release_integrity import atomic_write_json
from tools.release_schema import require_schema
from stock_tool.config.frontend_contract import FRONTEND_CONTRACT_SCHEMA_VERSION


CONTRACT_PATH = ROOT / "stock_tool" / "config" / "frontend_parity_contract.json"
PROMOTION_RECORD_PATH = ROOT / "stock_tool" / "config" / "v3_rc0_promotion_record.json"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate(manifest: dict[str, Any]) -> dict[str, str]:
    artifacts = manifest["artifacts"]
    return {
        "candidate_id": str(manifest["candidate_id"]),
        "git_commit": str(manifest["git_commit"]),
        "installer_sha256": str(artifacts["installer"]["sha256"]),
        "commercial_zip_sha256": str(artifacts["commercial_zip"]["sha256"]),
        "source_zip_sha256": str(artifacts["source_zip"]["sha256"]),
    }


def promotion_record(release_dir: Path) -> dict[str, Any]:
    manifest_path = release_dir / "RELEASE_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 5 or manifest.get("candidate_stage") != "rc0":
        raise RuntimeError("转正只能使用 schema 5 的 RC0 候选。")
    report_path = release_dir / "V3_P7_人工验收报告.json"
    if not report_path.is_file():
        raise RuntimeError("RC0 P7 报告缺失，不能转正。")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    candidate = _session_candidate(release_dir, manifest) if (release_dir / "P7_SESSION.json").is_file() else _candidate(manifest)
    errors, warnings = validate_p7_report(report, release_dir, candidate)
    if errors:
        raise RuntimeError("RC0 P7 不满足转正条件：\n" + "\n".join(errors))
    devices = report.get("devices")
    if not isinstance(devices, list):  # Covered by validate_p7_report; keeps type narrowing explicit.
        raise RuntimeError("RC0 P7 缺少设备矩阵。")
    rows = {str(row.get("id")): row for row in report.get("items", []) if isinstance(row, dict)}
    win10_status = str(rows.get("win10_22h2_compat", {}).get("status") or "").upper()
    fingerprints = []
    for case_id, row in rows.items():
        for screenshot in row.get("screenshots", []):
            if isinstance(screenshot, dict):
                fingerprints.append(
                    {
                        "case_id": case_id,
                        "sha256": str(screenshot.get("sha256") or ""),
                        "pixel_sha256": str(screenshot.get("pixel_sha256") or ""),
                        "dhash64": str(screenshot.get("dhash64") or ""),
                    }
                )
    record = {
        "schema_version": 2,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "rc0": candidate,
        "rc0_manifest_sha256": sha256_file(manifest_path),
        "p7_report_sha256": sha256_file(report_path),
        "screenshot_fingerprints": fingerprints,
        "device_matrix": [
            {key: str(device.get(key) or "") for key in ("device_id", "windows_version", "os_label")}
            for device in devices
            if isinstance(device, dict)
        ],
        "compatibility": {
            "win10_22h2": {
                "status": win10_status,
                "officially_supported": False,
                "verified_passed": win10_status == "PASS",
            }
        },
        "warnings": warnings,
        "conclusion": "accepted",
    }
    require_schema("promotion", record)
    return record


def apply_promotion(record: dict[str, Any]) -> None:
    require_schema("promotion", record)
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("schema_version") != FRONTEND_CONTRACT_SCHEMA_VERSION:
        raise RuntimeError(
            f"前端合同必须先升级到 schema {FRONTEND_CONTRACT_SCHEMA_VERSION}。"
        )
    record_ref = {
        "path": str(PROMOTION_RECORD_PATH.relative_to(ROOT)).replace("\\", "/"),
        "sha256": hashlib.sha256(
            (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        ).hexdigest(),
    }
    required_ids = set(contract.get("default_required_feature_ids", []))
    found_ids: set[str] = set()
    for feature in contract.get("features", []):
        if not isinstance(feature, dict) or feature.get("feature_id") not in required_ids:
            continue
        found_ids.add(str(feature["feature_id"]))
        tests = feature.get("automated_tests")
        if not isinstance(tests, list) or not tests:
            raise RuntimeError(f"默认能力缺少自动化测试：{feature.get('feature_id')}")
        feature["status"] = "已对齐"
        feature["promotion_evidence"] = {
            "record_path": record_ref["path"],
            "record_sha256": record_ref["sha256"],
            "automated_tests": tests,
        }
    missing_ids = sorted(required_ids - found_ids)
    if missing_ids:
        raise RuntimeError(f"前端合同缺少默认能力：{', '.join(missing_ids)}")
    contract["promotion_record"] = record_ref
    atomic_write_json(PROMOTION_RECORD_PATH, record)
    atomic_write_json(CONTRACT_PATH, contract)


def main() -> int:
    parser = argparse.ArgumentParser(description="将 accepted RC0 P7 固化为 RC1 合同转正输入。")
    parser.add_argument("--release-dir", required=True)
    args = parser.parse_args()
    record = promotion_record(Path(args.release_dir).resolve())
    apply_promotion(record)
    print(f"[OK] RC0 转正记录：{PROMOTION_RECORD_PATH}")
    print("下一步：审查并提交该记录与前端合同，再以 --candidate-stage rc1 全新构建。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
