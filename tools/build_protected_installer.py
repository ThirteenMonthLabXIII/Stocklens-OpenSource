from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build"
PROTECTED_SOURCE_DIR = BUILD_DIR / "protected_source"
PYARMOR_PLAIN_DIR = BUILD_DIR / "pyarmor_plain"
PYARMOR_BCC_DIR = BUILD_DIR / "pyarmor_bcc"
PYINSTALLER_WORK_DIR = BUILD_DIR / "pyinstaller_work"
INSTALLER_OUTPUT_DIR = ROOT / "installer_output"
DIST_DIR = ROOT / "dist"
MANIFEST_PATH = BUILD_DIR / "protected_installer_manifest.json"
COMBINED_TRACE_PATH = BUILD_DIR / "pyarmor_bcc_trace_combined.log"

APP_NAME = "Stocklens"
DIST_APP_DIR = DIST_DIR / APP_NAME
DEFAULT_VERSION = "0.2.7"
INSTALLER_BASENAME = "Stocklens_V2"
INSTALLER_NAME = f"{INSTALLER_BASENAME}.exe"
PROTECTION_MODES = ("free-pyarmor", "bcc", "plain-installer")

PLAIN_PROTECTION_TARGETS = [
    "main.py",
    "stock_tool/core/screener_engine.py",
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
FREE_PYARMOR_TARGETS = PLAIN_PROTECTION_TARGETS
SKIPPED_BIG_SCRIPT_TARGETS = [
    "stock_tool/core/data_fetcher.py",
    "stock_tool/core/strategy_store.py",
    "stock_tool/ui/qt_workbench.py",
]

BCC_TARGETS: dict[str, list[str]] = {
    "stock_tool.core.screener_engine": [
        "passes_filter",
        "_score",
        "_sort_results",
        "screen_market",
        "screen_factor_snapshots",
    ],
    "stock_tool.core.query_schema": [
        "query_spec_from_dict",
        "query_spec_from_json",
        "validate_query_spec",
        "query_spec_to_screen_filter",
        "apply_local_query_guardrails",
    ],
    "stock_tool.core.local_query_parser": [
        "_parse_market_cap",
        "_parse_field_conditions",
        "_parse_sort_phrases",
        "parse_local_query",
    ],
    "stock_tool.core.technical_profile": [
        "_clean_history",
        "_rsi",
        "_kdj_j",
        "_atr",
        "build_technical_profile",
    ],
    "stock_tool.core.trade_observer": [
        "infer_main_force_behavior",
        "build_trade_reference",
        "review_tracking_task",
    ],
    "stock_tool.core.trade_ai": [
        "build_trade_decision_prompt",
        "validate_trade_ai_payload",
        "analyze_trade_observation_with_ai",
    ],
    "stock_tool.core.ai_ranker": [
        "build_rank_prompt",
        "rank_candidates",
    ],
}

NON_BCC_CORE_MODULES = [
    "stock_tool.core.data_fetcher",
    "stock_tool.core.cache",
    "stock_tool.core.v1_database",
    "stock_tool.core.strategy_store",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_rmtree(path: Path) -> None:
    resolved = path.resolve()
    root = ROOT.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise RuntimeError(f"拒绝删除非项目内目录：{path}")
    if path.exists():
        shutil.rmtree(path)


def clean_build_outputs() -> None:
    for path in [BUILD_DIR, DIST_DIR, INSTALLER_OUTPUT_DIR]:
        safe_rmtree(path)
    trace_log = ROOT / "pyarmor.trace.log"
    if trace_log.exists():
        trace_log.unlink()
    BUILD_DIR.mkdir(parents=True, exist_ok=True)


def run_command(command: list[str], *, env: dict[str, str] | None = None, allow_failure: bool = False) -> dict:
    started_at = datetime.now().isoformat(timespec="seconds")
    print("运行：" + " ".join(command))
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=env)
    output = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
    if completed.stdout:
        print(completed.stdout)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr)
    payload = {
        "command": command,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "output_tail": output.strip()[-8000:],
    }
    if completed.returncode != 0 and not allow_failure:
        raise RuntimeError(f"命令失败：{' '.join(command)}\n{payload['output_tail']}")
    return payload


def command_version(command: list[str]) -> str:
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    return output.splitlines()[0] if output else ""


def find_executable(candidates: list[str | Path]) -> str | None:
    for candidate in candidates:
        if not candidate:
            continue
        candidate_text = str(candidate)
        path = Path(candidate_text)
        if path.is_absolute() and path.exists():
            return str(path)
        found = shutil.which(candidate_text)
        if found:
            return found
    return None


def find_pyarmor_command(explicit_path: str = "") -> list[str] | None:
    explicit = find_executable([explicit_path]) if explicit_path else None
    if explicit:
        return [explicit]
    pyarmor = find_executable(["pyarmor"])
    if pyarmor:
        return [pyarmor]
    scripts_dir = sysconfig.get_path("scripts")
    script_name = "pyarmor.exe" if os.name == "nt" else "pyarmor"
    scripts_pyarmor = Path(scripts_dir) / script_name if scripts_dir else ""
    pyarmor = find_executable([scripts_pyarmor])
    if pyarmor:
        return [pyarmor]
    return None


def file_records(rel_paths: list[str]) -> list[dict]:
    records = []
    missing = []
    for rel_path in rel_paths:
        path = ROOT / rel_path
        if not path.exists():
            missing.append(rel_path)
            continue
        records.append(
            {
                "path": rel_path,
                "size_bytes": path.stat().st_size,
            }
        )
    if missing:
        raise FileNotFoundError("保护目标文件不存在：\n" + "\n".join(missing))
    return records


def skipped_big_script_records() -> list[dict]:
    records = file_records(SKIPPED_BIG_SCRIPT_TARGETS)
    for record in records:
        record["reason"] = "large_script_or_high_risk_ui_kept_as_pyinstaller_bytecode"
    return records


def validate_free_pyarmor_targets() -> list[dict]:
    """Require every declared target; PyArmor itself supports scripts above 32KB."""
    return file_records(FREE_PYARMOR_TARGETS)


def find_inno_setup(explicit_path: str = "") -> str | None:
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
    program_files = os.environ.get("ProgramFiles", "")
    candidates: list[str | Path] = [
        explicit_path,
        os.environ.get("ISCC", ""),
        Path(program_files_x86) / "Inno Setup 6" / "ISCC.exe" if program_files_x86 else "",
        Path(program_files) / "Inno Setup 6" / "ISCC.exe" if program_files else "",
        "ISCC.exe",
        "iscc",
    ]
    return find_executable(candidates)


def normalize_app_version(version: str) -> str:
    text = version.strip()
    if text.startswith("v"):
        text = text[1:]
    text = text.split("_", 1)[0]
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}(?:[-._][A-Za-z0-9]+)?", text):
        raise ValueError(f"安装包版本号格式不安全：{version}")
    return text


def default_release_version(app_version: str) -> str:
    return f"v{app_version}_phase9"


def version_note_file(release_version: str, app_version: str) -> Path:
    candidates = [
        ROOT / "docs" / f"版本说明_{release_version}.txt",
        ROOT / "docs" / f"版本说明_v{app_version}_phase9.txt",
        ROOT / "docs" / f"版本说明_v{app_version}.txt",
    ]
    for path in candidates:
        if path.exists():
            return path
    latest = sorted((ROOT / "docs").glob("版本说明_*.txt"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    if latest:
        return latest[0]
    raise FileNotFoundError("未找到版本说明文档：docs/版本说明_*.txt")


def module_to_path(module: str) -> Path:
    return ROOT / Path(*module.split(".")).with_suffix(".py")


def prepare_staging_source() -> None:
    PROTECTED_SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "main.py", PROTECTED_SOURCE_DIR / "main.py")
    shutil.copytree(
        ROOT / "assets",
        PROTECTED_SOURCE_DIR / "assets",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        dirs_exist_ok=True,
    )
    shutil.copytree(
        ROOT / "stock_tool",
        PROTECTED_SOURCE_DIR / "stock_tool",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        dirs_exist_ok=True,
    )


def overlay_tree(source: Path, target: Path) -> None:
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        dest = target / rel
        if path.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def collect_trace_text(bcc_result: dict) -> tuple[str, list[str]]:
    trace_paths = [
        ROOT / "pyarmor.trace.log",
        PYARMOR_BCC_DIR / "pyarmor.trace.log",
        PYARMOR_BCC_DIR / "pyarmor.debug.log",
        BUILD_DIR / "pyarmor.trace.log",
    ]
    parts = [str(bcc_result.get("output_tail") or "")]
    found: list[str] = []
    for path in trace_paths:
        if path.exists():
            found.append(str(path))
            try:
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                parts.append(path.read_text(errors="replace"))
    text = "\n".join(part for part in parts if part)
    COMBINED_TRACE_PATH.write_text(text, encoding="utf-8")
    found.append(str(COMBINED_TRACE_PATH))
    return text, found


def verify_bcc_trace(trace_text: str) -> dict:
    if not trace_text.strip():
        raise RuntimeError("未发现 PyArmor BCC trace 或等价输出，拒绝生成商业安装包。")

    normalized_lines = [line.replace("\\", "/") for line in trace_text.splitlines()]
    errors: list[str] = []
    verified: dict[str, list[str]] = {}
    skipped: list[str] = []
    skip_markers = ("skip", "skipped", "unsupported", "failed", "error", "can not", "cannot")

    for module, functions in BCC_TARGETS.items():
        module_path = module.replace(".", "/")
        module_file = module_path + ".py"
        short_file = module.rsplit(".", 1)[-1] + ".py"
        module_lines = [
            line
            for line in normalized_lines
            if module in line or module_path in line or module_file in line or short_file in line
        ]
        if not module_lines:
            errors.append(f"BCC trace 缺少目标模块：{module}")
            continue
        module_mentions_bcc = any("bcc" in line.lower() for line in module_lines)
        if not module_mentions_bcc:
            errors.append(f"BCC trace 未显示模块进入 BCC：{module}")
            continue
        verified[module] = []
        for function_name in functions:
            function_lines = [line for line in module_lines if function_name in line]
            bad_lines = [line for line in function_lines if any(marker in line.lower() for marker in skip_markers)]
            if bad_lines:
                skipped.append(f"{module}.{function_name}")
                continue
            if not function_lines:
                errors.append(f"BCC trace 缺少目标函数：{module}.{function_name}")
                continue
            verified[module].append(function_name)

    if skipped:
        errors.extend(f"BCC 目标函数被跳过：{item}" for item in skipped)
    if errors:
        raise RuntimeError("BCC 加固证明校验失败：\n" + "\n".join(errors))
    return {
        "verified": verified,
        "target_count": sum(len(items) for items in verified.values()),
    }


def preflight(args: argparse.Namespace) -> dict:
    protection_mode = args.protection_mode
    pyarmor_command = find_pyarmor_command(args.pyarmor_path) if protection_mode in {"free-pyarmor", "bcc"} else None
    if protection_mode in {"free-pyarmor", "bcc"} and not pyarmor_command:
        raise RuntimeError("未检测到 PyArmor。请先安装 PyArmor 免费版，或用 --pyarmor-path 指定可执行文件。")

    compiler = None
    if protection_mode == "bcc":
        compiler = find_executable([args.compiler_path, "clang", "clang-cl", "cl", "gcc"])
        if not compiler:
            raise RuntimeError("未检测到 clang/C 编译器。PyArmor BCC 需要可用 C 编译器。")

    iscc = find_inno_setup(args.iscc_path)
    if not iscc:
        raise RuntimeError("未检测到 Inno Setup 6 的 ISCC.exe。请安装 Inno Setup 6 后重试。")

    pyinstaller_version = command_version([sys.executable, "-m", "PyInstaller", "--version"])
    if not pyinstaller_version:
        raise RuntimeError("未检测到 PyInstaller。请先安装 PyInstaller。")

    return {
        "python": sys.version.split()[0],
        "protection_mode": protection_mode,
        "pyarmor": {
            "command": pyarmor_command or [],
            "path": pyarmor_command[0] if pyarmor_command else "",
            "version": command_version([*pyarmor_command, "--version"]) if pyarmor_command else "",
        },
        "pyinstaller": {
            "path": sys.executable,
            "version": pyinstaller_version,
        },
        "c_compiler": {
            "path": compiler or "",
            "version": (
                command_version([compiler, "--version"]) if compiler and Path(compiler).name.lower() != "cl.exe"
                else (command_version([compiler]) if compiler else "")
            ),
        },
        "inno_setup": {
            "path": iscc,
            "version": command_version([iscc, "/?"]),
        },
    }


def verify_pyarmor_output(output_dir: Path, target_files: list[str]) -> None:
    missing = [rel_path for rel_path in target_files if not (output_dir / rel_path).exists()]
    if missing:
        raise FileNotFoundError("PyArmor 输出缺少保护目标：\n" + "\n".join(missing))


def relocate_flat_pyarmor_outputs(output_dir: Path, target_files: list[str]) -> None:
    seen_names: set[str] = set()
    duplicate_names: set[str] = set()
    for rel_path in target_files:
        name = Path(rel_path).name
        if name in seen_names:
            duplicate_names.add(name)
        seen_names.add(name)
    if duplicate_names:
        raise RuntimeError("PyArmor 白名单文件名重复，无法从平铺输出安全归位：" + "、".join(sorted(duplicate_names)))
    for rel_path in target_files:
        target = output_dir / rel_path
        if target.exists():
            continue
        flat_output = output_dir / Path(rel_path).name
        if not flat_output.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(flat_output), str(target))


def build_free_pyarmor_output(pyarmor_command: list[str]) -> tuple[dict, list[dict]]:
    target_records = validate_free_pyarmor_targets()
    if PYARMOR_PLAIN_DIR.exists():
        safe_rmtree(PYARMOR_PLAIN_DIR)
    result = run_command(
        [
            *pyarmor_command,
            "gen",
            "-O",
            str(PYARMOR_PLAIN_DIR),
            *FREE_PYARMOR_TARGETS,
        ]
    )
    relocate_flat_pyarmor_outputs(PYARMOR_PLAIN_DIR, FREE_PYARMOR_TARGETS)
    verify_pyarmor_output(PYARMOR_PLAIN_DIR, FREE_PYARMOR_TARGETS)
    overlay_tree(PYARMOR_PLAIN_DIR, PROTECTED_SOURCE_DIR)
    return result, target_records


def build_bcc_output(pyarmor_command: list[str]) -> tuple[dict, dict]:
    target_files = [str(module_to_path(module).relative_to(ROOT)) for module in BCC_TARGETS]
    missing = [path for path in target_files if not (ROOT / path).exists()]
    if missing:
        raise FileNotFoundError("BCC 目标模块不存在：\n" + "\n".join(missing))

    bcc_commands = [
        [
            *pyarmor_command,
            "gen",
            "-O",
            str(PYARMOR_BCC_DIR),
            "--enable",
            "bcc",
            *target_files,
        ],
        [
            *pyarmor_command,
            "gen",
            "-O",
            str(PYARMOR_BCC_DIR),
            "--enable-bcc",
            *target_files,
        ],
    ]
    bcc_attempts = []
    bcc_result = None
    for command in bcc_commands:
        if PYARMOR_BCC_DIR.exists():
            safe_rmtree(PYARMOR_BCC_DIR)
        result = run_command(command, allow_failure=True)
        bcc_attempts.append(result)
        if result["ok"]:
            bcc_result = dict(result)
            break
    if bcc_result is None:
        tails = "\n\n".join(str(item.get("output_tail") or "") for item in bcc_attempts)
        raise RuntimeError("PyArmor BCC 命令失败，拒绝降级为普通混淆。\n" + tails)
    bcc_result["attempts"] = bcc_attempts
    overlay_tree(PYARMOR_BCC_DIR, PROTECTED_SOURCE_DIR)
    trace_text, trace_paths = collect_trace_text(bcc_result)
    bcc_verification = verify_bcc_trace(trace_text)
    bcc_verification["trace_paths"] = trace_paths
    return bcc_result, bcc_verification


def build_pyinstaller() -> dict:
    env = os.environ.copy()
    env["STOCK_TOOL_PYI_SOURCE_ROOT"] = str(PROTECTED_SOURCE_DIR)
    result = run_command(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--workpath",
            str(PYINSTALLER_WORK_DIR),
            "--distpath",
            str(DIST_DIR),
            "build.spec",
        ],
        env=env,
    )
    exe_path = DIST_APP_DIR / f"{APP_NAME}.exe"
    if not exe_path.exists():
        raise FileNotFoundError(f"PyInstaller onedir 产物缺少主程序：{exe_path}")
    return result


def build_inno_installer(iscc: str, app_version: str, release_version: str) -> tuple[dict, Path]:
    note = version_note_file(release_version, app_version)
    release_note_rel = str(note.relative_to(ROOT)).replace("/", "\\")
    result = run_command(
        [
            iscc,
            f"/DMyAppVersion={app_version}",
            f"/DReleaseNoteFile={release_note_rel}",
            f"/DInstallerBaseFilename={INSTALLER_BASENAME}",
            "installer.iss",
        ]
    )
    installer = INSTALLER_OUTPUT_DIR / INSTALLER_NAME
    if not installer.exists():
        candidates = sorted(INSTALLER_OUTPUT_DIR.glob("*.exe"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not candidates:
            raise FileNotFoundError("Inno Setup 未生成安装包：installer_output/*.exe")
        installer = candidates[0]
    return result, installer


def protection_level_for_mode(protection_mode: str) -> str:
    if protection_mode == "bcc":
        return "bcc"
    if protection_mode == "free-pyarmor":
        return "trial_obfuscation"
    return "none"


def write_manifest(
    *,
    protection_mode: str,
    app_version: str,
    release_version: str,
    tools: dict,
    pyarmor_result: dict | None,
    bcc_result: dict | None,
    pyinstaller_result: dict,
    inno_result: dict,
    bcc_verification: dict | None,
    obfuscated_target_records: list[dict],
    skipped_big_scripts: list[dict],
    installer: Path,
) -> dict:
    pyarmor_enabled = protection_mode in {"free-pyarmor", "bcc"}
    bcc_enabled = protection_mode == "bcc"
    installer_sha256 = sha256_file(installer)
    obfuscated_targets = [str(record["path"]) for record in obfuscated_target_records] if pyarmor_enabled else []
    manifest = {
        "schema_version": 1,
        "title": f"A股选股仪 {protection_mode} 安装包构建证明",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "version": app_version,
        "release_version": release_version,
        "tools": tools,
        "commands": {
            "pyarmor_obfuscation": pyarmor_result,
            "pyarmor_plain": pyarmor_result,
            "pyarmor_bcc": bcc_result,
            "pyinstaller": pyinstaller_result,
            "inno_setup": inno_result,
        },
        "protection": {
            "mode": protection_mode,
            "protection_level": protection_level_for_mode(protection_mode),
            "pyarmor_enabled": pyarmor_enabled,
            "bcc_enabled": bcc_enabled,
            "bcc_required": protection_mode == "bcc",
            "bcc_verified": bool(bcc_verification) if protection_mode == "bcc" else False,
            "trial_build": protection_mode == "free-pyarmor",
            "obfuscated_targets": obfuscated_targets,
            "obfuscated_target_details": obfuscated_target_records if pyarmor_enabled else [],
            "plain_obfuscation_targets": obfuscated_targets,
            "protected_targets": obfuscated_targets,
            "skipped_big_scripts": skipped_big_scripts,
            "bcc_targets": BCC_TARGETS if protection_mode == "bcc" else {},
            "bcc_verification": bcc_verification or {},
            "non_bcc_core_modules": NON_BCC_CORE_MODULES,
            "staging_source_dir": str(PROTECTED_SOURCE_DIR),
            "installer_name": installer.name,
            "installer_sha256": installer_sha256,
        },
        "dist": {
            "mode": "onedir",
            "path": str(DIST_APP_DIR),
        },
        "installer": {
            "path": str(installer),
            "name": installer.name,
            "sha256": installer_sha256,
            "size_bytes": installer.stat().st_size,
        },
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 A股选股仪安装包，并按指定模式写入保护构建证明。")
    parser.add_argument("--version", default=DEFAULT_VERSION, help="安装包版本号，例如 0.2.7。")
    parser.add_argument("--release-version", default="", help="交付版本号，用于选择版本说明，默认 v<version>_phase9。")
    parser.add_argument(
        "--protection-mode",
        default="free-pyarmor",
        choices=PROTECTION_MODES,
        help="保护模式：free-pyarmor 免费混淆；bcc 付费 BCC；plain-installer 仅内部兜底。",
    )
    parser.add_argument("--pyarmor-path", default="", help="PyArmor 可执行文件路径；默认从 PATH 查找。")
    parser.add_argument("--compiler-path", default="", help="clang/cl/gcc 路径；默认自动查找。")
    parser.add_argument("--iscc-path", default="", help="Inno Setup ISCC.exe 路径；默认自动查找。")
    parser.add_argument("--skip-clean", action="store_true", help="保留现有 build/dist/installer_output，仅供排查问题。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app_version = normalize_app_version(args.version)
    release_version = args.release_version.strip() or default_release_version(app_version)
    protection_mode = args.protection_mode

    tools = preflight(args)
    if not args.skip_clean:
        clean_build_outputs()
    else:
        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        INSTALLER_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    prepare_staging_source()
    pyarmor_result = None
    bcc_result = None
    bcc_verification = None
    obfuscated_target_records: list[dict] = []
    if protection_mode in {"free-pyarmor", "bcc"}:
        pyarmor_command = tools["pyarmor"]["command"]
        pyarmor_result, obfuscated_target_records = build_free_pyarmor_output(pyarmor_command)
    if protection_mode == "bcc":
        pyarmor_command = tools["pyarmor"]["command"]
        bcc_result, bcc_verification = build_bcc_output(pyarmor_command)
    pyinstaller_result = build_pyinstaller()
    inno_result, installer = build_inno_installer(tools["inno_setup"]["path"], app_version, release_version)
    manifest = write_manifest(
        protection_mode=protection_mode,
        app_version=app_version,
        release_version=release_version,
        tools=tools,
        pyarmor_result=pyarmor_result,
        bcc_result=bcc_result,
        pyinstaller_result=pyinstaller_result,
        inno_result=inno_result,
        bcc_verification=bcc_verification,
        obfuscated_target_records=obfuscated_target_records,
        skipped_big_scripts=skipped_big_script_records(),
        installer=installer,
    )
    print("[OK] 安装包生成完成")
    print(f"保护模式：{protection_mode}")
    print(f"安装包：{manifest['installer']['path']}")
    print(f"SHA256：{manifest['installer']['sha256']}")
    print(f"保护证明：{MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
