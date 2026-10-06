from __future__ import annotations

"""P3 战绩统计的真实点击、过滤与 UTF-8 BOM 导出。"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

from stock_tool.ui import qt_workbench
from stock_tool.core.notify import MemoryNotificationChannel, Notifier
from tools.test_performance_loop import _insert_run
from tools.test_qt_workbench_smoke import FakeWebEngineView, wait_for


def _button(window, action_id):
    return next(item for item in window.findChildren(qt_workbench.QPushButton) if item.property("action_id") == action_id)


def test_performance_filters_and_export_by_real_click() -> None:
    from PySide6.QtTest import QTest
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
        data_dir = Path(raw) / "data"; data_dir.mkdir()
        qt_workbench.read_data_dir = lambda: data_dir; qt_workbench.read_configured_data_dir = lambda: None; qt_workbench.write_data_dir = lambda _p: None
        qt_workbench.QWebEngineView = FakeWebEngineView
        app = qt_workbench.QApplication.instance() or qt_workbench.QApplication([])
        window = qt_workbench.StockWorkbenchWindow(notifier=Notifier([MemoryNotificationChannel()])); window.show(); wait_for(window, "data_refresh")
        assert window.v1_database and window.performance_store
        run_id, _ = _insert_run(window.v1_database, code="000101", name="P3样例", industry="测试行业")
        assert window.performance_store.create_observation_samples_for_run(run_id, horizons=(3,)) == 1
        window._navigate("review")
        window.performance_code_input.setText("000101")
        window.performance_industry_input.setText("测试行业")
        window.performance_start_input.setText("2026-01-01")
        window.performance_end_input.setText("2099-01-01")
        window.performance_strategy_id_input.setText("1")
        window.performance_strategy_name_input.setText("测试")
        window.performance_strategy_version_input.setText("v1")
        window.performance_horizon_combo.setCurrentIndex(1)
        window.performance_regime_combo.setCurrentIndex(4)
        window.performance_template_input.setText("default")
        window.performance_weight_input.setText("v1")
        window.performance_quality_combo.setCurrentIndex(0)
        window.performance_candidate_combo.setCurrentIndex(0)
        args = window._performance_filter_args(limit=100)
        assert args["code"] == "000101" and args["industry"] == "测试行业" and args["strategy_id"] == 1
        window.performance_strategy_id_input.clear(); window.performance_strategy_name_input.clear(); window.performance_strategy_version_input.clear()
        window.performance_start_input.clear(); window.performance_end_input.clear(); window.performance_template_input.clear(); window.performance_weight_input.clear()
        window.performance_horizon_combo.setCurrentIndex(0); window.performance_regime_combo.setCurrentIndex(0)
        QTest.mouseClick(_button(window, "review.performance.refresh"), qt_workbench.Qt.LeftButton)
        assert window.performance_table.rowCount() == 1
        QTest.mouseDClick(_button(window, "review.performance.refresh"), qt_workbench.Qt.LeftButton)
        assert window.performance_table.rowCount() == 1
        export_path = data_dir / "performance.csv"
        window._choose_csv_path = lambda *_args: str(export_path)
        QTest.mouseClick(_button(window, "review.performance.export"), qt_workbench.Qt.LeftButton)
        assert export_path.read_bytes().startswith(b"\xef\xbb\xbf")
        window.close(); window.v1_database.close(); app.processEvents()


def test_late_background_callback_is_discarded_after_close() -> None:
    from PySide6.QtTest import QTest
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
        data_dir = Path(raw) / "data"; data_dir.mkdir()
        qt_workbench.read_data_dir = lambda: data_dir; qt_workbench.read_configured_data_dir = lambda: None; qt_workbench.write_data_dir = lambda _p: None
        qt_workbench.QWebEngineView = FakeWebEngineView
        app = qt_workbench.QApplication.instance() or qt_workbench.QApplication([])
        window = qt_workbench.StockWorkbenchWindow(notifier=Notifier([MemoryNotificationChannel()])); window.show(); wait_for(window, "data_refresh")
        started, release, completed = threading.Event(), threading.Event(), []
        def task():
            started.set(); release.wait(3); return "late"
        assert window._run_background("P3 late", task, lambda value: completed.append(value), task_key="p3_late")
        assert started.wait(2)
        window.close(); release.set()
        deadline = time.time() + 2
        while time.time() < deadline:
            app.processEvents(); time.sleep(0.02)
        assert not completed
        if window.v1_database is not None: window.v1_database.close()


if __name__ == "__main__":
    test_performance_filters_and_export_by_real_click()
    test_late_background_callback_is_discarded_after_close()
    print("[OK] Qt 战绩统计真实点击测试通过")
