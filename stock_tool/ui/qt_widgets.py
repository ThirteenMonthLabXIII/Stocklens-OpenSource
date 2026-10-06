from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QByteArray, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stock_tool.ui.qt_flow_layout import FlowLayout
from stock_tool.ui.qt_theme import PALETTE


def _icons_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for candidate in (
        base / "stock_tool" / "ui" / "web" / "icons",
        Path(__file__).resolve().parent / "web" / "icons",
    ):
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent / "web" / "icons"


_ICON_CACHE: dict[tuple[str, str, int, float], QPixmap] = {}


def themed_pixmap(name: str, color: str, size: int = 18) -> QPixmap:
    dpr = QApplication.primaryScreen().devicePixelRatio() if QApplication.instance() else 1.0
    key = (name, color, size, round(dpr, 2))
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    raw = (_icons_dir() / f"{name}.svg").read_text(encoding="utf-8").replace("#000000", color)
    renderer = QSvgRenderer(QByteArray(raw.encode("utf-8")))
    pixmap = QPixmap(int(size * dpr), int(size * dpr))
    pixmap.fill(Qt.transparent)
    pixmap.setDevicePixelRatio(dpr)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    _ICON_CACHE[key] = pixmap
    return pixmap


def themed_icon(name: str, color: str, size: int = 18) -> QIcon:
    return QIcon(themed_pixmap(name, color, size))


class Card(QWidget):
    def __init__(self, title: str = "", subtitle: str = "") -> None:
        super().__init__()
        self.setObjectName("card")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(14, 14, 14, 14)
        self.layout.setSpacing(10)
        if title:
            header = QVBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            header.setSpacing(3)
            title_label = QLabel(title)
            title_label.setObjectName("cardTitle")
            header.addWidget(title_label)
            if subtitle:
                subtitle_label = QLabel(subtitle)
                subtitle_label.setObjectName("cardSubtitle")
                subtitle_label.setWordWrap(True)
                header.addWidget(subtitle_label)
            self.layout.addLayout(header)


class KpiCard(QWidget):
    """A concise dashboard metric that can optionally lead to its work page."""

    clicked = Signal()

    def __init__(
        self,
        title: str,
        value: str = "-",
        caption: str = "-",
        icon_name: str = "",
        hue: str = "blue",
        action_id: str = "",
        action_description: str = "",
    ) -> None:
        super().__init__()
        self.setObjectName("kpiCard")
        self._actionable = bool(action_id)
        if self._actionable:
            self.setProperty("action_id", action_id)
            self.setProperty("action_kind", "navigate")
            self.setCursor(Qt.PointingHandCursor)
            self.setFocusPolicy(Qt.StrongFocus)
            self.setAccessibleName(title)
            self.setAccessibleDescription(action_description or f"打开与{title}相关的工作页面")
            self.setToolTip(action_description or "点击查看详情")
        layout = QGridLayout(self)
        layout.setContentsMargins(14, 13, 14, 13)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(4)

        hue = hue if hue in {"blue", "purple", "amber", "teal"} else "blue"
        self.icon_label = QLabel()
        self.icon_label.setObjectName("kpiIcon")
        self.icon_label.setProperty("hue", hue)
        self.icon_label.setAlignment(Qt.AlignCenter)
        if icon_name:
            self.icon_label.setPixmap(themed_pixmap(icon_name, PALETTE[f"kpi_{hue}"], 18))
        layout.addWidget(self.icon_label, 0, 0, 2, 1)

        title_label = QLabel(title)
        title_label.setObjectName("kpiCaption")
        layout.addWidget(title_label, 0, 1)

        self.value_label = QLabel(value)
        self.value_label.setObjectName("kpiValue")
        layout.addWidget(self.value_label, 1, 1)

        self.caption_label = QLabel(caption)
        self.caption_label.setObjectName("kpiCaption")
        self.caption_label.setWordWrap(True)
        layout.addWidget(self.caption_label, 2, 0, 1, 2)
        layout.setColumnStretch(1, 1)

    def set_data(self, value: str | int, caption: str) -> None:
        self.value_label.setText(str(value))
        self.caption_label.setText(caption)

    def mouseReleaseEvent(self, event) -> None:
        if self._actionable and event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if self._actionable and event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


def status_pill(text: str, status: str = "info") -> QLabel:
    label = QLabel(text)
    label.setProperty("status", status)
    label.setAlignment(Qt.AlignCenter)
    return label


class QuickTile(QFrame):
    clicked = Signal(str)

    def __init__(self, action: str, icon_name: str, title: str, caption: str) -> None:
        super().__init__()
        self.setObjectName("quickTile")
        self._action = action
        self.setProperty("action_id", f"home.quick.{action}")
        self.setProperty("action_kind", "execute" if action == "refresh" else "navigate")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(title)
        self.setAccessibleDescription(caption)
        self.setToolTip(f"{title}：{caption}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(6)

        icon = QLabel()
        icon.setPixmap(themed_pixmap(icon_name, PALETTE["accent"], 22))
        icon.setAlignment(Qt.AlignLeft)
        title_label = QLabel(title)
        title_label.setObjectName("quickTileTitle")
        caption_label = QLabel(caption)
        caption_label.setObjectName("quickTileCaption")
        caption_label.setWordWrap(True)
        for widget in (icon, title_label, caption_label):
            layout.addWidget(widget)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._action)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit(self._action)
            event.accept()
            return
        super().keyPressEvent(event)


_TAG_STATUS = {
    "观察中": "watching",
    "持仓观察": "holding",
    "待复盘": "review",
    "价格预警": "triggered",
    "数据缺失": "gap",
    "自选": "info",
}


def status_cell(tags: Iterable[str] | None) -> QWidget:
    widget = QWidget()
    layout = FlowLayout(spacing=4)
    layout.setContentsMargins(2, 2, 2, 2)
    for tag in tags or ["自选"]:
        text = str(tag)
        layout.addWidget(status_pill(text, _TAG_STATUS.get(text, "info")))
    widget.setLayout(layout)
    return widget


def apply_change_color(item: QTableWidgetItem, value) -> None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return
    if numeric > 0:
        item.setForeground(QColor(PALETTE["up"]))
    elif numeric < 0:
        item.setForeground(QColor(PALETTE["down"]))


def build_table(headers: Iterable[str]) -> QTableWidget:
    header_labels = list(headers)
    table = QTableWidget(0, len(header_labels))
    table.setHorizontalHeaderLabels(header_labels)
    table.setAlternatingRowColors(False)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.setSortingEnabled(False)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(34)
    header = table.horizontalHeader()
    header.setHighlightSections(False)
    header.setSectionResizeMode(QHeaderView.Stretch)
    header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return table


def clear_layout(layout: QHBoxLayout | QVBoxLayout | QGridLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            widget.deleteLater()
        elif child_layout is not None:
            clear_layout(child_layout)
