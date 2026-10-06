from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_ROOT = ROOT / "release"
COMMERCIAL_NAME = "01_商业出售版_不含源码"
SOURCE_NAME = "02_GitHub源码版_可二次开发"
DEFAULT_APP_VERSION = "0.2.7"
DEFAULT_INSTALLER_NAME = "Stocklens_V2.exe"
INSTALLER_NAME_RE = re.compile(r"^Stocklens_V2\.exe$")
PROTECTED_BUILD_MANIFEST = ROOT / "build" / "protected_installer_manifest.json"
ZIP_FIXED_DATE_TIME = (2024, 1, 1, 0, 0, 0)
RELEASE_MANUAL_JSON = "manual_ui_acceptance.json"
RELEASE_MANUAL_TXT = "manual_ui_acceptance.txt"
COMMERCIAL_PROTECTION_MODES = {"free-pyarmor", "bcc"}
INTERNAL_PROTECTION_MODES = COMMERCIAL_PROTECTION_MODES | {"plain-installer"}
MINIMUM_OBFUSCATED_TARGETS = {
    "main.py",
    "stock_tool/core/screener_engine.py",
    "stock_tool/core/performance_loop.py",
    "stock_tool/core/professional_formula.py",
    "stock_tool/core/query_schema.py",
    "stock_tool/core/local_query_parser.py",
}

COMMERCIAL_DOC_FILES = [
    (ROOT / "docs" / "买家使用说明.txt", "使用说明.txt"),
    (ROOT / "docs" / "免责声明.txt", "免责声明.txt"),
    (ROOT / "docs" / "常见问题.txt", "常见问题.txt"),
    (ROOT / "docs" / "示例策略_v1.json", "示例策略_v1.json"),
]
COMMERCIAL_DOC_NAMES = {dest_name for _src, dest_name in COMMERCIAL_DOC_FILES}
COMMERCIAL_DOC_NAMES.add("版本说明.txt")
COMMERCIAL_ALLOWED_FILES = set(COMMERCIAL_DOC_NAMES)
COMMERCIAL_ALLOWED_FILES.add(DEFAULT_INSTALLER_NAME)
SENSITIVE_SUFFIXES = {
    ".cer",
    ".crt",
    ".db",
    ".der",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
}
SENSITIVE_NAME_PARTS = {
    "credential",
    "api_key",
    "apikey",
    "password",
    "private_key",
    "secret",
    "token",
}

SOURCE_EXCLUDE_DIRS = {
    ".git",
    ".pyarmor",
    ".pytest_cache",
    ".toolcache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "installer_output",
    "my_data",
    "private_keys",
    "release",
    "venv",
}
SOURCE_EXCLUDE_FILES = {
    "data_location.txt",
    "license_private_key.json",
    "pyarmor.trace.log",
}
SOURCE_EXCLUDE_SUFFIXES = {
    ".cer",
    ".crt",
    ".db",
    ".der",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
}
SOURCE_EXCLUDE_DOC_PARTS = {
    ("docs", "acceptance_reports"),
}

COMMERCIAL_FORBIDDEN_NAMES = {
    "data_location.txt",
}
COMMERCIAL_FORBIDDEN_DIRS = {
    ".git",
    "__pycache__",
    "_internal",
    "build",
    "dist",
    "my_data",
    "release",
    "stock_tool",
    "tools",
}
COMMERCIAL_FORBIDDEN_SUFFIXES = {
    ".cer",
    ".crt",
    ".db",
    ".der",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".py",
    ".pyc",
    ".pyo",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
}
SOURCE_FORBIDDEN_DIRS = {
    ".pyarmor",
    ".toolcache",
    "build",
    "dist",
    "installer_output",
    "my_data",
    "private_keys",
    "release",
    "__pycache__",
}
SOURCE_FORBIDDEN_NAMES = {
    "data_location.txt",
    "license_private_key.json",
    "pyarmor.trace.log",
}
SOURCE_FORBIDDEN_SUFFIXES = {
    ".cer",
    ".crt",
    ".db",
    ".der",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
}
GITIGNORE_REQUIRED_PATTERNS = [
    ".toolcache/",
    "my_data/",
    "release/",
    "docs/acceptance_reports/",
    "data_location.txt",
    "*.sqlite3",
    ".env",
    ".env.*",
    "*.log",
    "*.private.json",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.crt",
    "*.cer",
    "*.der",
    "private_keys/",
    ".pyarmor/",
    "installer_output/",
    "pyarmor.trace.log",
    "build/protected_installer_manifest.json",
]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(command: list[str], *, allow_failure: bool = False) -> dict:
    started_at = datetime.now().isoformat(timespec="seconds")
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    output = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
    payload = {
        "command": command,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "output_tail": output.strip()[-4000:],
    }
    if completed.returncode != 0 and not allow_failure:
        raise RuntimeError(f"命令失败：{' '.join(command)}\n{payload['output_tail']}")
    return payload


def version_note_file(version: str) -> Path:
    exact = ROOT / "docs" / f"版本说明_{version}.txt"
    if exact.exists():
        return exact
    candidates = sorted((ROOT / "docs").glob("版本说明_*.txt"), key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    if candidates:
        return candidates[0]
    raise FileNotFoundError("未找到版本说明文档：docs/版本说明_*.txt")


def commercial_allowed_files(installer_name: str) -> set[str]:
    return set(COMMERCIAL_DOC_NAMES) | {installer_name}


def is_installer_file_name(name: str) -> bool:
    return bool(INSTALLER_NAME_RE.fullmatch(name))


def is_allowed_commercial_file(rel_name: str) -> bool:
    return rel_name in COMMERCIAL_DOC_NAMES or ("/" not in rel_name and is_installer_file_name(rel_name))


def commercial_files_for_version(version: str, installer_path: Path) -> list[tuple[Path, str]]:
    return [(installer_path, installer_path.name)] + COMMERCIAL_DOC_FILES + [(version_note_file(version), "版本说明.txt")]


def _resolve_input_path(path_text: str, *, default: Path) -> Path:
    if not path_text:
        return default
    path = Path(path_text)
    if path.is_absolute():
        return path
    return ROOT / path


def load_protection_manifest(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            "缺少商业保护构建证明："
            f"{path}\n请先运行 tools/build_protected_installer.py 或 生成安装包.bat。"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"保护构建证明格式无效：{path}")
    return payload


def normalize_protection_mode(protection: dict) -> str:
    mode = str(protection.get("mode") or "").strip()
    if mode:
        return mode
    if bool(protection.get("bcc_enabled")):
        return "bcc"
    if bool(protection.get("pyarmor_enabled")):
        return "free-pyarmor"
    return "plain-installer"


def protection_level_for_mode(mode: str) -> str:
    if mode == "bcc":
        return "bcc"
    if mode == "free-pyarmor":
        return "trial_obfuscation"
    return "none"


def protection_targets(protection: dict) -> list[str]:
    for key in ("obfuscated_targets", "protected_targets", "plain_obfuscation_targets"):
        value = protection.get(key)
        if isinstance(value, list):
            return [str(item) for item in value]
    return []


def resolve_installer_and_protection(args: argparse.Namespace) -> tuple[Path, dict]:
    manifest_path = _resolve_input_path(args.protection_manifest, default=PROTECTED_BUILD_MANIFEST).resolve()
    payload = load_protection_manifest(manifest_path)
    installer_payload = payload.get("installer") if isinstance(payload.get("installer"), dict) else {}
    installer_path_text = args.installer_path or str(installer_payload.get("path") or "")
    if not installer_path_text:
        raise FileNotFoundError("保护构建证明缺少 installer.path，且未传入 --installer-path。")
    installer_path = _resolve_input_path(installer_path_text, default=Path(installer_path_text)).resolve()
    if not installer_path.exists():
        raise FileNotFoundError(f"最终安装包不存在：{installer_path}")
    if not is_installer_file_name(installer_path.name):
        raise ValueError(f"安装包文件名不符合商业白名单规则：{installer_path.name}")

    protection = payload.get("protection") if isinstance(payload.get("protection"), dict) else {}
    mode = normalize_protection_mode(protection)
    if mode not in INTERNAL_PROTECTION_MODES:
        raise RuntimeError(f"保护构建证明 mode 不受支持：{mode}")
    if mode in COMMERCIAL_PROTECTION_MODES and not bool(protection.get("pyarmor_enabled")):
        raise RuntimeError("保护构建证明显示 PyArmor 未启用，拒绝生成商业交付包。")
    if mode == "bcc":
        if not bool(protection.get("bcc_enabled")):
            raise RuntimeError("保护构建证明显示 BCC 未启用，拒绝生成商业交付包。")
        if not bool(protection.get("bcc_verified")):
            raise RuntimeError("保护构建证明显示 BCC 未完成验证，拒绝生成商业交付包。")

    actual_installer_sha = sha256_file(installer_path)
    expected_installer_sha = str(installer_payload.get("sha256") or "")
    if expected_installer_sha and actual_installer_sha != expected_installer_sha:
        raise RuntimeError("最终安装包 SHA256 与保护构建证明不一致，拒绝生成商业交付包。")

    tools = payload.get("tools") if isinstance(payload.get("tools"), dict) else {}
    pyarmor = tools.get("pyarmor") if isinstance(tools.get("pyarmor"), dict) else {}
    pyinstaller = tools.get("pyinstaller") if isinstance(tools.get("pyinstaller"), dict) else {}
    compiler = tools.get("c_compiler") if isinstance(tools.get("c_compiler"), dict) else {}
    targets = protection_targets(protection)
    pyarmor_enabled = bool(protection.get("pyarmor_enabled"))
    bcc_enabled = bool(protection.get("bcc_enabled"))
    bcc_verified = bool(protection.get("bcc_verified"))
    release_protection = {
        "required": True,
        "mode": mode,
        "protection_level": str(protection.get("protection_level") or protection_level_for_mode(mode)),
        "build_manifest_path": str(manifest_path),
        "build_manifest_sha256": sha256_file(manifest_path),
        "pyarmor_enabled": pyarmor_enabled,
        "pyarmor_version": str(pyarmor.get("version") or ""),
        "pyinstaller_version": str(pyinstaller.get("version") or ""),
        "c_compiler": {
            "path": str(compiler.get("path") or ""),
            "version": str(compiler.get("version") or ""),
        },
        "bcc_enabled": bcc_enabled,
        "bcc_required": bool(protection.get("bcc_required")) or mode == "bcc",
        "bcc_verified": bcc_verified,
        "trial_build": bool(protection.get("trial_build")),
        "protected_targets": targets,
        "obfuscated_targets": targets,
        "obfuscated_target_details": (
            protection.get("obfuscated_target_details") if isinstance(protection.get("obfuscated_target_details"), list) else []
        ),
        "skipped_big_scripts": protection.get("skipped_big_scripts") if isinstance(protection.get("skipped_big_scripts"), list) else [],
        "bcc_targets": protection.get("bcc_targets") if isinstance(protection.get("bcc_targets"), dict) else {},
        "bcc_verification": protection.get("bcc_verification") if isinstance(protection.get("bcc_verification"), dict) else {},
        "non_bcc_core_modules": protection.get("non_bcc_core_modules") if isinstance(protection.get("non_bcc_core_modules"), list) else [],
        "installer_name": installer_path.name,
        "installer_sha256": actual_installer_sha,
        "installer_size_bytes": installer_path.stat().st_size,
    }
    return installer_path, release_protection


def clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def should_skip_source(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = rel.parts
    if any(part in SOURCE_EXCLUDE_DIRS for part in parts):
        return True
    if len(parts) >= 2 and tuple(parts[:2]) in SOURCE_EXCLUDE_DOC_PARTS:
        return True
    name = path.name.lower()
    if path.name in SOURCE_EXCLUDE_FILES:
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    if name.endswith(".private.json"):
        return True
    if path.is_file() and any(part in name for part in SENSITIVE_NAME_PARTS):
        return True
    if path.suffix.lower() in SOURCE_EXCLUDE_SUFFIXES:
        return True
    return False


def copy_source_tree(target: Path) -> None:
    for path in ROOT.rglob("*"):
        if path == target or target in path.parents:
            continue
        if should_skip_source(path):
            continue
        rel = path.relative_to(ROOT)
        dest = target / rel
        if path.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)


def copy_commercial_package(target: Path, version: str, installer_path: Path) -> None:
    files = commercial_files_for_version(version, installer_path)
    missing = [str(src) for src, _name in files if not src.exists()]
    if missing:
        raise FileNotFoundError("商业包缺少必需文件：\n" + "\n".join(missing))
    for src, dest_name in files:
        shutil.copy2(src, target / dest_name)


def zip_dir(source_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                arcname = str(path.relative_to(source_dir.parent)).replace("\\", "/")
                info = zipfile.ZipInfo(arcname, ZIP_FIXED_DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, path.read_bytes())


def list_files(base: Path) -> list[str]:
    return [
        str(path.relative_to(base)).replace("\\", "/")
        for path in sorted(base.rglob("*"))
        if path.is_file()
    ]


def validate_package(base: Path, *, package_type: str) -> list[str]:
    errors: list[str] = []
    if not base.exists():
        return [f"{package_type} 目录不存在：{base}"]
    for path in base.rglob("*"):
        rel_parts = path.relative_to(base).parts
        lowered_parts = {part.lower() for part in rel_parts}
        name = path.name.lower()
        suffix = path.suffix.lower()
        sensitive_name = (
            name == ".env"
            or name.startswith(".env.")
            or name.endswith(".private.json")
            or any(part in name for part in SENSITIVE_NAME_PARTS)
        )
        if package_type == "commercial":
            rel_name = str(path.relative_to(base)).replace("\\", "/")
            if path.is_file() and not is_allowed_commercial_file(rel_name):
                errors.append(f"商业包包含非白名单文件：{path.relative_to(base)}")
            if path.is_dir() and name in COMMERCIAL_FORBIDDEN_DIRS:
                errors.append(f"商业包包含禁用目录：{path.relative_to(base)}")
            if path.is_file() and (name in COMMERCIAL_FORBIDDEN_NAMES or suffix in COMMERCIAL_FORBIDDEN_SUFFIXES or sensitive_name):
                errors.append(f"商业包包含禁用文件：{path.relative_to(base)}")
            if lowered_parts & COMMERCIAL_FORBIDDEN_DIRS:
                errors.append(f"商业包路径包含禁用目录名：{path.relative_to(base)}")
        else:
            if path.is_dir() and name in SOURCE_FORBIDDEN_DIRS:
                errors.append(f"源码包包含禁用目录：{path.relative_to(base)}")
            if path.is_file() and (name in SOURCE_FORBIDDEN_NAMES or suffix in SOURCE_FORBIDDEN_SUFFIXES or sensitive_name):
                errors.append(f"源码包包含禁用文件：{path.relative_to(base)}")
            if lowered_parts & SOURCE_FORBIDDEN_DIRS:
                errors.append(f"源码包路径包含禁用目录名：{path.relative_to(base)}")
    return sorted(set(errors))


def verify_gitignore() -> list[str]:
    path = ROOT / ".gitignore"
    if not path.exists():
        return [".gitignore 不存在"]
    lines = {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")}
    return [f".gitignore 缺少：{pattern}" for pattern in GITIGNORE_REQUIRED_PATTERNS if pattern not in lines]


def load_manual_ui_acceptance(report_path: str) -> dict:
    if not report_path:
        return {
            "status": "pending",
            "accepted": False,
            "json_report": "",
            "message": "尚未提供人工 UI 验收报告；商业外发前必须完成冷启动人工验收。",
        }
    path = Path(report_path)
    if not path.exists():
        raise FileNotFoundError(f"人工 UI 验收报告不存在：{path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    summary = payload.get("summary") if isinstance(payload, dict) else {}
    if not isinstance(summary, dict):
        raise ValueError("人工 UI 验收报告缺少 summary")
    txt_report = path.with_suffix(".txt")
    manual = {
        "status": str(summary.get("status") or "unknown"),
        "accepted": bool(summary.get("accepted")),
        "json_report": str(path.resolve()),
        "txt_report": str(txt_report.resolve()) if txt_report.exists() else "",
        "operator": str(payload.get("operator") or ""),
        "machine": str(payload.get("machine") or ""),
        "created_at": str(payload.get("created_at") or ""),
        "release_dir": str(payload.get("release_dir") or ""),
        "release_evidence": payload.get("release_evidence") if isinstance(payload.get("release_evidence"), dict) else {},
        "summary": summary,
        "items": payload.get("items") if isinstance(payload.get("items"), list) else [],
        "notes": str(payload.get("notes") or ""),
        "failed": int(summary.get("failed") or 0),
        "passed": int(summary.get("passed") or 0),
        "not_applicable": int(summary.get("not_applicable") or 0),
        "required_not_applicable": int(summary.get("required_not_applicable") or 0),
    }
    if path.exists():
        manual["json_report_sha256"] = sha256_file(path)
    if txt_report.exists():
        manual["txt_report_sha256"] = sha256_file(txt_report)
    return manual


def _release_path_text(path: Path, release_dir: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(release_dir.resolve())).replace("\\", "/")
    except ValueError:
        return str(resolved)


def _copy_if_different(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest


def persist_manual_ui_acceptance_report(release_dir: Path, report_path: str, manual: dict) -> dict:
    if not report_path:
        return manual
    source_json = Path(report_path).resolve()
    release_json = _copy_if_different(source_json, release_dir / RELEASE_MANUAL_JSON)
    manual["original_json_report"] = str(source_json)
    manual["json_report"] = _release_path_text(release_json, release_dir)
    manual["json_report_sha256"] = sha256_file(release_json)

    source_txt = source_json.with_suffix(".txt")
    if source_txt.exists():
        release_txt = _copy_if_different(source_txt, release_dir / RELEASE_MANUAL_TXT)
        manual["original_txt_report"] = str(source_txt)
        manual["txt_report"] = _release_path_text(release_txt, release_dir)
        manual["txt_report_sha256"] = sha256_file(release_txt)
    return manual


def build_release(args: argparse.Namespace) -> dict:
    timestamp = args.timestamp or now_stamp()
    if args.release_name:
        release_name = args.release_name.strip()
        if not release_name or release_name in {".", ".."} or "/" in release_name or "\\" in release_name:
            raise ValueError("--release-name 只能是单层目录名，例如 Stocklens_V2")
        release_dir = RELEASE_ROOT / release_name
    else:
        release_dir = RELEASE_ROOT / f"V1_商业交付_{timestamp}"
    commercial_dir = release_dir / COMMERCIAL_NAME
    source_dir = release_dir / SOURCE_NAME
    clean_dir(release_dir)
    commercial_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)
    installer_path, protection = resolve_installer_and_protection(args)

    offline_result = None
    if not args.skip_acceptance:
        txt_report = release_dir / "phase9_offline_acceptance.txt"
        json_report = release_dir / "phase9_offline_acceptance.json"
        offline_result = run_command(
            [
                sys.executable,
                "tools/release_acceptance.py",
                "--report",
                str(txt_report),
                "--json-report",
                str(json_report),
            ]
        )

    live_result = None
    if args.with_live:
        live_result = run_command(
            [sys.executable, "tools/test_fetch_success_rate.py", "--limit", "3", "--days", "30"],
            allow_failure=True,
        )
    manual_ui_acceptance = load_manual_ui_acceptance(args.manual_ui_report)

    copy_commercial_package(commercial_dir, args.version, installer_path)
    copy_source_tree(source_dir)

    commercial_errors = validate_package(commercial_dir, package_type="commercial")
    source_errors = validate_package(source_dir, package_type="source")
    gitignore_errors = verify_gitignore()
    commercial_files = list_files(commercial_dir)
    expected_commercial_files = commercial_allowed_files(protection["installer_name"])
    if set(commercial_files) != expected_commercial_files:
        commercial_errors.append("商业包文件白名单不一致：" + "、".join(sorted(commercial_files)))
    if commercial_errors or source_errors or gitignore_errors:
        errors = commercial_errors + source_errors + gitignore_errors
        raise RuntimeError("第九阶段交付自检失败：\n" + "\n".join(errors))

    commercial_zip = release_dir / f"{COMMERCIAL_NAME}.zip"
    source_zip = release_dir / f"{SOURCE_NAME}.zip"
    zip_dir(commercial_dir, commercial_zip)
    zip_dir(source_dir, source_zip)
    manual_ui_acceptance = persist_manual_ui_acceptance_report(
        release_dir,
        args.manual_ui_report,
        manual_ui_acceptance,
    )

    files_sha256 = {
        str(path.relative_to(release_dir)).replace("\\", "/"): sha256_file(path)
        for path in sorted(release_dir.rglob("*"))
        if path.is_file() and path.name != "RELEASE_MANIFEST.json"
    }
    manifest = {
        "title": "A股选股仪 第九阶段商业交付包",
        "version": args.version,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "release_dir": str(release_dir),
        "commercial_dir": str(commercial_dir),
        "source_dir": str(source_dir),
        "commercial_zip": str(commercial_zip),
        "source_zip": str(source_zip),
        "offline_acceptance": offline_result,
        "live_enabled": bool(args.with_live),
        "live_status": "not_run" if not args.with_live else ("pass" if live_result and live_result["ok"] else "risk"),
        "live_result": live_result,
        "manual_ui_acceptance": manual_ui_acceptance,
        "commercial_files": commercial_files,
        "source_file_count": len(list_files(source_dir)),
        "sha256": files_sha256,
        "protection": protection,
        "rules": {
            "commercial_no_source": True,
            "commercial_no_data_location": True,
            "commercial_allowlist_only": True,
            "commercial_installer_only": True,
            "commercial_requires_pyarmor": True,
            "commercial_allowed_protection_modes": sorted(COMMERCIAL_PROTECTION_MODES),
            "commercial_requires_pyarmor_bcc": False,
            "cold_start_requires_customer_data_dir": True,
            "source_no_personal_data": True,
            "live_failure_blocks_release": False,
            "manual_ui_acceptance_required_before_external_delivery": True,
        },
    }
    manifest_path = release_dir / "RELEASE_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成第九阶段商业交付包和 GitHub 私有源码包。")
    parser.add_argument("--version", default="v0.2.7_phase9", help="交付版本号。")
    parser.add_argument("--with-live", action="store_true", help="记录实时行情小样本结果；失败只记风险，不阻断离线交付。")
    parser.add_argument("--skip-acceptance", action="store_true", help="跳过离线验收，主要供交付脚本自身测试使用。")
    parser.add_argument("--timestamp", default="", help="指定交付目录时间戳，默认使用当前时间。")
    parser.add_argument("--release-name", default="", help="指定 release 下的交付目录名；设置后不再追加 V1_商业交付_ 前缀。")
    parser.add_argument("--manual-ui-report", default="", help="已完成的人工 UI 验收 JSON 报告；不提供则 manifest 标记为 pending。")
    parser.add_argument("--installer-path", default="", help="最终商业安装包路径；默认读取 build/protected_installer_manifest.json。")
    parser.add_argument("--protection-manifest", default="", help="保护构建证明路径；默认 build/protected_installer_manifest.json。")
    return parser.parse_args()


def legacy_main() -> int:
    args = parse_args()
    manifest = build_release(args)
    print("[OK] 第九阶段交付包已生成")
    print(f"目录：{manifest['release_dir']}")
    print(f"商业包：{manifest['commercial_zip']}")
    print(f"源码包：{manifest['source_zip']}")
    print(f"Live 状态：{manifest['live_status']}")
    return 0


if __name__ == "__main__":
    if "--legacy-phase9" in sys.argv:
        sys.argv.remove("--legacy-phase9")
        raise SystemExit(legacy_main())
    from tools.prepare_v3_release import main as v3_main

    raise SystemExit(v3_main())
