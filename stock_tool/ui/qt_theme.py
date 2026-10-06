from __future__ import annotations

"""深色专业终端主题：沉稳冷色调，低饱和，让人冷静。

只服务 PySide6 Qt 工作台，与已下线的旧前端无关。红涨绿跌为价格语义专用色，
绝不挪作交互/状态用色，交互色锁定冷蓝。

设计令牌集中在 PALETTE，QSS 由 build_workbench_qss() 生成，便于离线测试与统一改色。
中间 K 线 HTML 的配色见 web/stock_workbench.html，与此处保持同一套色板。
"""


# 设计令牌：深石板蓝底 + 柔白文字 + 冷钢蓝交互色。
PALETTE: dict[str, str] = {
    # 基础层：从窗体底到卡片、输入、选中，逐层提亮，制造无阴影的层次感。
    "bg": "#0e1620",
    "header": "#0b131c",
    "surface": "#16212e",
    "surface_alt": "#1b2734",
    "surface_hi": "#223040",
    "border": "#26323f",
    "border_strong": "#34424f",
    # 文字墨阶：不用纯白，柔一档更沉稳、久看不刺眼。
    "text": "#dce4ee",
    "text_soft": "#c8d2df",
    "muted": "#a2afbf",
    "faint": "#8c98aa",
    # 交互色：冷钢蓝，低饱和，理性、克制，不与红绿冲突。
    "accent": "#2f6fbd",
    "accent_hover": "#3a7ed1",
    "accent_press": "#255a9d",
    "accent_soft": "#16304d",
    "accent_disabled": "#2a3a4e",
    "kpi_blue": "#4e8edd",
    "kpi_blue_soft": "#16304d",
    "kpi_purple": "#8b86c9",
    "kpi_purple_soft": "#221f3a",
    "kpi_amber": "#cba65c",
    "kpi_amber_soft": "#352918",
    "kpi_teal": "#3f9e8f",
    "kpi_teal_soft": "#123432",
    "info_soft": "#13283f",
    "opportunity": "#4fc7b5",
    "opportunity_soft": "#123432",
    "risk": "#d6a14a",
    "risk_soft": "#352918",
    "gap_soft": "#202834",
    # 价格语义（锁定，不可重定义）：在深底上略提亮以保可读，仍保持沉稳。
    "up": "#f06a60",
    "down": "#2fb37e",
    # 状态：就绪/警示。
    "ok": "#36c08a",
    "warn": "#d6a14a",
    "watching": "#6ea8ff",
    "watching_soft": "#142b47",
    "holding": "#c0cad7",
    "holding_soft": "#202834",
    "review": "#d6a14a",
    "review_soft": "#352918",
    "triggered": "#f06a60",
    "triggered_soft": "#3a1f22",
}

FONT_FAMILY = '"Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun", "Segoe UI", sans-serif'
MONO_FONT = '"JetBrains Mono", Consolas, "Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun", "DejaVu Sans Mono", monospace'


def build_workbench_qss(palette: dict[str, str] | None = None) -> str:
    """根据令牌生成工作台全量 QSS。纯字符串，可在无显示环境下断言测试。"""
    c = {**PALETTE, **(palette or {})}
    return f"""
    QMainWindow, QWidget {{
        background: {c['bg']};
        color: {c['text']};
        font-family: {FONT_FAMILY};
        font-size: 13px;
    }}

    QLabel#title {{ font-size: 20px; font-weight: 600; color: {c['text']}; letter-spacing: 0.3px; }}
    QLabel#subtitle {{ color: {c['muted']}; font-size: 12px; }}
    QLabel#footer {{ color: {c['faint']}; font-size: 11px; padding: 2px 2px; }}
    QLabel#sectionLabel {{ font-weight: 600; color: {c['text_soft']}; }}
    QLabel#stockTitle {{ font-weight: 600; font-size: 15px; color: {c['text']}; }}
    QLabel#pageTitle {{ font-size: 19px; font-weight: 600; color: {c['text']}; }}
    QLabel#pageSubtitle {{ color: {c['muted']}; font-size: 12px; }}
    QLabel#searchHint {{ color: {c['muted']}; font-size: 12px; padding-left: 2px; }}
    QLabel#emptyState {{ color: {c['text_soft']}; line-height: 155%; }}
    QLabel#experimentBanner {{
        color: {c['text_soft']};
        background: {c['surface_alt']};
        border: 1px solid {c['border_strong']};
        border-radius: 8px;
        padding: 8px 11px;
    }}

    /* 顶栏状态药丸 */
    QLabel#statusPill, QPushButton#statusPill {{
        color: {c['text_soft']};
        background: {c['surface_alt']};
        border: 1px solid {c['border']};
        border-radius: 13px;
        padding: 5px 12px;
        font-family: {MONO_FONT};
    }}
    QPushButton#statusPill:hover {{
        color: {c['text']};
        background: {c['surface_hi']};
        border-color: {c['accent']};
    }}
    QPushButton#statusPill:pressed {{
        background: {c['surface']};
    }}
    QPushButton#statusPill:disabled {{
        color: {c['muted']};
        background: {c['surface']};
        border-color: {c['border']};
    }}

    /* 卡片面板 */
    QWidget#panel {{
        background: {c['surface']};
        border: 1px solid {c['border']};
        border-radius: 10px;
    }}

    QScrollArea#pageScroll {{
        border: 0;
        background: transparent;
    }}
    QScrollArea#pageScroll > QWidget > QWidget {{
        background: transparent;
    }}

    /* 左侧导航 */
    QWidget#sidebar {{
        background: {c['header']};
        border: 1px solid {c['border']};
        border-radius: 10px;
    }}
    QLabel#navGroup {{
        color: {c['faint']};
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 1.1px;
        padding: 10px 8px 2px 8px;
    }}
    QToolButton#navToggle {{
        background: {c['surface_alt']};
        color: {c['muted']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        padding: 7px 10px;
        text-align: left;
    }}
    QToolButton#navToggle:hover {{
        color: {c['text']};
        border-color: {c['border_strong']};
        background: {c['surface_hi']};
    }}
    QToolButton#navItem {{
        background: transparent;
        color: {c['text_soft']};
        border: 1px solid transparent;
        border-radius: 8px;
        padding: 8px 10px;
        text-align: left;
        font-weight: 500;
    }}
    QToolButton#navItem:hover {{
        background: {c['surface_alt']};
        border-color: {c['border']};
        color: {c['text']};
    }}
    QToolButton#navItem:checked {{
        background: {c['accent_soft']};
        border-color: {c['accent']};
        color: {c['text']};
    }}

    /* Dashboard cards */
    QWidget#card, QWidget#kpiCard {{
        background: {c['surface']};
        border: 1px solid {c['border']};
        border-radius: 8px;
    }}
    QLabel#cardTitle {{
        color: {c['text']};
        font-size: 14px;
        font-weight: 600;
    }}
    QLabel#cardSubtitle {{
        color: {c['muted']};
        font-size: 12px;
    }}
    QLabel#kpiIcon {{
        border-radius: 15px;
        border: 1px solid {c['border']};
        min-width: 30px;
        min-height: 30px;
        max-width: 30px;
        max-height: 30px;
    }}
    QLabel#kpiIcon[hue="blue"] {{
        background: {c['kpi_blue_soft']};
        border-color: {c['kpi_blue']};
    }}
    QLabel#kpiIcon[hue="purple"] {{
        background: {c['kpi_purple_soft']};
        border-color: {c['kpi_purple']};
    }}
    QLabel#kpiIcon[hue="amber"] {{
        background: {c['kpi_amber_soft']};
        border-color: {c['kpi_amber']};
    }}
    QLabel#kpiIcon[hue="teal"] {{
        background: {c['kpi_teal_soft']};
        border-color: {c['kpi_teal']};
    }}
    QLabel#kpiValue {{
        color: {c['text']};
        font-family: {MONO_FONT};
        font-size: 20px;
        font-weight: 700;
    }}
    QLabel#kpiCaption {{
        color: {c['muted']};
        font-size: 12px;
    }}

    /* 文本浏览区 / 列表 / 输入 / 下拉，统一卡片观感 */
    QTextBrowser, QListWidget, QLineEdit, QComboBox {{
        background: {c['surface_alt']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        color: {c['text']};
        selection-background-color: {c['accent_soft']};
        selection-color: {c['text']};
    }}
    QTextBrowser {{ padding: 10px; }}
    QTextBrowser#reportBox {{
        font-family: {MONO_FONT};
    }}

    /* 结构化情报面板：不用 QTextBrowser 拼 HTML，区块与标签都由真实控件承载。 */
    QWidget#intelligencePanel {{
        background: {c['surface_alt']};
        border: 1px solid {c['border']};
        border-radius: 8px;
    }}
    QFrame#intelSection {{
        background: transparent;
        border: 0;
        border-bottom: 1px solid {c['border']};
    }}
    QLabel#intelSectionTitle {{
        color: {c['muted']};
        font-size: 12px;
        font-weight: 600;
    }}
    QLabel#intelSummary {{
        color: {c['text']};
        font-size: 13px;
        line-height: 150%;
    }}
    QLabel#intelMetricLabel {{
        color: {c['muted']};
        font-size: 11px;
    }}
    QLabel#intelMetricValue {{
        color: {c['text']};
        font-family: {MONO_FONT};
        font-size: 13px;
        font-weight: 600;
    }}
    QLabel[tier="info"], QLabel[tier="opportunity"], QLabel[tier="risk"], QLabel[tier="gap"] {{
        border-radius: 11px;
        padding: 4px 9px;
        font-size: 12px;
        font-weight: 600;
    }}
    QLabel[tier="info"] {{
        background: {c['info_soft']};
        color: {c['accent_hover']};
        border: 1px solid {c['accent_soft']};
    }}
    QLabel[tier="opportunity"] {{
        background: {c['opportunity_soft']};
        color: {c['opportunity']};
        border: 1px solid #1f5f58;
    }}
    QLabel[tier="risk"] {{
        background: {c['risk_soft']};
        color: {c['risk']};
        border: 1px solid #684e24;
    }}
    QLabel[tier="gap"] {{
        background: {c['gap_soft']};
        color: {c['muted']};
        border: 1px solid {c['border_strong']};
    }}

    QListWidget {{ padding: 3px; outline: 0; }}
    QListWidget::item {{ padding: 8px 9px; border-radius: 6px; color: {c['text_soft']}; }}
    QListWidget::item:hover {{ background: {c['surface_hi']}; }}
    QListWidget::item:selected {{ background: {c['accent_soft']}; color: {c['text']}; }}

    QLineEdit, QComboBox {{ padding: 7px 9px; min-height: 26px; }}
    QLineEdit:focus, QComboBox:focus {{ border: 1px solid {c['accent']}; }}
    QLineEdit::placeholder {{ color: {c['faint']}; }}

    QComboBox::drop-down {{ border: 0; width: 22px; }}
    QComboBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {c['muted']};
        margin-right: 8px;
    }}
    QComboBox QAbstractItemView {{
        background: {c['surface']};
        border: 1px solid {c['border_strong']};
        border-radius: 8px;
        color: {c['text']};
        selection-background-color: {c['accent_soft']};
        selection-color: {c['text']};
        outline: 0;
        padding: 3px;
    }}

    /* 次要按钮（默认）：描边、底色克制 */
    QPushButton {{
        background: {c['surface_alt']};
        color: {c['text_soft']};
        border: 1px solid {c['border_strong']};
        border-radius: 8px;
        padding: 8px 14px;
    }}
    QPushButton:hover {{ background: {c['surface_hi']}; border-color: {c['accent']}; color: {c['text']}; }}
    QPushButton:pressed {{ background: {c['surface']}; }}
    QPushButton:disabled {{ color: {c['faint']}; background: {c['surface']}; border-color: {c['border']}; }}

    /* 主操作按钮：实心冷蓝 */
    QPushButton#primaryBtn {{
        background: {c['accent']};
        color: #ffffff;
        border: 0;
        padding: 8px 16px;
    }}
    QPushButton#primaryBtn:hover {{ background: {c['accent_hover']}; }}
    QPushButton#primaryBtn:pressed {{ background: {c['accent_press']}; }}
    QPushButton#primaryBtn:disabled {{ background: {c['accent_disabled']}; color: {c['muted']}; }}

    QPushButton#segmentedButton {{
        padding: 7px 10px;
        min-width: 48px;
    }}
    QPushButton#segmentedButton:checked {{
        background: {c['accent_soft']};
        color: {c['text']};
        border-color: {c['accent']};
    }}
    QFrame#quickTile {{
        background: {c['surface_alt']};
        border: 1px solid {c['border']};
        border-radius: 8px;
    }}
    QFrame#quickTile:hover {{
        border-color: {c['accent']};
        background: {c['surface_hi']};
    }}
    QFrame#quickTile:focus {{
        border-color: {c['accent_hover']};
        background: {c['surface_hi']};
    }}
    QLabel#quickTileTitle {{
        color: {c['text']};
        font-weight: 600;
    }}
    QLabel#quickTileCaption {{
        color: {c['muted']};
        font-size: 11px;
    }}

    QTableWidget {{
        background: {c['surface_alt']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        color: {c['text_soft']};
        outline: 0;
        selection-background-color: {c['accent_soft']};
        selection-color: {c['text']};
    }}
    QTableWidget::item {{
        border-bottom: 1px solid {c['border']};
        padding: 4px 6px;
    }}
    QTableWidget::item:selected {{
        background: {c['accent_soft']};
        color: {c['text']};
    }}
    QHeaderView::section {{
        background: {c['surface']};
        color: {c['muted']};
        border: 0;
        border-bottom: 1px solid {c['border_strong']};
        padding: 7px 6px;
        font-size: 12px;
        font-weight: 600;
    }}

    QLabel[status="ok"], QLabel[status="info"], QLabel[status="warn"],
    QLabel[status="watching"], QLabel[status="holding"], QLabel[status="review"],
    QLabel[status="triggered"], QLabel[status="gap"] {{
        border-radius: 10px;
        padding: 3px 8px;
        font-size: 12px;
        font-weight: 600;
    }}
    QLabel[status="ok"] {{
        background: {c['opportunity_soft']};
        color: {c['ok']};
        border: 1px solid #1f5f58;
    }}
    QLabel[status="info"], QLabel[status="watching"] {{
        background: {c['watching_soft']};
        color: {c['watching']};
        border: 1px solid {c['accent_soft']};
    }}
    QLabel[status="holding"] {{
        background: {c['holding_soft']};
        color: {c['holding']};
        border: 1px solid {c['border_strong']};
    }}
    QLabel[status="warn"], QLabel[status="review"] {{
        background: {c['review_soft']};
        color: {c['review']};
        border: 1px solid #684e24;
    }}
    QLabel[status="triggered"] {{
        background: {c['triggered_soft']};
        color: {c['triggered']};
        border: 1px solid #74323a;
    }}
    QLabel[status="gap"] {{
        background: {c['gap_soft']};
        color: {c['muted']};
        border: 1px solid {c['border_strong']};
    }}

    /* 细滚动条：消除 Qt 默认件的廉价感 */
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border_strong']}; border-radius: 5px; min-height: 28px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {c['border_strong']}; border-radius: 5px; min-width: 28px; }}
    QScrollBar::handle:horizontal:hover {{ background: {c['muted']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:horizontal {{ width: 10px; }}

    QToolButton#researchEdgeToggle {{
        background: {c['surface_hi']};
        color: {c['text_soft']};
        border: 1px solid {c['border_strong']};
        border-right: 0;
        border-top-left-radius: 8px;
        border-bottom-left-radius: 8px;
        padding: 0;
        font-size: 20px;
        font-weight: 600;
    }}
    QToolButton#researchEdgeToggle:hover {{
        background: {c['accent_soft']};
        border-color: {c['accent']};
        color: {c['text']};
    }}
    QToolButton#researchEdgeToggle:pressed {{
        background: {c['accent']};
        color: #ffffff;
    }}
    QToolButton#researchEdgeToggle:focus {{
        border-color: {c['accent']};
    }}

    QToolTip {{
        background: {c['surface_hi']};
        color: {c['text']};
        border: 1px solid {c['border_strong']};
        padding: 5px 8px;
    }}
    """.strip()
