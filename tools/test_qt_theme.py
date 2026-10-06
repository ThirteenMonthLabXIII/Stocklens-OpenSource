from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.ui.qt_theme import MONO_FONT, PALETTE, build_workbench_qss


def _relative_luminance(color: str) -> float:
    raw = color.lstrip("#")
    values = [int(raw[index:index + 2], 16) / 255 for index in (0, 2, 4)]

    def channel(value: float) -> float:
        if value <= 0.03928:
            return value / 12.92
        return ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = [channel(value) for value in values]
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(foreground: str, background: str) -> float:
    fg = _relative_luminance(foreground)
    bg = _relative_luminance(background)
    lighter = max(fg, bg)
    darker = min(fg, bg)
    return (lighter + 0.05) / (darker + 0.05)


def run() -> None:
    # 令牌完整性：构建 QSS 与窗体引用所依赖的关键键不能缺失。
    required = {
        "bg", "surface", "surface_alt", "surface_hi", "border", "border_strong",
        "text", "text_soft", "muted", "faint",
        "accent", "accent_hover", "accent_press", "accent_soft", "accent_disabled",
        "kpi_blue", "kpi_blue_soft", "kpi_purple", "kpi_purple_soft",
        "kpi_amber", "kpi_amber_soft", "kpi_teal", "kpi_teal_soft",
        "info_soft", "opportunity", "opportunity_soft", "risk", "risk_soft", "gap_soft",
        "up", "down", "ok", "warn",
        "watching", "watching_soft", "holding", "holding_soft",
        "review", "review_soft", "triggered", "triggered_soft",
    }
    missing = required - set(PALETTE)
    assert not missing, f"PALETTE 缺少令牌：{missing}"

    # 价格语义锁定：红涨绿跌不可与交互冷蓝撞色。
    assert PALETTE["accent"] not in {PALETTE["up"], PALETTE["down"]}, "交互色不得与价格色相同"

    qss = build_workbench_qss()
    assert isinstance(qss, str) and qss.strip(), "QSS 不能为空"
    assert "JetBrains Mono" in MONO_FONT, "MONO_FONT 应优先使用 JetBrains Mono"
    assert "JetBrains Mono" in qss, "QSS 未包含 JetBrains Mono 字体"

    # 深色基底真的被写入，避免回退成浅色。
    assert PALETTE["bg"] in qss, "QSS 未包含窗体底色令牌"
    assert PALETTE["bg"].lower().startswith("#0"), "底色应为深色"

    # 窗体里实际用到的选择器必须出现，否则改了主题但窗体没生效。
    for selector in (
        "QMainWindow",
        "QLabel#statusPill",
        "QWidget#sidebar",
        "QToolButton#navItem",
        "QToolButton#navItem:checked",
        "QLabel#navGroup",
        "QWidget#panel",
        "QWidget#card",
        "QWidget#kpiCard",
        "QLabel#kpiValue",
        "QLabel#kpiCaption",
        "QLabel#kpiIcon",
        'QLabel#kpiIcon[hue="blue"]',
        'QLabel#kpiIcon[hue="purple"]',
        'QLabel#kpiIcon[hue="amber"]',
        'QLabel#kpiIcon[hue="teal"]',
        "QFrame#quickTile",
        "QFrame#quickTile:hover",
        "QFrame#quickTile:focus",
        "QLabel#quickTileTitle",
        "QLabel#quickTileCaption",
        "QLabel#experimentBanner",
        "QTableWidget",
        "QHeaderView::section",
        "QWidget#intelligencePanel",
        "QFrame#intelSection",
        "QLabel#intelSectionTitle",
        "QLabel#intelMetricValue",
        'QLabel[tier="info"]',
        'QLabel[tier="opportunity"]',
        'QLabel[tier="risk"]',
        'QLabel[tier="gap"]',
        "QTextBrowser#reportBox",
        "QPushButton#primaryBtn",
        "QPushButton#segmentedButton:checked",
        "QScrollBar:vertical",
        "QComboBox QAbstractItemView",
        "QListWidget::item:selected",
        'QLabel[status="watching"]',
        'QLabel[status="triggered"]',
    ):
        assert selector in qss, f"QSS 缺少选择器：{selector}"

    intel_section_rule = qss.split("QFrame#intelSection", 1)[1].split("}", 1)[0]
    assert "padding" not in intel_section_rule, "情报分区底部空间必须由布局控制，不能由 QSS padding 压缩内容"

    # 自定义调色板可覆盖（保证主题层是真正可替换的 seam）。
    custom = build_workbench_qss({"bg": "#010203"})
    assert "#010203" in custom, "自定义调色板未生效"

    contrast_pairs = [
        ("text", "bg"),
        ("text_soft", "bg"),
        ("muted", "bg"),
        ("faint", "bg"),
        ("muted", "surface"),
        ("text_soft", "surface_alt"),
        ("watching", "watching_soft"),
        ("triggered", "triggered_soft"),
        ("review", "review_soft"),
        ("opportunity", "opportunity_soft"),
        ("ok", "opportunity_soft"),
        ("holding", "holding_soft"),
    ]
    for foreground, background in contrast_pairs:
        ratio = _contrast(PALETTE[foreground], PALETTE[background])
        assert ratio >= 4.5, f"{foreground}/{background} 对比度不足：{ratio:.2f}"
    primary_ratio = _contrast("#ffffff", PALETTE["accent"])
    assert primary_ratio >= 4.5, f"主按钮白字/蓝底对比度不足：{primary_ratio:.2f}"

    font_dir = ROOT / "stock_tool" / "ui" / "web" / "fonts"
    for name in ("JetBrainsMono-Regular.ttf", "JetBrainsMono-Medium.ttf", "OFL.txt"):
        assert (font_dir / name).exists(), f"缺少字体资产：{name}"

    html = (ROOT / "stock_tool" / "ui" / "web" / "stock_workbench.html").read_text(encoding="utf-8")
    assert "@font-face" in html and "JetBrainsMono-Regular.ttf" in html, "HTML 未注册 JetBrains Mono"
    assert '"JetBrains Mono", Consolas' in html, "Canvas 数字字体未优先使用 JetBrains Mono"
    assert "#a98cf0" in html, "副图紫色线未使用深底提亮后的色值"

    print("[OK] qt_theme 令牌与 QSS 选择器覆盖通过")


if __name__ == "__main__":
    run()
