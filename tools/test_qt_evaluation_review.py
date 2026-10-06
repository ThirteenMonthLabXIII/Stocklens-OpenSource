from __future__ import annotations

"""阶段7 Qt 系统评价页的真实数据展示验收。"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

from stock_tool.core.performance_loop import ObservationOutcome, PerformanceStore
from stock_tool.core.notify import MemoryNotificationChannel, Notifier
from stock_tool.ui import qt_workbench
from tools.test_performance_loop import _insert_run, _prepare_formal_certificate
from tools.test_phase7_evaluation import _add_lens
from tools.test_qt_workbench_smoke import FakeWebEngineView, wait_for


def test_qt_evaluation_review_by_real_fixture() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
        data_dir = Path(raw) / "data"
        data_dir.mkdir()
        qt_workbench.read_data_dir = lambda: data_dir
        qt_workbench.read_configured_data_dir = lambda: None
        qt_workbench.write_data_dir = lambda _path: None
        qt_workbench.QWebEngineView = FakeWebEngineView
        app = qt_workbench.QApplication.instance() or qt_workbench.QApplication([])
        window = qt_workbench.StockWorkbenchWindow(notifier=Notifier([MemoryNotificationChannel()]))
        window.show()
        wait_for(window, "data_refresh")
        # This test is fully offline and must not inherit a machine credential.
        window.api_key_input.clear()
        assert window.v1_database is not None
        assert window.performance_store is not None

        run_id, result_id = _insert_run(window.v1_database, created_at="2026-01-02T15:00:00")
        _add_lens(window.v1_database, result_id)
        certificate_id = _prepare_formal_certificate(window.v1_database, run_id)
        performance = PerformanceStore(window.v1_database)
        assert performance.create_formal_observation_samples(run_id, certificate_id, horizons=(3,)) == 1
        sample = performance.list_recent_outcomes(limit=10, evaluation_mode="formal")[0]
        performance.record_outcome(
            ObservationOutcome(
                sample_id=int(sample["sample_id"]),
                settled_at="2026-01-10T09:00:00+00:00",
                exit_date="2026-01-07",
                exit_price=10.8,
                return_pct=8.0,
                hs300_return_pct=2.0,
                excess_vs_hs300_pct=6.0,
                industry_return_pct=1.0,
                excess_vs_industry_pct=7.0,
                max_drawdown_pct=-2.0,
                rating="A",
            )
        )

        window._navigate("review")
        window._refresh_evaluation_review()
        assert window.evaluation_mode_combo.currentData() == "formal"
        headers = [window.evaluation_table.horizontalHeaderItem(index).text() for index in range(window.evaluation_table.columnCount())]
        assert "平均收益" in headers
        assert window.evaluation_table.rowCount() == 1
        assert window.evaluation_table.item(0, headers.index("平均收益")).text() == "8.000%"
        hint = window.evaluation_hint.text()
        assert "AI" in hint and "置信度" in hint
        assert "不读取" in hint or "不会用" in hint

        window.close()
        if window.v1_database is not None:
            window.v1_database.close()
        app.processEvents()


if __name__ == "__main__":
    test_qt_evaluation_review_by_real_fixture()
    print("[OK] Qt 阶段7系统评价真实 fixture 测试通过")
