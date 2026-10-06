from __future__ import annotations

import os
import json
import sys
import tempfile
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

try:
    import stock_tool.ui.qt_workbench as qt_workbench
except Exception as exc:  # pragma: no cover - optional local smoke.
    print(f"[SKIP] Qt smoke：PySide6/WebEngine 不可用：{exc}")
    raise SystemExit(0)

from stock_tool.core.factor_store import FactorSnapshot, build_factor_snapshot_manifest
from stock_tool.core.data_readiness import DataReadinessResult
from stock_tool.core.alert_monitor import TradingCalendar, TradingClock
from stock_tool.core.data_fetcher import QuoteSnapshot
from stock_tool.core.models import EventCatalyst, FinancialMetrics, MarketSentiment, MoneyFlowSnapshot, StockQuote
from stock_tool.core.notify import MemoryNotificationChannel, Notifier


class FakeObserverFetcher:
    def quotes_by_codes(self, codes):
        now = getattr(self, "snapshot_now", None) or datetime.now().astimezone()
        return {
            code: QuoteSnapshot(self.quote_by_code(code), "tencent", now, now)
            for code in codes
        }

    def quote_by_code(self, code: str):
        names = {"000101": "Qt样例", "000102": "Qt慢观察"}
        return StockQuote(code=code, name=names.get(code, f"Qt样例{code}"), price=12.5, change_pct=1.2)

    def spot_quotes(self, force: bool = False):
        return [
            StockQuote(code="000101", name="Qt样例", price=12.5, change_pct=1.2),
            StockQuote(code="000102", name="Qt慢观察", price=10.0, change_pct=-0.2),
        ], datetime.now().astimezone()

    def history(self, code: str, days: int = 180, force: bool = False):
        rows = []
        for index in range(80):
            close = 10 + index * 0.03
            rows.append({"开盘": close - 0.05, "最高": close + 0.12, "最低": close - 0.12, "收盘": close, "成交量": 100000 + index * 1000})
        return pd.DataFrame(rows), datetime.now().astimezone()

    def money_flow(self, code: str, days: int = 20, force: bool = False):
        return MoneyFlowSnapshot(
            code=code,
            rows=[{"date": "2026-06-01"}],
            main_3d=1200000,
            main_5d=3000000,
            main_10d=5000000,
            xlarge_5d=1000000,
            small_5d=-500000,
            streak=4,
            summary="近5日主力净流入",
        ), datetime.now().astimezone()

    def market_sentiment(self, force: bool = False):
        return (
            MarketSentiment(
                up=3200,
                down=1700,
                flat=200,
                limit_up=60,
                limit_down=8,
                avg_change_pct=0.8,
                summary="市场情绪偏暖",
            ),
            datetime.now().astimezone(),
        )

    def market_money_flow(self, force: bool = False):
        return {"000101": 1200000.0, "000102": -300000.0}

    def event_catalysts(self, code: str, limit: int = 6, force: bool = False):
        return [EventCatalyst(title="业绩预告改善", source="test", date="2026-06-01")], datetime.now().astimezone()

    def financial_metrics(self, code: str, force: bool = False):
        return FinancialMetrics(roe=18, net_margin=12, debt_ratio=45), datetime.now().astimezone()


class SlowObserverFetcher(FakeObserverFetcher):
    def __init__(self) -> None:
        self.history_started = threading.Event()
        self.release_history = threading.Event()

    def history(self, code: str, days: int = 180, force: bool = False):
        if code == "000102":
            self.history_started.set()
            if not self.release_history.wait(3):
                raise TimeoutError("Qt慢观察未释放")
        return super().history(code, days, force)


def wait_for(window, task_key: str, timeout: float = 8.0) -> None:
    app = qt_workbench.QApplication.instance()
    deadline = time.time() + timeout
    while task_key in window._running_tasks and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    if task_key in window._running_tasks:
        raise TimeoutError(f"{task_key} 未在预期时间内完成")


def _weekday_calendar(start: date, end: date) -> set[date]:
    days: set[date] = set()
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.add(cursor)
        cursor += timedelta(days=1)
    return days


def assert_action_registry(window) -> None:
    assert hasattr(window, "action_registry") and window.action_registry, "Qt 动作清单未初始化"
    widgets = []
    widgets.extend(window.findChildren(qt_workbench.QPushButton))
    widgets.extend(window.findChildren(qt_workbench.QToolButton))
    widgets.extend(
        frame
        for frame in window.findChildren(qt_workbench.QFrame)
        if frame.objectName() == "quickTile"
    )
    missing: list[str] = []
    unregistered: list[str] = []
    kind_mismatch: list[str] = []
    for widget in widgets:
        action_id = str(widget.property("action_id") or "")
        if not action_id:
            label = widget.text() if hasattr(widget, "text") else widget.objectName()
            missing.append(label or widget.objectName() or widget.__class__.__name__)
            continue
        descriptor = window.action_registry.get(action_id)
        if descriptor is None:
            unregistered.append(action_id)
            continue
        kind = str(widget.property("action_kind") or "")
        if kind and kind != descriptor.kind:
            kind_mismatch.append(f"{action_id}:{kind}!={descriptor.kind}")
        if descriptor.kind in {"execute", "navigate", "external"}:
            assert callable(descriptor.run), f"{action_id} 缺少执行函数"
    assert not missing, f"Qt 存在未登记 action_id 的按钮/快捷卡：{missing}"
    assert not unregistered, f"Qt action_id 未进入动作清单：{unregistered}"
    assert not kind_mismatch, f"Qt action kind 与清单不一致：{kind_mismatch}"
    contract_path = ROOT / "stock_tool" / "config" / "frontend_parity_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract_action_ids = {
        action_id
        for feature in contract["features"]
        for action_id in feature["qt_action_ids"]
    }
    missing_contract_actions = sorted(contract_action_ids - set(window.action_registry))
    assert not missing_contract_actions, f"功能合同 action 未注册：{missing_contract_actions}"
    uncontracted_actions = sorted(set(window.action_registry) - contract_action_ids)
    assert not uncontracted_actions, f"Qt 正式 action 未进入功能合同：{uncontracted_actions}"


def assert_data_action_paths(window) -> None:
    yes_button = getattr(qt_workbench.QMessageBox, "Yes", qt_workbench.QMessageBox.StandardButton.Yes)
    no_button = getattr(qt_workbench.QMessageBox, "No", qt_workbench.QMessageBox.StandardButton.No)
    original_question = qt_workbench.QMessageBox.question
    original_refresh_dashboard_snapshot = window._refresh_dashboard_snapshot
    original_fill_fetch_log_table = window._fill_fetch_log_table
    original_ensure = window.data_readiness.ensure
    original_pit_database = window.pit_database
    window.pit_database = object()
    window._refresh_dashboard_snapshot = lambda: None
    window._fill_fetch_log_table = lambda: None
    window._navigate("data")
    try:
        assert len(window.feature_ttl_inputs) == 8
        quote_ttl = window.feature_ttl_inputs["feature_refresh_quote_ttl_seconds"]
        quote_ttl.setText("1")
        window._save_feature_ttl("feature_refresh_quote_ttl_seconds")
        assert window.cache.get_setting("feature_refresh_quote_ttl_seconds", "") == "30"
        quote_ttl.setText("45")
        window._save_feature_ttl("feature_refresh_quote_ttl_seconds")
        assert window.cache.get_setting("feature_refresh_quote_ttl_seconds", "") == "45"

        qt_workbench.QMessageBox.question = lambda *_args, **_kwargs: no_button
        snapshot_cancel_calls: list[str] = []

        def unexpected_ensure(*_args, **_kwargs):
            snapshot_cancel_calls.append("called")
            raise AssertionError("取消确认后不应执行筛选快照更新")

        window.data_readiness.ensure = unexpected_ensure
        assert window._start_v1_snapshot_update() is False
        assert not snapshot_cancel_calls

        readiness_calls: list[object] = []
        qt_workbench.QMessageBox.question = lambda *_args, **_kwargs: yes_button

        def ready_snapshot(requirement, **_kwargs):
            readiness_calls.append(requirement)
            return DataReadinessResult(
                "refreshed",
                requirement.feature,
                requirement.kinds,
                snapshot_id="snapshot-test",
                message="测试完成",
            )

        window.data_readiness.ensure = ready_snapshot
        assert window._start_v1_snapshot_update() is True
        wait_for(window, "v1_snapshot_update")
        assert readiness_calls and readiness_calls[0].kinds == ("factor_snapshot",)
        assert readiness_calls[0].force is True
        assert "筛选快照已更新" in window.status_label.text()
        assert "筛选快照已更新" in window.data_status_pill.text()

        release_snapshot = threading.Event()
        snapshot_started = threading.Event()

        def blocking_snapshot(requirement, **_kwargs):
            snapshot_started.set()
            if not release_snapshot.wait(3):
                raise TimeoutError("筛选快照测试未释放")
            return DataReadinessResult(
                "refreshed",
                requirement.feature,
                requirement.kinds,
                snapshot_id="snapshot-test",
            )

        window.data_readiness.ensure = blocking_snapshot
        assert window._start_v1_snapshot_update() is True
        assert snapshot_started.wait(3), "筛选快照任务未启动"
        assert "v1_snapshot_update" in window._running_tasks
        assert window._start_v1_snapshot_update() is False
        release_snapshot.set()
        wait_for(window, "v1_snapshot_update")

        def failing_snapshot(*_args, **_kwargs):
            raise RuntimeError("测试失败")

        window.data_readiness.ensure = failing_snapshot
        assert window._start_v1_snapshot_update() is True
        wait_for(window, "v1_snapshot_update")
        assert "筛选快照更新失败" in window.status_label.text()

        pit_cancel_calls: list[str] = []
        qt_workbench.QMessageBox.question = lambda *_args, **_kwargs: no_button

        def unexpected_pit(*_args, **_kwargs):
            pit_cancel_calls.append("called")
            raise AssertionError("取消确认后不应执行 PIT 日增量")

        window.data_readiness.ensure = unexpected_pit
        assert window._start_pit_incremental() is False
        assert not pit_cancel_calls

        qt_workbench.QMessageBox.question = lambda *_args, **_kwargs: yes_button

        def ready_pit(requirement, **_kwargs):
            readiness_calls.append(requirement)
            return DataReadinessResult(
                "refreshed",
                requirement.feature,
                requirement.kinds,
                snapshot_id="pit:2026-07-05",
                refresh_event_id="pit-job:1",
                message="测试完成",
            )

        window.data_readiness.ensure = ready_pit
        assert window._start_pit_incremental() is True
        wait_for(window, "pit_incremental")
        assert readiness_calls[-1].kinds == ("pit_incremental",)
        assert readiness_calls[-1].force is True
        assert "测试完成" in window.status_label.text()
        assert "PIT 日增量完成" in window.data_status_pill.text()

        release_pit = threading.Event()
        pit_started = threading.Event()

        def blocking_pit(requirement, **_kwargs):
            pit_started.set()
            if not release_pit.wait(3):
                raise TimeoutError("PIT 日增量测试未释放")
            return DataReadinessResult("refreshed", requirement.feature, requirement.kinds)

        window.data_readiness.ensure = blocking_pit
        assert window._start_pit_incremental() is True
        assert pit_started.wait(3), "PIT 日增量任务未启动"
        assert "pit_incremental" in window._running_tasks
        assert window._start_pit_incremental() is False
        release_pit.set()
        wait_for(window, "pit_incremental")

        def failing_pit(*_args, **_kwargs):
            raise RuntimeError("测试失败")

        window.data_readiness.ensure = failing_pit
        assert window._start_pit_incremental() is True
        wait_for(window, "pit_incremental")
        assert "PIT 日增量失败" in window.status_label.text()
    finally:
        qt_workbench.QMessageBox.question = original_question
        window._refresh_dashboard_snapshot = original_refresh_dashboard_snapshot
        window._fill_fetch_log_table = original_fill_fetch_log_table
        window.data_readiness.ensure = original_ensure
        window.pit_database = original_pit_database


class FakeSignal:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, value) -> None:
        for callback in list(self._callbacks):
            callback(value)


class FakeWebPage(qt_workbench.QObject):
    def setWebChannel(self, _channel) -> None:
        return None

    def runJavaScript(self, _script: str) -> None:
        return None


class FakeWebEngineView(qt_workbench.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.loadFinished = FakeSignal()
        self._page = FakeWebPage()

    def page(self):
        return self._page

    def load(self, _url) -> None:
        self.loadFinished.emit(True)


def main() -> None:
    infos: list[str] = []
    warnings: list[str] = []
    yes_button = getattr(qt_workbench.QMessageBox, "Yes", qt_workbench.QMessageBox.StandardButton.Yes)
    qt_workbench.QMessageBox.information = lambda _parent, _title, message: infos.append(str(message))
    qt_workbench.QMessageBox.warning = lambda _parent, _title, message: warnings.append(str(message))
    qt_workbench.QMessageBox.question = lambda *_args, **_kwargs: yes_button

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        data_dir = Path(tmp) / "qt_data"
        data_dir.mkdir(parents=True, exist_ok=True)
        qt_workbench.read_data_dir = lambda: data_dir
        qt_workbench.write_data_dir = lambda _data_dir: None
        qt_workbench.read_configured_data_dir = lambda: None
        app = qt_workbench.QApplication.instance() or qt_workbench.QApplication([])
        qt_workbench.QWebEngineView = FakeWebEngineView
        try:
            memory_notifications = MemoryNotificationChannel()
            window = qt_workbench.StockWorkbenchWindow(notifier=Notifier([memory_notifications]))
        except Exception as exc:
            print(f"[SKIP] Qt smoke：窗口无法在当前环境实例化：{exc}")
            return
        window.hide()
        assert_action_registry(window)
        wait_for(window, "data_refresh")
        window.fetcher = FakeObserverFetcher()
        window.alert_monitor.fetcher = window.fetcher
        window.alert_monitor.trading_clock = TradingClock(TradingCalendar(_weekday_calendar))
        # Keep monitor time coherent with the fixture's real-source timestamps.
        # A future quote must be rejected by the alert monitor, so the source
        # and trading clock deliberately share one deterministic instant.
        alert_time = datetime(2026, 7, 10, 10, 0).astimezone()
        window.fetcher.snapshot_now = alert_time
        window.alert_monitor._now = lambda: alert_time
        window.data_readiness.policy._now = lambda: alert_time
        window.refresh_coordinator = qt_workbench.DataRefreshCoordinator(window.cache, window.fetcher)
        window.api_key_input.setText("test-key")
        window.current_quote = StockQuote(code="000101", name="Qt样例", price=12.5)
        window.current_history = pd.DataFrame()

        assert window.factor_store is not None
        seed_snapshot = FactorSnapshot(
            code="000101",
            name="Qt样例",
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
            technical_label="A 箱体横盘",
            technical_status="candidate",
            updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        slow_snapshot = replace(
            seed_snapshot,
            code="000102",
            name="Qt慢观察",
            price=10.0,
        )
        seed_manifest, seed_facts = build_factor_snapshot_manifest(
            [seed_snapshot, slow_snapshot],
            quote_updated_at=datetime.now().astimezone(),
            total=2,
            failed=0,
            limit=2,
        )
        window.factor_store.commit_snapshot([seed_snapshot, slow_snapshot], seed_manifest, seed_facts)
        window._navigate("screener")
        review_index = window.screening_mode_combo.findData("review_only")
        assert review_index >= 0
        window.screening_mode_combo.setCurrentIndex(review_index)
        window.screener_query_input.setText("ROE大于15，PE小于40，箱体横盘")
        parse_result = window.parse_screener_query()
        assert parse_result.status == "success", parse_result
        assert window.current_query_spec is not None
        assert window.screener_run_button.isEnabled()
        assert window.run_screener_query()
        wait_for(window, "screener_run")
        assert window.current_screen_rows and window.current_screen_rows[0]["code"] == "000101"
        assert window.performance_store is not None and window.current_screener_run_id is not None
        assert window.current_screen_report is not None
        assert window.current_screen_report.status == "partial"
        with window.v1_database.connect() as conn:
            sample_count = conn.execute(
                "SELECT COUNT(*) FROM observation_samples WHERE run_id = ?", (window.current_screener_run_id,)
            ).fetchone()[0]
        assert sample_count == 0, "partial 数据只允许保存审计记录，不得创建正式观察样本"
        save_result = window.save_screener_strategy()
        assert save_result.status == "success", save_result
        assert window.current_strategy_preset_id is not None
        csv_result = window.export_screener_csv()
        json_result = window.export_screener_json()
        assert csv_result.status == "success" and Path(csv_result.artifacts["csv"]).exists()
        assert json_result.status == "success" and Path(json_result.artifacts["json"]).exists()
        history_result = window.refresh_screener_history()
        assert history_result.status == "success", history_result
        assert window.screener_history_table.rowCount() >= 1

        # 价格预警必须走真实存储与后台检查闭环，而不是只验证导航占位页。
        window._navigate("alerts")
        window.alert_code_input.setText("000101")
        window.alert_name_input.setText("Qt样例")
        window.alert_kind_combo.setCurrentIndex(window.alert_kind_combo.findData("sell"))
        window.alert_threshold_input.setText("10")
        window.alert_repeat_check.setChecked(False)
        window.save_alert()
        assert window.alert_table.rowCount() == 1
        alert_id = int(window.alert_table.item(0, 0).text())
        assert window.run_alert_check(manual=True)
        wait_for(window, "alert_check")
        assert window.alert_history_table.rowCount() == 1
        assert len(memory_notifications.messages) == 1
        assert not next(item for item in window.cache.list_price_alerts() if item.id == alert_id).enabled
        window.alert_table.selectRow(0)
        window.alert_threshold_input.setText("11")
        window.save_alert()
        edited = next(item for item in window.cache.list_price_alerts() if item.id == alert_id)
        assert edited.threshold == 11 and edited.last_state == 0
        assert_data_action_paths(window)

        # The alert branch uses a fixed in-session quote time.  Restore normal
        # fresh fixture timestamps before testing feature readiness.
        window.fetcher.snapshot_now = None
        window.data_readiness.policy._now = lambda: datetime.now().astimezone()
        window.generate_trade_observation()
        wait_for(window, "observer:000101")
        assert window.current_trade_reference is not None
        assert "AI证据整理：未触发" in window.current_observation_note

        slow_fetcher = SlowObserverFetcher()
        window.fetcher = slow_fetcher
        window.refresh_coordinator = qt_workbench.DataRefreshCoordinator(window.cache, window.fetcher)
        before_count = len(window.cache.list_tracking_tasks(include_closed=True))
        window.current_quote = StockQuote(code="000102", name="Qt慢观察", price=10.0)
        window.generate_trade_observation()
        assert slow_fetcher.history_started.wait(3), "Qt慢观察任务未启动"
        window.save_tracking_task("watch")
        assert len(window.cache.list_tracking_tasks(include_closed=True)) == before_count
        assert infos and "观察生成中，请等待完成后再保存" in infos[-1]
        slow_fetcher.release_history.set()
        wait_for(window, "observer:000102")

        slow_fetcher.history_started.clear()
        slow_fetcher.release_history.clear()
        window.cache.add_watch("000102", "Qt慢观察")
        window._last_refresh_summary = None
        window.current_quote = StockQuote(code="000102", name="Qt慢观察", price=10.0)
        window.data_status_pill.click()
        assert slow_fetcher.history_started.wait(3), "数据刷新未进入当前个股详情"
        assert "data_refresh" in window._running_tasks
        assert window._start_data_refresh("manual") is False
        assert not window.data_status_pill.isEnabled()
        slow_fetcher.release_history.set()
        wait_for(window, "data_refresh")
        assert window.data_status_pill.isEnabled()
        assert window.refresh_stage_table.rowCount() >= 1
        assert "刷新" in window.data_status_pill.text() or "数据状态" in window.data_status_pill.text() or "缓存" in window.data_status_pill.text()

        original_ai = qt_workbench.analyze_trade_observation_with_ai
        original_readiness_ensure = window.data_readiness.ensure
        try:
            # This smoke segment isolates the old UI compatibility hook.  The
            # snapshot above was inserted directly for a fast offline setup,
            # so it intentionally has no ingestion event.  Production formal
            # paths reject that condition; provide an audited-ready result
            # here to exercise only the post-readiness AI rendering branch.
            window.data_readiness.ensure = lambda requirement, **_kwargs: DataReadinessResult(
                status="ready",
                feature=requirement.feature,
                kinds=requirement.kinds,
                snapshot_id=seed_manifest.snapshot_id,
                refresh_event_id="smoke-refresh-event",
            )
            qt_workbench.analyze_trade_observation_with_ai = lambda *_args, **_kwargs: "AI增强结论：Qt测试通过"
            window.analyze_trade_observation_with_ai()
            wait_for(window, "observer_ai:000102")
            assert "AI证据整理：已追加" in window.current_observation_note

            def failing_ai(*_args, **_kwargs):
                raise RuntimeError("模型临时不可用")

            qt_workbench.analyze_trade_observation_with_ai = failing_ai
            window.analyze_trade_observation_with_ai()
            wait_for(window, "observer_ai:000102")
            assert "AI证据整理：失败：模型临时不可用" in window.current_observation_note
            assert "AI证据整理：已追加" not in window.current_observation_note
            window.save_tracking_task("watch")
        finally:
            qt_workbench.analyze_trade_observation_with_ai = original_ai
            window.data_readiness.ensure = original_readiness_ensure
            window.close()
            window.deleteLater()
            app.processEvents()
    print("[OK] Qt 工作台 smoke 测试通过")


if __name__ == "__main__":
    main()
