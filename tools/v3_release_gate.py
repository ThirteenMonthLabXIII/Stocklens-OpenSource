from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any

try:  # Supports direct execution and imports from final_release_gate.
    from tools.build_v3_nuitka import dependency_lock
    from tools.prepare_v3_release import COMMERCIAL_NAME, RELEASE_MANIFEST_NAME, SOURCE_NAME, sha256_file, validate_package
    from tools.record_v3_p7_manual_acceptance import _session_candidate, validate_p7_report
    from tools.release_integrity import compare_inventories, directory_inventory, validate_zip_safety, zip_inventory
    from tools.verify_release_signing import verify_candidate_signatures
except ModuleNotFoundError:  # pragma: no cover - direct script compatibility
    from build_v3_nuitka import dependency_lock
    from prepare_v3_release import COMMERCIAL_NAME, RELEASE_MANIFEST_NAME, SOURCE_NAME, sha256_file, validate_package
    from record_v3_p7_manual_acceptance import _session_candidate, validate_p7_report
    from release_integrity import compare_inventories, directory_inventory, validate_zip_safety, zip_inventory
    from verify_release_signing import verify_candidate_signatures
from stock_tool.config.release_identity import APP_VERSION, INSTALLER_NAME, NUITKA_VERSION, as_manifest_identity


ROOT = Path(__file__).resolve().parents[1]


def _path(release_dir: Path, text: object) -> Path:
    path = Path(str(text or ""))
    return path if path.is_absolute() else release_dir / path


def _check_record(release_dir: Path, record: object, label: str) -> list[str]:
    if not isinstance(record, dict):
        return [f"{label} artifact record 缺失"]
    path = _path(release_dir, record.get("path"))
    if not path.is_file():
        return [f"{label} artifact 不存在：{path}"]
    errors = []
    if sha256_file(path) != record.get("sha256"):
        errors.append(f"{label} SHA256 不一致")
    if path.stat().st_size != record.get("size_bytes"):
        errors.append(f"{label} 大小不一致")
    return errors


def _check_zip(path: Path, *, commercial: bool) -> list[str]:
    if not path.is_file():
        return [f"zip 不存在：{path}"]
    errors = []
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                lower = name.lower()
                if lower.endswith((".db", ".key", ".log", ".pem", ".pfx", ".pyc", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".zip")) or Path(lower).name.startswith(".env"):
                    errors.append(f"zip 含敏感材料：{name}")
                if commercial and (lower.endswith(".py") or "/stock_tool/" in f"/{lower}" or "/tools/" in f"/{lower}"):
                    errors.append(f"商业 zip 含源码：{name}")
    except zipfile.BadZipFile:
        errors.append(f"zip 无法解压：{path}")
    return errors


def _manual_errors(release_dir: Path, manifest: dict[str, Any]) -> tuple[list[str], list[str]]:
    policy = manifest.get("p7_policy")
    if policy != {"required": True, "report_path": "V3_P7_人工验收报告.json", "schema_version": 3}:
        return ["V3 candidate manifest 的固定 P7 policy 不正确"], []
    report_path = release_dir / "V3_P7_人工验收报告.json"
    if not report_path.is_file():
        return ["V3 P7 人工 JSON 报告缺失"], []
    report = json.loads(report_path.read_text(encoding="utf-8"))
    artifacts = manifest.get("artifacts", {})
    try:
        expected_candidate = _session_candidate(release_dir, manifest, verify_manifest_hash=False)
    except (OSError, ValueError, RuntimeError) as exc:
        return [str(exc)], []
    prior_fingerprints: list[dict[str, Any]] = []
    promotion = manifest.get("promotion_record")
    if isinstance(promotion, dict):
        promotion_path = ROOT / str(promotion.get("path") or "")
        if promotion_path.is_file():
            try:
                promotion_payload = json.loads(promotion_path.read_text(encoding="utf-8"))
                raw = promotion_payload.get("screenshot_fingerprints", [])
                if isinstance(raw, list):
                    prior_fingerprints = [item for item in raw if isinstance(item, dict)]
            except (OSError, ValueError):
                pass
    return validate_p7_report(
        report,
        release_dir,
        expected_candidate,
        prior_screenshot_fingerprints=prior_fingerprints,
    )


def _inventory_errors(release_dir: Path, manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    commercial = release_dir / COMMERCIAL_NAME
    source = release_dir / SOURCE_NAME
    commercial_zip = _path(release_dir, artifacts.get("commercial_zip", {}).get("path"))
    source_zip = _path(release_dir, artifacts.get("source_zip", {}).get("path"))
    installer = commercial / INSTALLER_NAME
    installer_record = artifacts.get("installer") if isinstance(artifacts.get("installer"), dict) else {}
    if not installer.is_file() or sha256_file(installer) != installer_record.get("sha256") or installer.stat().st_size != installer_record.get("size_bytes"):
        errors.append("商业目录安装器与 release/build manifest installer 不一致")
    for label, directory, archive_path in (
        ("商业", commercial, commercial_zip),
        ("源码", source, source_zip),
    ):
        safety = validate_zip_safety(archive_path)
        errors.extend(f"{label} ZIP 安全校验失败：{issue}" for issue in safety)
        if safety:
            continue
        try:
            issues = compare_inventories(directory_inventory(directory), zip_inventory(archive_path))
        except ValueError as exc:
            errors.append(f"{label}目录与{label} ZIP inventory 无法计算：{exc}")
            continue
        errors.extend(f"{label}目录与{label} ZIP inventory 不一致：{issue}" for issue in issues)
    return errors


def _repository_errors(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, text=True, capture_output=True)
    if status.returncode != 0 or status.stdout.strip():
        errors.append("最终闸门要求当前 V3 工作树干净")
        return errors
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True)
    if head.returncode != 0 or head.stdout.strip() != manifest.get("git_commit"):
        errors.append("当前 HEAD 与 RC1 manifest 的 git_commit 不一致")
        return errors
    for key in ("frontend_contract", "promotion_record"):
        record = manifest.get(key)
        if not isinstance(record, dict):
            errors.append(f"RC1 manifest 缺少 {key}")
            continue
        path = ROOT / str(record.get("path") or "")
        if not path.is_file() or sha256_file(path) != record.get("sha256"):
            errors.append(f"RC1 {key} 文件或 SHA256 不一致")
            continue
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", str(path.relative_to(ROOT)).replace("\\", "/")], cwd=ROOT, text=True, capture_output=True)
        if tracked.returncode != 0:
            errors.append(f"RC1 {key} 未包含在当前 commit")
    promotion = manifest.get("promotion_record")
    if isinstance(promotion, dict) and (ROOT / str(promotion.get("path") or "")).is_file():
        payload = json.loads((ROOT / str(promotion["path"])).read_text(encoding="utf-8"))
        rc0_commit = str(payload.get("rc0", {}).get("git_commit") or "")
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", rc0_commit, manifest["git_commit"]], cwd=ROOT)
        if not rc0_commit or ancestor.returncode != 0:
            errors.append("RC1 commit 不包含 RC0 转正记录所绑定的 RC0 commit")
    return errors


def _build_manifest_errors(release_dir: Path, manifest: dict[str, Any]) -> list[str]:
    evidence = manifest.get("internal_evidence") if isinstance(manifest.get("internal_evidence"), dict) else {}
    build_record = evidence.get("build_manifest")
    if not isinstance(build_record, dict):
        return ["内部 build manifest 证据缺失"]
    path = _path(release_dir, build_record.get("path"))
    if not path.is_file():
        return ["内部 build manifest 文件缺失"]
    build_manifest = json.loads(path.read_text(encoding="utf-8"))
    errors = []
    if build_manifest.get("schema_version") != 5:
        errors.append("内部 build manifest schema_version 必须为 5")
    for key in ("candidate_id", "git_commit", "candidate_stage", "dependency_lock", "frontend_contract", "promotion_record"):
        if build_manifest.get(key) != manifest.get(key):
            errors.append(f"build/release manifest {key} 不一致")
    build_artifacts = build_manifest.get("artifacts") if isinstance(build_manifest.get("artifacts"), dict) else {}
    release_artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    for key in ("exe", "installer"):
        if build_artifacts.get(key, {}).get("sha256") != release_artifacts.get(key, {}).get("sha256"):
            errors.append(f"build/release manifest {key} SHA256 不一致")
    build_details = build_manifest.get("build") if isinstance(build_manifest.get("build"), dict) else {}
    for key in ("compile_report", "resource_manifest"):
        if build_details.get(key, {}).get("sha256") != evidence.get(key, {}).get("sha256"):
            errors.append(f"build/release manifest {key} SHA256 不一致")
    return errors


def evaluate_v3_release(release_dir: Path, *, verify_repository: bool = False) -> tuple[list[str], list[str], dict[str, Any]]:
    manifest_path = release_dir / RELEASE_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []
    if manifest.get("schema_version") != 5:
        errors.append("V3 release manifest schema_version 必须为 5")
    if verify_repository:
        errors.extend(f"候选签名：{item}" for item in verify_candidate_signatures(release_dir))
    for key, value in as_manifest_identity().items():
        if manifest.get(key) != value:
            errors.append(f"V3 manifest {key} 不匹配单一版本源")
    build = manifest.get("build") if isinstance(manifest.get("build"), dict) else {}
    if build.get("backend") != "nuitka" or build.get("nuitka_version") != NUITKA_VERSION:
        errors.append("V3 必须使用 Nuitka 4.1.3 构建")
    if build.get("mode") != "standalone" or build.get("onefile") is not False or build.get("compiler") != "nuitka-managed-mingw64":
        errors.append("V3 构建模式必须为 Nuitka managed MinGW64 standalone，且不得 onefile")
    if not manifest.get("candidate_id") or not manifest.get("git_commit"):
        errors.append("V3 manifest 缺少 candidate_id 或 Git commit")
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    for key in ("exe", "installer", "commercial_zip", "source_zip"):
        errors.extend(_check_record(release_dir, artifacts.get(key), key))
    commercial = release_dir / COMMERCIAL_NAME
    source = release_dir / SOURCE_NAME
    errors.extend(validate_package(commercial, "commercial"))
    errors.extend(validate_package(source, "source"))
    errors.extend(_check_zip(_path(release_dir, artifacts.get("commercial_zip", {}).get("path")), commercial=True))
    errors.extend(_check_zip(_path(release_dir, artifacts.get("source_zip", {}).get("path")), commercial=False))
    errors.extend(_inventory_errors(release_dir, manifest))
    evidence = manifest.get("internal_evidence") if isinstance(manifest.get("internal_evidence"), dict) else {}
    for key in ("compile_report", "resource_manifest", "build_manifest"):
        errors.extend(_check_record(release_dir, evidence.get(key), f"internal_evidence.{key}"))
    errors.extend(_build_manifest_errors(release_dir, manifest))
    automated = manifest.get("automated_acceptance") if isinstance(manifest.get("automated_acceptance"), dict) else {}
    if not automated.get("offline", {}).get("ok"):
        errors.append("V3 离线自动验收未通过或缺失")
    if not automated.get("packaged_smoke", {}).get("ok"):
        errors.append("V3 Nuitka 候选包 smoke 未通过或缺失")
    candidate_stage = str(manifest.get("candidate_stage") or "unknown")
    if candidate_stage != "rc1":
        errors.append(f"当前候选阶段 {candidate_stage} 不允许最终外发；最终外发只接受从合同转正 commit 全新构建的 RC1")
    default_contract = automated.get("default_contract")
    if not isinstance(default_contract, dict) or default_contract.get("ok") is not True:
        errors.append("RC1 缺少通过的默认 Qt 前端合同闸门")
    if manifest.get("sensitive_scan", {}).get("passed") is not True:
        errors.append("V3 敏感材料扫描未通过")
    manual_errors, manual_warnings = _manual_errors(release_dir, manifest)
    errors.extend(manual_errors)
    warnings.extend(manual_warnings)
    if verify_repository:
        errors.extend(_repository_errors(manifest))
        try:
            if manifest.get("dependency_lock") != dependency_lock():
                errors.append("RC1 manifest 依赖锁摘要与当前锁文件不一致")
        except RuntimeError as exc:
            errors.append(str(exc))
    return errors, warnings, {
        "release_dir": str(release_dir),
        "version": APP_VERSION,
        "manual_ui_status": "accepted" if not manual_errors else "missing_or_rejected",
        "release_root_forbidden_errors": [],
    }
