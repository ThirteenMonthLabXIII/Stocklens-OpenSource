from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.prepare_phase9_release import (
    COMMERCIAL_NAME,
    COMMERCIAL_FORBIDDEN_SUFFIXES,
    GITIGNORE_REQUIRED_PATTERNS,
    SOURCE_NAME,
    SOURCE_EXCLUDE_SUFFIXES,
    SOURCE_FORBIDDEN_SUFFIXES,
    load_manual_ui_acceptance,
    validate_package,
    verify_gitignore,
)


TEST_STAMP = f"PHASE9_TEST_{os.getpid()}"
TEST_INSTALLER_NAME = "Stocklens_V2.exe"
FREE_PYARMOR_TARGETS = [
    "main.py",
    "stock_tool/core/screener_engine.py",
    "stock_tool/core/performance_loop.py",
    "stock_tool/core/professional_formula.py",
    "stock_tool/core/query_schema.py",
    "stock_tool/core/local_query_parser.py",
    "stock_tool/core/technical_profile.py",
    "stock_tool/core/trade_observer.py",
    "stock_tool/core/trade_ai.py",
    "stock_tool/core/ai_ranker.py",
    "stock_tool/core/cache.py",
    "stock_tool/core/factor_store.py",
    "stock_tool/core/ai_client.py",
    "stock_tool/core/charting.py",
]
SKIPPED_BIG_SCRIPTS = [
    {"path": "stock_tool/core/data_fetcher.py", "size_bytes": 49591, "reason": "large_script_or_high_risk_ui_kept_as_pyinstaller_bytecode"},
    {"path": "stock_tool/core/strategy_store.py", "size_bytes": 43951, "reason": "large_script_or_high_risk_ui_kept_as_pyinstaller_bytecode"},
    {"path": "stock_tool/ui/qt_workbench.py", "size_bytes": 42396, "reason": "large_script_or_high_risk_ui_kept_as_pyinstaller_bytecode"},
]


def _release_dir(stamp: str = TEST_STAMP) -> Path:
    return ROOT / "release" / f"V1_商业交付_{stamp}"


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _fake_protection_inputs(base: Path, protection_mode: str = "free-pyarmor") -> tuple[Path, Path]:
    installer = base / TEST_INSTALLER_NAME
    installer.write_bytes(b"fake protected installer for phase9 tests")
    manifest = base / "protected_installer_manifest.json"
    pyarmor_enabled = protection_mode in {"free-pyarmor", "bcc"}
    bcc_enabled = protection_mode == "bcc"
    protection_level = "bcc" if protection_mode == "bcc" else ("trial_obfuscation" if protection_mode == "free-pyarmor" else "none")
    obfuscated_targets = FREE_PYARMOR_TARGETS if pyarmor_enabled else []
    bcc_targets = {"stock_tool.core.screener_engine": ["screen_market"]} if bcc_enabled else {}
    bcc_verification = {"verified": {"stock_tool.core.screener_engine": ["screen_market"]}, "target_count": 1} if bcc_enabled else {}
    payload = {
        "schema_version": 1,
        "title": "fake protected installer manifest",
        "tools": {
            "pyarmor": {"version": "PyArmor 9 test"},
            "pyinstaller": {"version": "6.20.0"},
            "c_compiler": {"path": "clang" if bcc_enabled else "", "version": "clang test" if bcc_enabled else ""},
        },
        "protection": {
            "mode": protection_mode,
            "protection_level": protection_level,
            "pyarmor_enabled": pyarmor_enabled,
            "bcc_enabled": bcc_enabled,
            "bcc_required": bcc_enabled,
            "bcc_verified": bcc_enabled,
            "trial_build": protection_mode == "free-pyarmor",
            "obfuscated_targets": obfuscated_targets,
            "protected_targets": obfuscated_targets,
            "plain_obfuscation_targets": obfuscated_targets,
            "obfuscated_target_details": [{"path": path, "size_bytes": 1} for path in obfuscated_targets],
            "skipped_big_scripts": SKIPPED_BIG_SCRIPTS,
            "bcc_targets": bcc_targets,
            "bcc_verification": bcc_verification,
            "non_bcc_core_modules": ["stock_tool.core.data_fetcher", "stock_tool.core.strategy_store"],
        },
        "installer": {
            "path": str(installer),
            "name": installer.name,
            "sha256": _sha256(installer),
            "size_bytes": installer.stat().st_size,
        },
    }
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return installer, manifest


def _run_prepare(stamp: str = TEST_STAMP, extra_args: list[str] | None = None, protection_mode: str = "free-pyarmor") -> Path:
    release_dir = _release_dir(stamp)
    if release_dir.exists():
        shutil.rmtree(release_dir)
    with tempfile.TemporaryDirectory() as tmp:
        installer, protection_manifest = _fake_protection_inputs(Path(tmp), protection_mode=protection_mode)
        command = [
            sys.executable,
            "tools/prepare_phase9_release.py",
            "--legacy-phase9",
            "--version",
            "v0.2.7_phase9",
            "--skip-acceptance",
            "--timestamp",
            stamp,
            "--installer-path",
            str(installer),
            "--protection-manifest",
            str(protection_manifest),
        ]
        command.extend(extra_args or [])
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert release_dir.exists()
    return release_dir


def _files(base: Path) -> set[str]:
    return {
        str(path.relative_to(base)).replace("\\", "/")
        for path in base.rglob("*")
        if path.is_file()
    }


def _attach_manual_report(release_dir: Path, report_path: Path) -> dict:
    manifest_path = release_dir / "RELEASE_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manual_ui_acceptance"] = load_manual_ui_acceptance(str(report_path))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def test_phase9_release_outputs_and_manifest() -> None:
    release_dir = _run_prepare()
    try:
        commercial_dir = release_dir / COMMERCIAL_NAME
        source_dir = release_dir / SOURCE_NAME
        commercial_zip = release_dir / f"{COMMERCIAL_NAME}.zip"
        source_zip = release_dir / f"{SOURCE_NAME}.zip"
        manifest_path = release_dir / "RELEASE_MANIFEST.json"

        assert not validate_package(commercial_dir, package_type="commercial")
        assert not validate_package(source_dir, package_type="source")
        assert {".sqlite3-wal", ".sqlite3-shm"}.issubset(SOURCE_EXCLUDE_SUFFIXES)
        assert {".sqlite3-wal", ".sqlite3-shm"}.issubset(SOURCE_FORBIDDEN_SUFFIXES)
        assert {".sqlite3-wal", ".sqlite3-shm"}.issubset(COMMERCIAL_FORBIDDEN_SUFFIXES)
        assert commercial_zip.exists()
        assert source_zip.exists()
        assert manifest_path.exists()

        commercial_files = _files(commercial_dir)
        assert TEST_INSTALLER_NAME in commercial_files
        assert "A股选股仪.exe" not in commercial_files
        assert "使用说明.txt" in commercial_files
        assert "免责声明.txt" in commercial_files
        assert "版本说明.txt" in commercial_files
        assert "常见问题.txt" in commercial_files
        assert "示例策略_v1.json" in commercial_files
        assert not any(path.endswith(".py") for path in commercial_files)
        assert "data_location.txt" not in commercial_files

        source_files = _files(source_dir)
        assert "main.py" in source_files
        assert "stock_tool/core/performance_loop.py" in source_files
        assert "stock_tool/core/professional_formula.py" in source_files
        assert "stock_tool/ui/qt_workbench.py" in source_files
        assert "tools/test_performance_loop.py" in source_files
        assert "tools/prepare_phase9_release.py" in source_files
        assert "tools/final_release_gate.py" in source_files
        assert "tools/build_protected_installer.py" in source_files
        assert "tools/sync_github_source_package.py" in source_files
        assert "tools/generate_license.py" not in source_files
        assert "tools/generate_license_keypair.py" not in source_files
        assert "stock_tool/core/license.py" not in source_files
        assert "生成第九阶段交付包.bat" in source_files
        assert ".github/PULL_REQUEST_TEMPLATE.md" in source_files
        assert ".github/ISSUE_TEMPLATE/bug_report.md" in source_files
        assert "data_location.txt" not in source_files
        assert not any(path.startswith("my_data/") for path in source_files)
        assert not any(path.startswith("dist/") for path in source_files)
        assert not any(path.startswith("build/") for path in source_files)
        assert not any(path.startswith("installer_output/") for path in source_files)
        assert not any(path.startswith(".pyarmor/") for path in source_files)
        assert not any(path.startswith(".toolcache/") for path in source_files)
        assert not any(path.startswith("release/") for path in source_files)
        assert not any(path.startswith("docs/acceptance_reports/") for path in source_files)
        assert not any(path.endswith(".sqlite3") for path in source_files)
        assert not any(path.endswith((".sqlite3-wal", ".sqlite3-shm")) for path in source_files)
        assert not any(path.startswith("private_keys/") for path in source_files)
        assert not any(path.endswith(".pem") for path in source_files)
        assert not any(path.endswith(".key") for path in source_files)
        assert not any(path.endswith((".p12", ".crt", ".der")) for path in source_files)
        assert ".env.local" not in source_files
        assert "service_private_key.json" not in source_files
        assert "private_keys/license_private_key.json" not in source_files

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["version"] == "v0.2.7_phase9"
        assert manifest["title"] == "A股选股仪 第九阶段商业交付包"
        assert manifest["live_status"] == "not_run"
        assert manifest["offline_acceptance"] is None
        assert manifest["manual_ui_acceptance"]["status"] == "pending"
        assert manifest["manual_ui_acceptance"]["accepted"] is False
        assert manifest["commercial_zip"] == str(commercial_zip)
        assert manifest["source_zip"] == str(source_zip)
        assert str(Path(manifest["commercial_dir"])) == str(commercial_dir)
        assert str(Path(manifest["source_dir"])) == str(source_dir)
        assert f"{COMMERCIAL_NAME}.zip" in manifest["sha256"]
        assert f"{SOURCE_NAME}.zip" in manifest["sha256"]
        assert manifest["protection"]["mode"] == "free-pyarmor"
        assert manifest["protection"]["protection_level"] == "trial_obfuscation"
        assert manifest["protection"]["pyarmor_enabled"] is True
        assert manifest["protection"]["bcc_enabled"] is False
        assert manifest["protection"]["bcc_required"] is False
        assert manifest["protection"]["bcc_verified"] is False
        assert manifest["protection"]["trial_build"] is True
        assert set(FREE_PYARMOR_TARGETS).issubset(set(manifest["protection"]["obfuscated_targets"]))
        assert manifest["protection"]["skipped_big_scripts"]
        assert manifest["protection"]["installer_name"] == TEST_INSTALLER_NAME
        assert manifest["protection"]["installer_sha256"] == manifest["sha256"][f"{COMMERCIAL_NAME}/{TEST_INSTALLER_NAME}"]
        assert manifest["rules"]["commercial_no_source"] is True
        assert manifest["rules"]["commercial_allowlist_only"] is True
        assert manifest["rules"]["commercial_installer_only"] is True
        assert manifest["rules"]["commercial_requires_pyarmor"] is True
        assert manifest["rules"]["commercial_allowed_protection_modes"] == ["bcc", "free-pyarmor"]
        assert manifest["rules"]["commercial_requires_pyarmor_bcc"] is False
        assert manifest["rules"]["cold_start_requires_customer_data_dir"] is True
        assert manifest["rules"]["source_no_personal_data"] is True
        assert manifest["rules"]["manual_ui_acceptance_required_before_external_delivery"] is True

        with zipfile.ZipFile(commercial_zip) as zf:
            names = set(zf.namelist())
            assert f"{COMMERCIAL_NAME}/{TEST_INSTALLER_NAME}" in names
            assert f"{COMMERCIAL_NAME}/A股选股仪.exe" not in names
            assert not any(name.endswith(".py") for name in names)
            assert not any(name.endswith("data_location.txt") for name in names)
            assert not any("generate_license" in name for name in names)
        with zipfile.ZipFile(source_zip) as zf:
            names = set(zf.namelist())
            assert f"{SOURCE_NAME}/main.py" in names
            assert not any("/my_data/" in name for name in names)
            assert not any(name.endswith(".sqlite3") for name in names)
            assert not any(name.endswith((".sqlite3-wal", ".sqlite3-shm")) for name in names)
            assert not any("/private_keys/" in name for name in names)
            assert not any(name.endswith("license_private_key.json") for name in names)
    finally:
        if release_dir.exists():
            shutil.rmtree(release_dir)


def test_gitignore_phase9_sensitive_patterns() -> None:
    errors = verify_gitignore()
    assert not errors, "\n".join(errors)
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in GITIGNORE_REQUIRED_PATTERNS:
        assert pattern in text


def test_commercial_build_includes_baostock_dependency() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "baostock==0.9.2" in requirements

    for spec_name in ["build.spec", "build_qt.spec"]:
        spec_text = (ROOT / spec_name).read_text(encoding="utf-8")
        assert "collect_submodules" in spec_text
        assert 'collect_submodules(\n    "baostock",' in spec_text
        assert 'is_module_or_submodule(name, "baostock.demo")' in spec_text


def test_phase9_source_package_excludes_common_sensitive_files() -> None:
    stamp = f"{TEST_STAMP}_SENSITIVE"
    sensitive_files = [
        ROOT / ".env.local",
        ROOT / "sample_certificate.p12",
        ROOT / "sample_certificate.crt",
        ROOT / "debug_export.log",
        ROOT / "sample.private.json",
        ROOT / "openai_api_key.txt",
        ROOT / "db_password.txt",
        ROOT / "service_private_key.json",
    ]
    release_dir = _release_dir(stamp)
    try:
        for path in sensitive_files:
            path.write_text("secret", encoding="utf-8")
        release_dir = _run_prepare(stamp)
        source_files = _files(release_dir / SOURCE_NAME)
        for path in sensitive_files:
            assert path.name not in source_files
        with zipfile.ZipFile(release_dir / f"{SOURCE_NAME}.zip") as zf:
            names = {Path(name).name for name in zf.namelist()}
        for path in sensitive_files:
            assert path.name not in names
    finally:
        for path in sensitive_files:
            if path.exists():
                path.unlink()
        if release_dir.exists():
            shutil.rmtree(release_dir)


def test_manual_ui_acceptance_report_can_feed_manifest() -> None:
    stamp = f"{TEST_STAMP}_MANUAL"
    release_dir = _run_prepare(stamp)
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp)
        completed = subprocess.run(
            [
                sys.executable,
                "tools/record_manual_ui_acceptance.py",
                "--release-dir",
                str(release_dir),
                "--operator",
                "自动化测试",
                "--machine",
                "test-machine",
                "--mark-all-pass",
                "--report-dir",
                str(report_dir),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        reports = sorted(report_dir.glob("manual_ui_acceptance_*.json"))
        assert len(reports) == 1
        payload = json.loads(reports[0].read_text(encoding="utf-8"))
        assert payload["summary"]["accepted"] is True
        assert payload["release_evidence"]["available"] is True
        assert payload["release_evidence"]["commercial_zip_sha256"]
        manifest = json.loads((release_dir / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
        try:
            manual = manifest["manual_ui_acceptance"]
            assert manual["status"] == "accepted"
            assert manual["accepted"] is True
            assert manual["operator"] == "自动化测试"
            assert manual["machine"] == "test-machine"
            assert manual["failed"] == 0
            assert manual["release_evidence"]["commercial_zip_sha256"]
            assert manual["json_report"] == "manual_ui_acceptance.json"
            assert manual["txt_report"] == "manual_ui_acceptance.txt"
            assert manual["original_json_report"] == str(reports[0].resolve())
            assert manual["items"]
            assert (release_dir / "manual_ui_acceptance.json").exists()
            assert (release_dir / "manual_ui_acceptance.txt").exists()
            assert manifest["sha256"]["manual_ui_acceptance.json"]
            assert manifest["sha256"]["manual_ui_acceptance.txt"]
        finally:
            if release_dir.exists():
                shutil.rmtree(release_dir)


def test_github_source_sync_script_copies_clean_source_package() -> None:
    stamp = f"{TEST_STAMP}_SYNC"
    release_dir = _run_prepare(stamp)
    try:
        source_dir = release_dir / SOURCE_NAME
        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp) / "private_repo"
            target_dir.mkdir()
            (target_dir / "old_file.txt").write_text("old", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "tools/sync_github_source_package.py",
                    "--source-dir",
                    str(source_dir),
                    "--target-dir",
                    str(target_dir),
                    "--allow-non-git",
                    "--report-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            files = _files(target_dir)
            assert "main.py" in files
            assert "tools/sync_github_source_package.py" in files
            assert "old_file.txt" not in files
            assert not any(path.startswith("private_keys/") for path in files)
            assert not any(path.endswith(".sqlite3") for path in files)
            reports = sorted(Path(tmp).glob("github_source_sync_*.json"))
            assert reports
            report = json.loads(reports[-1].read_text(encoding="utf-8"))
            assert report["summary"]["copied"] > 0
            assert report["target_forbidden_errors"] == []
    finally:
        if release_dir.exists():
            shutil.rmtree(release_dir)


def test_final_release_gate_blocks_pending_manual_and_passes_accepted() -> None:
    bcc_disabled_stamp = f"{TEST_STAMP}_GATE_BCC_DISABLED"
    bcc_disabled_release = _run_prepare(bcc_disabled_stamp, protection_mode="bcc")
    try:
        manifest_path = bcc_disabled_release / "RELEASE_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["protection"]["bcc_enabled"] = False
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    "tools/final_release_gate.py",
                    "--release-dir",
                    str(bcc_disabled_release),
                    "--allow-manual-pending",
                    "--allow-missing-offline",
                    "--allow-live-not-run",
                    "--internal-precheck",
                    "--report-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            assert completed.returncode != 0, completed.stdout + completed.stderr
            assert "bcc 模式下 protection.bcc_enabled 必须为 true" in completed.stdout
    finally:
        if bcc_disabled_release.exists():
            shutil.rmtree(bcc_disabled_release)

    plain_stamp = f"{TEST_STAMP}_GATE_PLAIN"
    plain_release = _run_prepare(plain_stamp, protection_mode="plain-installer")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    "tools/final_release_gate.py",
                    "--release-dir",
                    str(plain_release),
                    "--allow-manual-pending",
                    "--allow-missing-offline",
                    "--allow-live-not-run",
                    "--internal-precheck",
                    "--report-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            assert completed.returncode != 0, completed.stdout + completed.stderr
            assert "protection.mode 不允许商业外发：plain-installer" in completed.stdout
    finally:
        if plain_release.exists():
            shutil.rmtree(plain_release)

    relaxed_without_internal_stamp = f"{TEST_STAMP}_GATE_RELAXED_NO_INTERNAL"
    relaxed_without_internal_release = _run_prepare(relaxed_without_internal_stamp)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    "tools/final_release_gate.py",
                    "--release-dir",
                    str(relaxed_without_internal_release),
                    "--allow-missing-offline",
                    "--allow-live-not-run",
                    "--report-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            assert completed.returncode != 0, completed.stdout + completed.stderr
            assert "allow-* 放宽参数只能与 --internal-precheck" in completed.stdout
    finally:
        if relaxed_without_internal_release.exists():
            shutil.rmtree(relaxed_without_internal_release)

    pending_stamp = f"{TEST_STAMP}_GATE_PENDING"
    pending_release = _run_prepare(pending_stamp)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    "tools/final_release_gate.py",
                    "--release-dir",
                    str(pending_release),
                    "--allow-missing-offline",
                    "--allow-live-not-run",
                    "--internal-precheck",
                    "--report-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            assert completed.returncode != 0, completed.stdout + completed.stderr
            assert "人工 UI 验收未 accepted" in completed.stdout
    finally:
        if pending_release.exists():
            shutil.rmtree(pending_release)

    accepted_stamp = f"{TEST_STAMP}_GATE_ACCEPTED"
    accepted_release = _run_prepare(accepted_stamp)
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp) / "reports"
        completed = subprocess.run(
            [
                sys.executable,
                "tools/record_manual_ui_acceptance.py",
                "--release-dir",
                str(accepted_release),
                "--operator",
                "自动化测试",
                "--machine",
                "test-machine",
                "--mark-all-pass",
                "--report-dir",
                str(report_dir),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        manual_report = sorted(report_dir.glob("manual_ui_acceptance_*.json"))[-1]
        try:
            manifest = json.loads((accepted_release / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
            assert manifest["manual_ui_acceptance"]["json_report"] == "manual_ui_acceptance.json"
            assert (accepted_release / "manual_ui_acceptance.json").exists()
            shutil.rmtree(report_dir)
            completed = subprocess.run(
                [
                    sys.executable,
                    "tools/final_release_gate.py",
                    "--release-dir",
                    str(accepted_release),
                    "--allow-missing-offline",
                    "--allow-live-not-run",
                    "--internal-precheck",
                    "--report-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            gate_reports = sorted(Path(tmp).glob("final_release_gate_*.json"))
            assert gate_reports
            payload = json.loads(gate_reports[-1].read_text(encoding="utf-8"))
            assert payload["passed"] is True
            assert payload["summary"]["manual_ui_status"] == "accepted"

            (accepted_release / "leaked.sqlite3").write_text("secret", encoding="utf-8")
            (accepted_release / ".env.local").write_text("API_KEY=secret", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "tools/final_release_gate.py",
                    "--release-dir",
                    str(accepted_release),
                    "--allow-missing-offline",
                    "--allow-live-not-run",
                    "--internal-precheck",
                    "--report-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            assert completed.returncode != 0, completed.stdout + completed.stderr
            assert "release 根目录包含禁入文件：leaked.sqlite3" in completed.stdout
            assert "release 根目录包含禁入文件：.env.local" in completed.stdout
            gate_reports = sorted(Path(tmp).glob("final_release_gate_*.json"))
            payload = json.loads(gate_reports[-1].read_text(encoding="utf-8"))
            assert payload["passed"] is False
            assert payload["release_root_forbidden_errors"]
        finally:
            if accepted_release.exists():
                shutil.rmtree(accepted_release)

    wrong_report_stamp = f"{TEST_STAMP}_GATE_WRONG_REPORT"
    wrong_report_release = _run_prepare(wrong_report_stamp)
    mismatched_stamp = f"{TEST_STAMP}_GATE_MISMATCH"
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp) / "reports"
        completed = subprocess.run(
            [
                sys.executable,
                "tools/record_manual_ui_acceptance.py",
                "--release-dir",
                str(wrong_report_release),
                "--operator",
                "自动化测试",
                "--machine",
                "test-machine",
                "--mark-all-pass",
                "--report-dir",
                str(report_dir),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        manual_report = sorted(report_dir.glob("manual_ui_acceptance_*.json"))[-1]
        wrong_payload = json.loads(manual_report.read_text(encoding="utf-8"))
        wrong_payload["release_evidence"]["commercial_zip_sha256"] = "0" * 64
        manual_report.write_text(json.dumps(wrong_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        mismatched_release = _run_prepare(mismatched_stamp)
        _attach_manual_report(mismatched_release, manual_report)
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "tools/final_release_gate.py",
                    "--release-dir",
                    str(mismatched_release),
                    "--allow-missing-offline",
                    "--allow-live-not-run",
                    "--internal-precheck",
                    "--report-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            assert completed.returncode != 0, completed.stdout + completed.stderr
            assert "人工 UI 验收报告与当前商业 zip SHA256 不一致" in completed.stdout
        finally:
            if wrong_report_release.exists():
                shutil.rmtree(wrong_report_release)
            if mismatched_release.exists():
                shutil.rmtree(mismatched_release)

    na_stamp = f"{TEST_STAMP}_GATE_REQUIRED_NA"
    na_release = _run_prepare(na_stamp)
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp) / "reports"
        completed = subprocess.run(
            [
                sys.executable,
                "tools/record_manual_ui_acceptance.py",
                "--release-dir",
                str(na_release),
                "--operator",
                "自动化测试",
                "--machine",
                "test-machine",
                "--na",
                "commercial_first_data_dir",
                "--report-dir",
                str(report_dir),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        assert completed.returncode != 0, completed.stdout + completed.stderr
        reports = sorted(report_dir.glob("manual_ui_acceptance_*.json"))
        assert reports
        payload = json.loads(reports[-1].read_text(encoding="utf-8"))
        assert payload["summary"]["accepted"] is False
        assert payload["summary"]["required_not_applicable"] == 1

        payload["summary"]["accepted"] = True
        payload["summary"]["status"] = "accepted"
        forged_report = report_dir / "forged_required_na.json"
        forged_report.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _attach_manual_report(na_release, forged_report)
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "tools/final_release_gate.py",
                    "--release-dir",
                    str(na_release),
                    "--allow-missing-offline",
                    "--allow-live-not-run",
                    "--internal-precheck",
                    "--report-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            assert completed.returncode != 0, completed.stdout + completed.stderr
            assert "required 项存在不适用" in completed.stdout or "required 不适用项" in completed.stdout
        finally:
            if na_release.exists():
                shutil.rmtree(na_release)


def run_tests() -> None:
    test_phase9_release_outputs_and_manifest()
    test_gitignore_phase9_sensitive_patterns()
    test_commercial_build_includes_baostock_dependency()
    test_phase9_source_package_excludes_common_sensitive_files()
    test_manual_ui_acceptance_report_can_feed_manifest()
    test_github_source_sync_script_copies_clean_source_package()
    test_final_release_gate_blocks_pending_manual_and_passes_accepted()
    print("[OK] 第九阶段交付专项测试通过")


if __name__ == "__main__":
    run_tests()
