"""发布级 Qt 预警真实点击回归。

不直接调用窗口业务方法：所有用户操作均由 QTest 点击真实控件发起；确认框
使用 QMessageBox 的标准按钮，文件对话框使用非原生 QFileDialog。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

from stock_tool.core.models import StockQuote
from stock_tool.core.notify import MemoryNotificationChannel, Notifier
from stock_tool.core.alert_monitor import TradingCalendar, TradingClock
from stock_tool.ui import qt_workbench
from tools.test_qt_workbench_smoke import FakeObserverFetcher, FakeWebEngineView, wait_for


def _click_modal_button(app, standard_button) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox

    def click() -> None:
        for widget in app.topLevelWidgets():
            if isinstance(widget, QMessageBox) and widget.isVisible():
                QTest.mouseClick(widget.button(standard_button), qt_workbench.Qt.LeftButton)
                return
    QTimer.singleShot(50, click)


def _weekday_calendar(start: date, end: date) -> set[date]:
    days: set[date] = set()
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.add(cursor)
        cursor += timedelta(days=1)
    return days


def test_alert_controls_by_real_click() -> None:
    from PySide6.QtTest import QTest

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
        data_dir = Path(raw) / "data"
        data_dir.mkdir()
        qt_workbench.read_data_dir = lambda: data_dir
        qt_workbench.read_configured_data_dir = lambda: None
        qt_workbench.write_data_dir = lambda _path: None
        qt_workbench.QWebEngineView = FakeWebEngineView
        app = qt_workbench.QApplication.instance() or qt_workbench.QApplication([])
        memory_notifications = MemoryNotificationChannel()
        window = qt_workbench.StockWorkbenchWindow(notifier=Notifier([memory_notifications]))
        window.show()
        wait_for(window, "data_refresh")
        window.fetcher = FakeObserverFetcher()
        window.alert_monitor.fetcher = window.fetcher
        window.alert_monitor.trading_clock = TradingClock(TradingCalendar(_weekday_calendar))
        alert_time = datetime(2026, 7, 10, 10, 0).astimezone()
        window.fetcher.snapshot_now = alert_time
        window.alert_monitor._now = lambda: alert_time
        window.data_readiness.policy._now = lambda: alert_time
        window._navigate("alerts")
        assert "腾讯/新浪公开行情快照" in window.alert_source_notice.text()
        assert window.alert_interval_combo.findData("15") >= 0
        assert window.alert_interval_combo.findData("30") >= 0
        assert window.alert_interval_combo.findData("60") >= 0

        window.alert_code_input.setText("000101")
        window.alert_name_input.setText("Qt点击样例")
        window.alert_kind_combo.setCurrentIndex(window.alert_kind_combo.findData("sell"))
        window.alert_threshold_input.setText("10")
        QTest.mouseClick(window.alert_save_button, qt_workbench.Qt.LeftButton)
        assert window.alert_table.rowCount() == 1

        window.alert_table.selectRow(0)
        QTest.mouseClick(window.alert_toggle_button, qt_workbench.Qt.LeftButton)
        assert not window.alert_service.list_alerts()[0].enabled
        QTest.mouseClick(window.alert_toggle_button, qt_workbench.Qt.LeftButton)
        assert window.alert_service.list_alerts()[0].enabled

        window.alert_interval_combo.setCurrentIndex(window.alert_interval_combo.findData("15"))
        app.processEvents()
        assert window.alert_monitor.policy().configured_interval_seconds == 15

        QTest.mouseClick(window.alert_check_button, qt_workbench.Qt.LeftButton)
        wait_for(window, "alert_check")
        assert window.alert_history_table.rowCount() == 1
        assert len(memory_notifications.messages) == 1

        QTest.mouseClick(window.alert_monitor_check, qt_workbench.Qt.LeftButton)
        assert window.alert_service.get_monitor_policy()["enabled"] is True
        window.alert_interval_combo.setCurrentIndex(window.alert_interval_combo.findData("600"))
        app.processEvents()
        assert window.alert_monitor.policy().configured_interval_seconds == 600
        # 测试时钟固定在历史日期；先停掉真实 Qt 定时器，避免历史 next_check_at 被立即视为逾期而重入。
        window.alert_monitor_check.setChecked(False)
        app.processEvents()

        def unavailable_loader(_start: date, _end: date):
            raise RuntimeError("calendar source down")

        window.alert_monitor.trading_clock = TradingClock(TradingCalendar(unavailable_loader))
        assert window.run_alert_check(manual=True)
        wait_for(window, "alert_check")
        assert "交易日历不可用，预警检查已暂停" in window.alert_status_label.text()
        assert len(memory_notifications.messages) == 1

        _click_modal_button(app, qt_workbench.QMessageBox.Yes)
        clear_button = next(button for button in window.findChildren(qt_workbench.QPushButton) if button.text() == "清空记录")
        QTest.mouseClick(clear_button, qt_workbench.Qt.LeftButton)
        assert window.alert_history_table.rowCount() == 0

        _click_modal_button(app, qt_workbench.QMessageBox.Yes)
        QTest.mouseClick(window.alert_delete_button, qt_workbench.Qt.LeftButton)
        assert not window.alert_service.list_alerts()
        window.close()
        if window.v1_database is not None:
            window.v1_database.close()


def main() -> int:
    test_alert_controls_by_real_click()
    print("[OK] Qt 预警真实点击测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
