from __future__ import annotations

import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.alert_monitor import SHANGHAI_TZ, AlertMonitorCoordinator, TradingCalendar, TradingClock
from stock_tool.core.alert_service import AlertDraft, AlertService
from stock_tool.core.cache import CacheStore
from stock_tool.core.data_fetcher import QuoteSnapshot, _parse_sina_quote_batch, _parse_tencent_quote_batch
from stock_tool.core.models import StockQuote
from stock_tool.core.notify import MemoryNotificationChannel, Notifier


def _weekday_calendar(start: date, end: date) -> set[date]:
    days: set[date] = set()
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.add(cursor)
        cursor += timedelta(days=1)
    return days


class FakeBatchFetcher:
    def __init__(self, snapshots: dict[str, QuoteSnapshot], barrier: threading.Barrier | None = None) -> None:
        self.snapshots = snapshots
        self.calls: list[list[str]] = []
        self.barrier = barrier

    def quotes_by_codes(self, codes) -> dict[str, QuoteSnapshot]:
        self.calls.append(list(codes))
        if self.barrier is not None:
            self.barrier.wait(timeout=3)
        return {code: self.snapshots[code] for code in codes if code in self.snapshots}


def _snapshot(code: str, at: datetime, *, price: float = 11.0) -> QuoteSnapshot:
    return QuoteSnapshot(StockQuote(code, "样例", price=price, change_pct=3.0), "tencent", at, at)


def _coordinator(cache: CacheStore, fetcher: FakeBatchFetcher, now: datetime, channel: MemoryNotificationChannel | None = None) -> AlertMonitorCoordinator:
    return AlertMonitorCoordinator(
        AlertService(cache),
        fetcher,
        notifier=Notifier([channel]) if channel is not None else None,
        trading_clock=TradingClock(TradingCalendar(_weekday_calendar)),
        now_provider=lambda: now,
    )


def test_policy_migration_and_dynamic_lower_bound() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "cache.sqlite3")
        cache.set_setting("alert_interval_min", "5")
        coordinator = _coordinator(cache, FakeBatchFetcher({}), datetime(2026, 7, 10, 10, tzinfo=None))
        assert coordinator.policy().configured_interval_seconds == 300
        for index in range(8):
            cache.add_price_alert(f"000{index:03d}", "样例", "sell", 10)
        assert coordinator.update_policy(True, 15).effective_interval_seconds == 15
        for index in range(8, 11):
            cache.add_price_alert(f"000{index:03d}", "样例", "sell", 10)
        policy = coordinator.enforce_policy()
        assert policy.configured_interval_seconds == policy.effective_interval_seconds == 30
        assert "自动上调" in policy.adjustment_reason
        for index in range(11, 31):
            cache.add_price_alert(f"000{index:03d}", "样例", "sell", 10)
        assert coordinator.enforce_policy().effective_interval_seconds == 60
        for alert in cache.list_price_alerts()[20:]:
            cache.set_price_alert_enabled(alert.id, False)
        assert coordinator.enforce_policy().configured_interval_seconds == 60


def test_clock_boundaries_lunch_and_next_session() -> None:
    clock = TradingClock(TradingCalendar(_weekday_calendar))
    assert clock.session(datetime(2026, 7, 10, 9, 30)).is_trading
    assert not clock.session(datetime(2026, 7, 10, 11, 30)).is_trading
    assert clock.session(datetime(2026, 7, 10, 13, 0)).is_trading
    assert not clock.session(datetime(2026, 7, 10, 15, 0)).is_trading
    assert clock.next_check_at(datetime(2026, 7, 10, 12, 59, 59), 30).strftime("%H:%M:%S") == "13:00:00"
    assert clock.next_check_at(datetime(2026, 7, 10, 14, 59, 59), 30).strftime("%Y-%m-%d %H:%M:%S") == "2026-07-13 09:30:00"


def test_calendar_unavailable_is_fail_closed_for_automatic_and_manual_checks() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "cache.sqlite3")
        alert_id = cache.add_price_alert("000001", "样例", "sell", 10, repeat=True)
        before = cache.get_price_alert(alert_id)
        channel = MemoryNotificationChannel()
        fetcher = FakeBatchFetcher({"000001": _snapshot("000001", datetime(2026, 7, 10, 10, 0))})

        def unavailable_loader(_start: date, _end: date):
            raise RuntimeError("calendar source down")

        now = datetime(2026, 7, 10, 10, 0)
        coordinator = AlertMonitorCoordinator(
            AlertService(cache),
            fetcher,
            notifier=Notifier([channel]),
            trading_clock=TradingClock(TradingCalendar(unavailable_loader)),
            now_provider=lambda: now,
        )
        for manual in (False, True):
            result = coordinator.check(manual=manual)
            assert result.code == "calendar_unavailable"
            assert result.session.state == "calendar_unavailable" and not result.session.is_trading
            assert result.next_check_at == datetime(2026, 7, 10, 10, 5, tzinfo=SHANGHAI_TZ)
            assert result.checked_count == 0 and not result.matches and not result.triggers
            assert fetcher.calls == []
            assert not cache.list_alert_history() and not channel.messages
            after = cache.get_price_alert(alert_id)
            assert after is not None and before is not None
            assert (after.last_state, after.last_triggered_at, after.trigger_count) == (
                before.last_state,
                before.last_triggered_at,
                before.trigger_count,
            )


def test_empty_missing_and_malformed_calendar_are_unavailable() -> None:
    now = datetime(2026, 7, 10, 10, 0)
    loaders = (
        lambda _start, _end: set(),
        lambda _start, _end: {date(2026, 8, 3)},
        lambda _start, _end: {"not-a-date"},
    )
    for loader in loaders:
        session = TradingClock(TradingCalendar(loader)).session(now)
        assert session.state == "calendar_unavailable"
        assert not session.is_trading and not session.calendar_available


def test_stale_quote_and_after_close_preview_never_change_state() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "cache.sqlite3")
        alert_id = cache.add_price_alert("000001", "样例", "sell", 10, repeat=True)
        now = datetime(2026, 7, 10, 10, 0)
        stale = _snapshot("000001", now - timedelta(days=1))
        result = _coordinator(cache, FakeBatchFetcher({"000001": stale}), now).check(manual=False)
        assert result.stale_codes == ("000001",) and not result.triggers
        assert cache.get_price_alert(alert_id).last_state == 0
        channel = MemoryNotificationChannel()
        weekend = datetime(2026, 7, 11, 10, 0)
        preview = _coordinator(cache, FakeBatchFetcher({"000001": _snapshot("000001", weekend)}), weekend, channel).check(manual=True)
        assert preview.preview and len(preview.matches) == 1 and not preview.triggers
        assert not cache.list_alert_history() and not channel.messages and cache.get_price_alert(alert_id).last_state == 0


def test_known_holiday_and_weekend_skip_automatic_but_allow_read_only_preview() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "cache.sqlite3")
        alert_id = cache.add_price_alert("000001", "样例", "sell", 10, repeat=True)
        holiday = date(2026, 10, 1)

        def calendar_without_holiday(start: date, end: date) -> set[date]:
            return {day for day in _weekday_calendar(start, end) if day != holiday}

        now = datetime(2026, 10, 1, 10, 0)
        auto_fetcher = FakeBatchFetcher({"000001": _snapshot("000001", now)})
        automatic = AlertMonitorCoordinator(
            AlertService(cache),
            auto_fetcher,
            trading_clock=TradingClock(TradingCalendar(calendar_without_holiday)),
            now_provider=lambda: now,
        ).check(manual=False)
        assert automatic.session.state == "non_trading_day"
        assert auto_fetcher.calls == [] and automatic.checked_count == 0

        preview_fetcher = FakeBatchFetcher({"000001": _snapshot("000001", now)})
        channel = MemoryNotificationChannel()
        preview = AlertMonitorCoordinator(
            AlertService(cache),
            preview_fetcher,
            notifier=Notifier([channel]),
            trading_clock=TradingClock(TradingCalendar(calendar_without_holiday)),
            now_provider=lambda: now,
        ).check(manual=True)
        record = cache.get_price_alert(alert_id)
        assert preview.preview and len(preview.matches) == 1 and not preview.triggers
        assert preview_fetcher.calls == [["000001"]]
        assert record is not None and record.last_state == 0 and record.trigger_count == 0
        assert not cache.list_alert_history() and not channel.messages


def test_quote_freshness_accepts_only_current_non_future_snapshots() -> None:
    now = datetime(2026, 7, 10, 10, 0)
    cases = (
        ("fresh_zero", now, True),
        ("fresh_boundary", now - timedelta(seconds=120), True),
        ("too_old", now - timedelta(seconds=121), False),
        ("future", now + timedelta(seconds=1), False),
        ("cross_day", now - timedelta(days=1), False),
        ("missing_time", None, False),
    )
    for _label, quote_at, expected_fresh in cases:
        with tempfile.TemporaryDirectory() as raw:
            cache = CacheStore(Path(raw) / "cache.sqlite3")
            alert_id = cache.add_price_alert("000001", "样例", "sell", 10, repeat=True)
            snapshot = QuoteSnapshot(
                StockQuote("000001", "样例", price=11, change_pct=3),
                "tencent",
                quote_at,
                now,
            )
            result = _coordinator(cache, FakeBatchFetcher({"000001": snapshot}), now).check(manual=False)
            record = cache.get_price_alert(alert_id)
            assert record is not None
            if expected_fresh:
                assert result.checked_count == 1 and len(result.triggers) == 1
                assert record.trigger_count == 1 and len(cache.list_alert_history()) == 1
            else:
                assert result.checked_count == 0 and result.stale_codes == ("000001",)
                assert not result.triggers and record.last_state == 0 and record.trigger_count == 0
                assert not cache.list_alert_history()


def test_legacy_quote_without_real_timestamp_fails_closed() -> None:
    class LegacyFetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def quote_by_code(self, code: str) -> StockQuote:
            self.calls.append(code)
            return StockQuote(code, "样例", price=11, change_pct=3)

    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "cache.sqlite3")
        alert_id = cache.add_price_alert("000001", "样例", "sell", 10, repeat=True)
        fetcher = LegacyFetcher()
        result = AlertMonitorCoordinator(
            AlertService(cache),
            fetcher,
            trading_clock=TradingClock(TradingCalendar(_weekday_calendar)),
            now_provider=lambda: datetime(2026, 7, 10, 10, 0),
        ).check(manual=False)
        record = cache.get_price_alert(alert_id)
        assert fetcher.calls == ["000001"]
        assert result.stale_codes == ("000001",) and not result.triggers
        assert record is not None and record.last_state == 0 and record.trigger_count == 0
        assert not cache.list_alert_history()


def test_public_batch_timestamp_parsing_and_backoff() -> None:
    fetched_at = datetime(2026, 7, 10, 10, 0)
    tencent_fields = [""] * 40
    tencent_fields[1], tencent_fields[3], tencent_fields[4], tencent_fields[30], tencent_fields[32] = "腾讯样例", "10.5", "10", "20260710100000", "5"
    tencent = _parse_tencent_quote_batch(f'v_sz000001="{"~".join(tencent_fields)}";', {"000001"}, fetched_at)
    assert tencent["000001"].source == "tencent" and tencent["000001"].quote_at is not None
    sina_fields = [""] * 32
    sina_fields[0], sina_fields[2], sina_fields[3], sina_fields[30], sina_fields[31] = "新浪样例", "10", "10.5", "2026-07-10", "10:00:00"
    sina = _parse_sina_quote_batch(f'var hq_str_sz000002="{",".join(sina_fields)}";', {"000002"}, fetched_at)
    assert sina["000002"].source == "sina" and sina["000002"].quote_at is not None

    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "cache.sqlite3")
        cache.add_price_alert("000001", "样例", "sell", 10)
        now = datetime(2026, 7, 10, 10, 0)
        coordinator = _coordinator(cache, FakeBatchFetcher({}), now)
        assert coordinator.check(manual=False).policy.effective_interval_seconds == 60
        assert coordinator.check(manual=False).policy.effective_interval_seconds == 120
        assert coordinator.check(manual=False).policy.effective_interval_seconds == 300


def test_close_between_fetch_and_commit_does_not_trigger() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "cache.sqlite3")
        alert_id = cache.add_price_alert("000001", "样例", "sell", 10, repeat=True)
        ticks = iter((datetime(2026, 7, 10, 14, 59, 59), datetime(2026, 7, 10, 15, 0, 0)))
        coordinator = AlertMonitorCoordinator(
            AlertService(cache),
            FakeBatchFetcher({"000001": _snapshot("000001", datetime(2026, 7, 10, 14, 59, 59))}),
            trading_clock=TradingClock(TradingCalendar(_weekday_calendar)),
            now_provider=lambda: next(ticks),
        )
        result = coordinator.check(manual=False)
        assert not result.triggers and not cache.list_alert_history() and cache.get_price_alert(alert_id).last_state == 0


def test_two_coordinators_claim_one_trigger_and_one_notification() -> None:
    with tempfile.TemporaryDirectory() as raw:
        db_path = Path(raw) / "cache.sqlite3"
        first_cache = CacheStore(db_path)
        first_cache.add_price_alert("000001", "样例", "sell", 10, repeat=True)
        now = datetime(2026, 7, 10, 10, 0)
        barrier = threading.Barrier(2)
        channel_a, channel_b = MemoryNotificationChannel(), MemoryNotificationChannel()
        a = _coordinator(first_cache, FakeBatchFetcher({"000001": _snapshot("000001", now)}, barrier), now, channel_a)
        b = _coordinator(CacheStore(db_path), FakeBatchFetcher({"000001": _snapshot("000001", now)}, barrier), now, channel_b)
        results = []
        threads = [threading.Thread(target=lambda coordinator=coordinator: results.append(coordinator.check(manual=False))) for coordinator in (a, b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert len(results) == 2
        assert sum(len(item.triggers) for item in results) == 1
        assert len(CacheStore(db_path).list_alert_history()) == 1
        assert len(channel_a.messages) + len(channel_b.messages) == 1


def main() -> int:
    test_policy_migration_and_dynamic_lower_bound()
    test_clock_boundaries_lunch_and_next_session()
    test_calendar_unavailable_is_fail_closed_for_automatic_and_manual_checks()
    test_empty_missing_and_malformed_calendar_are_unavailable()
    test_stale_quote_and_after_close_preview_never_change_state()
    test_known_holiday_and_weekend_skip_automatic_but_allow_read_only_preview()
    test_quote_freshness_accepts_only_current_non_future_snapshots()
    test_legacy_quote_without_real_timestamp_fails_closed()
    test_public_batch_timestamp_parsing_and_backoff()
    test_close_between_fetch_and_commit_does_not_trigger()
    test_two_coordinators_claim_one_trigger_and_one_notification()
    print("[OK] 免费行情准实时预警协调器测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
