from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from stock_tool.ui.qt_theme import PALETTE
from stock_tool.ui.qt_widgets import themed_icon


NAVIGATION_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("工作台", (("home", "home", "今日概览"),)),
    (
        "研究",
        (
            ("research", "search", "个股研究"),
            ("screener", "filter", "条件选股"),
            ("watchlist", "star", "自选股"),
        ),
    ),
    (
        "跟踪",
        (
            ("alerts", "bell", "价格预警"),
            ("review", "cpu", "观察与复盘"),
        ),
    ),
    (
        "管理",
        (
            ("data", "database", "数据与更新"),
            ("settings", "settings", "设置"),
        ),
    ),
)
NAVIGATION_LABELS = {
    page_key: label
    for _group, items in NAVIGATION_GROUPS
    for page_key, _icon_name, label in items
}


class Sidebar(QWidget):
    navigated = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("sidebar")
        self._collapsed = False
        self._buttons: dict[str, QToolButton] = {}
        self._group_labels: list[QLabel] = []

        self.setFixedWidth(204)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.buttonClicked.connect(self._emit_navigation)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        for group_index, (group_name, items) in enumerate(NAVIGATION_GROUPS):
            if group_index:
                layout.addSpacing(2)
            self._add_group(layout, group_name)
            for page_key, icon_name, label in items:
                layout.addWidget(self._make_nav(page_key, icon_name, label))
        layout.addStretch(1)

        # 折叠开关钉在最底部，与设计图一致
        layout.addSpacing(6)
        self._toggle_button = QToolButton()
        self._toggle_button.setObjectName("navToggle")
        self._toggle_button.setText("折叠")
        self._toggle_button.setProperty("action_id", "nav.toggle")
        self._toggle_button.setProperty("action_kind", "info")
        self._toggle_button.setIcon(themed_icon("chevron-left", PALETTE["muted"], 16))
        self._toggle_button.setIconSize(QSize(16, 16))
        self._toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._toggle_button.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        layout.addWidget(self._toggle_button)

        self.set_current("home")

    def set_current(self, page_key: str) -> None:
        button = self._buttons.get(page_key)
        if button is not None:
            button.setChecked(True)

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = bool(collapsed)
        self.setFixedWidth(62 if self._collapsed else 204)
        self._toggle_button.setText("" if self._collapsed else "折叠")
        self._toggle_button.setToolButtonStyle(Qt.ToolButtonIconOnly if self._collapsed else Qt.ToolButtonTextBesideIcon)
        for label in self._group_labels:
            label.setVisible(not self._collapsed)
        for button in self._buttons.values():
            button.setToolButtonStyle(Qt.ToolButtonIconOnly if self._collapsed else Qt.ToolButtonTextBesideIcon)

    def _add_group(self, layout: QVBoxLayout, title: str) -> None:
        label = QLabel(title)
        label.setObjectName("navGroup")
        self._group_labels.append(label)
        layout.addWidget(label)

    def _make_nav(self, page_key: str, icon_name: str, text: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("navItem")
        button.setText(text)
        button.setIcon(themed_icon(icon_name, PALETTE["muted"], 18))
        button.setIconSize(QSize(18, 18))
        button.setToolTip(text)
        button.setCheckable(True)
        button.setAutoRaise(False)
        button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button.setMinimumHeight(38)
        button.setProperty("pageKey", page_key)
        button.setProperty("action_id", f"nav.{page_key}")
        button.setProperty("action_kind", "navigate")
        self._buttons[page_key] = button
        self._group.addButton(button)
        return button

    def _emit_navigation(self, button: QToolButton) -> None:
        page_key = button.property("pageKey")
        if page_key:
            self.navigated.emit(str(page_key))
