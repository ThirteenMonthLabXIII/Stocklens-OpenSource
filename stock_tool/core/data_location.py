from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
import sys
from pathlib import Path


DB_FILENAME = "stock_tool.sqlite3"
LOCATION_FILENAME = "data_location.txt"


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(sys.argv[0]).resolve().parent


def location_file() -> Path:
    return app_base_dir() / LOCATION_FILENAME


def read_data_dir() -> Path | None:
    data_dir = read_configured_data_dir()
    return data_dir if data_dir and data_dir.exists() else None


def read_configured_data_dir() -> Path | None:
    path = location_file()
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()


def write_data_dir(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    location_file().write_text(str(data_dir.resolve()), encoding="utf-8")


def db_path_for(data_dir: Path) -> Path:
    return data_dir / DB_FILENAME


@dataclass(frozen=True)
class DataMigrationPreflight:
    source: Path
    target: Path
    file_count: int
    total_bytes: int


def preflight_data_migration(old_dir: Path, new_dir: Path, *, require_empty: bool = False) -> DataMigrationPreflight:
    """Validate a safe, restart-required data-directory migration.

    Qt's P4 migration passes ``require_empty=True`` so a failed copy can never
    merge partial data with a pre-existing user directory.  The default retains
    the legacy merge behavior used by existing callers.  The configured location
    is not changed here; callers update it only after :func:`migrate_data_dir`
    succeeds.
    """
    old_dir = old_dir.resolve()
    new_dir = new_dir.resolve()
    if old_dir == new_dir:
        raise ValueError("新旧数据目录相同，无需迁移")
    if not old_dir.exists():
        raise FileNotFoundError(f"原数据目录不存在：{old_dir}")
    if not old_dir.is_dir():
        raise NotADirectoryError(f"原数据目录不是文件夹：{old_dir}")
    if old_dir in new_dir.parents or new_dir in old_dir.parents:
        raise ValueError("新目录不能位于旧目录内，旧目录也不能位于新目录内")
    if require_empty and new_dir.exists() and any(new_dir.iterdir()):
        raise ValueError("新数据目录必须为空，避免覆盖或混入既有数据")

    file_count = 0
    total_bytes = 0
    for path in old_dir.rglob("*"):
        if path.is_file():
            file_count += 1
            total_bytes += path.stat().st_size
    return DataMigrationPreflight(old_dir, new_dir, file_count, total_bytes)


def migrate_data_dir(old_dir: Path, new_dir: Path, *, require_empty: bool = False) -> DataMigrationPreflight:
    """Copy all data through a staging directory, then atomically publish it.

    If copying fails the original directory and the location pointer remain
    untouched.  A best-effort cleanup removes the staging directory; callers can
    safely retry after fixing the reported filesystem problem.
    """
    plan = preflight_data_migration(old_dir, new_dir, require_empty=require_empty)
    if not require_empty:
        plan.target.mkdir(parents=True, exist_ok=True)
        for item in plan.source.iterdir():
            target = plan.target / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            elif item.is_file():
                shutil.copy2(item, target)
        return plan
    stage_root = Path(tempfile.mkdtemp(prefix=".stock_tool_migrate_", dir=str(plan.target.parent)))
    staged_data = stage_root / "data"
    try:
        shutil.copytree(plan.source, staged_data)
        if plan.target.exists():
            plan.target.rmdir()
        os.replace(staged_data, plan.target)
        return plan
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
