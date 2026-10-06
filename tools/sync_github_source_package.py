from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_ROOT = ROOT / "release"
SOURCE_NAME = "02_GitHub源码版_可二次开发"
REPORT_DIR = ROOT / "docs" / "acceptance_reports"

FORBIDDEN_PARTS = {
    ".pyarmor",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "installer_output",
    "my_data",
    "private_keys",
    "release",
}
FORBIDDEN_NAMES = {
    "data_location.txt",
    "license_private_key.json",
    "pyarmor.trace.log",
}
FORBIDDEN_SUFFIXES = {
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
    ".zip",
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
REQUIRED_FILES = {
    ".gitignore",
    "README.md",
    "main.py",
    "requirements.txt",
    "build.spec",
    "tools/prepare_phase9_release.py",
    "tools/sync_github_source_package.py",
    ".github/PULL_REQUEST_TEMPLATE.md",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把第九阶段源码包同步到 GitHub 私有仓库工作目录。")
    parser.add_argument("--target-dir", required=True, help="私有 GitHub 仓库本地工作目录。")
    parser.add_argument("--release-dir", default="", help="指定 release/V1_商业交付_* 目录；为空则自动选择最新。")
    parser.add_argument("--source-dir", default="", help="直接指定源码包目录；优先级高于 --release-dir。")
    parser.add_argument("--version", default="v0.2.7_phase9", help="同步版本号，用于报告和建议 tag。")
    parser.add_argument("--dry-run", action="store_true", help="只检查和打印计划，不改动目标目录。")
    parser.add_argument("--allow-non-git", action="store_true", help="允许目标目录不是 Git 仓库，主要用于首次空目录或自动化测试。")
    parser.add_argument("--init-git", action="store_true", help="目标目录没有 .git 时自动 git init。")
    parser.add_argument("--report-dir", default="", help="同步报告输出目录，默认 docs/acceptance_reports。")
    return parser.parse_args()


def _rel(path: Path, base: Path) -> str:
    return str(path.relative_to(base)).replace("\\", "/")


def latest_release_dir() -> Path:
    candidates = [path for path in RELEASE_ROOT.glob("V1_商业交付_*") if (path / SOURCE_NAME).is_dir()]
    if not candidates:
        raise FileNotFoundError("未找到 release/V1_商业交付_* 源码包目录，请先运行 prepare_phase9_release.py。")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def resolve_source_dir(args: argparse.Namespace) -> Path:
    if args.source_dir:
        source = Path(args.source_dir)
    else:
        release_dir = Path(args.release_dir) if args.release_dir else latest_release_dir()
        source = release_dir / SOURCE_NAME
    source = source.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"源码包目录不存在：{source}")
    return source


def scan_forbidden(base: Path) -> list[str]:
    errors: list[str] = []
    for path in base.rglob("*"):
        if path.is_dir() and path.name == ".git":
            continue
        rel = _rel(path, base)
        lowered_parts = {part.lower() for part in path.relative_to(base).parts}
        name = path.name.lower()
        suffix = path.suffix.lower()
        sensitive_name = (
            name == ".env"
            or name.startswith(".env.")
            or name.endswith(".private.json")
            or any(part in name for part in SENSITIVE_NAME_PARTS)
        )
        if lowered_parts & FORBIDDEN_PARTS:
            errors.append(f"包含禁用目录片段：{rel}")
        if path.is_file() and (name in FORBIDDEN_NAMES or suffix in FORBIDDEN_SUFFIXES or sensitive_name):
            errors.append(f"包含禁用文件：{rel}")
    return sorted(set(errors))


def validate_source(source_dir: Path) -> None:
    files = {_rel(path, source_dir) for path in source_dir.rglob("*") if path.is_file()}
    missing = sorted(REQUIRED_FILES - files)
    errors = scan_forbidden(source_dir)
    if missing:
        errors.extend(f"源码包缺少必需文件：{item}" for item in missing)
    if errors:
        raise RuntimeError("源码包同步前检查失败：\n" + "\n".join(errors))


def ensure_target(args: argparse.Namespace) -> Path:
    target = Path(args.target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    if args.init_git and not (target / ".git").exists() and not args.dry_run:
        completed = subprocess.run(["git", "init"], cwd=target, text=True, capture_output=True)
        if completed.returncode != 0:
            raise RuntimeError("git init 失败：\n" + (completed.stdout or "") + (completed.stderr or ""))
    if not (target / ".git").exists() and not args.allow_non_git:
        raise RuntimeError("目标目录不是 Git 仓库。首次空目录请加 --init-git，测试目录请加 --allow-non-git。")
    return target


def validate_target_path(target: Path) -> None:
    target = target.resolve()
    if target == ROOT.resolve():
        raise RuntimeError("目标目录不能是当前源码工作目录。")
    if target == target.anchor:
        raise RuntimeError("目标目录不能是磁盘根目录。")
    if target in {Path.home().resolve(), Path.home().parent.resolve()}:
        raise RuntimeError("目标目录过宽，拒绝同步。")


def list_source_files(source_dir: Path) -> list[Path]:
    return [path for path in sorted(source_dir.rglob("*")) if path.is_file()]


def list_target_paths(target_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(target_dir.rglob("*")):
        if ".git" in path.relative_to(target_dir).parts:
            continue
        paths.append(path)
    return paths


def sync_tree(source_dir: Path, target_dir: Path, *, dry_run: bool) -> dict[str, int | list[str]]:
    source_files = list_source_files(source_dir)
    source_rel_files = {_rel(path, source_dir) for path in source_files}
    target_existing = list_target_paths(target_dir)
    target_rel_files = {_rel(path, target_dir) for path in target_existing if path.is_file()}
    target_rel_dirs = sorted((_rel(path, target_dir) for path in target_existing if path.is_dir()), reverse=True)

    to_delete_files = sorted(target_rel_files - source_rel_files)
    to_copy_files = sorted(source_rel_files)
    if not dry_run:
        for rel in to_delete_files:
            (target_dir / rel).unlink()
        for rel in target_rel_dirs:
            path = target_dir / rel
            if path.exists() and path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        for rel in to_copy_files:
            src = source_dir / rel
            dest = target_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

    return {
        "copied": len(to_copy_files),
        "deleted": len(to_delete_files),
        "source_file_count": len(source_rel_files),
        "deleted_files": to_delete_files[:200],
    }


def git_status(target_dir: Path) -> dict[str, str | bool]:
    if not (target_dir / ".git").exists():
        return {"available": False, "short": "", "message": "目标目录不是 Git 仓库"}
    completed = subprocess.run(["git", "status", "--short"], cwd=target_dir, text=True, capture_output=True)
    return {
        "available": completed.returncode == 0,
        "short": completed.stdout.strip(),
        "message": completed.stderr.strip(),
    }


def write_report(payload: dict, report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"github_source_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main() -> int:
    args = parse_args()
    source_dir = resolve_source_dir(args)
    validate_source(source_dir)
    target_dir = ensure_target(args)
    validate_target_path(target_dir)

    summary = sync_tree(source_dir, target_dir, dry_run=args.dry_run)
    target_errors = [] if args.dry_run else scan_forbidden(target_dir)
    status = git_status(target_dir)
    payload = {
        "title": "A股选股仪 GitHub 私有源码同步报告",
        "version": args.version,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": bool(args.dry_run),
        "source_dir": str(source_dir),
        "target_dir": str(target_dir),
        "summary": summary,
        "target_forbidden_errors": target_errors,
        "git_status": status,
        "suggested_commands": [
            "git status",
            "git add .",
            f'git commit -m "Release {args.version} source package"',
            f"git tag {args.version}",
            "git push",
            "git push --tags",
        ],
    }
    report_path = write_report(payload, Path(args.report_dir) if args.report_dir else REPORT_DIR)
    print(f"同步报告：{report_path}")
    print(f"源码包：{source_dir}")
    print(f"目标目录：{target_dir}")
    print(f"复制文件：{summary['copied']}，删除旧文件：{summary['deleted']}，dry-run={args.dry_run}")
    if target_errors:
        print("[FAIL] 目标目录包含禁用文件：")
        for item in target_errors:
            print(item)
        return 1
    if status["available"] and status["short"]:
        print("Git status --short:")
        print(status["short"])
    print("[OK] GitHub 私有源码同步检查完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
