from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QToolButton

from stock_tool.ui.qt_home import HomeDashboard
from stock_tool.ui.qt_sidebar import Sidebar
from stock_tool.ui.qt_widgets import themed_pixmap


ICON_NAMES = {
    "home",
    "search",
    "filter",
    "star",
    "bell",
    "cpu",
    "database",
    "settings",
    "eye",
    "refresh",
    "file-text",
    "activity",
    "chevron-left",
}


def test_svg_assets_exist() -> None:
    icon_dir = ROOT / "stock_tool" / "ui" / "web" / "icons"
    missing = sorted(name for name in ICON_NAMES if not (icon_dir / f"{name}.svg").exists())
    assert not missing, f"缺少 SVG 图标：{missing}"


def test_themed_pixmaps_render() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    for name in ICON_NAMES:
        pixmap = themed_pixmap(name, "#4e8edd")
        assert not pixmap.isNull(), f"{name} pixmap 为空"
        assert pixmap.width() > 0 and pixmap.height() > 0, f"{name} pixmap 尺寸异常"


def test_home_and_sidebar_icon_smoke() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    home = HomeDashboard()
    sidebar = Sidebar()
    home.apply_stats(
        {
            "tracking": {"pool": 2, "watching": 1, "holding": 1, "review": 1, "done": 3, "in_progress": 2},
            "alerts": {"monitoring": 1, "triggered_today": 1},
            "health_rows": [("行情", "正常", "5分钟前"), ("财务", "偏旧", "10天前")],
            "watch_rows": [
                {
                    "code": "000001",
                    "name": "平安银行",
                    "price": 10.5,
                    "change_pct": 1.2,
                    "tags": ["观察中", "待复盘", "价格预警"],
                    "updated_at": "5分钟前",
                }
            ],
        }
    )
    for card in (home.kpi_pool, home.kpi_alert, home.kpi_review, home.kpi_health):
        pixmap = card.icon_label.pixmap()
        assert pixmap is not None and not pixmap.isNull(), "KPI 图标未渲染"
    assert home.kpi_review.value_label.text() == "1", "待复核任务 KPI 应显示待复盘数"
    assert home.watch_table.cellWidget(0, 4) is not None, "首页自选状态列应使用药丸控件"

    nav_buttons = [button for button in sidebar.findChildren(QToolButton) if button.objectName() == "navItem"]
    assert len(nav_buttons) == 8, "侧栏导航按钮数量异常"
    for button in nav_buttons:
        assert not button.icon().isNull(), f"{button.text()} 图标为空"

    sidebar.set_collapsed(True)
    assert all(button.toolButtonStyle() == Qt.ToolButtonIconOnly for button in nav_buttons)


def run() -> None:
    test_svg_assets_exist()
    test_themed_pixmaps_render()
    test_home_and_sidebar_icon_smoke()
    print("[OK] qt_icons 图标资源与离屏冒烟通过")


if __name__ == "__main__":
    run()
