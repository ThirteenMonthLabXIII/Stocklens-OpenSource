from __future__ import annotations

"""P2 的首页、研究和自选股真实点击验收。

筛选的策略、CSV/JSON 与重启一致性由 ``test_qt_screening_interactions`` 和
``test_frontend_consistency`` 负责；本文件刻意只覆盖此前未被真实 QTest 覆盖的
首页 KPI 路由、个股搜索/加入自选和自选列表操作。
"""

import os
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

from stock_tool.core.models import StockQuote
from stock_tool.core.notify import MemoryNotificationChannel, Notifier
from stock_tool.ui import qt_workbench
from tools.test_qt_workbench_smoke import FakeObserverFetcher, FakeWebEngineView, wait_for


class P2Fetcher(FakeObserverFetcher):
    def search(self, keyword: str) -> list[StockQuote]:
        if keyword == "不可用":
            raise RuntimeError("测试搜索不可用")
        return [StockQuote(code="000101", name="Qt样例", price=12.5, change_pct=1.2)]


def _button(window, action_id: str):
    return next(
        widget
        for widget in window.findChildren(qt_workbench.QPushButton)
        if widget.property("action_id") == action_id
    )


def _action_widget(window, action_id: str):
    return next(widget for widget in window.findChildren(qt_workbench.QWidget) if widget.property("action_id") == action_id)


def _wait_until(app, predicate, message: str, timeout: float = 6.0) -> None:
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    assert predicate(), message


def _new_window(data_dir: Path, *, wait_initial_refresh: bool = True, suppress_initial_refresh: bool = False):
    qt_workbench.read_data_dir = lambda: data_dir
    qt_workbench.read_configured_data_dir = lambda: None
    qt_workbench.write_data_dir = lambda _path: None
    qt_workbench.QWebEngineView = FakeWebEngineView
    app = qt_workbench.QApplication.instance() or qt_workbench.QApplication([])
    original_start_refresh = qt_workbench.StockWorkbenchWindow._start_data_refresh
    if suppress_initial_refresh:
        qt_workbench.StockWorkbenchWindow._start_data_refresh = lambda _self, _reason: False
    try:
        window = qt_workbench.StockWorkbenchWindow(notifier=Notifier([MemoryNotificationChannel()]))
    finally:
        qt_workbench.StockWorkbenchWindow._start_data_refresh = original_start_refresh
    window.show()
    window.fetcher = P2Fetcher()
    window.refresh_coordinator = qt_workbench.DataRefreshCoordinator(window.cache, window.fetcher)
    if wait_initial_refresh:
        wait_for(window, "data_refresh")
    return app, window


def _close(window, app) -> None:
    window.close()
    if window.v1_database is not None:
        window.v1_database.close()
    window.deleteLater()
    app.processEvents()


def test_p2_home_research_and_watchlist_by_real_click() -> None:
    from PySide6.QtTest import QTest

    messages: list[str] = []
    original_information = qt_workbench.QMessageBox.information
    qt_workbench.QMessageBox.information = lambda _parent, _title, message: messages.append(str(message))
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            data_dir = Path(raw) / "data"
            data_dir.mkdir()
            app, window = _new_window(data_dir)
            try:
                # KPI 必须展示缓存中的真实业务计数，且点击后进入相应的工作页面。
                window.cache.add_watch("000101", "Qt样例")
                created = window.review_service.create_tracking_task(
                    code="000101",
                    name="Qt样例",
                    entry_price=12.5,
                    fail_price=11.5,
                    target_price=14.0,
                    horizon_days=3,
                    due_date="2099-01-01",
                    note="P2 KPI fixture",
                    actor="test",
                )
                assert created.ok
                window._refresh_dashboard_snapshot()
                assert window.home_page.kpi_pool.value_label.text() == "1"
                for card, page_key in (
                    (window.home_page.kpi_pool, "watchlist"),
                    (window.home_page.kpi_alert, "alerts"),
                    (window.home_page.kpi_review, "review"),
                    (window.home_page.kpi_health, "data"),
                ):
                    window._navigate("home")
                    QTest.mouseClick(card, qt_workbench.Qt.LeftButton)
                    assert window.stack.currentWidget() is window.pages[page_key]

                # 首页筛选入口应把用户输入带入并完成本地解析，而非只改变导航。
                window._navigate("home")
                window.home_page.search_input.setText("ROE大于15，PE小于40")
                QTest.mouseClick(_button(window, "home.search.submit"), qt_workbench.Qt.LeftButton)
                assert window.stack.currentWidget() is window.pages["screener"]
                assert window.current_query_spec is not None

                # 搜索、加载详情与加入自选全部走真实控件；空输入给出可执行提示。
                QTest.mouseClick(_action_widget(window, "home.quick.research"), qt_workbench.Qt.LeftButton)
                assert window.stack.currentWidget() is window.pages["research"]
                QTest.mouseClick(_button(window, "research.search"), qt_workbench.Qt.LeftButton)
                assert messages and "请输入股票代码或名称" in messages[-1]
                window.search_input.setText("Qt样例")
                QTest.mouseClick(_button(window, "research.search"), qt_workbench.Qt.LeftButton)
                _wait_until(app, lambda: window.search_list.count() == 1, "搜索结果未显示")
                item = window.search_list.item(0)
                QTest.mouseClick(window.search_list.viewport(), qt_workbench.Qt.LeftButton, pos=window.search_list.visualItemRect(item).center())
                _wait_until(app, lambda: window.current_quote is not None and window.stock_title.text().startswith("Qt样例"), "详情未加载")
                QTest.mouseClick(_button(window, "research.add_watch"), qt_workbench.Qt.LeftButton)
                assert [row["code"] for row in window.cache.list_watch()] == ["000101"]

                # 自选页提供真实打开、生成观察和删除操作；删除后显示空状态。
                QTest.mouseClick(_action_widget(window, "home.quick.watchlist"), qt_workbench.Qt.LeftButton)
                assert window.watchlist_page.table.rowCount() == 1
                QTest.mouseClick(window.watchlist_page.open_button, qt_workbench.Qt.LeftButton)
                _wait_until(app, lambda: window.stack.currentWidget() is window.pages["research"], "自选打开未跳转到研究页")
                QTest.mouseClick(_action_widget(window, "home.quick.watchlist"), qt_workbench.Qt.LeftButton)
                QTest.mouseClick(window.watchlist_page.observe_button, qt_workbench.Qt.LeftButton)
                _wait_until(app, lambda: window.current_trade_reference is not None, "自选观察未生成")

                # 重开前先验证落盘数据；P2 的重启持久化不依赖进程内状态。
                _close(window, app)
                app, window = _new_window(data_dir, wait_initial_refresh=False, suppress_initial_refresh=True)
                assert [row["code"] for row in window.cache.list_watch()] == ["000101"]
                window._navigate("watchlist")
                assert window.watchlist_page.table.item(0, 0).text() == "000101"
                QTest.mouseClick(window.watchlist_page.remove_button, qt_workbench.Qt.LeftButton)
                assert not window.cache.list_watch()
                assert "还没有自选股" in window.watchlist_page.table.item(0, 0).text()
            finally:
                _close(window, app)
    finally:
        qt_workbench.QMessageBox.information = original_information


def main() -> int:
    test_p2_home_research_and_watchlist_by_real_click()
    print("[OK] Qt P2 首页、研究与自选真实点击验收通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
