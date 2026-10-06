from __future__ import annotations

"""Release-only static guard for the Qt surface; missing Qt is a failure, never a SKIP."""

import json
import inspect
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _harden_inspect_for_pyside() -> None:
    getsourcefile = inspect.getsourcefile
    getsource = inspect.getsource

    def safe_getsourcefile(obj):
        try:
            return getsourcefile(obj)
        except Exception:
            return None

    def safe_getsource(obj):
        try:
            return getsource(obj)
        except Exception:
            return ""

    inspect.getsourcefile = safe_getsourcefile
    inspect.getsource = safe_getsource


def main() -> int:
    _harden_inspect_for_pyside()
    try:
        import PySide6  # noqa: F401
        import stock_tool.ui.qt_workbench  # noqa: F401
    except Exception as exc:
        raise AssertionError(f"Qt 默认前端必须可导入，不能 SKIP：{exc}") from exc

    workbench = (ROOT / "stock_tool" / "ui" / "qt_workbench.py").read_text(encoding="utf-8")
    sidebar = (ROOT / "stock_tool" / "ui" / "qt_sidebar.py").read_text(encoding="utf-8")
    assert "实验工作台" not in workbench, "默认 Qt 不得保留实验工作台标识"
    assert "迁移中" not in sidebar, "正式导航不得包含迁移中或占位文案"
    assert "_build_placeholder_page" not in workbench, "正式 Qt 不得保留可路由的 placeholder 页面"

    contract = json.loads((ROOT / "stock_tool" / "config" / "frontend_parity_contract.json").read_text(encoding="utf-8"))
    required_action_ids = {
        action_id
        for feature in contract["features"]
        if feature["required_for_default"]
        for action_id in feature["qt_action_ids"]
    }
    action_source = workbench + "\n" + sidebar
    missing = sorted(
        action_id
        for action_id in required_action_ids
        if action_id not in action_source
        and not (action_id.startswith("nav.") and 'f"nav.{page_key}"' in sidebar)
    )
    assert not missing, f"正式 action 未在 Qt 页面代码中出现：{missing}"
    print("[OK] Qt 正式导航与 action 静态检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
