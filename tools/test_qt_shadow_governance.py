from __future__ import annotations

"""CandidateLens 生产入口的 Qt 合同回归。历史公式不再有产品入口。"""

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
from tools.test_qt_workbench_smoke import FakeWebEngineView, wait_for


def _button(window, action_id):
    return next(item for item in window.findChildren(qt_workbench.QPushButton) if item.property("action_id") == action_id)


def test_formula_product_surface_is_removed() -> None:
    from PySide6.QtTest import QTest
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
        data_dir = Path(raw) / "data"; data_dir.mkdir()
        qt_workbench.read_data_dir = lambda: data_dir; qt_workbench.read_configured_data_dir = lambda: None; qt_workbench.write_data_dir = lambda _p: None
        qt_workbench.QWebEngineView = FakeWebEngineView
        app = qt_workbench.QApplication.instance() or qt_workbench.QApplication([])
        window = qt_workbench.StockWorkbenchWindow(notifier=Notifier([MemoryNotificationChannel()])); window.show(); wait_for(window, "data_refresh")
        window._navigate("review")
        assert not any(
            button.property("action_id",) in {
                "review.formula.generate", "review.formula.continue", "review.formula.activate", "review.formula.rollback"
            }
            for button in window.findChildren(qt_workbench.QPushButton)
        )
        assert not hasattr(window, "formula_candidates_table")
        assert hasattr(window, "exploratory_proposals_table")
        window.close(); window.v1_database.close(); app.processEvents()


def test_exploratory_formalization_binds_the_proposed_lens() -> None:
    source = (ROOT / "stock_tool" / "ui" / "qt_workbench.py").read_text(encoding="utf-8")
    assert "lens_ids=(record.proposal.proposed_lens_id,)" in source
    assert "正式复核结果未绑定原始快照、候选摘要或提议 Lens。" in source


if __name__ == "__main__":
    test_formula_product_surface_is_removed()
    test_exploratory_formalization_binds_the_proposed_lens()
    print("[OK] Qt 公式产品入口移除与探索待审入口检查通过")
