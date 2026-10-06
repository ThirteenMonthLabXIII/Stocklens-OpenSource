"""P2 筛选闭环的真实 Qt 点击回归。

本测试只通过可见控件完成解析、执行、保存、历史和导出，确保工作台并非
仅在直接调用窗口方法时可用。行情、WebEngine 与数据目录均使用离线 fixture。
"""
from __future__ import annotations

import os
import sys
import tempfile
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

from stock_tool.core.data_snapshot import DataSnapshotFact, DataSnapshotManifest, fact_set_digest
from stock_tool.core.factor_store import FactorSnapshot, build_factor_snapshot_manifest
from stock_tool.core.notify import MemoryNotificationChannel, Notifier
from stock_tool.ui import qt_workbench
from tools.test_qt_workbench_smoke import FakeObserverFetcher, FakeWebEngineView, wait_for


def _button(window, action_id: str):
    return next(
        widget
        for widget in window.findChildren(qt_workbench.QPushButton)
        if widget.property("action_id") == action_id
    )


def _seed_snapshot(window) -> None:
    assert window.factor_store is not None
    snapshot = FactorSnapshot(
        code="000101",
        name="Qt点击样例",
        price=12.5,
        pe=20,
        pb=1.8,
        market_cap_yi=260,
        roe=18,
        debt_ratio=45,
        box_score=76,
        box_range_pct=14,
        box_is_sideways=True,
        box_summary="箱体评分76，处于横盘蓄势区间",
        industry="测试行业",
        industry_strength=72,
        industry_sample_count=30,
        technical_label="A 箱体横盘",
        technical_status="candidate",
        updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    manifest, facts = build_factor_snapshot_manifest(
        [snapshot],
        quote_updated_at=None,
        total=1,
        failed=0,
        limit=1,
    )
    available_at = snapshot.updated_at
    selection_values = {
        "board_id": "main_board",
        "security_status": "listed",
        "listing_age_trading_days": 180,
        "valid_turnover_days_20d": 20,
        "valid_price_volume_days": 20,
        "median_turnover_value_20d": 30_000_000,
        "snapshot_quality_status": "verified",
        "financial_quality_status": "verified",
        "financial_reports": [{
            "report_date": "2026-03-31",
            "available_at": "2026-04-30T00:00:00+08:00",
            "net_profit": 10,
            "operating_cashflow": 5,
            "quality_status": "verified",
        }],
        "verified_report_periods": 4,
        "net_profit_positive_periods": 4,
        "operating_cashflow_positive_periods": 4,
        "industry_sector": "测试行业",
        "industry_classification_id": "csrc2012",
        "industry_classification_version": "2026.1",
        "peer_set_digest": "qt-click-peer-digest",
        "debt_sector_percentile": 50,
        "adjusted_price_series": True,
        "adjustment_factor_missing": False,
        "adjustment_factor": 1.0,
        "corporate_action_fact_id": "qt-click-ca",
        "adjustment_method": "split_adjusted",
        "valid_price_days_20d": 20,
        "valid_volume_days_20d": 20,
        "relative_strength_20d": 3,
        "technical_fields_quality": "verified",
        "events": [{
            "code": snapshot.code,
            "event_title": "定期报告披露",
            "event_date": "2026-07-22",
            "available_at": "2026-07-22T09:00:00+08:00",
            "event_source": "exchange",
            "source_tier": 1,
            "source_route_verified": True,
            "deduped": True,
        }],
    }
    selection_facts = [
        DataSnapshotFact(
            snapshot_id=manifest.snapshot_id,
            code=snapshot.code,
            field_name=field_name,
            value=value,
            effective_at=available_at,
            available_at=available_at,
            source="qt-selection-fixture",
            source_version="v1",
            fetched_at=available_at,
        )
        for field_name, value in selection_values.items()
    ]
    all_facts = [*facts, *selection_facts]
    manifest = DataSnapshotManifest.create(
        asof_at=manifest.asof_at,
        availability_cutoff=manifest.availability_cutoff,
        data_source=manifest.data_source,
        universe_id=manifest.universe_id,
        universe_version="qt-click-universe-v1",
        dataset_version=manifest.dataset_version,
        source_policy_version=manifest.source_policy_version,
        source_versions=dict(manifest.source_versions),
        facts_digest=fact_set_digest(all_facts),
        quality_status="verified",
        quality_summary=json.loads(manifest.quality_summary_json),
        created_at=manifest.created_at,
    )
    all_facts = [replace(fact, snapshot_id=manifest.snapshot_id) for fact in all_facts]
    window.factor_store.commit_snapshot([snapshot], manifest, all_facts)


def test_screening_controls_by_real_click() -> None:
    from PySide6.QtTest import QTest

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
        data_dir = Path(raw) / "data"
        data_dir.mkdir()
        qt_workbench.read_data_dir = lambda: data_dir
        qt_workbench.read_configured_data_dir = lambda: None
        qt_workbench.write_data_dir = lambda _path: None
        qt_workbench.QWebEngineView = FakeWebEngineView
        app = qt_workbench.QApplication.instance() or qt_workbench.QApplication([])
        window = qt_workbench.StockWorkbenchWindow(notifier=Notifier([MemoryNotificationChannel()]))
        # 该测试使用全离线 fixture，不得继承本机配置的真实 API Key。
        window.api_key_input.clear()
        window.show()
        wait_for(window, "data_refresh")
        window.fetcher = FakeObserverFetcher()
        window.refresh_coordinator = qt_workbench.DataRefreshCoordinator(window.cache, window.fetcher)
        _seed_snapshot(window)
        # 该 fixture 有意保留部分字段为空，用于点击链路而非正式数据质量验收；
        # formal 路径现在会严格阻断 partial 快照，因此显式走历史复核模式。
        review_index = window.screening_mode_combo.findData("review_only")
        assert review_index >= 0
        window.screening_mode_combo.setCurrentIndex(review_index)
        window._navigate("screener")

        window.screener_query_input.setText("ROE大于15，PE小于40，箱体横盘")
        assert window.current_query_spec is None
        assert _button(window, "screener.run").isEnabled()
        QTest.mouseClick(_button(window, "screener.run"), qt_workbench.Qt.LeftButton)
        wait_for(window, "screener_run")
        assert window.current_query_spec is not None
        assert window.current_screen_rows[0]["code"] == "000101"
        assert window.current_screener_run_id is not None

        window.screener_strategy_name_input.setText("P2 Qt 点击策略")
        QTest.mouseClick(_button(window, "screener.save_strategy"), qt_workbench.Qt.LeftButton)
        assert window.current_strategy_preset_id is not None
        assert window.screener_strategy_combo.currentData() == window.current_strategy_preset_id

        # 同名保存必须产生版本，而不是静默覆盖且不可恢复。
        QTest.mouseClick(_button(window, "screener.save_strategy"), qt_workbench.Qt.LeftButton)
        assert window.screener_strategy_version_combo.count() >= 2
        QTest.mouseClick(_button(window, "screener.strategy.load"), qt_workbench.Qt.LeftButton)
        assert window.screener_query_input.text() == "ROE大于15，PE小于40，箱体横盘"

        yes_button = getattr(qt_workbench.QMessageBox, "Yes", qt_workbench.QMessageBox.StandardButton.Yes)
        qt_workbench.QMessageBox.question = lambda *_args, **_kwargs: yes_button
        QTest.mouseClick(_button(window, "screener.strategy.restore"), qt_workbench.Qt.LeftButton)
        assert window.screener_strategy_version_combo.count() >= 3

        # 已保存策略连续运行两次后，比较与比较 CSV 都必须可用。
        QTest.mouseClick(_button(window, "screener.run"), qt_workbench.Qt.LeftButton)
        wait_for(window, "screener_run")
        QTest.mouseClick(_button(window, "screener.run"), qt_workbench.Qt.LeftButton)
        wait_for(window, "screener_run")

        QTest.mouseClick(_button(window, "screener.history"), qt_workbench.Qt.LeftButton)
        assert window.screener_history_table.rowCount() >= 1
        QTest.mouseClick(_button(window, "screener.history.compare"), qt_workbench.Qt.LeftButton)
        assert "已对比执行" in window.screener_summary_box.toPlainText()
        QTest.mouseClick(_button(window, "screener.history.export_compare_csv"), qt_workbench.Qt.LeftButton)

        QTest.mouseClick(_button(window, "screener.export_csv"), qt_workbench.Qt.LeftButton)
        QTest.mouseClick(_button(window, "screener.export_json"), qt_workbench.Qt.LeftButton)
        app.processEvents()
        exported = list((data_dir / "exports").glob("qt_screener_*"))
        assert any(path.suffix == ".csv" for path in exported)
        assert any(path.suffix == ".json" for path in exported)
        assert any("compare" in path.stem and path.suffix == ".csv" for path in exported)

        import_file = next(path for path in exported if path.suffix == ".json")
        qt_workbench.QFileDialog.getOpenFileName = lambda *_args, **_kwargs: (str(import_file), "JSON 文件 (*.json)")
        QTest.mouseClick(_button(window, "screener.strategy.import_preview"), qt_workbench.Qt.LeftButton)
        assert "导入预览" in window.screener_summary_box.toPlainText()

        QTest.mouseClick(_button(window, "screener.strategy.delete"), qt_workbench.Qt.LeftButton)
        assert window.screener_strategy_combo.findText("P2 Qt 点击策略", qt_workbench.Qt.MatchStartsWith) < 0

        window.close()
        if window.v1_database is not None:
            window.v1_database.close()


def main() -> int:
    test_screening_controls_by_real_click()
    print("[OK] Qt 筛选真实点击测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
