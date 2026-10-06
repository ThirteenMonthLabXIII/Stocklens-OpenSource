from __future__ import annotations

"""Keep the Qt migration baseline and its P5/P6 promotion facts consistent."""

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtWidgets import QApplication, QToolButton

from stock_tool.core.pit_database import PIT_SCHEMA_VERSION
from stock_tool.core.v1_database import V1_SCHEMA_VERSION
from stock_tool.ui.qt_sidebar import NAVIGATION_GROUPS, Sidebar
from test_frontend_default_gate import REQUIRED_DEFAULT_FEATURE_IDS, validate_contract


BASELINE_COMMIT = "ad5c45a5e73276e7d289a406de71fcf03fcdd6fd"
EXPECTED_NAVIGATION = (
    ("工作台", (("home", "今日概览"),)),
    ("研究", (("research", "个股研究"), ("screener", "条件选股"), ("watchlist", "自选股"))),
    ("跟踪", (("alerts", "价格预警"), ("review", "观察与复盘"))),
    ("管理", (("data", "数据与更新"), ("settings", "设置"))),
)
SENSITIVE_ROOTS = {
    "build",
    "dist",
    "installer_output",
    "my_data",
    "private_keys",
    "release",
    "docs/acceptance_reports",
    "research/out",
}
SENSITIVE_NAMES = {".env", "data_location.txt"}
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
    ".private.json",
    ".sqlite3",
    ".zip",
}


def _run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )


def _navigation_from_contract(payload: dict) -> tuple:
    return tuple(
        (
            group["group"],
            tuple((item["page_key"], item["label"]) for item in group["items"]),
        )
        for group in payload["qt_navigation_groups"]
    )


def _navigation_from_code() -> tuple:
    return tuple(
        (
            group_name,
            tuple((page_key, label) for page_key, _icon_name, label in items),
        )
        for group_name, items in NAVIGATION_GROUPS
    )


def _worktree_category(path: str) -> str:
    if path.startswith("stock_tool/core/"):
        return "共享核心"
    if path.startswith("stock_tool/ui/"):
        return "前端"
    if path.startswith("stock_tool/config/"):
        return "配置与合同"
    if path.startswith("tools/"):
        return "测试与交付工具"
    if path.startswith("docs/"):
        return "文档"
    if path.startswith("research/"):
        return "研究源码"
    if path.startswith(".github/"):
        return "持续集成"
    return "仓库根配置"


def _is_sensitive(path: str) -> bool:
    normalized = path.replace("\\", "/").strip('"').lower()
    if normalized in SENSITIVE_NAMES:
        return True
    if any(normalized == root or normalized.startswith(f"{root}/") for root in SENSITIVE_ROOTS):
        return True
    return any(normalized.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)


def test_frozen_runtime_facts() -> None:
    head = _run("git", "rev-parse", "HEAD").stdout.strip()
    baseline_in_history = _run(
        "git", "merge-base", "--is-ancestor", BASELINE_COMMIT, head, check=False
    )
    assert baseline_in_history.returncode == 0, f"P0 Git 基线不在当前历史中：baseline={BASELINE_COMMIT} HEAD={head}"
    assert sys.version_info[:3] == (3, 12, 0), f"Python 基线漂移：{sys.version.split()[0]}"
    assert PYSIDE_VERSION == "6.11.1", f"PySide6 基线漂移：{PYSIDE_VERSION}"
    assert V1_SCHEMA_VERSION == 16, f"V1 schema 基线漂移：{V1_SCHEMA_VERSION}"
    assert PIT_SCHEMA_VERSION == 3, f"PIT schema 基线漂移：{PIT_SCHEMA_VERSION}"

    app_entry = (ROOT / "stock_tool" / "ui" / "app_entry.py").read_text(encoding="utf-8")
    default_spec = (ROOT / "build.spec").read_text(encoding="utf-8")
    qt_spec = (ROOT / "build_qt.spec").read_text(encoding="utf-8")
    assert "stock_tool.ui.qt_workbench" in app_entry
    assert "stock_tool.ui." + "main_window" not in app_entry
    assert "STOCK_TOOL_" + "QT_WORKBENCH" not in app_entry
    assert 'excludes=["PyQt6", "PyQt5", "PySide2"]' in default_spec
    assert "QtWebEngineProcess.exe" in default_spec
    assert "PySide6.QtWebEngineWidgets" in default_spec
    assert "backend_tkagg" not in default_spec
    assert "PySide6" in qt_spec and "QtWebEngineProcess.exe" in qt_spec
    assert "backend_tkagg" not in qt_spec
    release_identity = (ROOT / "stock_tool" / "config" / "release_identity.py").read_text(encoding="utf-8")
    assert "QT_WINDOW_TITLE" in release_identity and "Qt 工作台" in release_identity


def test_contract_and_navigation_are_consistent() -> None:
    payload = json.loads(
        (ROOT / "stock_tool" / "config" / "frontend_parity_contract.json").read_text(encoding="utf-8")
    )
    failures = validate_contract(payload, strict=False)
    assert not failures, "\n".join(failures)
    assert set(payload["default_required_feature_ids"]) == REQUIRED_DEFAULT_FEATURE_IDS
    assert _navigation_from_contract(payload) == EXPECTED_NAVIGATION
    assert _navigation_from_code() == EXPECTED_NAVIGATION

    app = QApplication.instance() or QApplication([])
    assert app is not None
    sidebar = Sidebar()
    buttons = {
        str(button.property("pageKey")): button.text()
        for button in sidebar.findChildren(QToolButton)
        if button.objectName() == "navItem"
    }
    assert buttons == {page_key: label for _group, items in EXPECTED_NAVIGATION for page_key, label in items}

    qt_sources = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "stock_tool/ui/qt_sidebar.py",
            "stock_tool/ui/qt_home.py",
            "stock_tool/ui/qt_watchlist.py",
            "stock_tool/ui/qt_workbench.py",
        )
    )
    assert "AI复盘任务" not in qt_sources
    assert "观察与复盘（迁移中）" not in qt_sources


def test_strict_gate_blocks_until_all_features_are_proven() -> None:
    payload = json.loads(
        (ROOT / "stock_tool" / "config" / "frontend_parity_contract.json").read_text(encoding="utf-8")
    )
    strict_failures = validate_contract(payload, strict=True)
    completed = _run(sys.executable, "tools/test_frontend_default_gate.py", check=False)
    if strict_failures:
        assert completed.returncode != 0, "仍有未对齐能力时严格转正闸门必须失败"
        assert "[BLOCKED]" in completed.stdout
        for feature in payload["features"]:
            if feature["required_for_default"] and feature["status"] != "已对齐":
                assert feature["feature_id"] in completed.stdout
    else:
        assert completed.returncode == 0, completed.stdout


def test_every_dirty_path_is_classified_and_sensitive_paths_are_excluded() -> None:
    _run("git", "diff", "--check")
    _run("git", "diff", "--cached", "--check")
    rows = [line for line in _run("git", "status", "--porcelain=v1").stdout.splitlines() if line]
    audited: list[tuple[str, str, str]] = []
    sensitive: list[str] = []
    for row in rows:
        status = row[:2]
        path = row[3:].split(" -> ")[-1]
        audited.append((status, path, _worktree_category(path)))
        if _is_sensitive(path):
            sensitive.append(path)
    assert len(audited) == len(rows), "工作区存在未分类路径"
    assert not sensitive, f"工作区出现禁止纳入迁移的敏感/产物路径：{sensitive}"
    summary = Counter(category for _status, _path, category in audited)
    print(f"[AUDIT] 已逐项分类 {len(audited)} 个工作区路径：{dict(sorted(summary.items()))}")

    ignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for required_rule in ("research/out/", "private_keys/", "release/", "*.sqlite3", "*.db", "*.zip", ".env"):
        assert required_rule in ignore_text, f"缺少数据/交付排除规则：{required_rule}"


def test_p0_exit_checklist_is_closed() -> None:
    baseline = (ROOT / "docs" / "archive" / "frontend-migration" / "Qt迁移基线.md").read_text(encoding="utf-8")
    exit_section = baseline.split("## P0 退出判定", 1)[1]
    assert "- [ ]" not in exit_section, "P0 退出清单仍有未完成项"
    assert "python tools\\test_qt_p0_acceptance.py" in baseline


def main() -> int:
    test_frozen_runtime_facts()
    test_contract_and_navigation_are_consistent()
    test_strict_gate_blocks_until_all_features_are_proven()
    test_every_dirty_path_is_classified_and_sensitive_paths_are_excluded()
    test_p0_exit_checklist_is_closed()
    print("[OK] Qt 迁移 P0 基线、合同、导航、工作区边界和阻断路径验收通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
