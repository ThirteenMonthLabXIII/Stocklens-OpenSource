from __future__ import annotations

"""Assemble a V3 candidate release from a verified Nuitka build manifest."""

import argparse
import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.config.release_identity import (
    APP_VERSION,
    INSTALLER_NAME,
    NUITKA_VERSION,
    RELEASE_DIRECTORY_PREFIX,
    VERSION_NOTE_FILE,
    as_manifest_identity,
)
from tools.release_integrity import (
    atomic_write_json,
    compare_inventories,
    directory_inventory,
    file_record as integrity_file_record,
    validate_zip_safety,
    zip_inventory,
)
from tools.release_schema import require_schema
from tools.verify_release_signing import sign_candidate


RELEASE_ROOT = ROOT / "release"
BUILD_MANIFEST_PATH = ROOT / "build" / "v3_build_manifest.json"
COMPILE_REPORT = ROOT / "build" / "nuitka-compilation-report.xml"
RESOURCE_MANIFEST = ROOT / "build" / "nuitka-resource-manifest.json"
COMMERCIAL_NAME = "01_商业出售版_不含源码"
SOURCE_NAME = "02_GitHub源码版_可二次开发"
RELEASE_MANIFEST_NAME = "RELEASE_MANIFEST.json"
CHECKLIST_JSON = "V3_P7_人工验收清单.json"
CHECKLIST_TXT = "V3_P7_人工验收清单.txt"
OFFLINE_JSON = "v3_offline_acceptance.json"
OFFLINE_TXT = "v3_offline_acceptance.txt"
P7_SESSION_NAME = "P7_SESSION.json"

COMMERCIAL_DOCS = [
    (ROOT / "docs" / "买家使用说明.txt", "使用说明.txt"),
    (ROOT / "docs" / "免责声明.txt", "免责声明.txt"),
    (ROOT / "docs" / "常见问题.txt", "常见问题.txt"),
    (ROOT / "docs" / "示例策略_v1.json", "示例策略_v1.json"),
    (ROOT / VERSION_NOTE_FILE, "版本说明.txt"),
]
SOURCE_EXCLUDED_DIRS = {
    ".git", ".pytest_cache", ".venv", "__pycache__", "build", "dist", "installer_output", "my_data", "private_keys", "release", "venv"
}
SOURCE_EXCLUDED_NAMES = {"data_location.txt", "license_private_key.json", "pyarmor.trace.log"}
SOURCE_ALLOWED_TOP_LEVEL = {
    ".github", ".gitignore", "AGENTS.md", "PRODUCT.md", "README.md", "assets",
    "build.spec", "build_qt.spec", "docs", "installer.iss", "launch.ps1", "main.py",
    "requirements-build.txt", "requirements-optional.txt", "requirements-v3.lock", "requirements.txt",
    "research", "stock_tool", "task_contract_v1.4.json", "tools", "启动软件.bat", "安装依赖.bat",
    "打包exe.bat", "生成V3商业交付包.bat", "生成安装包.bat", "生成第九阶段交付包.bat",
}
SENSITIVE_SUFFIXES = {
    ".db", ".db3", ".key", ".log", ".p12", ".pem", ".pfx", ".ppk", ".pyc", ".pyo",
    ".sqlite", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal",
}
CREDENTIAL_DATA_SUFFIXES = {".cfg", ".conf", ".ini", ".json", ".toml", ".txt", ".yaml", ".yml"}
CREDENTIAL_DATA_STEMS = {
    "api_key", "apikey", "aws_credentials", "credential", "credentials", "deepseek_api_key",
    "github_token", "openai_api_key", "password", "private_key", "secret", "secrets",
    "token", "tokens", "tushare_token",
}
CREDENTIAL_ENV_LABELS = {"dev", "local", "prod", "production", "staging", "test", "user"}
EXACT_SENSITIVE_NAMES = {"credentials", "id_dsa", "id_ed25519", "id_rsa"}

MANUAL_CASES = [
    ("win11_25h2_full", "Windows 11 25H2 x64 完整 P7", True),
    ("win11_24h2_core", "Windows 11 24H2 x64 安装、启动、重启、升级与缩放", True),
    ("win10_22h2_compat", "Windows 10 22H2 x64 兼容性验证（失败则撤销兼容声明）", False),
    ("install_hash", "winver 与安装包 SHA256 已核对", True),
    ("qt_home", "默认 Qt 首页与首次目录选择", True),
    ("scaling", "100% / 125% / 150% 与 1180×720、常用分辨率", True),
    ("restart", "关闭重启与卸载重装", True),
    ("upgrade_v027", "v0.2.7 phase9 数据升级、记录数量不减少", True),
    ("network_degraded", "断网和行情部分失败的降级提示", True),
    ("credential_failures", "错误 Key、无 Tushare Token 或权限", True),
    ("export_denied", "无权限导出提示", True),
    ("qt_startup_failure", "Qt 故障：显示脱敏错误并以非零状态退出", True),
    ("process_cleanup", "退出后无 Stocklens/QtWebEngineProcess 残留或迟到写入", True),
    ("screenshot_sanitized", "截图无 API Key、Token、个人路径和真实数据库内容", True),
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def clean_dir(path: Path) -> None:
    resolved = path.resolve()
    if not resolved.is_relative_to(RELEASE_ROOT.resolve()) or resolved == RELEASE_ROOT.resolve():
        raise RuntimeError(f"拒绝清理 release 根目录外的路径：{path}")
    if path.exists():
        shutil.rmtree(path)


def list_files(directory: Path) -> list[str]:
    return [str(path.relative_to(directory)).replace("\\", "/") for path in sorted(directory.rglob("*")) if path.is_file()]


def relative_artifact(path: Path, release_dir: Path) -> str:
    return str(path.resolve().relative_to(release_dir.resolve())).replace("\\", "/")


def file_record(path: Path, release_dir: Path) -> dict[str, str | int]:
    return integrity_file_record(path, root=release_dir, role="release_artifact").to_dict()


def load_build_manifest() -> dict:
    if not BUILD_MANIFEST_PATH.is_file():
        raise RuntimeError("缺少 build/v3_build_manifest.json；请先运行 tools/build_v3_nuitka.py。")
    payload = json.loads(BUILD_MANIFEST_PATH.read_text(encoding="utf-8"))
    require_schema("build", payload)
    if payload.get("schema_version") != 5 or payload.get("build", {}).get("backend") != "nuitka":
        raise RuntimeError("V3 构建 manifest schema/backend 不正确。")
    if payload.get("product_version") != APP_VERSION or payload.get("build", {}).get("nuitka_version") != NUITKA_VERSION:
        raise RuntimeError("V3 构建 manifest 的版本或 Nuitka 固定版本不正确。")
    return payload


def current_git_commit() -> str:
    completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True)
    return completed.stdout.strip()


def require_clean_commit() -> str:
    completed = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, text=True, capture_output=True, check=True)
    if completed.stdout.strip():
        raise RuntimeError("V3 交付候选必须从干净 commit 生成。")
    return current_git_commit()


def copy_commercial_package(destination: Path, installer: Path) -> None:
    if installer.name != INSTALLER_NAME or not installer.is_file():
        raise RuntimeError(f"缺少正式 V3 安装包：{installer}")
    shutil.copy2(installer, destination / installer.name)
    for source, destination_name in COMMERCIAL_DOCS:
        if not source.is_file():
            raise RuntimeError(f"商业包说明文件不存在：{source}")
        shutil.copy2(source, destination / destination_name)


def is_sensitive_material(relative: Path) -> bool:
    lowered_name = relative.name.lower()
    suffix = relative.suffix.lower()
    if lowered_name == ".env" or lowered_name.startswith(".env.") or lowered_name == ".envrc":
        return True
    if lowered_name in SOURCE_EXCLUDED_NAMES or lowered_name in EXACT_SENSITIVE_NAMES:
        return True
    if suffix in SENSITIVE_SUFFIXES:
        return True
    if suffix not in CREDENTIAL_DATA_SUFFIXES:
        return False
    stem = relative.stem.lower().replace("-", "_").replace(".", "_")
    if stem in CREDENTIAL_DATA_STEMS:
        return True
    return any(
        stem == f"{label}_{credential}" or stem == f"{credential}_{label}"
        for label in CREDENTIAL_ENV_LABELS
        for credential in CREDENTIAL_DATA_STEMS
    )


def should_exclude_source(relative: Path) -> bool:
    if not relative.parts or relative.parts[0] not in SOURCE_ALLOWED_TOP_LEVEL:
        return True
    lowered_parts = {part.lower() for part in relative.parts[:-1]}
    if any(part in SOURCE_EXCLUDED_DIRS for part in lowered_parts):
        return True
    if is_sensitive_material(relative) or relative.suffix.lower() == ".zip":
        return True
    return relative.parts[:2] == ("docs", "acceptance_reports")


def copy_source_tree(destination: Path) -> None:
    completed = subprocess.run(["git", "ls-tree", "-r", "-z", "--full-tree", "HEAD"], cwd=ROOT, text=False, capture_output=True, check=True)
    for raw_entry in completed.stdout.split(b"\0"):
        if not raw_entry:
            continue
        metadata, separator, raw_relative = raw_entry.partition(b"\t")
        if not separator:
            raise RuntimeError("git ls-tree 返回了无法解析的条目。")
        mode, object_type, _object_id = metadata.decode("ascii").split(" ", 2)
        relative = Path(raw_relative.decode("utf-8"))
        if should_exclude_source(relative):
            continue
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise RuntimeError(f"源码白名单含非普通 Git blob：{relative.as_posix()}")
        blob = subprocess.run(
            ["git", "cat-file", "blob", f"HEAD:{relative.as_posix()}"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)


def validate_package(directory: Path, package_type: str) -> list[str]:
    errors: list[str] = []
    for relative_text in list_files(directory):
        relative = Path(relative_text)
        lowered_parts = {part.lower() for part in relative.parts}
        lowered_name = relative.name.lower()
        if is_sensitive_material(relative):
            errors.append(f"{package_type} 含敏感材料：{relative_text}")
        if relative.suffix.lower() == ".zip":
            errors.append(f"{package_type} 含禁入材料：{relative_text}")
        if package_type == "commercial":
            if relative.suffix.lower() == ".py" or "stock_tool" in lowered_parts or "tools" in lowered_parts:
                errors.append(f"商业包含源码：{relative_text}")
            if any(part in {"build", "dist", "release", "my_data", "private_keys", "_internal"} for part in lowered_parts):
                errors.append(f"商业包含禁止目录：{relative_text}")
        else:
            if any(part in SOURCE_EXCLUDED_DIRS for part in lowered_parts) or relative.parts[:2] == ("docs", "acceptance_reports"):
                errors.append(f"源码包含构建或个人产物：{relative_text}")
    return errors


def zip_dir(source: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())


def run_command(command: list[str], *, env: dict[str, str] | None = None) -> dict:
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=env)
    output = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
    payload = {"command": command, "ok": completed.returncode == 0, "returncode": completed.returncode, "output_tail": output[-8000:]}
    if not payload["ok"]:
        raise RuntimeError("验收命令失败：\n" + payload["output_tail"])
    return payload


def write_checklist(release_dir: Path, manifest: dict, *, challenge_id: str = "") -> dict[str, str]:
    artifacts = manifest["artifacts"]
    payload = {
        "schema_version": 3,
        "candidate_id": manifest["candidate_id"],
        "git_commit": manifest["git_commit"],
        "candidate_stage": manifest["candidate_stage"],
        "challenge_id": challenge_id,
        "artifacts": {
            key: {name: value for name, value in record.items() if name in {"sha256", "size_bytes", "path"}}
            for key, record in artifacts.items()
        },
        "devices": [],
        "items": [{"id": key, "title": title, "required": required, "status": "PENDING", "device_id": "", "screenshots": [], "note": ""} for key, title, required in MANUAL_CASES],
        "evidence_rules": [
            "每个 required 项必须为 PASS，且关联至少一张可解码、尺寸非零的 PNG/JPEG/WebP 脱敏截图。",
            "截图不得包含 API Key、Token、个人路径或真实数据库内容；同一截图复用时每个相关项必须填写 shared_evidence_reason。",
            "devices 必须记录 device_id、windows_version 和 os_label；win11_25h2_full 与 win11_24h2_core 必须分别绑定相应设备。",
            "win10_22h2_compat 为 PASS/FAIL 时必须绑定 win10_22h2 设备；NA 时可不提供 Win10 设备，FAIL 只撤销兼容声明，不阻断 Windows 11 发布。",
            "不得用 --mark-all-pass；请由 tools/record_v3_p7_manual_acceptance.py 校验并生成正式报告。",
        ],
    }
    json_path = release_dir / CHECKLIST_JSON
    txt_path = release_dir / CHECKLIST_TXT
    atomic_write_json(json_path, payload)
    lines = [
        "Stocklens V3 P7 人工验收清单（候选绑定）",
        f"candidate_id：{payload['candidate_id']}",
        f"Git commit：{payload['git_commit']}",
        f"P7 challenge：{challenge_id or 'legacy-schema4'}",
        f"安装包 SHA256：{artifacts['installer']['sha256']}",
        f"商业 zip SHA256：{artifacts['commercial_zip']['sha256']}",
        f"源码 zip SHA256：{artifacts['source_zip']['sha256']}",
        "",
    ]
    lines.extend(f"- [PENDING] {'必需' if required else '兼容性'}：{title} ({key})" for key, title, required in MANUAL_CASES)
    lines.extend(["", "请返回脱敏截图和每项 PASS/FAIL；不要手工修改候选哈希。"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": relative_artifact(json_path, release_dir), "txt": relative_artifact(txt_path, release_dir)}


def build_release(*, run_acceptance: bool = True, candidate_stage: str = "rc0") -> Path:
    if candidate_stage not in {"rc0", "rc1"}:
        raise ValueError("candidate_stage 必须为 rc0 或 rc1")
    build_manifest = load_build_manifest()
    commit = require_clean_commit()
    if build_manifest.get("git_commit") != commit:
        raise RuntimeError("当前 HEAD 与 build manifest 的 git_commit 不一致；必须重新构建。")
    if build_manifest.get("candidate_stage") != candidate_stage:
        raise RuntimeError("build manifest candidate_stage 与交付候选阶段不一致；必须使用对应阶段全新构建。")
    release_dir = RELEASE_ROOT / f"{RELEASE_DIRECTORY_PREFIX}{now_stamp()}"
    clean_dir(release_dir)
    commercial_dir = release_dir / COMMERCIAL_NAME
    source_dir = release_dir / SOURCE_NAME
    evidence_dir = release_dir / "release_local" / "build_evidence"
    commercial_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    evidence_dir.mkdir(parents=True)

    exe = ROOT / str(build_manifest["artifacts"]["exe"]["path"])
    installer = ROOT / str(build_manifest["artifacts"]["installer"]["path"])
    for label, source in (("exe", exe), ("installer", installer), ("compile_report", COMPILE_REPORT), ("resource_manifest", RESOURCE_MANIFEST), ("build_manifest", BUILD_MANIFEST_PATH)):
        if not source.is_file():
            raise RuntimeError(f"缺少构建证据：{label}={source}")
    for label, source in (("exe", exe), ("installer", installer)):
        record = build_manifest["artifacts"].get(label, {})
        if sha256_file(source) != record.get("sha256") or source.stat().st_size != record.get("size_bytes"):
            raise RuntimeError(f"构建 manifest 与实际 {label} 哈希或大小不一致。")
    for label, source in (("compile_report", COMPILE_REPORT), ("resource_manifest", RESOURCE_MANIFEST)):
        record = build_manifest["build"].get(label, {})
        if sha256_file(source) != record.get("sha256"):
            raise RuntimeError(f"构建 manifest 与实际 {label} SHA256 不一致。")
    copy_commercial_package(commercial_dir, installer)
    copy_source_tree(source_dir)
    commercial_errors = validate_package(commercial_dir, "commercial")
    source_errors = validate_package(source_dir, "source")
    if commercial_errors or source_errors:
        raise RuntimeError("V3 包边界扫描失败：\n" + "\n".join(commercial_errors + source_errors))

    evidence_sources = {
        "exe": exe,
        "installer": installer,
        "compile_report": COMPILE_REPORT,
        "resource_manifest": RESOURCE_MANIFEST,
        "build_manifest": BUILD_MANIFEST_PATH,
    }
    for source in evidence_sources.values():
        shutil.copy2(source, evidence_dir / source.name)

    offline = None
    packaged_smoke = None
    default_contract = None
    if run_acceptance:
        offline = run_command(
            [sys.executable, "tools/release_acceptance.py", "--report", str(release_dir / OFFLINE_TXT), "--json-report", str(release_dir / OFFLINE_JSON)]
        )
        env = dict(__import__("os").environ)
        env["STOCK_TOOL_PACKAGED_DIR"] = str((ROOT / str(build_manifest["artifacts"]["exe"]["path"])).parent)
        packaged_smoke = run_command([sys.executable, "tools/test_qt_packaged_smoke.py"], env=env)
        if candidate_stage == "rc1":
            default_contract = run_command([sys.executable, "tools/test_frontend_default_gate.py"])

    commercial_zip = release_dir / f"{COMMERCIAL_NAME}.zip"
    source_zip = release_dir / f"{SOURCE_NAME}.zip"
    zip_dir(commercial_dir, commercial_zip)
    zip_dir(source_dir, source_zip)
    for label, directory, archive in (
        ("商业", commercial_dir, commercial_zip),
        ("源码", source_dir, source_zip),
    ):
        safety = validate_zip_safety(archive)
        if safety:
            raise RuntimeError(f"{label} ZIP 安全校验失败：\n" + "\n".join(str(issue) for issue in safety))
        inventory_issues = compare_inventories(directory_inventory(directory), zip_inventory(archive))
        if inventory_issues:
            raise RuntimeError(f"{label}目录与 ZIP inventory 不一致：\n" + "\n".join(str(issue) for issue in inventory_issues))
    challenge_id = f"{candidate_stage.upper()}-{secrets.token_hex(16)}"
    manifest = {
        "schema_version": 5,
        **as_manifest_identity(),
        "candidate_id": build_manifest["candidate_id"],
        "git_commit": build_manifest["git_commit"],
        "candidate_stage": candidate_stage,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "build": build_manifest["build"],
        "dependency_lock": build_manifest["dependency_lock"],
        "frontend_contract": build_manifest.get("frontend_contract"),
        "promotion_record": build_manifest.get("promotion_record"),
        "commercial_dir": COMMERCIAL_NAME,
        "source_dir": SOURCE_NAME,
        "artifacts": {
            "exe": file_record(evidence_dir / exe.name, release_dir),
            "installer": file_record(evidence_dir / installer.name, release_dir),
            "commercial_zip": file_record(commercial_zip, release_dir),
            "source_zip": file_record(source_zip, release_dir),
        },
        "inventories": {
            "commercial": directory_inventory(commercial_dir).to_dict(),
            "source": directory_inventory(source_dir).to_dict(),
        },
        "internal_evidence": {name: file_record(evidence_dir / path.name, release_dir) for name, path in evidence_sources.items() if name in {"compile_report", "resource_manifest", "build_manifest"}},
        "automated_acceptance": {"offline": offline, "packaged_smoke": packaged_smoke, "default_contract": default_contract},
        "sensitive_scan": {"commercial": commercial_errors, "source": source_errors, "passed": not commercial_errors and not source_errors},
        "p7_policy": {
            "required": True,
            "report_path": "V3_P7_人工验收报告.json",
            "schema_version": 3,
        },
    }
    manifest_path = release_dir / RELEASE_MANIFEST_NAME
    manifest["manual_checklist"] = write_checklist(release_dir, manifest, challenge_id=challenge_id)
    require_schema("release", manifest)
    atomic_write_json(manifest_path, manifest)
    artifacts = manifest["artifacts"]
    session = {
        "schema_version": 1,
        "candidate_id": manifest["candidate_id"],
        "candidate_stage": candidate_stage,
        "git_commit": manifest["git_commit"],
        "candidate_manifest_sha256": sha256_file(manifest_path),
        "installer_sha256": artifacts["installer"]["sha256"],
        "commercial_zip_sha256": artifacts["commercial_zip"]["sha256"],
        "source_zip_sha256": artifacts["source_zip"]["sha256"],
        "challenge_id": challenge_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "p7_case_ids": [key for key, _title, _required in MANUAL_CASES],
    }
    atomic_write_json(release_dir / P7_SESSION_NAME, session)
    signature_index = sign_candidate(release_dir)
    print(f"[OK] 候选四类 subject 签名索引：{signature_index}")
    print(f"[OK] V3 RC 候选目录：{release_dir}")
    return release_dir


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 Nuitka V3 构建证据生成 RC 候选交付包。")
    parser.add_argument("--skip-acceptance", action="store_true", help="仅限内部调试；最终闸门会拒绝没有自动验收的候选。")
    parser.add_argument("--candidate-stage", choices=("rc0", "rc1"), default="rc0", help="RC0 保持合同部分对齐；RC1 强制默认前端合同闸门。")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    build_release(run_acceptance=not args.skip_acceptance, candidate_stage=args.candidate_stage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
