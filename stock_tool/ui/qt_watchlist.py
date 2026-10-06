from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stock_tool.core.models import fmt_number
from stock_tool.ui.qt_widgets import Card, apply_change_color, build_table, status_cell


class WatchlistPage(QWidget):
    refreshRequested = Signal()
    openStock = Signal(dict)
    generateObservation = Signal(dict)
    removeRequested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        header = QHBoxLayout()
        title_wrap = QVBoxLayout()
        title = QLabel("自选股")
        title.setObjectName("pageTitle")
        subtitle = QLabel("集中查看关注股票及其观察、预警状态；双击即可继续研究。")
        subtitle.setObjectName("pageSubtitle")
        title_wrap.addWidget(title)
        title_wrap.addWidget(subtitle)
        header.addLayout(title_wrap, 1)

        self.refresh_button = QPushButton("刷新行情")
        self.refresh_button.setProperty("action_id", "watchlist.refresh")
        self.refresh_button.setProperty("action_kind", "execute")
        self.refresh_button.clicked.connect(self.refreshRequested.emit)
        self.open_button = QPushButton("查看")
        self.open_button.setObjectName("primaryBtn")
        self.open_button.setProperty("action_id", "watchlist.open")
        self.open_button.setProperty("action_kind", "navigate")
        self.open_button.clicked.connect(self._open_selected)
        self.observe_button = QPushButton("生成观察记录")
        self.observe_button.setProperty("action_id", "watchlist.observe")
        self.observe_button.setProperty("action_kind", "execute")
        self.observe_button.clicked.connect(self._generate_selected)
        self.remove_button = QPushButton("移出选中")
        self.remove_button.setProperty("action_id", "watchlist.remove")
        self.remove_button.setProperty("action_kind", "execute")
        self.remove_button.clicked.connect(self._remove_selected)
        for button in (self.refresh_button, self.open_button, self.observe_button, self.remove_button):
            header.addWidget(button)
        root.addLayout(header)

        self.card = Card("自选列表", "状态标签会合并观察任务、价格提醒和行情缺口。")
        self.table = build_table(["代码", "名称", "最新价", "涨跌幅", "状态", "更新时间"])
        self.table.cellDoubleClicked.connect(lambda row, column: self._open_row(row))
        self.card.layout.addWidget(self.table)
        root.addWidget(self.card, 1)
        self.apply_rows([])

    def apply_rows(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)
        if not self._rows:
            self.table.setRowCount(1)
            self.table.setItem(0, 0, QTableWidgetItem("还没有自选股"))
            self.table.setItem(0, 1, QTableWidgetItem("去个股研究搜索并加入"))
            for col in range(2, self.table.columnCount()):
                self.table.setItem(0, col, QTableWidgetItem("-"))
            self._set_actions_enabled(False)
            return

        self.table.setRowCount(len(self._rows))
        for row_index, row in enumerate(self._rows):
            values = (
                row.get("code", "-"),
                row.get("name", "-"),
                fmt_number(row.get("price"), "元"),
                fmt_number(row.get("change_pct"), "%"),
                "",
                row.get("updated_at", "-"),
            )
            for col, value in enumerate(values):
                if col == 4:
                    self.table.setCellWidget(row_index, col, status_cell(row.get("tags")))
                    continue
                item = QTableWidgetItem(str(value))
                if col == 0:
                    item.setData(Qt.UserRole, row)
                if col == 3:
                    apply_change_color(item, row.get("change_pct"))
                self.table.setItem(row_index, col, item)
        self.table.selectRow(0)
        self._set_actions_enabled(True)

    def _set_actions_enabled(self, enabled: bool) -> None:
        for button in (self.open_button, self.observe_button, self.remove_button):
            button.setEnabled(enabled)

    def _selected_row(self) -> dict[str, Any] | None:
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            return None
        row = selected[0].row()
        item = self.table.item(row, 0)
        data = item.data(Qt.UserRole) if item is not None else None
        return data if isinstance(data, dict) else None

    def _open_row(self, row: int) -> None:
        item = self.table.item(row, 0)
        data = item.data(Qt.UserRole) if item is not None else None
        if isinstance(data, dict):
            self.openStock.emit(data)

    def _open_selected(self) -> None:
        row = self._selected_row()
        if row:
            self.openStock.emit(row)

    def _generate_selected(self) -> None:
        row = self._selected_row()
        if row:
            self.generateObservation.emit(row)

    def _remove_selected(self) -> None:
        row = self._selected_row()
        if row:
            self.removeRequested.emit(str(row.get("code") or ""))
