from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.release_schema import SCHEMA_PATH, validate_schema
from tools.test_frontend_default_gate import CONTRACT_PATH, validate_contract
from stock_tool.config.frontend_contract import FRONTEND_CONTRACT_SCHEMA_VERSION
from stock_tool.config.release_identity import as_manifest_identity


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        from jsonschema import Draft202012Validator
    except ModuleNotFoundError:
        # build-venv 只包含 requirements-v3.lock 的 55 项依赖；详细 payload
        # 校验仍由 tools.release_schema.validate_schema() 的 canonical fallback 完成。
        assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
        assert isinstance(schema.get("$defs"), dict)
        assert {"final_decision", "release", "promotion"} <= set(schema["$defs"])
    else:
        Draft202012Validator.check_schema(schema)
    decision = {
        "schema_version": 1,
        "created_at": "2026-07-17T12:00:00",
        "allowed_for_external_release": True,
        "rc1_manifest_sha256": "1" * 64,
        "rc1_p7_sha256": "2" * 64,
        "promotion_sha256": "3" * 64,
        "sections": {
            "automated": "pass",
            "manual_acceptance": "pass",
            "supply_chain_integrity": "pass",
            "known_risks": "unknown publisher",
            "external_release": "allowed",
        },
    }
    assert not validate_schema("final_decision", decision)
    decision["unknown"] = True
    assert any("Additional properties" in error for error in validate_schema("final_decision", decision))
    decision.pop("unknown")
    decision["schema_version"] = 4
    assert validate_schema("final_decision", decision)
    artifact = {"path": "release_local/evidence.bin", "role": "test", "sha256": "4" * 64, "size_bytes": 1}
    release = {
        "schema_version": 5,
        **as_manifest_identity(),
        "candidate_id": "v3-0123456789ab",
        "git_commit": "0123456789abcdef",
        "candidate_stage": "rc1",
        "created_at": "2026-07-17T12:00:00",
        "build": {"backend": "nuitka"},
        "dependency_lock": {"path": "requirements-v3.lock", "sha256": "5" * 64, "python_version": "3.12.0", "packages": {}, "hashes": {}},
        "frontend_contract": {"path": "stock_tool/config/frontend_parity_contract.json", "sha256": "6" * 64},
        "promotion_record": {"path": "stock_tool/config/v3_rc0_promotion_record.json", "sha256": "7" * 64},
        "commercial_dir": "01_商业出售版_不含源码",
        "source_dir": "02_GitHub源码版_可二次开发",
        "artifacts": {name: artifact for name in ("exe", "installer", "commercial_zip", "source_zip")},
        "inventories": {},
        "internal_evidence": {name: artifact for name in ("compile_report", "resource_manifest", "build_manifest")},
        "automated_acceptance": {name: {"ok": True} for name in ("offline", "packaged_smoke", "default_contract")},
        "sensitive_scan": {"commercial": [], "source": [], "passed": True},
        "p7_policy": {"required": True, "report_path": "V3_P7_人工验收报告.json", "schema_version": 3},
        "manual_checklist": {"json": "checklist.json", "txt": "checklist.txt"},
    }
    assert not validate_schema("release", release)
    rc0_release = json.loads(json.dumps(release))
    rc0_release["candidate_stage"] = "rc0"
    rc0_release["frontend_contract"] = None
    rc0_release["promotion_record"] = None
    rc0_release["automated_acceptance"]["default_contract"] = None
    assert not validate_schema("release", rc0_release)

    frontend_contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert frontend_contract["schema_version"] == FRONTEND_CONTRACT_SCHEMA_VERSION
    assert not validate_contract(frontend_contract, strict=False)
    assert any("严格前端合同缺少 RC0 转正记录" in error for error in validate_contract(frontend_contract, strict=True))
    wrong_frontend_contract = json.loads(json.dumps(frontend_contract))
    wrong_frontend_contract["schema_version"] = 2
    assert any(
        f"schema_version 必须为 {FRONTEND_CONTRACT_SCHEMA_VERSION}" in error
        for error in validate_contract(wrong_frontend_contract, strict=False)
    )

    promotion = {
        "schema_version": 2,
        "created_at": "2026-07-17T12:00:00",
        "rc0": {
            "candidate_id": "v3-0123456789ab",
            "git_commit": "0123456789abcdef",
            "installer_sha256": "8" * 64,
            "commercial_zip_sha256": "9" * 64,
            "source_zip_sha256": "a" * 64,
            "candidate_manifest_sha256": "b" * 64,
            "challenge_id": "RC0-" + "c" * 32,
        },
        "rc0_manifest_sha256": "b" * 64,
        "p7_report_sha256": "d" * 64,
        "screenshot_fingerprints": [],
        "device_matrix": [],
        "compatibility": {
            "win10_22h2": {"status": "NA", "officially_supported": False, "verified_passed": False}
        },
        "warnings": [],
        "conclusion": "accepted",
    }
    assert not validate_schema("promotion", promotion)
    promotion["unknown"] = True
    assert any("Additional properties" in error for error in validate_schema("promotion", promotion))
    promotion.pop("unknown")
    promotion["schema_version"] = 1
    assert validate_schema("promotion", promotion)

    release["build"]["unknown_nested_field"] = True
    assert any("Additional properties" in error for error in validate_schema("release", release))
    print("[OK] Draft 2020-12 schema 与 unknown/schema 负向测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
