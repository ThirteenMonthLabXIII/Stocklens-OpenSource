"""Export a source-only public candidate without touching the working copy.

This does not create a Git repository, publish files, or run commercial release
tooling. The chosen project license and third-party notices are preserved.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 5 * 1024 * 1024
ROOT_FILES = {
    "main.py", ".gitignore", ".gitleaks.toml", "AGENTS.md", "PRODUCT.md",
    "requirements.txt", "requirements-optional.txt", "requirements-build.txt",
    "requirements-v3.lock", "build.spec", "build_qt.spec", "installer.iss",
    "launch.ps1", "安装依赖.bat", "启动软件.bat", "打包exe.bat",
    "生成安装包.bat", "生成第九阶段交付包.bat", "生成V3商业交付包.bat",
    "LICENSE", "NOTICE",
}
PUBLIC_DOCS = {
    "开发说明.md", "数据快照契约.md", "选股闭环设计.md", "代码区块测试清单.md",
    "Qt功能合同.md", "V3_发布说明.md", "V3_工作区纳入清单.md",
    "资金流_3a_3b_3c_3d收口说明.md", "MVP_CLOSED_SET_RUNBOOK.md",
    "免责声明.txt", "开源准备说明.md", "开源价值与模块导览.md",
    "阶段0_契约冻结.md", "阶段0.5_选股需求规格.md",
    "阶段1_确定性候选层.md", "阶段2_证据包与事实引用校验.md",
    "阶段3_基本面Agent.md", "阶段4_五Agent编排.md",
    "阶段5_证据汇总与候选状态.md", "阶段6_Qt工作台改造.md",
    "阶段7_观察复盘与评价.md",
}
PUBLIC_RESEARCH_CONFIGS = {
    "adaptive_baseline_policy.json", "weights_preregister.json",
    "weights_preregister_v2.json", "requirements-research.txt",
}
EXCLUDED_DIRS = {
    ".git", ".claude", ".codebase-memory", ".toolcache", ".ci_artifacts",
    "__pycache__", ".pytest_cache", ".venv", "venv", "build", "dist",
    "release", "installer_output", "acceptance_reports", "release_signing",
    "private_keys", "data", "out", "cache", "logs", "pit_reports",
}
EXCLUDED_SUFFIXES = {
    ".sqlite", ".sqlite3", ".db", ".duckdb", ".zip", ".exe", ".dll",
    ".pyc", ".pyo", ".log", ".key", ".pem", ".p12", ".pfx", ".der",
    ".crt", ".cer", ".csv", ".parquet", ".xlsx", ".pkl", ".pickle",
}
TEMPLATE_MAP = {
    "README.md": "README.md",
    "CONTRIBUTING.md": "CONTRIBUTING.md",
    "SECURITY.md": "SECURITY.md",
    "THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
    "ci.yml": ".github/workflows/ci.yml",
    "bug_report.md": ".github/ISSUE_TEMPLATE/bug_report.md",
    "support.md": ".github/ISSUE_TEMPLATE/support.md",
    "PULL_REQUEST_TEMPLATE.md": ".github/PULL_REQUEST_TEMPLATE.md",
}
PUBLIC_IGNORE = """
# Source-only public checkout: keep runtime data and generated output local.
my_data*/
pit_reports/
research/data/
.ci_artifacts/
.codebase-memory/
*.sqlite
*.sqlite-*
*.duckdb
*.parquet
*.csv
*.xlsx
*.pkl
*.pickle
"""


def excluded(relative: Path) -> bool:
    parts = tuple(part.casefold() for part in relative.parts)
    name = relative.name.casefold()
    return (
        any(part in EXCLUDED_DIRS or part.startswith("my_data") for part in parts)
        or name == "data_location.txt"
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith(".private.json")
        or ".sqlite3-" in name
        or ".sqlite-" in name
        or relative.suffix.casefold() in EXCLUDED_SUFFIXES
    )


def allowed(relative: Path) -> bool:
    if excluded(relative):
        return False
    parts = relative.parts
    if len(parts) == 1:
        return relative.name in ROOT_FILES
    if parts[0] == "stock_tool":
        return relative.suffix.casefold() in {
            ".py", ".json", ".html", ".svg", ".ttf", ".txt", ".png",
        }
    if parts[0] == "tools":
        return relative.suffix.casefold() in {".py", ".ps1", ".js"}
    if parts[0] == "research":
        return relative.suffix == ".py" or (
            len(parts) == 2 and relative.name in PUBLIC_RESEARCH_CONFIGS
        )
    if parts[0] == "assets":
        return len(parts) == 2 and relative.name in {"stocklens.ico", "stocklens_icon.png"}
    if parts[0] == "docs":
        return (
            len(parts) == 2
            and (relative.name in PUBLIC_DOCS or relative.name.startswith("版本说明_"))
        ) or (
            len(parts) == 3 and parts[1] == "open_source"
            and relative.name in set(TEMPLATE_MAP) | {"LICENSE", "NOTICE"}
        )
    return False


def check_source_path(path: Path, source: Path) -> None:
    is_junction = getattr(path, "is_junction", lambda: False)
    if path.is_symlink() or is_junction() or not path.resolve().is_relative_to(source):
        raise ValueError(f"Linked or external source path refused: {path.relative_to(source)}")


def source_files(source: Path) -> list[Path]:
    files: list[Path] = []
    for directory, names, filenames in os.walk(source, followlinks=False):
        base = Path(directory)
        names[:] = sorted(
            name for name in names
            if not excluded((base / name).relative_to(source))
            and ((base / name).relative_to(source).parts[0] in {
                "stock_tool", "tools", "research", "assets", "docs",
            })
        )
        for name in names:
            check_source_path(base / name, source)
        for name in sorted(filenames):
            path = base / name
            if not allowed(path.relative_to(source)):
                continue
            check_source_path(path, source)
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError(f"Source file exceeds 5 MiB: {path.relative_to(source)}")
            files.append(path)
    return files


def export_source(source: Path, output: Path) -> tuple[int, int]:
    source = source.resolve(strict=True)
    output = output.resolve()
    if not source.is_dir() or output == source or source.is_relative_to(output):
        raise ValueError("Output must be a separate new directory")
    if output.is_relative_to(source):
        top = output.relative_to(source).parts[0].casefold()
        if top not in {"release", ".toolcache"}:
            raise ValueError("An output inside the source must be under release/ or .toolcache/")
    if output.exists():
        raise ValueError("Output already exists; choose a new directory (no files are overwritten)")
    files = source_files(source)
    templates = source / "docs" / "open_source"
    for name in TEMPLATE_MAP:
        if not (templates / name).is_file():
            raise ValueError(f"Missing public template: docs/open_source/{name}")
    if not (source / "main.py").is_file():
        raise ValueError("Missing main.py")
    output.mkdir(parents=True, exist_ok=False)
    for path in files:
        target = output / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for name, relative in TEMPLATE_MAP.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(templates / name, target)
    for name in ("LICENSE", "NOTICE"):
        path = templates / name
        if path.is_file():
            check_source_path(path, source)
            shutil.copy2(path, output / name)
            # Keep a selected license in the reusable export templates as well.
            shutil.copy2(path, output / "docs" / "open_source" / name)
    ignore = output / ".gitignore"
    existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    ignore.write_text(existing.rstrip() + "\n" + PUBLIC_IGNORE, encoding="utf-8")
    exported = [path for path in output.rglob("*") if path.is_file()]
    return len(exported), sum(path.stat().st_size for path in exported)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        count, size = export_source(args.source, args.output)
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] Source candidate: {args.output.resolve()}")
    print(f"Files: {count}; size: {size / 1024 / 1024:.2f} MiB")
    print("Project license and third-party notices are preserved; no runtime data or Git history is included.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
