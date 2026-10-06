"""免费公开行情的准实时预警协调器。

本模块是 Qt 工作台调用的唯一监控入口：UI 只调用 ``check`` 并按返回的
``next_check_at`` 做单次调度，不能自行逐股拉取或直接推进预警状态机。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as daytime, timedelta
import threading
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

from stock_tool.core.alert_service import AlertCheckResult, AlertMatch, AlertService, AlertTrigger
from stock_tool.core.data_fetcher import QuoteSnapshot
from stock_tool.core.models import StockQuote
from stock_tool.core.notify import Notifier, NotifyMessage


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SUPPORTED_INTERVAL_SECONDS = (15, 30, 60, 5 * 60, 10 * 60, 30 * 60)
FREE_SOURCE_NOTICE = (
    "免费行情准实时提醒\n"
    "当前使用腾讯/新浪公开行情快照，不是交易所逐笔或推送行情。实际延迟受网络、数据源更新、限流和软件运行状态影响，"
    "可能出现延迟、缺失或短时不可用，不保证精准触达。软件退出后停止提醒。\n"
    "数量与最低间隔：1–10只为15秒，11–30只为30秒，31只以上为60秒。"
)


class QuoteBatchFetcher(Protocol):
    def quotes_by_codes(self, codes: list[str] | set[str] | tuple[str, ...]) -> dict[str, QuoteSnapshot]:
        ...


@dataclass(frozen=True)
class TradingSession:
    state: str
    is_trading: bool
    calendar_available: bool


@dataclass(frozen=True)
class MonitorPolicy:
    enabled: bool
    configured_interval_seconds: int
    effective_interval_seconds: int
    covered_codes: int
    minimum_interval_seconds: int
    failure_streak: int
    adjustment_reason: str = ""


@dataclass(frozen=True)
class AlertMonitorResult:
    checked_count: int
    matches: tuple[AlertMatch, ...] = ()
    triggers: tuple[AlertTrigger, ...] = ()
    failed_codes: tuple[str, ...] = ()
    stale_codes: tuple[str, ...] = ()
    quote_sources: Mapping[str, str] | None = None
    quote_times: Mapping[str, datetime] | None = None
    session: TradingSession = TradingSession("non_trading_day", False, False)
    next_check_at: datetime | None = None
    last_check_at: datetime | None = None
    policy: MonitorPolicy | None = None
    preview: bool = False
    code: str = "ok"
    message: str = ""

    @property
    def source_label(self) -> str:
        sources = {value for value in (self.quote_sources or {}).values() if value}
        labels = {"tencent": "腾讯", "sina": "新浪", "legacy": "免费快照"}
        return "/".join(labels.get(item, item) for item in sorted(sources)) or "免费行情快照"


def as_shanghai(value: datetime | None = None) -> datetime:
    current = value or datetime.now(SHANGHAI_TZ)
    if current.tzinfo is None:
        return current.replace(tzinfo=SHANGHAI_TZ)
    return current.astimezone(SHANGHAI_TZ)


def minimum_interval_seconds(covered_codes: int) -> int:
    if covered_codes <= 10:
        return 15
    if covered_codes <= 30:
        return 30
    return 60


class TradingCalendar:
    """按日缓存本月和下月交易日历；源不可用时必须安全失败关闭。"""

    def __init__(self, loader: Callable[[date, date], set[date]] | None = None) -> None:
        self._loader = loader or self._load_baostock_days
        self._months: dict[tuple[int, int], set[date] | None] = {}
        self._refreshed_on: date | None = None

    @staticmethod
    def _month_bounds(value: date) -> tuple[date, date]:
        start = value.replace(day=1)
        if start.month == 12:
            next_month = date(start.year + 1, 1, 1)
        else:
            next_month = date(start.year, start.month + 1, 1)
        return start, next_month - timedelta(days=1)

    @staticmethod
    def _next_month(value: date) -> date:
        return date(value.year + 1, 1, 1) if value.month == 12 else date(value.year, value.month + 1, 1)

    @staticmethod
    def _load_baostock_days(start: date, end: date) -> set[date]:
        """复用 PIT 的 Baostock 查询通道，避免为预警另建一条日历链路。"""
        from stock_tool.core.pit_providers import BaostockPITProvider

        provider = BaostockPITProvider(timeout_sec=5)
        try:
            rows = provider._query("trade_dates", start_date=start.isoformat(), end_date=end.isoformat())
        finally:
            provider.close()
        return {
            datetime.strptime(str(row.get("calendar_date", "")), "%Y-%m-%d").date()
            for row in rows
            if str(row.get("is_trading_day", "")) == "1" and row.get("calendar_date")
        }

    def _refresh_months(self, today: date) -> None:
        if self._refreshed_on == today:
            return
        self._months.clear()
        current_month = today.replace(day=1)
        next_month = self._next_month(today).replace(day=1)
        current_start, _ = self._month_bounds(current_month)
        _, next_end = self._month_bounds(next_month)
        try:
            loaded_days = set(self._loader(current_start, next_end))
            if not loaded_days or not all(isinstance(item, date) for item in loaded_days):
                raise ValueError("交易日历返回为空或结构异常")
            current_days = {
                item for item in loaded_days if (item.year, item.month) == (current_month.year, current_month.month)
            }
            next_days = {
                item for item in loaded_days if (item.year, item.month) == (next_month.year, next_month.month)
            }
            self._months[(current_month.year, current_month.month)] = current_days or None
            self._months[(next_month.year, next_month.month)] = next_days or None
        except Exception:
            self._months[(current_month.year, current_month.month)] = None
            self._months[(next_month.year, next_month.month)] = None
        self._refreshed_on = today

    def is_trading_day(self, value: date, *, today: date | None = None) -> tuple[bool, bool]:
        self._refresh_months(today or value)
        calendar = self._months.get((value.year, value.month))
        if calendar is None:
            return False, False
        return value in calendar, True


class TradingClock:
    morning = (daytime(9, 30), daytime(11, 30))
    afternoon = (daytime(13, 0), daytime(15, 0))

    def __init__(self, calendar: TradingCalendar | None = None) -> None:
        self.calendar = calendar or TradingCalendar()

    def session(self, now: datetime | None = None) -> TradingSession:
        current = as_shanghai(now)
        trading_day, calendar_available = self.calendar.is_trading_day(current.date(), today=current.date())
        if not calendar_available:
            return TradingSession("calendar_unavailable", False, False)
        if not trading_day:
            return TradingSession("non_trading_day", False, calendar_available)
        current_time = current.timetz().replace(tzinfo=None)
        if self.morning[0] <= current_time < self.morning[1] or self.afternoon[0] <= current_time < self.afternoon[1]:
            return TradingSession("trading", True, calendar_available)
        if self.morning[1] <= current_time < self.afternoon[0]:
            return TradingSession("lunch_break", False, calendar_available)
        if current_time < self.morning[0]:
            return TradingSession("before_open", False, calendar_available)
        return TradingSession("after_close", False, calendar_available)

    def _next_trading_day(self, value: date) -> date:
        candidate = value + timedelta(days=1)
        for _ in range(370):
            if self.calendar.is_trading_day(candidate, today=value)[0]:
                return candidate
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def _at(value: date, clock_time: daytime) -> datetime:
        return datetime.combine(value, clock_time, tzinfo=SHANGHAI_TZ)

    def next_check_at(self, now: datetime, interval_seconds: int) -> datetime:
        current = as_shanghai(now)
        session = self.session(current)
        if session.state == "calendar_unavailable":
            return current + timedelta(minutes=5)
        if session.state == "before_open":
            return self._at(current.date(), self.morning[0])
        if session.state == "lunch_break":
            return self._at(current.date(), self.afternoon[0])
        if session.state in {"after_close", "non_trading_day"}:
            return self._at(self._next_trading_day(current.date()), self.morning[0])

        start_time, end_time = self.morning if current.time() < self.morning[1] else self.afternoon
        start = self._at(current.date(), start_time)
        end = self._at(current.date(), end_time)
        elapsed = max(0.0, (current - start).total_seconds())
        next_slot = start + timedelta(seconds=(int(elapsed // interval_seconds) + 1) * interval_seconds)
        if next_slot < end:
            return next_slot
        if end_time == self.morning[1]:
            return self._at(current.date(), self.afternoon[0])
        return self._at(self._next_trading_day(current.date()), self.morning[0])


class AlertMonitorCoordinator:
    """协调免费批量行情、交易时钟、状态提交和通知的共享监控入口。"""

    def __init__(
        self,
        alert_service: AlertService,
        fetcher: QuoteBatchFetcher | Any,
        *,
        notifier: Notifier | None = None,
        trading_clock: TradingClock | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.alert_service = alert_service
        self.fetcher = fetcher
        self.notifier = notifier
        self.trading_clock = trading_clock or TradingClock()
        self._now = now_provider or (lambda: datetime.now(SHANGHAI_TZ))
        self._run_lock = threading.Lock()

    @property
    def cache(self):
        return self.alert_service.cache

    def _configured_interval(self) -> int:
        raw = self.cache.get_setting("alert_interval_seconds", "")
        try:
            interval = int(raw)
        except (TypeError, ValueError):
            interval = 0
        if interval in SUPPORTED_INTERVAL_SECONDS:
            return interval
        # 兼容已安装用户的分钟设置，绝不把既有 1/5/10/30 分钟悄悄缩短。
        legacy = self.cache.get_setting("alert_interval_min", "")
        try:
            legacy_seconds = int(legacy) * 60
        except (TypeError, ValueError):
            legacy_seconds = 0
        interval = legacy_seconds if legacy_seconds in SUPPORTED_INTERVAL_SECONDS else 30
        self.cache.set_setting("alert_interval_seconds", str(interval))
        return interval

    def _failure_streak(self) -> int:
        try:
            return max(0, int(self.cache.get_setting("alert_monitor_failure_streak", "0")))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _backoff_seconds(streak: int) -> int:
        if streak <= 0:
            return 0
        if streak == 1:
            return 60
        if streak == 2:
            return 120
        return 300

    def policy(self) -> MonitorPolicy:
        alerts = self.alert_service.list_alerts(enabled_only=True)
        covered = len({item.code for item in alerts})
        configured = self._configured_interval()
        minimum = minimum_interval_seconds(covered)
        failure_streak = self._failure_streak()
        effective = max(configured, minimum, self._backoff_seconds(failure_streak))
        reasons: list[str] = []
        adjustment_note = self.cache.get_setting("alert_interval_adjustment_note", "").strip()
        if adjustment_note:
            reasons.append(adjustment_note)
        if failure_streak:
            reasons.append(f"公开行情连续完整失败 {failure_streak} 次，退避至至少 {self._backoff_seconds(failure_streak)} 秒")
        return MonitorPolicy(
            enabled=self.cache.get_setting("alert_monitor_enabled", "0") == "1",
            configured_interval_seconds=configured,
            effective_interval_seconds=effective,
            covered_codes=covered,
            minimum_interval_seconds=minimum,
            failure_streak=failure_streak,
            adjustment_reason="；".join(reasons),
        )

    def update_policy(self, enabled: bool, interval_seconds: int) -> MonitorPolicy:
        try:
            requested = int(interval_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("监控间隔只允许 15 秒、30 秒、1/5/10/30 分钟") from exc
        if requested not in SUPPORTED_INTERVAL_SECONDS:
            raise ValueError("监控间隔只允许 15 秒、30 秒、1/5/10/30 分钟")
        covered = len({item.code for item in self.alert_service.list_alerts(enabled_only=True)})
        minimum = minimum_interval_seconds(covered)
        configured = max(requested, minimum)
        self.cache.set_setting("alert_monitor_enabled", "1" if enabled else "0")
        self.cache.set_setting("alert_interval_seconds", str(configured))
        self.cache.set_setting(
            "alert_interval_adjustment_note",
            f"覆盖 {covered} 只股票，已将 {requested} 秒自动上调为 {configured} 秒" if configured > requested else "",
        )
        # 给仍在读取旧设置的低版本入口保留分钟值；秒级值不能伪装为 0 分钟。
        if configured >= 60:
            self.cache.set_setting("alert_interval_min", str(configured // 60))
        return self.policy()

    def enforce_policy(self) -> MonitorPolicy:
        """预警启停、创建、删除后应用数量下限；数量减少时保持用户当前配置。"""
        current = self._configured_interval()
        covered = len({item.code for item in self.alert_service.list_alerts(enabled_only=True)})
        minimum = minimum_interval_seconds(covered)
        if current < minimum:
            self.cache.set_setting("alert_interval_seconds", str(minimum))
            self.cache.set_setting("alert_interval_adjustment_note", f"覆盖 {covered} 只股票，已将间隔自动上调为 {minimum} 秒")
            if minimum >= 60:
                self.cache.set_setting("alert_interval_min", str(minimum // 60))
        return self.policy()

    @staticmethod
    def _snapshot_is_fresh(snapshot: QuoteSnapshot, now: datetime) -> bool:
        quote_at = snapshot.quote_at
        if not isinstance(quote_at, datetime):
            return False
        try:
            stamp = as_shanghai(quote_at)
            current = as_shanghai(now)
        except (AttributeError, TypeError, ValueError):
            return False
        age = (current - stamp).total_seconds()
        return stamp.date() == current.date() and 0 <= age <= 120

    def _fetch_snapshots(
        self,
        codes: list[str],
        now: datetime,
        *,
        quote_provider: Any | None = None,
    ) -> dict[str, QuoteSnapshot]:
        provider = quote_provider or self.fetcher
        method = getattr(provider, "quotes_by_codes", None)
        if callable(method):
            return dict(method(codes) or {})
        # 仅兼容旧的测试替身；生产 DataFetcher 必须具备批量方法。
        fallback: dict[str, QuoteSnapshot] = {}
        for code in codes:
            try:
                quote = provider.quote_by_code(code)
            except Exception:
                quote = None
            if quote is not None:
                # 旧 quote_by_code() 只返回 StockQuote，没有真实行情时间；宁可拒绝触发，
                # 也不能把抓取时间伪装成行情时间而放行未来/旧行情。
                if isinstance(quote, QuoteSnapshot):
                    fallback[code] = quote
                else:
                    fallback[code] = QuoteSnapshot(quote, "legacy", None, now)
        return fallback

    def _record_batch_outcome(self, *, automatic: bool, fresh_count: int, expected_count: int) -> None:
        if not automatic or expected_count <= 0:
            return
        if fresh_count == 0:
            self.cache.set_setting("alert_monitor_failure_streak", str(self._failure_streak() + 1))
        elif fresh_count == expected_count:
            self.cache.set_setting("alert_monitor_failure_streak", "0")

    def _send_notifications(self, triggers: tuple[AlertTrigger, ...], result: AlertMonitorResult) -> None:
        if not triggers or self.notifier is None:
            return
        source = result.source_label
        timestamps = sorted((result.quote_times or {}).values())
        quote_time = timestamps[-1].strftime("%H:%M:%S") if timestamps else "未知"
        lines = ["免费快照 · 数据整理与研究辅助，不构成投资建议。"]
        lines.extend(trigger.message for trigger in triggers[:5])
        lines.append(f"来源：{source}；行情时间：{quote_time}")
        self.notifier.send(
            NotifyMessage(
                title="价格预警触发（免费快照）",
                markdown="\n".join(["# 价格预警触发（免费快照）", "", *lines]),
                plaintext="\n".join(lines),
                candidate_count=len(triggers),
            )
        )

    def check(self, *, manual: bool, quote_provider: Any | None = None) -> AlertMonitorResult:
        now = as_shanghai(self._now())
        policy = self.enforce_policy()
        session = self.trading_clock.session(now)
        next_check_at = self.trading_clock.next_check_at(now, policy.effective_interval_seconds)
        if not self._run_lock.acquire(blocking=False):
            return AlertMonitorResult(0, session=session, next_check_at=next_check_at, last_check_at=now, policy=policy, code="already_running", message="预警检查仍在运行。")
        try:
            if not session.calendar_available:
                return AlertMonitorResult(
                    0,
                    session=session,
                    next_check_at=next_check_at,
                    last_check_at=now,
                    policy=policy,
                    code="calendar_unavailable",
                    message="交易日历不可用，预警检查已暂停；将在 5 分钟后重试。",
                )
            alerts = self.alert_service.list_alerts(enabled_only=True)
            codes = list(dict.fromkeys(item.code for item in alerts))
            if not codes:
                return AlertMonitorResult(0, session=session, next_check_at=next_check_at, last_check_at=now, policy=policy, message="没有启用中的预警。")
            if not manual and not session.is_trading:
                return AlertMonitorResult(0, session=session, next_check_at=next_check_at, last_check_at=now, policy=policy, message="当前非交易时段，自动检查暂停。")

            snapshots = self._fetch_snapshots(codes, now, quote_provider=quote_provider)
            quote_sources = {code: item.source for code, item in snapshots.items()}
            quote_times = {code: as_shanghai(item.quote_at) for code, item in snapshots.items() if item.quote_at is not None}
            stale_codes = tuple(code for code, item in snapshots.items() if not self._snapshot_is_fresh(item, now))
            fresh = {code: item for code, item in snapshots.items() if code not in stale_codes}
            failed_codes = tuple(code for code in codes if code not in snapshots)
            quotes: dict[str, StockQuote] = {code: item.quote for code, item in fresh.items()}
            matches = self.alert_service.match_alerts(alerts, quotes)

            commit_now = as_shanghai(self._now())
            commit_session = self.trading_clock.session(commit_now)
            preview = manual and not session.is_trading
            can_commit = not preview and commit_session.is_trading
            check_result = AlertCheckResult(len(quotes), failed_codes=failed_codes, matches=matches, stale_codes=stale_codes)
            if can_commit:
                check_result = self.alert_service.check_alerts(
                    quotes,
                    now=commit_now,
                    quote_sources=quote_sources,
                    quote_times=quote_times,
                )
            self._record_batch_outcome(automatic=not manual, fresh_count=len(fresh), expected_count=len(codes))
            policy = self.policy()
            next_check_at = self.trading_clock.next_check_at(commit_now, policy.effective_interval_seconds)
            result = AlertMonitorResult(
                checked_count=len(quotes),
                matches=matches,
                triggers=check_result.triggers if can_commit else (),
                failed_codes=failed_codes,
                stale_codes=stale_codes,
                quote_sources=quote_sources,
                quote_times=quote_times,
                session=commit_session,
                next_check_at=next_check_at,
                last_check_at=commit_now,
                policy=policy,
                preview=preview,
                message=("这是收盘快照预览，不会记录为触发或发送通知。" if preview else ""),
            )
            if can_commit:
                self._send_notifications(result.triggers, result)
            return result
        finally:
            self._run_lock.release()
