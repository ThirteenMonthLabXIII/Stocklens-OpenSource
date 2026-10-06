from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "build" / "ui_layout_evidence"
WINDOW_SIZES = ((1180, 720), (1440, 860), (1920, 1080))
SCALE_FACTORS = ("1", "1.25", "1.5")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _configure_qt() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")


def _sections() -> dict[str, object]:
    return {
        "quote_metrics": [
            ("最新价", "12.58"),
            ("涨跌幅", "+3.26%"),
            ("成交额", "18.34 亿"),
            ("换手率", "4.82%"),
        ],
        "trend_summary": "短期趋势向上但接近前期压力位，价格仍位于五日、十日与二十日均线上方；需结合量能和后续公告确认趋势延续性。",
        "ma_metrics": [
            ("MA5", "12.18"),
            ("MA10", "11.96"),
            ("MA20", "11.63"),
            ("偏离率", "+7.94%"),
        ],
        "momentum_metrics": [
            ("RSI(14)", "68.2"),
            ("KDJ", "K 76.4 / D 69.8"),
            ("MACD", "DIF 0.42 / DEA 0.31"),
            ("量比", "2.37"),
        ],
        "opportunities": [
            "均线多头排列仍在延续",
            "近五日主力资金保持净流入",
            "放量突破后回踩尚未失守关键均线",
        ],
        "risks": [
            "接近前期密集成交区，追高风险需要观察",
            "短线涨幅偏大，波动可能显著放大",
            "公开数据更新时点可能与盘中实际状态不同",
        ],
        "gaps": [
            "缺少最新机构持仓变动与完整公告解读",
            "行业相对强弱数据待下一次 PIT 快照补齐",
        ],
    }


def _process_events(app) -> None:
    for _ in range(4):
        app.processEvents()


def _assert_section_geometry(panel, qt_workbench) -> None:
    frames = [
        frame
        for frame in panel.content_widget.findChildren(qt_workbench.QFrame)
        if frame.parentWidget() is panel.content_widget and frame.objectName() == "intelSection"
    ]
    assert len(frames) == 7, f"情报分区数量异常：{len(frames)}"
    ordered = sorted(frames, key=lambda frame: frame.geometry().top())
    for previous, current in zip(ordered, ordered[1:]):
        assert previous.geometry().bottom() < current.geometry().top(), "相邻情报分区发生几何重叠"
    for frame in ordered:
        layout = frame.layout()
        assert layout is not None
        text_widgets = frame.findChildren(qt_workbench.QLabel)
        assert text_widgets, "分区缺少文本控件"
        for label in text_widgets:
            if not label.wordWrap() or label.objectName() in {"intelMetricLabel", "intelMetricValue"}:
                assert label.height() >= label.fontMetrics().height(), f"单行标签被压缩：{label.text()}"
            rect = label.geometry()
            assert rect.bottom() < frame.contentsRect().bottom(), f"分隔线穿过文字：{label.text()}"
        for index, first in enumerate(text_widgets):
            for second in text_widgets[index + 1:]:
                assert not first.geometry().intersects(second.geometry()), "指标或标签覆盖相邻单元格"


def _assert_bottom_sections_accessible(panel, qt_workbench, app) -> None:
    scroll = panel.scroll_area
    scrollbar = scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
    _process_events(app)
    viewport = scroll.viewport().rect()
    gap_label = next(
        item
        for item in panel.content_widget.findChildren(qt_workbench.QLabel)
        if item.objectName() == "intelSectionTitle" and item.text() == "数据缺口"
    )
    gap_frame = gap_label.parentWidget()
    gap_top = gap_frame.mapTo(scroll.viewport(), gap_frame.rect().topLeft())
    gap_bottom = gap_frame.mapTo(scroll.viewport(), gap_frame.rect().bottomRight())
    assert viewport.contains(gap_top) and viewport.contains(gap_bottom), "滚动到底部后数据缺口分区不完整"
    for title in ("风险", "数据缺口"):
        label = next(
            item
            for item in panel.content_widget.findChildren(qt_workbench.QLabel)
            if item.objectName() == "intelSectionTitle" and item.text() == title
        )
        frame = label.parentWidget()
        scroll.ensureWidgetVisible(frame)
        _process_events(app)
        top = frame.mapTo(scroll.viewport(), frame.rect().topLeft())
        bottom = frame.mapTo(scroll.viewport(), frame.rect().bottomRight())
        assert viewport.contains(top) and viewport.contains(bottom), f"无法完整访问{title}分区"


def _run_case(width: int, height: int, scale: str) -> None:
    _configure_qt()
    from stock_tool.ui import qt_workbench
    from stock_tool.ui.qt_theme import build_workbench_qss

    app = qt_workbench.QApplication.instance() or qt_workbench.QApplication([])
    window = qt_workbench.QWidget()
    window.resize(360, height)
    window.setStyleSheet(build_workbench_qss())
    layout = qt_workbench.QVBoxLayout(window)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(8)
    title = qt_workbench.QLabel("测试股票")
    title.setObjectName("stockTitle")
    layout.addWidget(title)
    hover_box = qt_workbench.QTextBrowser()
    hover_box.setMaximumHeight(130)
    hover_box.setHtml("十字光标数据会显示在这里。")
    layout.addWidget(hover_box)
    panel = qt_workbench.IntelligencePanel()
    layout.addWidget(panel, 2)
    report_box = qt_workbench.QTextBrowser()
    report_box.setObjectName("reportBox")
    report_box.setMinimumHeight(180)
    report_box.setHtml("研究报告内容")
    layout.addWidget(report_box, 1)
    window.show()
    _process_events(app)

    panel.set_sections(**_sections())
    _process_events(app)

    assert panel.scroll_area.verticalScrollBarPolicy() == qt_workbench.Qt.ScrollBarPolicy.ScrollBarAsNeeded
    assert panel.scroll_area.horizontalScrollBarPolicy() == qt_workbench.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert panel.scroll_area.horizontalScrollBar().maximum() == 0, "情报栏不应出现水平滚动"
    assert panel.scroll_area.verticalScrollBar().maximum() > 0, "空间不足时应以垂直滚动而非压缩标签处理"
    summary = next(
        item
        for item in panel.content_widget.findChildren(qt_workbench.QLabel)
        if item.objectName() == "intelSummary"
    )
    assert summary.minimumWidth() == 0, "趋势摘要必须允许随右栏宽度收缩"
    assert summary.height() > summary.fontMetrics().height(), "长趋势摘要未在右栏内换行"
    assert summary.width() <= panel.scroll_area.viewport().width(), "趋势摘要超出右栏可用宽度"
    _assert_section_geometry(panel, qt_workbench)
    _assert_bottom_sections_accessible(panel, qt_workbench, app)

    for index in range(3):
        replacement = _sections()
        replacement["trend_summary"] = f"第 {index + 1} 次更新后的趋势摘要，必须完全替换旧控件而不是累积旧控件。"
        panel.set_sections(**replacement)
        _process_events(app)
        assert len(
            [
                frame
                for frame in panel.content_widget.findChildren(qt_workbench.QFrame)
                if frame.parentWidget() is panel.content_widget and frame.objectName() == "intelSection"
            ]
        ) == 7, "重复更新后残留旧分区"
        assert not any(
            "第 1 次更新" in label.text()
            for label in panel.content_widget.findChildren(qt_workbench.QLabel)
        ) or index == 0, "重复更新后残留旧标签"

    panel.set_empty()
    _process_events(app)
    assert "加载个股后显示结构化情报。" in {
        label.text() for label in panel.content_widget.findChildren(qt_workbench.QLabel)
    }, "空数据状态未正常显示"
    panel.set_sections(**_sections())
    _process_events(app)
    _assert_bottom_sections_accessible(panel, qt_workbench, app)

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence = EVIDENCE_DIR / f"intelligence_{width}x{height}_{scale.replace('.', '_')}.png"
    assert window.grab().save(str(evidence)), f"无法写入布局证据：{evidence}"
    window.close()
    _process_events(app)
    print(f"[OK] intelligence layout {width}x{height} scale={scale}")


def _run_matrix() -> int:
    for width, height in WINDOW_SIZES:
        for scale in SCALE_FACTORS:
            env = os.environ.copy()
            env["QT_SCALE_FACTOR"] = scale
            command = [sys.executable, str(Path(__file__).resolve()), "--case", f"{width}x{height}", "--scale", scale]
            subprocess.run(command, cwd=ROOT, env=env, check=True)
    print("[OK] Qt intelligence layout matrix passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 Qt 研究页右侧情报栏的可滚动布局。")
    parser.add_argument("--case", help="单个窗口尺寸，例如 1180x720")
    parser.add_argument("--scale", default="1", help="Qt 缩放比例")
    args = parser.parse_args()
    if not args.case:
        return _run_matrix()
    width_text, height_text = args.case.lower().split("x", 1)
    _run_case(int(width_text), int(height_text), args.scale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
