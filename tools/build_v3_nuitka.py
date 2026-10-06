from __future__ import annotations

"""Build the formal V3 standalone package with the pinned Nuitka toolchain."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import winreg
from datetime import datetime
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.config.release_identity import (
    APP_VERSION,
    DIST_DIRECTORY_NAME,
    EXE_NAME,
    INSTALL_DIRECTORY_NAME,
    INSTALLER_BASE_FILENAME,
    INSTALLER_FILE_DESCRIPTION,
    INSTALLER_NAME,
    NUITKA_VERSION,
    PRODUCT_NAME,
    PUBLISHER_NAME,
    VERSION_NOTE_FILE,
    WINDOWS_FILE_VERSION,
    as_manifest_identity,
)
from tools.release_integrity import atomic_write_json, file_record as integrity_file_record
from tools.release_schema import require_schema


BUILD_ROOT = ROOT / "build"
NUITKA_OUTPUT_ROOT = BUILD_ROOT / "nuitka"
COMPILATION_REPORT = BUILD_ROOT / "nuitka-compilation-report.xml"
RESOURCE_MANIFEST = BUILD_ROOT / "nuitka-resource-manifest.json"
BUILD_MANIFEST = BUILD_ROOT / "v3_build_manifest.json"
DIST_DIR = ROOT / "dist" / DIST_DIRECTORY_NAME
INSTALLER_OUTPUT_DIR = ROOT / "installer_output"
INSTALLER_PATH = INSTALLER_OUTPUT_DIR / INSTALLER_NAME
DEPENDENCY_LOCK_PATH = ROOT / "requirements-v3.lock"
FRONTEND_CONTRACT_PATH = ROOT / "stock_tool" / "config" / "frontend_parity_contract.json"
PROMOTION_RECORD_PATH = ROOT / "stock_tool" / "config" / "v3_rc0_promotion_record.json"
BUILD_IDENTITY_RELATIVE_PATH = Path("stock_tool/config/v3_build_identity.json")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tool_record(
    path: Path,
    version_command: list[str],
    role: str,
    *,
    accepted_returncodes: tuple[int, ...] = (0,),
) -> dict[str, object]:
    completed = subprocess.run(version_command, text=True, capture_output=True, timeout=30)
    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    if completed.returncode not in accepted_returncodes or not output:
        raise RuntimeError(f"无法确认构建工具版本：{path.name}")
    return {**integrity_file_record(path, root=path.parent, role=role).to_dict(), "path": str(path.resolve()), "version_output": output[:1000]}


def _installed_inno_version(path: Path) -> str:
    uninstall_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    resolved_path = path.resolve()
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                root_key = winreg.OpenKey(hive, uninstall_key, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with root_key:
                for index in range(winreg.QueryInfoKey(root_key)[0]):
                    try:
                        child_name = winreg.EnumKey(root_key, index)
                        child_key = winreg.OpenKey(root_key, child_name)
                        with child_key:
                            display_name = str(winreg.QueryValueEx(child_key, "DisplayName")[0])
                            version = str(winreg.QueryValueEx(child_key, "DisplayVersion")[0]).strip()
                            install_location = str(winreg.QueryValueEx(child_key, "InstallLocation")[0]).strip()
                    except OSError:
                        continue
                    if not display_name.startswith("Inno Setup version 6") or not version or not install_location:
                        continue
                    registered_iscc = (Path(install_location) / "ISCC.exe").resolve()
                    if registered_iscc == resolved_path:
                        return version
    raise RuntimeError(f"无法从 Windows 安装记录确认 Inno Setup 精确版本：{path}")


def _inno_record(path: Path) -> dict[str, object]:
    # ISCC 6.7.x prints valid help for /? but intentionally exits with code 1.
    record = _tool_record(
        path,
        [str(path), "/?"],
        "inno_setup_compiler",
        accepted_returncodes=(0, 1),
    )
    record["version_output"] = f"Inno Setup {_installed_inno_version(path)}\n{record['version_output']}"[:1000]
    return record


def _mingw_record() -> dict[str, object]:
    roots = []
    override = os.environ.get("NUITKA_CACHE_DIR", "").strip()
    if override:
        roots.append(Path(override))
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        roots.append(Path(local_app_data) / "Nuitka" / "Nuitka" / "Cache" / "downloads" / "gcc")
    candidates = [path for root in roots if root.is_dir() for path in root.rglob("gcc.exe")]
    if not candidates:
        raise RuntimeError("Nuitka 构建完成后未找到 managed MinGW64 gcc.exe，无法固化工具链。")
    gcc = max(candidates, key=lambda item: item.stat().st_mtime)
    return _tool_record(gcc, [str(gcc), "--version"], "nuitka_managed_mingw64")


def git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def require_clean_commit() -> str:
    completed = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True, capture_output=True, check=True
    )
    if completed.stdout.strip():
        raise RuntimeError("V3 候选必须从干净 commit 构建；请提交或清理当前 worktree 后重试。")
    return git_commit()


def require_nuitka_version() -> str:
    try:
        installed = importlib.metadata.version("Nuitka")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError(f"缺少 Nuitka=={NUITKA_VERSION}；请先安装 requirements-build.txt。") from exc
    if installed != NUITKA_VERSION:
        raise RuntimeError(f"Nuitka 版本必须为 {NUITKA_VERSION}，当前为 {installed}。")
    return installed


def dependency_lock() -> dict[str, object]:
    if not DEPENDENCY_LOCK_PATH.is_file():
        raise RuntimeError(f"缺少 V3 构建依赖锁：{DEPENDENCY_LOCK_PATH}")
    python_version = ""
    packages: dict[str, str] = {}
    hashes: dict[str, list[str]] = {}
    canonical_names: set[str] = set()
    current_name = ""
    for raw_line in DEPENDENCY_LOCK_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# python=="):
            python_version = line.removeprefix("# python==").strip()
            continue
        if line.startswith("#"):
            continue
        if line.startswith("--hash=sha256:"):
            digest = line.removeprefix("--hash=sha256:").rstrip(" \\")
            if not current_name or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise RuntimeError(f"V3 构建依赖锁 hash 格式无效：{raw_line}")
            hashes[current_name].append(digest)
            continue
        name, separator, version = line.partition("==")
        if not separator or not name or not version:
            raise RuntimeError(f"V3 构建依赖锁格式无效：{raw_line}")
        version = version.rstrip(" \\")
        canonical_name = canonicalize_name(name)
        if canonical_name in canonical_names:
            raise RuntimeError(f"V3 构建依赖锁包含重复包名：{name}")
        canonical_names.add(canonical_name)
        packages[name] = version
        hashes[name] = []
        current_name = name
    if not python_version or not packages:
        raise RuntimeError("V3 构建依赖锁必须记录 Python 与完整依赖版本集合。")
    missing_hashes = [name for name, values in hashes.items() if not values]
    if missing_hashes:
        raise RuntimeError("V3 构建依赖锁缺少 SHA256：" + "、".join(missing_hashes))
    return {
        "path": str(DEPENDENCY_LOCK_PATH.relative_to(ROOT)).replace("\\", "/"),
        "sha256": sha256_file(DEPENDENCY_LOCK_PATH),
        "python_version": python_version,
        "packages": dict(sorted(packages.items())),
        "hashes": {name: hashes[name] for name in sorted(hashes)},
    }


def dependency_lock_gaps(lock: dict[str, object]) -> list[str]:
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        return ["依赖锁 packages 格式无效"]
    locked_versions = {canonicalize_name(str(name)): str(version) for name, version in packages.items()}
    environment = default_environment()
    environment["extra"] = ""
    gaps: list[str] = []
    for locked_name in sorted(packages):
        try:
            distribution = importlib.metadata.distribution(str(locked_name))
        except importlib.metadata.PackageNotFoundError:
            continue
        for requirement_text in distribution.requires or []:
            requirement = Requirement(requirement_text)
            if requirement.marker and not requirement.marker.evaluate(environment):
                continue
            dependency_name = canonicalize_name(requirement.name)
            locked_version = locked_versions.get(dependency_name)
            if locked_version is None:
                gaps.append(f"{locked_name} 的活动传递依赖未锁定：{requirement.name}{requirement.specifier}")
            elif requirement.specifier and locked_version not in requirement.specifier:
                gaps.append(
                    f"{locked_name} 要求 {requirement.name}{requirement.specifier}，锁文件固定为 {locked_version}"
                )
    return sorted(set(gaps))


def require_dependency_lock() -> dict[str, object]:
    lock = dependency_lock()
    actual_python = sys.version.split()[0]
    if actual_python != lock["python_version"]:
        raise RuntimeError(f"Python 必须为锁定版本 {lock['python_version']}，当前为 {actual_python}。")
    for name, expected in lock["packages"].items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"缺少锁定依赖 {name}=={expected}。") from exc
        if actual != expected:
            raise RuntimeError(f"依赖 {name} 必须为 {expected}，当前为 {actual}。")
    gaps = dependency_lock_gaps(lock)
    if gaps:
        raise RuntimeError("V3 构建依赖锁不是完整活动传递依赖闭包：\n" + "\n".join(gaps))
    return lock


def _tracked_file(path: Path) -> None:
    relative = str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    completed = subprocess.run(
        ["git", "ls-files", "--error-unmatch", relative], cwd=ROOT, text=True, capture_output=True
    )
    if completed.returncode != 0:
        raise RuntimeError(f"RC1 来源文件必须已纳入当前 Git commit：{relative}")


def rc1_promotion_inputs() -> tuple[dict[str, object], dict[str, object]]:
    if not PROMOTION_RECORD_PATH.is_file():
        raise RuntimeError("RC1 构建缺少 RC0 转正记录。")
    if not FRONTEND_CONTRACT_PATH.is_file():
        raise RuntimeError("RC1 构建缺少前端合同。")
    _tracked_file(PROMOTION_RECORD_PATH)
    _tracked_file(FRONTEND_CONTRACT_PATH)
    promotion = json.loads(PROMOTION_RECORD_PATH.read_text(encoding="utf-8"))
    contract = json.loads(FRONTEND_CONTRACT_PATH.read_text(encoding="utf-8"))
    require_schema("promotion", promotion)
    if promotion.get("schema_version") != 2 or promotion.get("conclusion") != "accepted":
        raise RuntimeError("RC1 构建要求已 accepted 的 RC0 转正记录。")
    try:
        from tools.test_frontend_default_gate import validate_contract

        failures = validate_contract(contract, strict=True)
    except Exception as exc:  # pragma: no cover - defensive build boundary
        raise RuntimeError(f"无法验证 RC1 前端合同：{exc}") from exc
    if failures:
        raise RuntimeError("RC1 前端合同未转正：\n" + "\n".join(failures))
    promotion_record = {
        "path": str(PROMOTION_RECORD_PATH.relative_to(ROOT)).replace("\\", "/"),
        "sha256": sha256_file(PROMOTION_RECORD_PATH),
    }
    frontend_contract = {
        "path": str(FRONTEND_CONTRACT_PATH.relative_to(ROOT)).replace("\\", "/"),
        "sha256": sha256_file(FRONTEND_CONTRACT_PATH),
    }
    return promotion_record, frontend_contract


def run_command(command: list[str], *, env: dict[str, str] | None = None) -> dict:
    started_at = datetime.now().isoformat(timespec="seconds")
    print("运行：" + " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=env)
    output = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    payload = {
        "command": command,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "output_tail": output.strip()[-8000:],
    }
    if not payload["ok"]:
        raise RuntimeError("Nuitka 命令失败：\n" + payload["output_tail"])
    return payload


def safe_rmtree(path: Path) -> None:
    resolved = path.resolve()
    root = ROOT.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise RuntimeError(f"拒绝删除项目外路径：{path}")
    if path.exists():
        shutil.rmtree(path)


def nuitka_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--mingw64",
        "--assume-yes-for-downloads",
        # Dependency Walker 2.2 corrupts non-ASCII Python installation paths.
        # Nuitka 4.1.3 ships pefile and exposes this deterministic alternative.
        "--experimental=force-dependencies-pefile",
        "--enable-plugin=pyside6",
        # QtWebEngine/WebChannel are Python modules (included below), not valid
        # Nuitka PySide6 Qt-plugin family names.  Keep this list to families
        # accepted by Nuitka 4.1.3; its PySide6 package configuration follows
        # the imported WebEngine module to include QtWebEngineProcess/resources.
        "--include-qt-plugins=platforms,styles,imageformats,webview",
        "--include-module=PySide6.QtWebEngineCore",
        "--include-module=PySide6.QtWebEngineWidgets",
        "--include-module=PySide6.QtWebChannel",
        "--include-package=tickflow",
        "--include-data-dir=stock_tool/config=stock_tool/config",
        "--include-data-dir=stock_tool/ui/web=stock_tool/ui/web",
        "--include-data-dir=assets=assets",
        "--windows-console-mode=disable",
        "--windows-icon-from-ico=assets/stocklens.ico",
        f"--file-version={WINDOWS_FILE_VERSION}",
        f"--product-version={WINDOWS_FILE_VERSION}",
        f"--product-name={PRODUCT_NAME}",
        f"--file-description={INSTALLER_FILE_DESCRIPTION}",
        f"--report={COMPILATION_REPORT}",
        f"--output-dir={NUITKA_OUTPUT_ROOT}",
        f"--output-filename={EXE_NAME}",
        "main.py",
    ]


def locate_standalone_dir() -> Path:
    candidates = sorted(path for path in NUITKA_OUTPUT_ROOT.rglob("*.dist") if (path / EXE_NAME).is_file())
    if len(candidates) != 1:
        raise RuntimeError(f"无法唯一定位 Nuitka standalone 目录：{candidates}")
    return candidates[0]


def resource_records(directory: Path) -> list[dict[str, str | int]]:
    return [
        {
            "path": str(path.relative_to(directory)).replace("\\", "/"),
            "role": "standalone_resource",
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]


def build_installer() -> dict:
    iscc = os.environ.get("ISCC_EXE", "").strip() or shutil.which("ISCC.exe") or shutil.which("ISCC")
    if not iscc:
        raise RuntimeError("未找到 Inno Setup 6 的 ISCC.exe；可设置 ISCC_EXE 后重试。")
    INSTALLER_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        str(iscc),
        f"/DMyAppName={PRODUCT_NAME}",
        f"/DMyPublisher={PUBLISHER_NAME}",
        f"/DMyAppVersion={APP_VERSION}",
        f"/DMyAppExeName={EXE_NAME}",
        f"/DMyDistDir=dist\\{DIST_DIRECTORY_NAME}",
        f"/DReleaseNoteFile={VERSION_NOTE_FILE}",
        f"/DInstallerBaseFilename={INSTALLER_BASE_FILENAME}",
        f"/DMyDefaultDirName={INSTALL_DIRECTORY_NAME}",
        "/DInstallerOutputDir=installer_output",
        "installer.iss",
    ]
    result = run_command(command)
    if not INSTALLER_PATH.is_file():
        raise RuntimeError(f"Inno Setup 未生成预期安装包：{INSTALLER_PATH}")
    result["installer"] = integrity_file_record(INSTALLER_PATH, root=ROOT, role="customer_installer").to_dict()
    iscc_path = Path(iscc).resolve()
    result["tool"] = _inno_record(iscc_path)
    return result


def build(*, candidate_stage: str = "rc0") -> dict:
    if candidate_stage not in {"rc0", "rc1"}:
        raise ValueError("candidate_stage 必须为 rc0 或 rc1")
    commit = require_clean_commit()
    nuitka_version = require_nuitka_version()
    lock = require_dependency_lock()
    promotion_record: dict[str, object] | None = None
    frontend_contract: dict[str, object] | None = None
    if candidate_stage == "rc1":
        promotion_record, frontend_contract = rc1_promotion_inputs()
    safe_rmtree(NUITKA_OUTPUT_ROOT)
    safe_rmtree(DIST_DIR)
    for path in (COMPILATION_REPORT, RESOURCE_MANIFEST, BUILD_MANIFEST):
        if path.exists():
            path.unlink()
    NUITKA_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    nuitka_result = run_command(nuitka_command())
    source_dir = locate_standalone_dir()
    DIST_DIR.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source_dir), str(DIST_DIR))
    exe_path = DIST_DIR / EXE_NAME
    if not exe_path.is_file() or not COMPILATION_REPORT.is_file():
        raise RuntimeError("Nuitka 输出缺少主程序或 compilation report。")
    build_identity_path = DIST_DIR / BUILD_IDENTITY_RELATIVE_PATH
    atomic_write_json(
        build_identity_path,
        {
            "schema_version": 1,
            "candidate_id": f"v3-{commit[:12]}",
            "candidate_stage": candidate_stage,
            "git_commit": commit,
            "product_version": APP_VERSION,
        },
    )
    resources = resource_records(DIST_DIR)
    RESOURCE_MANIFEST.write_text(
        json.dumps({"root": str(DIST_DIR), "files": resources}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    installer_result = build_installer()
    manifest = {
        "schema_version": 5,
        **as_manifest_identity(),
        "candidate_id": f"v3-{commit[:12]}",
        "git_commit": commit,
        "candidate_stage": candidate_stage,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "build": {
            "backend": "nuitka",
            "nuitka_version": nuitka_version,
            "python_version": sys.version.split()[0],
            "pyside6_version": importlib.metadata.version("PySide6"),
            "compiler": "nuitka-managed-mingw64",
            "mode": "standalone",
            "onefile": False,
            "command": nuitka_result["command"],
            "compile_report": integrity_file_record(COMPILATION_REPORT, root=ROOT, role="nuitka_compile_report").to_dict(),
            "resource_manifest": integrity_file_record(RESOURCE_MANIFEST, root=ROOT, role="nuitka_resource_manifest").to_dict(),
            "build_identity": integrity_file_record(build_identity_path, root=ROOT, role="embedded_build_identity").to_dict(),
            "toolchain": {
                "python": {**integrity_file_record(Path(sys.executable), root=Path(sys.executable).parent, role="python_interpreter").to_dict(), "version": sys.version},
                "pip_version": importlib.metadata.version("pip"),
                "nuitka_version": nuitka_version,
                "pyside6_version": importlib.metadata.version("PySide6"),
                "mingw": _mingw_record(),
                "inno": installer_result["tool"],
                "installed_distributions": {
                    distribution.metadata["Name"]: distribution.version
                    for distribution in sorted(importlib.metadata.distributions(), key=lambda item: (item.metadata["Name"] or "").lower())
                    if distribution.metadata["Name"]
                },
            },
        },
        "dependency_lock": lock,
        "frontend_contract": frontend_contract,
        "promotion_record": promotion_record,
        "artifacts": {
            "exe": integrity_file_record(exe_path, root=ROOT, role="application_executable").to_dict(),
            "installer": installer_result["installer"],
        },
    }
    require_schema("build", manifest)
    atomic_write_json(BUILD_MANIFEST, manifest)
    print(f"[OK] V3 standalone：{DIST_DIR}")
    print(f"[OK] V3 安装包：{INSTALLER_PATH}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="从干净 commit 构建 Stocklens V3 Nuitka standalone 与安装包。")
    parser.add_argument("--candidate-stage", choices=("rc0", "rc1"), default="rc0")
    args = parser.parse_args()
    build(candidate_stage=args.candidate_stage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
