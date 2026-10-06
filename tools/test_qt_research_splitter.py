from __future__ import annotations

"""Regression test for the research page's right-hand splitter panel."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.ui.qt_workbench import (
    QApplication,
    RESEARCH_RIGHT_PANEL_MIN_WIDTH,
    ResearchEdgeToggle,
    QSplitter,
    Qt,
    QWidget,
    _configure_research_splitter,
)
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest


def test_right_panel_cannot_be_collapsed_by_dragging() -> None:
    app = QApplication.instance() or QApplication([])
    splitter = QSplitter(Qt.Orientation.Horizontal)
    left = QWidget()
    center = QWidget()
    right = QWidget()
    for panel in (left, center, right):
        splitter.addWidget(panel)

    splitter.resize(1000, 240)
    splitter.show()
    _configure_research_splitter(splitter, right)
    app.processEvents()

    splitter.moveSplitter(splitter.width(), 2)
    app.processEvents()

    assert not splitter.isCollapsible(splitter.indexOf(right))
    assert splitter.sizes()[2] >= RESEARCH_RIGHT_PANEL_MIN_WIDTH

    splitter.moveSplitter(splitter.width() - RESEARCH_RIGHT_PANEL_MIN_WIDTH - 20, 2)
    app.processEvents()
    assert splitter.sizes()[2] >= RESEARCH_RIGHT_PANEL_MIN_WIDTH


def test_edge_toggle_reveals_and_restores_panel() -> None:
    app = QApplication.instance() or QApplication([])
    page = QWidget()
    page.resize(1000, 240)
    splitter = QSplitter(Qt.Orientation.Horizontal, page)
    splitter.setGeometry(page.rect())
    left = QWidget()
    center = QWidget()
    right = QWidget()
    for panel in (left, center, right):
        splitter.addWidget(panel)

    _configure_research_splitter(splitter, right)
    edge_toggle = ResearchEdgeToggle(page, splitter, right)
    page.show()
    app.processEvents()

    QTest.mouseMove(page, QPoint(page.width() - 2, page.height() // 2))
    app.processEvents()
    assert edge_toggle.button.isVisible()

    edge_toggle.toggle()
    app.processEvents()
    assert not right.isVisible()
    assert splitter.sizes()[2] == 0

    edge_toggle.toggle()
    app.processEvents()
    assert right.isVisible()
    assert splitter.sizes()[2] >= RESEARCH_RIGHT_PANEL_MIN_WIDTH


if __name__ == "__main__":
    test_right_panel_cannot_be_collapsed_by_dragging()
    test_edge_toggle_reveals_and_restores_panel()
    print("[OK] 研究页右侧情报栏支持边缘隐藏、悬浮唤出和宽度恢复")
