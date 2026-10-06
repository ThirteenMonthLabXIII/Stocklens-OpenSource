from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

from stock_tool.ui import qt_workbench
from stock_tool.core.notify import MemoryNotificationChannel, Notifier
from tools.test_qt_workbench_smoke import FakeObserverFetcher, FakeWebEngineView, wait_for


def _button(window, action_id: str):
    return next(widget for widget in window.findChildren(qt_workbench.QPushButton) if widget.property("action_id") == action_id)


def test_tracking_review_by_real_click() -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
        data_dir = Path(raw) / "data"; data_dir.mkdir()
        qt_workbench.read_data_dir = lambda: data_dir
        qt_workbench.read_configured_data_dir = lambda: None
        qt_workbench.write_data_dir = lambda _path: None
        qt_workbench.QWebEngineView = FakeWebEngineView
        app = qt_workbench.QApplication.instance() or qt_workbench.QApplication([])
        window = qt_workbench.StockWorkbenchWindow(notifier=Notifier([MemoryNotificationChannel()])); window.show(); wait_for(window, "data_refresh")
        window.fetcher = FakeObserverFetcher()
        created = window.review_service.create_tracking_task(code="000101", name="Qt样例", fail_price=9, target_price=11, due_date="2099-01-01", actor="fixture")
        assert created.ok
        window._navigate("review")
        window.review_tracking_table.selectRow(0)
        QTest.mouseClick(_button(window, "review.tracking.review"), qt_workbench.Qt.LeftButton)
        wait_for(window, "tracking_review")
        task = window.review_service.list_tracking_tasks()[0]
        assert task.result == "达到目标观察条件"
        explanation_item = window.review_tracking_table.item(0, 7)
        assert explanation_item is not None
        assert explanation_item.text() == task.last_review
        assert explanation_item.toolTip() == task.last_review
        assert window.review_tracking_table.wordWrap()
        assert window.review_tracking_table.textElideMode() == qt_workbench.Qt.ElideNone
        assert "失效价是下限" in window.review_tracking_hint.text()

        def confirm() -> None:
            for item in app.topLevelWidgets():
                if isinstance(item, QMessageBox) and item.isVisible():
                    QTest.mouseClick(item.button(qt_workbench.QMessageBox.Yes), qt_workbench.Qt.LeftButton)
                    return
        QTimer.singleShot(50, confirm)
        QTest.mouseClick(_button(window, "review.tracking.delete"), qt_workbench.Qt.LeftButton)
        assert not window.review_service.list_tracking_tasks()
        window.close()
        if window.v1_database is not None: window.v1_database.close()


if __name__ == "__main__":
    test_tracking_review_by_real_click()
    print("[OK] Qt 观察复盘真实点击测试通过")
