from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stock_tool.core.models import fmt_number
from stock_tool.ui.qt_dashboard_data import classify_search_intent
from stock_tool.ui.qt_theme import PALETTE
from stock_tool.ui.qt_widgets import Card, KpiCard, QuickTile, apply_change_color, build_table, status_cell


class HomeDashboard(QWidget):
    searchSubmitted = Signal(str)
    quickAction = Signal(str)
    kpiAction = Signal(str)
    openStock = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self._forced_intent: str | None = None
        self._watch_rows: list[dict[str, Any]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        search_card = Card("今天要研究什么？", "输入股票代码/名称，或直接写筛选条件。")
        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索股票代码/名称，或直接输入筛选条件")
        self.search_input.textChanged.connect(self._refresh_search_hint)
        self.search_input.returnPressed.connect(self._submit_search)
        search_row.addWidget(self.search_input, 1)

        self.stock_mode_button = QPushButton("股票")
        self.stock_mode_button.setCheckable(True)
        self.stock_mode_button.setProperty("action_id", "home.mode.stock")
        self.stock_mode_button.setProperty("action_kind", "info")
        self.filter_mode_button = QPushButton("筛选")
        self.filter_mode_button.setCheckable(True)
        self.filter_mode_button.setProperty("action_id", "home.mode.filter")
        self.filter_mode_button.setProperty("action_kind", "info")
        for button in (self.stock_mode_button, self.filter_mode_button):
            button.setObjectName("segmentedButton")
            search_row.addWidget(button)
        self.intent_group = QButtonGroup(self)
        self.intent_group.setExclusive(True)
        self.intent_group.addButton(self.stock_mode_button)
        self.intent_group.addButton(self.filter_mode_button)
        self.stock_mode_button.clicked.connect(lambda: self._force_intent("stock"))
        self.filter_mode_button.clicked.connect(lambda: self._force_intent("filter"))

        go_button = QPushButton("跳转")
        go_button.setObjectName("primaryBtn")
        go_button.setProperty("action_id", "home.search.submit")
        go_button.setProperty("action_kind", "navigate")
        go_button.clicked.connect(self._submit_search)
        search_row.addWidget(go_button)
        search_card.layout.addLayout(search_row)
        self.search_hint = QLabel("")
        self.search_hint.setObjectName("searchHint")
        search_card.layout.addWidget(self.search_hint)
        root.addWidget(search_card)

        kpi_grid = QGridLayout()
        kpi_grid.setContentsMargins(0, 0, 0, 0)
        kpi_grid.setHorizontalSpacing(10)
        kpi_grid.setVerticalSpacing(10)
        self.kpi_pool = KpiCard(
            "我的观察池", "-", "观察中 0 / 持仓 0 / 待复盘 0", "eye", "purple",
            "home.kpi.watchlist", "打开自选股，继续处理观察对象。",
        )
        self.kpi_alert = KpiCard(
            "价格提醒", "-", "今日触发 0", "bell", "amber",
            "home.kpi.alerts", "打开价格预警，查看提醒和监控状态。",
        )
        self.kpi_review = KpiCard(
            "待复核任务", "-", "已完成 0", "cpu", "teal",
            "home.kpi.review", "打开观察与复盘，处理待复核任务。",
        )
        self.kpi_health = KpiCard(
            "数据健康度", "-", "等待刷新", "activity", "blue",
            "home.kpi.data", "打开数据与更新，查看缓存与数据源状态。",
        )
        for card, page_key in (
            (self.kpi_pool, "watchlist"),
            (self.kpi_alert, "alerts"),
            (self.kpi_review, "review"),
            (self.kpi_health, "data"),
        ):
            card.clicked.connect(lambda page_key=page_key: self.kpiAction.emit(page_key))
        for index, card in enumerate((self.kpi_pool, self.kpi_alert, self.kpi_review, self.kpi_health)):
            kpi_grid.addWidget(card, 0, index)
        root.addLayout(kpi_grid)

        quick_card = Card("三步开始")
        quick_row = QHBoxLayout()
        quick_row.setSpacing(8)
        for action, icon_name, title, caption in (
            ("research", "search", "搜索个股", "查找行情、K线和本地观察"),
            ("screener", "filter", "写选股条件", "解析条件并用本地快照选股"),
            ("watchlist", "star", "查看自选股", "打开自选清单并继续个股研究"),
        ):
            tile = QuickTile(action, icon_name, title, caption)
            tile.clicked.connect(self.quickAction.emit)
            quick_row.addWidget(tile, 1)
        quick_card.layout.addLayout(quick_row)
        root.addWidget(quick_card)

        body_grid = QGridLayout()
        body_grid.setContentsMargins(0, 0, 0, 0)
        body_grid.setHorizontalSpacing(10)
        body_grid.setVerticalSpacing(10)

        self.health_card = Card("今日数据状态", "从本地缓存时间推导；没有探针的类别会明确标注。")
        self.health_table = build_table(["类别", "状态", "更新时间"])
        self.health_card.layout.addWidget(self.health_table)
        body_grid.addWidget(self.health_card, 0, 0, 1, 1)

        self.watch_card = Card("自选股", "双击股票进入个股研究。")
        self.watch_table = build_table(["代码", "名称", "最新价", "涨跌幅", "状态", "更新时间"])
        self.watch_table.cellDoubleClicked.connect(self._open_watch_row)
        self.watch_card.layout.addWidget(self.watch_table)
        body_grid.addWidget(self.watch_card, 0, 1, 1, 2)

        self.alert_card = Card("最近提醒")
        self.alert_table = build_table(["时间", "股票", "提醒"])
        self.alert_card.layout.addWidget(self.alert_table)
        body_grid.addWidget(self.alert_card, 1, 0, 1, 2)

        self.market_card = Card("市场概览")
        self.market_table = build_table(["项目", "状态", "下一步"])
        self.market_card.layout.addWidget(self.market_table)
        body_grid.addWidget(self.market_card, 1, 2, 1, 1)
        body_grid.setColumnStretch(0, 1)
        body_grid.setColumnStretch(1, 1)
        body_grid.setColumnStretch(2, 1)
        root.addLayout(body_grid, 1)
        self._refresh_search_hint("")
        self.apply_stats({})

    def current_search_intent(self) -> str:
        if self._forced_intent:
            return self._forced_intent
        return classify_search_intent(self.search_input.text())

    def set_search_text(self, text: str) -> None:
        self.search_input.setText(text)
        self.search_input.setFocus(Qt.OtherFocusReason)

    def apply_stats(self, stats: dict[str, Any]) -> None:
        tracking = stats.get("tracking") or {}
        alerts = stats.get("alerts") or {}
        health_rows = stats.get("health_rows") or []
        watch_rows = stats.get("watch_rows") or []
        alert_rows = stats.get("alert_rows") or []
        self._watch_rows = list(watch_rows)

        self.kpi_pool.set_data(
            tracking.get("pool", 0),
            f"观察中 {tracking.get('watching', 0)} / 持仓 {tracking.get('holding', 0)} / 待复盘 {tracking.get('review', 0)}",
        )
        self.kpi_alert.set_data(alerts.get("monitoring", 0), f"今日触发 {alerts.get('triggered_today', 0)}")
        self.kpi_review.set_data(tracking.get("review", 0), f"已完成 {tracking.get('done', 0)}")
        stale_count = sum(1 for _name, status, _age in health_rows if status != "正常")
        if not health_rows:
            self.kpi_health.set_data("待刷新", "正在读取本地缓存")
        else:
            self.kpi_health.set_data("正常" if stale_count == 0 else f"{stale_count}项", "异常/未知类别需复核" if stale_count else "缓存状态可用")

        self._fill_health_table(health_rows)
        self._fill_watch_table(watch_rows)
        self._fill_alert_table(alert_rows)
        self._fill_market_table()

    def _force_intent(self, intent: str) -> None:
        self._forced_intent = intent
        self._refresh_search_hint(self.search_input.text())

    def _refresh_search_hint(self, text: str) -> None:
        if not text.strip():
            self._forced_intent = None
        intent = self.current_search_intent()
        self.stock_mode_button.setChecked(intent == "stock")
        self.filter_mode_button.setChecked(intent == "filter")
        destination = "个股研究" if intent == "stock" else "条件选股"
        suffix = "并加载" if intent == "stock" else "并带入条件"
        self.search_hint.setText(f"↵ 跳转到「{destination}」{suffix}")

    def _submit_search(self) -> None:
        text = self.search_input.text().strip()
        if text:
            self.searchSubmitted.emit(text)

    def _fill_health_table(self, rows: list[tuple[str, str, str]]) -> None:
        if not rows:
            rows = [("缓存", "暂不可用", "尚未产生缓存记录")]
        self.health_table.setRowCount(len(rows))
        for row_index, (category, status, age) in enumerate(rows):
            for col, value in enumerate((category, status, age)):
                item = _health_status_item(str(value)) if col == 1 else QTableWidgetItem(str(value))
                self.health_table.setItem(row_index, col, item)

    def _fill_watch_table(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            self.watch_table.setRowCount(1)
            self.watch_table.setItem(0, 0, QTableWidgetItem("还没有自选股"))
            self.watch_table.setItem(0, 1, QTableWidgetItem("去个股研究搜索并加入"))
            for col in range(2, self.watch_table.columnCount()):
                self.watch_table.setItem(0, col, QTableWidgetItem("-"))
            return
        self.watch_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
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
                    self.watch_table.setCellWidget(row_index, col, status_cell(row.get("tags")))
                    continue
                item = QTableWidgetItem(str(value))
                if col == 0:
                    item.setData(Qt.UserRole, row)
                if col == 3:
                    apply_change_color(item, row.get("change_pct"))
                self.watch_table.setItem(row_index, col, item)

    def _fill_alert_table(self, rows: list[dict[str, str]]) -> None:
        if not rows:
            rows = [{"time": "-", "stock": "暂无触发记录", "message": "有价格提醒触发后会显示在这里"}]
        rows = rows[:8]
        self.alert_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for col, value in enumerate((row.get("time", "-"), row.get("stock", "-"), row.get("message", "-"))):
                self.alert_table.setItem(row_index, col, QTableWidgetItem(str(value)))

    def _fill_market_table(self) -> None:
        rows = [
            ("主要指数", "数据源未接入", "Stage 2 增加指数行情"),
            ("沪深300走势", "暂不可用", "接入历史指数后点亮"),
        ]
        self.market_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for col, value in enumerate(row):
                self.market_table.setItem(row_index, col, QTableWidgetItem(value))

    def _open_watch_row(self, row: int, _column: int) -> None:
        item = self.watch_table.item(row, 0)
        data = item.data(Qt.UserRole) if item is not None else None
        if isinstance(data, dict):
            self.openStock.emit(data)


def _health_status_item(status: str) -> QTableWidgetItem:
    item = QTableWidgetItem(f"● {status}")
    if status == "正常":
        color = PALETTE["ok"]
    elif status in {"偏旧", "延迟"}:
        color = PALETTE["warn"]
    else:
        color = PALETTE["muted"]
    item.setForeground(QColor(color))
    return item
