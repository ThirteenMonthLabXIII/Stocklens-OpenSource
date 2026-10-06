from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.alert_monitor import TradingCalendar, TradingClock, as_shanghai
from stock_tool.core.cache import CacheStore
from stock_tool.core.data_fetcher import QuoteSnapshot
from stock_tool.core.models import StockQuote
from stock_tool.core.performance_loop import ObservationSample, PriceBar, PriceWindow
from stock_tool.core.review_automation import ReviewAutomationCoordinator
from stock_tool.core.review_workflow import ReviewWorkflowService


class FakePerformanceStore:
    def __init__(self, samples: list[ObservationSample]) -> None:
        self.samples = samples
        self.outcomes = []

    def list_due_samples(self, asof_date: str | None = None, limit: int = 200):
        return self.samples[:limit]

    def record_outcome(self, outcome) -> None:
        if not any(item.sample_id == outcome.sample_id for item in self.outcomes):
            self.outcomes.append(outcome)


class FakeQuoteProvider:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def quotes_by_codes(self, codes):
        return {code: self.snapshots[code] for code in codes if code in self.snapshots}


class FakePriceSource:
    def __init__(self, windows):
        self.windows = windows

    def price_window(self, code, start_date, horizon_days):
        return self.windows.get(code, PriceWindow(code, "fixture", []))

    def benchmark_window(self, start_date, horizon_days):
        return self.windows.get("000300", PriceWindow("000300", "fixture", []))

    def industry_return(self, industry, start_date, horizon_days):
        return 5.0


class MissingComparatorPriceSource(FakePriceSource):
    def benchmark_window(self, start_date, horizon_days):
        return PriceWindow("000300", "fixture", [])

    def industry_return(self, industry, start_date, horizon_days):
        return None


class FailingPriceSource(FakePriceSource):
    def __init__(self):
        super().__init__({})
        self.calls = 0

    def price_window(self, code, start_date, horizon_days):
        self.calls += 1
        raise RuntimeError("fixture price source unavailable")


def _calendar() -> TradingCalendar:
    return TradingCalendar(loader=lambda _start, _end: {
        datetime(2026, 7, 9).date(),
        datetime(2026, 7, 10).date(),
    })


def _sample(sample_id: int, code: str, sample_date: str, due_date: str) -> ObservationSample:
    return ObservationSample(
        id=sample_id,
        result_id=1,
        run_id=1,
        horizon_days=3,
        sample_date=sample_date,
        due_date=due_date,
        code=code,
        name="样例",
        entry_price=10.0,
        rank_order=1,
        local_score=80.0,
        industry="银行",
        metrics={},
        score_breakdown={},
        ai_score=None,
        ai_level="",
        created_at="2026-07-01T09:00:00",
    )


def _coordinator(cache, performance_store):
    service = ReviewWorkflowService(cache, performance_store)
    return ReviewAutomationCoordinator(
        cache,
        service,
        performance_store,
        trading_clock=TradingClock(_calendar()),
        app_version="test",
    )


def test_defaults_and_cross_process_claim() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "stock_tool.sqlite3")
        assert cache.get_setting("review_automation_enabled") == "1"
        tokens: list[int | None] = []

        def claim() -> None:
            tokens.append(cache.claim_review_automation_run(
                run_key="review_automation", trigger="timer", owner_token="x" + str(len(tokens)),
                business_date="2026-07-10", started_at="2026-07-10T10:00:00+08:00", app_version="test",
            ))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(item is not None for item in tokens) == 1

        stale = cache.latest_review_automation_run()
        assert stale is not None
        cache.finish_review_automation_run(stale.id, stale.owner_token, status="running", finished_at="2026-07-10T09:00:00+08:00")
        takeover = cache.claim_review_automation_run(
            run_key="review_automation", trigger="startup", owner_token="new",
            business_date="2026-07-10", started_at="2026-07-10T10:00:00+08:00", app_version="test",
        )
        assert takeover is not None
        assert cache.list_review_automation_runs(limit=3)[1].status == "interrupted"


def test_tracking_priority_and_freshness() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "stock_tool.sqlite3")
        service = ReviewWorkflowService(cache)
        created = service.create_tracking_task(
            code="000001", name="样例", entry_price=10, fail_price=8, target_price=12,
            due_date="2026-07-09", actor="test",
        )
        assert created.ok
        coordinator = _coordinator(cache, FakePerformanceStore([]))
        now = datetime(2026, 7, 10, 10, 0)
        quote = QuoteSnapshot(StockQuote("000001", "样例", price=12.5), "fixture", now, now)
        report = coordinator.run_once(
            trigger="startup", quote_provider=FakeQuoteProvider({"000001": quote}),
            price_source=FakePriceSource({}), actor="test", now=now,
        )
        assert report.status == "success"
        task = service.list_tracking_tasks({"include_closed": True})[0]
        assert task.status == "closed" and task.result == "达到目标观察条件"

        created = service.create_tracking_task(code="000002", name="缺失", entry_price=10, due_date="2026-07-09", actor="test")
        assert created.ok
        stale = QuoteSnapshot(StockQuote("000002", "缺失", price=10), "fixture", now - timedelta(seconds=121), now)
        report = coordinator.run_once(
            trigger="manual", quote_provider=FakeQuoteProvider({"000002": stale}),
            price_source=FakePriceSource({}), actor="test", force_tracking=True, now=now + timedelta(minutes=1),
        )
        assert report.tracking_missing_data == 1 and report.tracking_closed == 0
        task = next(item for item in service.list_tracking_tasks({"include_closed": True}) if item.code == "000002")
        assert task.status == "open"

        created = service.create_tracking_task(
            code="000005", name="今日到期", entry_price=10, due_date="2026-07-10", actor="test"
        )
        assert created.ok
        current_quote = QuoteSnapshot(
            StockQuote("000005", "今日到期", price=10), "fixture", now, now
        )
        report = coordinator.run_once(
            trigger="manual", quote_provider=FakeQuoteProvider({"000005": current_quote}),
            price_source=FakePriceSource({}), actor="test", force_tracking=True, now=now,
        )
        assert report.tracking_closed == 1
        task = next(item for item in service.list_tracking_tasks({"include_closed": True}) if item.code == "000005")
        assert task.result == "到期未触发"


def test_tracking_and_settlement_cooldowns_are_independent() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "stock_tool.sqlite3")
        coordinator = _coordinator(cache, FakePerformanceStore([]))
        provider = FakeQuoteProvider({})
        source = FakePriceSource({})
        first = coordinator.run_once(
            trigger="startup", quote_provider=provider, price_source=source,
            actor="test", now=datetime(2026, 7, 10, 10, 0),
        )
        assert first.tracking_status == "success" and first.settlement_status == "success"

        quarter = coordinator.run_once(
            trigger="timer", quote_provider=provider, price_source=source,
            actor="test", now=datetime(2026, 7, 10, 10, 15),
        )
        assert quarter.tracking_status == "success" and quarter.settlement_status == "skipped"

        half = coordinator.run_once(
            trigger="timer", quote_provider=provider, price_source=source,
            actor="test", now=datetime(2026, 7, 10, 10, 30),
        )
        assert half.tracking_status == "success" and half.settlement_status == "skipped"

        hour = coordinator.run_once(
            trigger="timer", quote_provider=provider, price_source=source,
            actor="test", now=datetime(2026, 7, 10, 11, 0),
        )
        assert hour.settlement_status == "success"


def test_settlement_uses_asof_and_keeps_missing_price_retryable() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "stock_tool.sqlite3")
        samples = [_sample(1, "000003", "2026-07-01", "2026-07-09")]
        perf = FakePerformanceStore(samples)
        coordinator = _coordinator(cache, perf)
        bars = [
            PriceBar("2026-07-01", 10, 10.5, 9.5),
            PriceBar("2026-07-02", 10.5, 11, 10),
            PriceBar("2026-07-03", 11, 11.5, 10.5),
            PriceBar("2026-07-06", 11.5, 12, 11),
            PriceBar("2026-07-10", 20, 20.5, 19.5),  # future bar must be clipped
        ]
        report = coordinator.run_once(
            trigger="startup", quote_provider=FakeQuoteProvider({}),
            price_source=FakePriceSource({
                "000003": PriceWindow("000003", "fixture", bars),
                "000300": PriceWindow("000300", "fixture", bars),
            }),
            actor="test", now=datetime(2026, 7, 10, 16, 0),
        )
        assert report.valid_settled == 1
        assert perf.outcomes[0].exit_price == 11.5

        perf.samples = [_sample(2, "000004", "2026-07-09", "2026-07-09")]
        report = coordinator.run_once(
            trigger="manual", quote_provider=FakeQuoteProvider({}),
            price_source=FakePriceSource({"000004": PriceWindow("000004", "fixture", [])}),
            actor="test", force_settlement=True, now=datetime(2026, 7, 10, 16, 1),
        )
        assert report.retryable_count == 1 and not any(item.sample_id == 2 for item in perf.outcomes)


def test_settlement_quality_and_retry_schedule() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "stock_tool.sqlite3")
        perf = FakePerformanceStore([_sample(3, "000006", "2026-07-01", "2026-07-09")])
        coordinator = _coordinator(cache, perf)
        bars = [
            PriceBar("2026-07-01", 10, 10.5, 9.5),
            PriceBar("2026-07-02", 10.5, 11, 10),
            PriceBar("2026-07-03", 11, 11.5, 10.5),
            PriceBar("2026-07-06", 11.5, 12, 11),
        ]
        quality = coordinator.run_once(
            trigger="startup", quote_provider=FakeQuoteProvider({}),
            price_source=MissingComparatorPriceSource({"000006": PriceWindow("000006", "fixture", bars)}),
            actor="test", now=datetime(2026, 7, 10, 16, 0),
        )
        assert quality.invalid_settled == 1 and quality.valid_settled == 0
        assert "missing_benchmark" in perf.outcomes[0].data_quality
        assert "missing_industry" in perf.outcomes[0].data_quality

        perf.samples = [_sample(4, "000007", "2026-07-01", "2026-07-09")]
        failing = FailingPriceSource()
        base = as_shanghai(datetime(2026, 7, 10, 16, 0))
        first = coordinator.run_once(
            trigger="manual", quote_provider=FakeQuoteProvider({}), price_source=failing,
            actor="test", force_settlement=True, now=base,
        )
        retry_1 = cache.get_review_automation_retry(4)
        assert first.retryable_count == 1 and retry_1 is not None and retry_1[0] == 1
        assert datetime.fromisoformat(retry_1[1]) - base == timedelta(minutes=1)

        second = coordinator.run_once(
            trigger="manual", quote_provider=FakeQuoteProvider({}), price_source=failing,
            actor="test", force_settlement=True, now=base + timedelta(minutes=1),
        )
        retry_2 = cache.get_review_automation_retry(4)
        assert second.retryable_count == 1 and retry_2 is not None and retry_2[0] == 2
        assert datetime.fromisoformat(retry_2[1]) - (base + timedelta(minutes=1)) == timedelta(minutes=5)

        third = coordinator.run_once(
            trigger="manual", quote_provider=FakeQuoteProvider({}), price_source=failing,
            actor="test", force_settlement=True, now=base + timedelta(minutes=6),
        )
        retry_3 = cache.get_review_automation_retry(4)
        assert third.retryable_count == 1 and retry_3 is not None and retry_3[0] == 3
        assert datetime.fromisoformat(retry_3[1]) - (base + timedelta(minutes=6)) == timedelta(minutes=15)


if __name__ == "__main__":
    test_defaults_and_cross_process_claim()
    test_tracking_priority_and_freshness()
    test_tracking_and_settlement_cooldowns_are_independent()
    test_settlement_uses_asof_and_keeps_missing_price_retryable()
    test_settlement_quality_and_retry_schedule()
    print("test_review_automation passed")
