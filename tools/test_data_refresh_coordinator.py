from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.cache import CacheMetadata, CacheRecord
from stock_tool.core.data_refresh import DataRefreshCoordinator
from stock_tool.core.models import EventCatalyst, FinancialMetrics, MarketSentiment, MoneyFlowSnapshot, StockQuote


NOW = datetime(2026, 7, 2, 10, 0, 0)


@dataclass(frozen=True)
class Task:
    code: str
    task_type: str = "watch"
    status: str = "open"


@dataclass(frozen=True)
class Alert:
    code: str
    enabled: bool = True


@dataclass(frozen=True)
class History:
    code: str
    created_at: datetime


class FakeCache:
    def __init__(
        self,
        watches: list[dict[str, Any]] | None = None,
        *,
        tasks: list[Task] | None = None,
        alerts: list[Alert] | None = None,
        history: list[History] | None = None,
        records: dict[str, CacheRecord] | None = None,
    ) -> None:
        self.watches = watches or []
        self.tasks = tasks or []
        self.alerts = alerts or []
        self.history = history or []
        self.records = records or {}

    def list_watch(self) -> list[dict[str, Any]]:
        return list(self.watches)

    def list_tracking_tasks(self, include_closed: bool = True) -> list[Task]:
        if include_closed:
            return list(self.tasks)
        return [task for task in self.tasks if task.status == "open"]

    def list_price_alerts(self, enabled_only: bool = False) -> list[Alert]:
        if enabled_only:
            return [alert for alert in self.alerts if alert.enabled]
        return list(self.alerts)

    def list_alert_history(self, limit: int = 200, since: datetime | None = None) -> list[History]:
        rows = list(self.history)
        if since is not None:
            rows = [row for row in rows if row.created_at >= since]
        return rows[:limit]

    def list_cache_metadata(self, limit: int = 1000) -> list[CacheMetadata]:
        rows = [CacheMetadata(key=key, updated_at=record.updated_at) for key, record in self.records.items()]
        return rows[:limit]

    def get_json(self, key: str, max_age: timedelta | None = None) -> CacheRecord | None:
        record = self.records.get(key)
        if record is None:
            return None
        if max_age and NOW - record.updated_at > max_age:
            return None
        return record

    def set_json(self, key: str, payload: Any) -> None:
        self.records[key] = CacheRecord(key, payload, NOW)


class FakeFetcher:
    def __init__(
        self,
        *,
        quote_failures: set[str] | None = None,
        quote_prices: dict[str, float] | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        self.quote_failures = quote_failures or set()
        self.quote_prices = quote_prices or {}
        self.updated_at = updated_at or NOW
        self.quote_calls: list[str] = []
        self.spot_force_calls: list[bool] = []
        self.financial_force_calls: list[tuple[str, bool]] = []

    def quote_by_code(self, code: str):
        self.quote_calls.append(code)
        if code in self.quote_failures:
            return None
        return StockQuote(code=code, name=f"样例{code}", price=self.quote_prices.get(code, 10.0), change_pct=1.0)

    def spot_quotes(self, force: bool = False):
        self.spot_force_calls.append(force)
        quotes = [StockQuote(code=str(index).zfill(6), name=f"样例{index}") for index in range(1, 60)]
        return quotes, self.updated_at

    def history(self, code: str, days: int = 365, force: bool = False):
        return pd.DataFrame(
            [
                {"开盘": 10 + index * 0.1, "最高": 10.2 + index * 0.1, "最低": 9.8 + index * 0.1, "收盘": 10 + index * 0.1, "成交量": 1000 + index}
                for index in range(40)
            ]
        ), self.updated_at

    def financial_metrics(self, code: str, force: bool = False):
        self.financial_force_calls.append((code, force))
        return FinancialMetrics(roe=12.0), self.updated_at

    def money_flow(self, code: str, days: int = 20, force: bool = False):
        return MoneyFlowSnapshot(code=code, rows=[{"date": "2026-07-02", "main": 1.0}]), self.updated_at

    def event_catalysts(self, code: str, limit: int = 6, force: bool = False):
        return [EventCatalyst(title="测试事件")], self.updated_at

    def market_sentiment(self, force: bool = False):
        return MarketSentiment(up=3000, down=1000, flat=500, summary="偏暖"), self.updated_at

    def market_money_flow(self, force: bool = False):
        return {"000001": 1.0}


def _coordinator(cache: FakeCache, fetcher: FakeFetcher | None = None) -> DataRefreshCoordinator:
    return DataRefreshCoordinator(cache, fetcher or FakeFetcher(), now_fn=lambda: NOW)


def test_prioritized_watch_codes() -> None:
    cache = FakeCache(
        watches=[
            {"code": "000008", "name": "其他旧"},
            {"code": "000007", "name": "最近加入"},
            {"code": "000004", "name": "普通观察"},
            {"code": "000006", "name": "今日触发"},
            {"code": "000003", "name": "提醒"},
            {"code": "000002", "name": "持仓"},
            {"code": "000002", "name": "持仓重复"},
        ],
        tasks=[Task("000002", "holding"), Task("000004", "watch")],
        alerts=[Alert("000003"), Alert("000009", enabled=False)],
        history=[History("000006", NOW)],
    )
    ordered = _coordinator(cache).prioritized_watch_codes("000005")
    assert ordered == ["000005", "000002", "000003", "000006", "000004", "000008", "000007"]


def test_small_watchlist_uses_direct_quotes_and_writes_watch_quotes() -> None:
    fetcher = FakeFetcher(quote_prices={"000001": 11.2, "000002": 12.3})
    cache = FakeCache(watches=[{"code": "000001"}, {"code": "000002"}])
    summary = _coordinator(cache, fetcher).run("startup")
    step = summary.stage_result("watch_quotes")
    assert fetcher.quote_calls == ["000001", "000002"]
    assert fetcher.spot_force_calls == []
    assert step is not None
    assert step.status == "success"
    assert step.fresh == 2
    record = cache.get_json("watch_quotes")
    assert record is not None
    prices = {item["code"]: item["price"] for item in record.payload}
    assert prices == {"000001": 11.2, "000002": 12.3}


def test_large_watchlist_uses_forced_market_quotes() -> None:
    fetcher = FakeFetcher()
    cache = FakeCache(watches=[{"code": str(index).zfill(6)} for index in range(1, 26)])
    summary = _coordinator(cache, fetcher).run("startup")
    step = summary.stage_result("watch_quotes")
    assert fetcher.quote_calls == []
    assert fetcher.spot_force_calls == [True]
    assert step is not None
    assert step.fresh == 25
    record = cache.get_json("watch_quotes")
    assert record is not None
    assert len(record.payload) == 25
    assert {item["code"] for item in record.payload} == {str(index).zfill(6) for index in range(1, 26)}


def test_cached_detail_marks_summary_partial() -> None:
    fetcher = FakeFetcher(updated_at=NOW - timedelta(hours=2))
    cache = FakeCache()
    summary = _coordinator(cache, fetcher).run("startup", current_code="000001")
    step = summary.stage_result("current_detail")
    assert step is not None
    assert step.status == "partial"
    assert step.fresh == 1
    assert step.cache == 4
    assert step.failed == 0
    assert summary.status == "partial"
    assert summary.message == "自选优先刷新完成，部分数据来自缓存；详情见刷新中心。"


def test_single_quote_failure_does_not_stop_run() -> None:
    fetcher = FakeFetcher(quote_failures={"000002"})
    cache = FakeCache(watches=[{"code": "000001"}, {"code": "000002"}, {"code": "000003"}])
    summary = _coordinator(cache, fetcher).run("startup")
    step = summary.stage_result("watch_quotes")
    assert summary.status == "partial"
    assert step is not None
    assert step.fresh == 2
    assert step.failed == 1
    assert any("000002" in error for error in step.errors)


def test_financial_refresh_uses_one_day_ttl() -> None:
    fresh_cache = FakeCache(
        records={"financial:000001": CacheRecord("financial:000001", {}, NOW - timedelta(hours=2))}
    )
    fresh_fetcher = FakeFetcher()
    _coordinator(fresh_cache, fresh_fetcher).run("startup", current_code="000001")
    assert fresh_fetcher.financial_force_calls[-1] == ("000001", False)

    stale_cache = FakeCache(
        records={"financial:000001": CacheRecord("financial:000001", {}, NOW - timedelta(days=2))}
    )
    stale_fetcher = FakeFetcher()
    _coordinator(stale_cache, stale_fetcher).run("startup", current_code="000001")
    assert stale_fetcher.financial_force_calls[-1] == ("000001", True)

    missing_fetcher = FakeFetcher()
    _coordinator(FakeCache(), missing_fetcher).run("startup", current_code="000001")
    assert missing_fetcher.financial_force_calls[-1] == ("000001", True)


def test_feature_refresh_forces_fresh_financial_data() -> None:
    cache = FakeCache(
        records={"financial:000001": CacheRecord("financial:000001", {}, NOW - timedelta(hours=2))}
    )
    fetcher = FakeFetcher()
    summary = _coordinator(cache, fetcher).run("feature", current_code="000001")
    assert fetcher.financial_force_calls[-1] == ("000001", True)
    step = summary.stage_result("current_detail")
    assert step is not None
    assert step.cache == 0
    assert step.fresh == 5


def test_auto_refresh_only_polls_quotes() -> None:
    fetcher = FakeFetcher()
    cache = FakeCache(watches=[{"code": "000001"}])
    summary = _coordinator(cache, fetcher).run("auto")
    step = summary.stage_result("watch_quotes")
    assert step is not None
    assert step.status == "success"
    assert step.fresh == 1
    assert summary.stage_result("current_detail") is None
    assert not fetcher.financial_force_calls


def run() -> None:
    test_prioritized_watch_codes()
    test_small_watchlist_uses_direct_quotes_and_writes_watch_quotes()
    test_large_watchlist_uses_forced_market_quotes()
    test_cached_detail_marks_summary_partial()
    test_single_quote_failure_does_not_stop_run()
    test_financial_refresh_uses_one_day_ttl()
    test_feature_refresh_forces_fresh_financial_data()
    test_auto_refresh_only_polls_quotes()
    print("[OK] DataRefreshCoordinator 离线回归通过")


if __name__ == "__main__":
    run()
