from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import warnings
import zipfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.config.release_identity import APP_VERSION, NUITKA_VERSION, as_manifest_identity
from tools.build_v3_nuitka import _tool_record, dependency_lock, dependency_lock_gaps, nuitka_command, require_dependency_lock
from tools import prepare_v3_release
from tools.prepare_v3_release import COMMERCIAL_NAME, MANUAL_CASES, SOURCE_NAME, file_record, sha256_file, validate_package
from tools.record_v3_p7_manual_acceptance import image_metadata, record, validate_p7_report
from tools.promote_v3_rc0 import CONTRACT_PATH, PROMOTION_RECORD_PATH, apply_promotion, promotion_record
from stock_tool.config.frontend_contract import FRONTEND_CONTRACT_SCHEMA_VERSION
from tools.release_integrity import directory_inventory, validate_zip_safety, zip_inventory
from tools.release_schema import validate_schema
from tools.v3_release_gate import evaluate_v3_release


def _write(path: Path, content: bytes = b"candidate") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _png(path: Path) -> Path:
    return _image(path, "PNG")


def _image(path: Path, image_format: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (3, 2), (40, 90, 140)).save(path, format=image_format)
    return path


def _zip(directory: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w") as archive:
        for path in directory.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(directory).as_posix())


def _manifest(release_dir: Path) -> dict:
    commercial = release_dir / COMMERCIAL_NAME
    source = release_dir / SOURCE_NAME
    _write(commercial / "Stocklens_V3_Setup.exe")
    _write(commercial / "使用说明.txt")
    _write(source / "main.py", b"print('source')\n")
    commercial_zip = release_dir / f"{COMMERCIAL_NAME}.zip"
    source_zip = release_dir / f"{SOURCE_NAME}.zip"
    _zip(commercial, commercial_zip)
    _zip(source, source_zip)
    evidence_dir = release_dir / "release_local" / "build_evidence"
    exe = _write(evidence_dir / "Stocklens.exe")
    installer = _write(evidence_dir / "Stocklens_V3_Setup.exe")
    report = _write(evidence_dir / "nuitka-compilation-report.xml", b"<report />")
    resources = _write(evidence_dir / "nuitka-resource-manifest.json", b"{}")
    build_manifest = evidence_dir / "v3_build_manifest.json"
    lock = dependency_lock()
    frontend_contract = {"path": "stock_tool/config/frontend_parity_contract.json", "sha256": "0" * 64}
    promotion_record = {"path": "stock_tool/config/v3_rc0_promotion_record.json", "sha256": "0" * 64}
    build_manifest.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "candidate_id": "v3-0123456789ab",
                "git_commit": "0123456789abcdef",
                "candidate_stage": "rc1",
                "dependency_lock": lock,
                "frontend_contract": frontend_contract,
                "promotion_record": promotion_record,
                "artifacts": {"exe": file_record(exe, release_dir), "installer": file_record(installer, release_dir)},
                "build": {
                    "compile_report": {"sha256": sha256_file(report)},
                    "resource_manifest": {"sha256": sha256_file(resources)},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        "schema_version": 5,
        **as_manifest_identity(),
        "candidate_id": "v3-0123456789ab",
        "git_commit": "0123456789abcdef",
        "candidate_stage": "rc1",
        "build": {
            "backend": "nuitka",
            "nuitka_version": NUITKA_VERSION,
            "python_version": "3.12.0",
            "pyside6_version": "6.11.1",
            "compiler": "nuitka-managed-mingw64",
            "mode": "standalone",
            "onefile": False,
        },
        "dependency_lock": lock,
        "frontend_contract": frontend_contract,
        "promotion_record": promotion_record,
        "artifacts": {
            "exe": file_record(exe, release_dir),
            "installer": file_record(installer, release_dir),
            "commercial_zip": file_record(commercial_zip, release_dir),
            "source_zip": file_record(source_zip, release_dir),
        },
        "internal_evidence": {
            key: file_record(path, release_dir)
            for key, path in {
                "compile_report": report,
                "resource_manifest": resources,
                "build_manifest": build_manifest,
            }.items()
        },
        "automated_acceptance": {"offline": {"ok": True}, "packaged_smoke": {"ok": True}, "default_contract": {"ok": True}},
        "sensitive_scan": {"commercial": [], "source": [], "passed": True},
        "p7_policy": {"required": True, "report_path": "V3_P7_人工验收报告.json", "schema_version": 3},
    }


def _write_candidate_manifest(release_dir: Path, manifest: dict) -> Path:
    manifest_path = release_dir / "RELEASE_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    session = {
        "candidate_id": manifest["candidate_id"],
        "candidate_stage": manifest["candidate_stage"],
        "git_commit": manifest["git_commit"],
        "candidate_manifest_sha256": sha256_file(manifest_path),
        "installer_sha256": manifest["artifacts"]["installer"]["sha256"],
        "commercial_zip_sha256": manifest["artifacts"]["commercial_zip"]["sha256"],
        "source_zip_sha256": manifest["artifacts"]["source_zip"]["sha256"],
        "challenge_id": f"{manifest['candidate_stage'].upper()}-{'a' * 32}",
    }
    (release_dir / "P7_SESSION.json").write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def _evidence(
    release_dir: Path,
    manifest: dict,
    image: Path,
    *,
    shared_reason: str = "同一屏幕同时展示启动状态与数据目录",
    win10_status: str = "PASS",
    win10_device_id: str = "win10-22h2-test",
    include_win10_device: bool = True,
) -> dict:
    session = json.loads((release_dir / "P7_SESSION.json").read_text(encoding="utf-8"))
    candidate = {
        "candidate_id": manifest["candidate_id"],
        "git_commit": manifest["git_commit"],
        "installer_sha256": manifest["artifacts"]["installer"]["sha256"],
        "commercial_zip_sha256": manifest["artifacts"]["commercial_zip"]["sha256"],
        "source_zip_sha256": manifest["artifacts"]["source_zip"]["sha256"],
        "candidate_manifest_sha256": session["candidate_manifest_sha256"],
        "challenge_id": session["challenge_id"],
    }
    devices = [
        {"device_id": "win11-25h2-test", "windows_version": "Windows 11 25H2 x64", "os_label": "win11_25h2"},
        {"device_id": "win11-24h2-test", "windows_version": "Windows 11 24H2 x64", "os_label": "win11_24h2"},
    ]
    if include_win10_device:
        devices.append({"device_id": "win10-22h2-test", "windows_version": "Windows 10 22H2 x64", "os_label": "win10_22h2"})
    items = []
    for key, _title, _required in MANUAL_CASES:
        if key == "win11_25h2_full":
            status = "PASS"
            device_id = "win11-25h2-test"
        elif key == "win10_22h2_compat":
            status = win10_status
            device_id = win10_device_id
        else:
            status = "PASS"
            device_id = "win11-24h2-test"
        screenshots = [] if key == "win10_22h2_compat" and status == "NA" and not device_id else [str(image)]
        items.append(
            {
                "id": key,
                "status": status,
                "device_id": device_id,
                "screenshots": screenshots,
                "shared_evidence_reason": shared_reason if screenshots else "",
                "note": "使用虚构数据完成操作，界面状态符合预期。",
            }
        )
    return {
        "candidate": candidate,
        "operator": "automation",
        "devices": devices,
        "items": items,
    }


def test_version_source_nuitka_command_and_lock() -> None:
    command = nuitka_command()
    lock = dependency_lock()
    assert APP_VERSION == "3.0.0"
    assert f"--report={ROOT / 'build' / 'nuitka-compilation-report.xml'}" in command
    assert "--standalone" in command and "--mingw64" in command and "--onefile" not in command
    assert "--enable-plugin=pyside6" in command
    assert "--enable-plugin=tk-inter" not in command
    assert "matplotlib.backends.backend_tkagg" not in command
    assert lock["python_version"] == "3.12.0"
    assert lock["packages"]["Nuitka"] == NUITKA_VERSION
    for transitive in ("numpy", "scipy", "lxml", "certifi", "pillow", "urllib3"):
        assert transitive in lock["packages"], f"V3 锁文件缺少传递/standalone 依赖：{transitive}"
    assert len(lock["packages"]) >= 50, "V3 锁文件不应退化为仅固定直接依赖"
    assert not dependency_lock_gaps(lock), dependency_lock_gaps(lock)
    assert require_dependency_lock() == lock


def test_tool_record_accepts_declared_nonzero_help_exit() -> None:
    record = _tool_record(
        Path(sys.executable),
        [sys.executable, "-c", "print('tool version 1.2.3'); raise SystemExit(1)"],
        "test_tool",
        accepted_returncodes=(0, 1),
    )
    assert record["version_output"] == "tool version 1.2.3"


def test_build_environment_delegates_to_strict_schema_validator() -> None:
    errors = validate_schema(
        "candidate_signature_index",
        {
            "schema_version": 1,
            "key_id": "invalid",
            "public_key_path": "wrong",
            "subjects": [],
        },
    )
    assert errors, "build venv 缺少 jsonschema 时仍必须执行 Draft 2020-12 深层校验"
    assert any("key_id" in error or "subjects" in error for error in errors), errors


def test_source_package_copies_only_tracked_non_sensitive_files() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        temp = Path(temporary_dir)
        subprocess.run(["git", "init"], cwd=temp, check=True, capture_output=True)
        _write(temp / "main.py", b"print('tracked')\n")
        _write(temp / "stock_tool" / "core" / "secrets.py", b"class SecretStore: pass\n")
        _write(temp / "stock_tool" / "core" / "token_bucket.py", b"class TokenBucket: pass\n")
        _write(temp / "docs" / "secret_policy.txt", b"normal source documentation\n")
        _write(temp / "docs" / "token_notes.json", b"{}\n")
        _write(temp / ".env", b"must-not-package")
        _write(temp / "credentials.json", b"must-not-package")
        _write(temp / "openai_api_key.txt", b"must-not-package")
        _write(temp / "my_data" / "personal.sqlite3", b"must-not-package")
        subprocess.run(
            [
                "git", "add", "-f", "main.py", "stock_tool/core/secrets.py", "stock_tool/core/token_bucket.py",
                "docs/secret_policy.txt", "docs/token_notes.json", ".env", "credentials.json",
                "openai_api_key.txt", "my_data/personal.sqlite3",
            ],
            cwd=temp,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "-c", "user.name=Stocklens Test", "-c", "user.email=test@stocklens.invalid", "commit", "-m", "fixture"], cwd=temp, check=True, capture_output=True)
        _write(temp / "main.py", b"print('dirty-worktree-must-not-package')\n")
        _write(temp / "untracked.py", b"print('untracked')\n")
        destination = temp / "package"
        original_root = prepare_v3_release.ROOT
        try:
            prepare_v3_release.ROOT = temp
            prepare_v3_release.copy_source_tree(destination)
        finally:
            prepare_v3_release.ROOT = original_root
        assert (destination / "main.py").is_file()
        assert destination.joinpath("main.py").read_bytes() == b"print('tracked')\n"
        assert not (destination / "untracked.py").exists()
        assert (destination / "stock_tool" / "core" / "secrets.py").is_file()
        assert (destination / "stock_tool" / "core" / "token_bucket.py").is_file()
        assert (destination / "docs" / "secret_policy.txt").is_file()
        assert (destination / "docs" / "token_notes.json").is_file()
        assert not (destination / ".env").exists()
        assert not (destination / "credentials.json").exists()
        assert not (destination / "openai_api_key.txt").exists()
        assert not (destination / "my_data").exists()
        assert not validate_package(destination, "source")


def test_real_tracked_source_tree_passes_full_boundary_scan() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        destination = Path(temporary_dir) / "source"
        prepare_v3_release.copy_source_tree(destination)
        assert (destination / "stock_tool" / "core" / "secrets.py").is_file()
        errors = validate_package(destination, "source")
        assert not errors, errors


def test_p7_rejects_truncated_png_jpeg_and_webp() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        temp = Path(temporary_dir)
        cases = [
            ("PNG", ".png", 24),
            ("JPEG", ".jpg", None),
            ("WEBP", ".webp", 30),
        ]
        for image_format, suffix, fixed_length in cases:
            valid = _image(temp / f"valid{suffix}", image_format)
            metadata = image_metadata(valid)
            assert metadata["format"] == image_format.lower().replace("jpg", "jpeg")
            data = valid.read_bytes()
            truncated_length = fixed_length if fixed_length is not None else max(24, len(data) // 2)
            truncated = _write(temp / f"truncated{suffix}", data[:truncated_length])
            try:
                image_metadata(truncated)
            except RuntimeError as exc:
                assert "完整解码" in str(exc)
            else:
                raise AssertionError(f"截断 {image_format} 必须被拒绝")


def test_p7_rejects_invalid_and_undeclared_shared_images() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        temp = Path(temporary_dir)
        release_dir = temp / "V3_商业交付_test"
        release_dir.mkdir()
        manifest = _manifest(release_dir)
        _write_candidate_manifest(release_dir, manifest)
        invalid = _write(temp / "not-an-image.png")
        try:
            record(release_dir, _evidence(release_dir, manifest, invalid), "automation")
        except RuntimeError as exc:
            assert "图片" in str(exc)
        else:
            raise AssertionError("任意字节伪装图片必须被拒绝")

        image = _png(temp / "sanitized.png")
        try:
            record(release_dir, _evidence(release_dir, manifest, image, shared_reason=""), "automation")
        except RuntimeError as exc:
            assert "共用" in str(exc)
        else:
            raise AssertionError("复用截图但未声明理由必须被拒绝")


def test_v3_final_gate_requires_hash_bound_schema3_p7_evidence() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        temp = Path(temporary_dir)
        release_dir = temp / "V3_商业交付_test"
        release_dir.mkdir()
        manifest = _manifest(release_dir)
        manifest_path = release_dir / "RELEASE_MANIFEST.json"
        _write_candidate_manifest(release_dir, manifest)
        record(release_dir, _evidence(release_dir, manifest, _png(temp / "sanitized.png")), "automation")
        errors, warnings, summary = evaluate_v3_release(release_dir, verify_repository=False)
        assert not errors, errors
        assert not warnings
        assert summary["manual_ui_status"] == "accepted"

        report_path = release_dir / "V3_P7_人工验收报告.json"
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        payload["candidate"]["commercial_zip_sha256"] = "0" * 64
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        errors, _warnings, _summary = evaluate_v3_release(release_dir, verify_repository=False)
        assert any("候选哈希" in error for error in errors), errors


def test_gate_handles_rc0_null_default_contract_without_exception() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        release_dir = Path(temporary_dir) / "V3_商业交付_rc0"
        release_dir.mkdir()
        manifest = _manifest(release_dir)
        manifest["candidate_stage"] = "rc0"
        manifest["frontend_contract"] = None
        manifest["promotion_record"] = None
        manifest["automated_acceptance"]["default_contract"] = None

        build_manifest_path = release_dir / manifest["internal_evidence"]["build_manifest"]["path"]
        build_manifest = json.loads(build_manifest_path.read_text(encoding="utf-8"))
        build_manifest["candidate_stage"] = "rc0"
        build_manifest["frontend_contract"] = None
        build_manifest["promotion_record"] = None
        build_manifest_path.write_text(json.dumps(build_manifest, ensure_ascii=False), encoding="utf-8")
        manifest["internal_evidence"]["build_manifest"] = file_record(build_manifest_path, release_dir)
        _write_candidate_manifest(release_dir, manifest)

        errors, _warnings, _summary = evaluate_v3_release(release_dir, verify_repository=False)
        assert any("不允许最终外发" in error for error in errors), errors
        assert any("RC1 缺少通过的默认 Qt 前端合同闸门" in error for error in errors), errors


def test_gate_rejects_rc1_null_default_contract() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        release_dir = Path(temporary_dir) / "V3_商业交付_rc1"
        release_dir.mkdir()
        manifest = _manifest(release_dir)
        manifest["automated_acceptance"]["default_contract"] = None
        _write_candidate_manifest(release_dir, manifest)

        errors, _warnings, _summary = evaluate_v3_release(release_dir, verify_repository=False)
        assert any("RC1 缺少通过的默认 Qt 前端合同闸门" in error for error in errors), errors


def test_rc0_promotion_record_omits_screenshots_and_keeps_device_matrix() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        temp = Path(temporary_dir)
        release_dir = temp / "V3_商业交付_test"
        release_dir.mkdir()
        manifest = _manifest(release_dir)
        manifest["candidate_stage"] = "rc0"
        _write_candidate_manifest(release_dir, manifest)
        record(release_dir, _evidence(release_dir, manifest, _png(temp / "sanitized.png")), "automation")
        promoted = promotion_record(release_dir)
        assert promoted["schema_version"] == 2
        assert json.loads((release_dir / "V3_P7_人工验收报告.json").read_text(encoding="utf-8"))["schema_version"] == 3
        assert manifest["schema_version"] == 5
        assert promoted["rc0"]["candidate_id"] == manifest["candidate_id"]
        assert len(promoted["device_matrix"]) == 3
        assert promoted["compatibility"]["win10_22h2"]["verified_passed"] is True
        assert promoted["compatibility"]["win10_22h2"]["officially_supported"] is False
        serialized = json.dumps(promoted, ensure_ascii=False).lower()
        assert "screenshot_fingerprints" in serialized
        assert "release_local" not in serialized and "same.png" not in serialized


def test_rc0_promotion_schema4_contract_updates_22_default_features() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        temp = Path(temporary_dir)
        release_dir = temp / "V3_商业交付_test"
        release_dir.mkdir()
        manifest = _manifest(release_dir)
        manifest["candidate_stage"] = "rc0"
        _write_candidate_manifest(release_dir, manifest)
        record(release_dir, _evidence(release_dir, manifest, _png(temp / "sanitized.png")), "automation")
        promoted = promotion_record(release_dir)
        assert promoted["schema_version"] == 2

        contract_path = temp / "config" / "frontend_parity_contract.json"
        contract_path.parent.mkdir()
        contract_path.write_bytes(CONTRACT_PATH.read_bytes())
        promotion_path = temp / "config" / "v3_rc0_promotion_record.json"
        import tools.promote_v3_rc0 as promotion_module

        original_root = promotion_module.ROOT
        original_contract_path = promotion_module.CONTRACT_PATH
        original_promotion_path = promotion_module.PROMOTION_RECORD_PATH
        try:
            promotion_module.ROOT = temp
            promotion_module.CONTRACT_PATH = contract_path
            promotion_module.PROMOTION_RECORD_PATH = promotion_path
            apply_promotion(promoted)
        finally:
            promotion_module.ROOT = original_root
            promotion_module.CONTRACT_PATH = original_contract_path
            promotion_module.PROMOTION_RECORD_PATH = original_promotion_path

        updated = json.loads(contract_path.read_text(encoding="utf-8"))
        assert updated["schema_version"] == FRONTEND_CONTRACT_SCHEMA_VERSION
        assert json.loads(promotion_path.read_text(encoding="utf-8"))["schema_version"] == 2
        required = set(updated["default_required_feature_ids"])
        features = {item["feature_id"]: item for item in updated["features"]}
        assert len(required) == 22
        assert required <= set(features)
        assert all(features[feature_id]["status"] == "已对齐" for feature_id in required)
        assert all("promotion_evidence" in features[feature_id] for feature_id in required)


def test_failed_promotion_does_not_rewrite_repository_files() -> None:
    contract_before = CONTRACT_PATH.read_bytes()
    promotion_existed = PROMOTION_RECORD_PATH.exists()
    promotion_before = PROMOTION_RECORD_PATH.read_bytes() if promotion_existed else None
    try:
        apply_promotion({"schema_version": 999})
    except RuntimeError:
        pass
    else:
        raise AssertionError("非法 promotion schema 必须失败")
    assert CONTRACT_PATH.read_bytes() == contract_before
    assert PROMOTION_RECORD_PATH.exists() is promotion_existed
    if promotion_existed:
        assert PROMOTION_RECORD_PATH.read_bytes() == promotion_before


def test_win10_binding_na_and_failure_are_non_blocking() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        temp = Path(temporary_dir)
        image = _png(temp / "sanitized.png")

        wrong_release = temp / "wrong-device"
        wrong_release.mkdir()
        wrong_manifest = _manifest(wrong_release)
        wrong_manifest["candidate_stage"] = "rc0"
        _write_candidate_manifest(wrong_release, wrong_manifest)
        try:
            record(
                wrong_release,
                _evidence(wrong_release, wrong_manifest, image, win10_status="PASS", win10_device_id="win11-24h2-test"),
                "automation",
            )
        except RuntimeError as exc:
            assert "系统矩阵" in str(exc)
        else:
            raise AssertionError("Win10 PASS 不得绑定 Windows 11 设备")

        na_release = temp / "not-applicable"
        na_release.mkdir()
        na_manifest = _manifest(na_release)
        na_manifest["candidate_stage"] = "rc0"
        _write_candidate_manifest(na_release, na_manifest)
        record(
            na_release,
            _evidence(na_release, na_manifest, image, win10_status="NA", win10_device_id="", include_win10_device=False),
            "automation",
        )
        na_promoted = promotion_record(na_release)
        assert na_promoted["compatibility"]["win10_22h2"]["status"] == "NA"
        assert na_promoted["compatibility"]["win10_22h2"]["verified_passed"] is False
        assert not na_promoted["warnings"]

        fail_release = temp / "compat-failed"
        fail_release.mkdir()
        fail_manifest = _manifest(fail_release)
        fail_manifest["candidate_stage"] = "rc0"
        _write_candidate_manifest(fail_release, fail_manifest)
        record(fail_release, _evidence(fail_release, fail_manifest, image, win10_status="FAIL"), "automation")
        fail_promoted = promotion_record(fail_release)
        assert fail_promoted["conclusion"] == "accepted"
        assert fail_promoted["compatibility"]["win10_22h2"]["status"] == "FAIL"
        assert fail_promoted["compatibility"]["win10_22h2"]["verified_passed"] is False
        assert any("撤销" in warning for warning in fail_promoted["warnings"])

        version_note = (ROOT / "docs" / "版本说明_v3.0.0.txt").read_text(encoding="utf-8")
        assert "不在正式支持范围" in version_note and "不承诺已验证通过" in version_note


def test_gate_rejects_commercial_installer_different_from_manifest() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        temp = Path(temporary_dir)
        release_dir = temp / "installer-mismatch"
        release_dir.mkdir()
        manifest = _manifest(release_dir)
        manifest_path = release_dir / "RELEASE_MANIFEST.json"
        _write_candidate_manifest(release_dir, manifest)
        record(release_dir, _evidence(release_dir, manifest, _png(temp / "sanitized.png")), "automation")
        _write(release_dir / COMMERCIAL_NAME / "Stocklens_V3_Setup.exe", b"different-installer")
        errors, _warnings, _summary = evaluate_v3_release(release_dir, verify_repository=False)
        assert any("商业目录安装器" in error for error in errors), errors


def test_p7_record_does_not_modify_candidate_manifest() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        temp = Path(temporary_dir)
        release_dir = temp / "immutable-manifest"
        release_dir.mkdir()
        manifest = _manifest(release_dir)
        manifest_path = release_dir / "RELEASE_MANIFEST.json"
        _write_candidate_manifest(release_dir, manifest)
        before = manifest_path.read_bytes()
        record(release_dir, _evidence(release_dir, manifest, _png(temp / "sanitized.png")), "automation")
        assert manifest_path.read_bytes() == before


def test_final_gate_rejects_unsigned_candidate_manifest() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        release_dir = Path(temporary_dir) / "unsigned-candidate"
        release_dir.mkdir()
        manifest = _manifest(release_dir)
        (release_dir / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        errors, _warnings, _summary = evaluate_v3_release(release_dir, verify_repository=True)
        assert any("候选签名：缺少签名索引 CANDIDATE_SIGNATURES.json" in error for error in errors), errors


def test_gate_rejects_directory_and_zip_inventory_mismatch() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        temp = Path(temporary_dir)
        release_dir = temp / "zip-inventory-mismatch"
        release_dir.mkdir()
        manifest = _manifest(release_dir)
        commercial_zip = release_dir / f"{COMMERCIAL_NAME}.zip"
        with zipfile.ZipFile(commercial_zip, "w") as archive:
            archive.writestr("Stocklens_V3_Setup.exe", b"candidate")
        manifest["artifacts"]["commercial_zip"] = file_record(commercial_zip, release_dir)
        manifest_path = release_dir / "RELEASE_MANIFEST.json"
        _write_candidate_manifest(release_dir, manifest)
        record(release_dir, _evidence(release_dir, manifest, _png(temp / "sanitized.png")), "automation")
        errors, _warnings, _summary = evaluate_v3_release(release_dir, verify_repository=False)
        assert any("商业目录与商业 ZIP inventory" in error for error in errors), errors


def test_rc1_rejects_reused_rc0_screenshot_fingerprint() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        temp = Path(temporary_dir)
        release_dir = temp / "rc0"
        release_dir.mkdir()
        manifest = _manifest(release_dir)
        manifest["candidate_stage"] = "rc0"
        manifest_path = release_dir / "RELEASE_MANIFEST.json"
        _write_candidate_manifest(release_dir, manifest)
        record(release_dir, _evidence(release_dir, manifest, _png(temp / "same.png")), "automation")
        promoted = promotion_record(release_dir)
        assert promoted.get("screenshot_fingerprints"), "promotion 必须保存 RC0 截图指纹"
        report = json.loads((release_dir / "V3_P7_人工验收报告.json").read_text(encoding="utf-8"))
        errors, _warnings = validate_p7_report(
            report,
            release_dir,
            report["candidate"],
            prior_screenshot_fingerprints=promoted["screenshot_fingerprints"],
        )
        assert any("RC0" in error and "复用" in error for error in errors), errors


def test_packaged_smoke_detects_delayed_candidate_write() -> None:
    from tools.test_qt_packaged_smoke import assert_candidate_unchanged_after_quiet_window

    with tempfile.TemporaryDirectory() as temporary_dir:
        candidate = Path(temporary_dir) / "candidate"
        _write(candidate / "Stocklens.exe")
        before = directory_inventory(candidate)

        def delayed_write() -> None:
            time.sleep(0.05)
            _write(candidate / "late.log", b"late")

        writer = threading.Thread(target=delayed_write)
        writer.start()
        issues = assert_candidate_unchanged_after_quiet_window(candidate, before, quiet_seconds=0.15)
        writer.join()
        assert issues and any("late.log" in str(issue) for issue in issues), issues


def test_release_inventory_rejects_unsafe_zip_shapes() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        temp = Path(temporary_dir)
        cases: list[tuple[str, list[tuple[zipfile.ZipInfo | str, bytes]], str]] = []
        cases.append(("duplicate.zip", [("same.txt", b"a"), ("same.txt", b"b")], "duplicate_zip_entry"))
        cases.append(("casefold.zip", [("Readme.txt", b"a"), ("README.txt", b"a")], "casefold_collision"))
        cases.append(("traversal.zip", [("../escape.txt", b"x")], "unsafe_zip_path"))
        cases.append(("nested.zip", [("payload.zip", b"not-a-zip")], "unsafe_zip_path"))
        symlink = zipfile.ZipInfo("link.txt")
        symlink.create_system = 3
        symlink.external_attr = (0o120777 << 16)
        cases.append(("symlink.zip", [(symlink, b"target")], "zip_symlink"))
        for filename, entries, expected_code in cases:
            path = temp / filename
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(path, "w") as archive:
                    for name, content in entries:
                        archive.writestr(name, content)
            issues = validate_zip_safety(path)
            assert any(issue.code == expected_code for issue in issues), (filename, issues)
            try:
                zip_inventory(path)
            except ValueError:
                pass
            else:
                raise AssertionError(f"不安全 ZIP 必须拒绝 inventory：{filename}")
        corrupt = _write(temp / "corrupt.zip", b"PK\x03\x04broken")
        assert any(issue.code == "invalid_zip" for issue in validate_zip_safety(corrupt))


def main() -> int:
    test_version_source_nuitka_command_and_lock()
    test_source_package_copies_only_tracked_non_sensitive_files()
    test_real_tracked_source_tree_passes_full_boundary_scan()
    test_p7_rejects_truncated_png_jpeg_and_webp()
    test_p7_rejects_invalid_and_undeclared_shared_images()
    test_v3_final_gate_requires_hash_bound_schema3_p7_evidence()
    test_gate_handles_rc0_null_default_contract_without_exception()
    test_gate_rejects_rc1_null_default_contract()
    test_rc0_promotion_record_omits_screenshots_and_keeps_device_matrix()
    test_rc0_promotion_schema4_contract_updates_22_default_features()
    test_win10_binding_na_and_failure_are_non_blocking()
    test_gate_rejects_commercial_installer_different_from_manifest()
    test_p7_record_does_not_modify_candidate_manifest()
    test_final_gate_rejects_unsigned_candidate_manifest()
    test_gate_rejects_directory_and_zip_inventory_mismatch()
    test_rc1_rejects_reused_rc0_screenshot_fingerprint()
    test_packaged_smoke_detects_delayed_candidate_write()
    test_release_inventory_rejects_unsafe_zip_shapes()
    print("[OK] V3 Nuitka、依赖锁、manifest、候选哈希与 P7 证据交付测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
