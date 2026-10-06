"""本地自动复盘协调器。

该模块只编排业务，不依赖 Qt，也不在内部创建行情客户端。Qt 负责把已有的
行情提供器和价格源传入后台线程；本模块负责业务日期、运行抢占、观察复核、
样本结算、重试和可观察的运行报告。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import secrets
import sqlite3
from typing import Any, Literal, Mapping
from zoneinfo import ZoneInfo

from stock_tool import __version__
from stock_tool.core.alert_monitor import TradingCalendar, TradingClock, as_shanghai
from stock_tool.core.cache import CacheStore
from stock_tool.core.data_fetcher import QuoteSnapshot
from stock_tool.core.models import StockQuote
from stock_tool.core.performance_loop import (
    ObservationSettlementService,
    PerformanceStore,
    PriceSource,
)
from stock_tool.core.review_workflow import ReviewWorkflowService


AutomationTrigger = Literal["startup", "timer", "manual"]
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
RUN_KEY = "review_automation"


@dataclass(frozen=True)
class AutomationPolicy:
    enabled: bool = True
    tracking_interval_minutes: int = 15
    settlement_interval_minutes: int = 60
    stale_run_minutes: int = 30
    quote_max_age_seconds: int = 120
    batch_size: int = 200

    @classmethod
    def from_cache(cls, cache: CacheStore) -> "AutomationPolicy":
        def integer(key: str, default: int, minimum: int = 1) -> int:
            try:
                return max(minimum, int(cache.get_setting(key, str(default))))
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=cache.get_setting("review_automation_enabled", "1") == "1",
            tracking_interval_minutes=integer("review_tracking_interval_minutes", 15),
            settlement_interval_minutes=integer("review_settlement_interval_minutes", 60),
            stale_run_minutes=integer("review_automation_stale_minutes", 30),
            quote_max_age_seconds=integer("review_quote_max_age_seconds", 120),
            batch_size=integer("review_automation_batch_size", 200, minimum=1),
        )


@dataclass(frozen=True)
class AutomationPhaseReport:
    status: str = "skipped"
    scanned: int = 0
    reviewed: int = 0
    closed: int = 0
    missing_data: int = 0
    settled: int = 0
    valid_settled: int = 0
    invalid_settled: int = 0
    retryable_count: int = 0
    errors: tuple[str, ...] = ()
    next_retry_at: datetime | None = None


@dataclass(frozen=True)
class AutomationRunReport:
    run_id: int | None
    status: str
    trigger: AutomationTrigger
    effective_asof_date: str
    tracking_scanned: int = 0
    tracking_reviewed: int = 0
    tracking_closed: int = 0
    tracking_missing_data: int = 0
    samples_scanned: int = 0
    samples_settled: int = 0
    valid_settled: int = 0
    invalid_settled: int = 0
    retryable_count: int = 0
    error_summary: str = ""
    next_tracking_at: datetime | None = None
    next_settlement_at: datetime | None = None
    next_run_at: datetime | None = None
    next_retry_at: datetime | None = None
    tracking_status: str = "skipped"
    settlement_status: str = "skipped"


def _iso(value: datetime) -> str:
    return as_shanghai(value).isoformat(timespec="seconds")


def _parse_datetime(value: str) -> datetime | None:
    try:
        return as_shanghai(datetime.fromisoformat(value)) if value else None
    except (TypeError, ValueError):
        return None


def _fresh_quote(value: Any, now: datetime, max_age_seconds: int) -> StockQuote | None:
    if not isinstance(value, QuoteSnapshot):
        return None
    if not isinstance(value.quote_at, datetime) or value.quote is None or value.quote.price is None:
        return None
    quote_at = as_shanghai(value.quote_at)
    current = as_shanghai(now)
    age = (current - quote_at).total_seconds()
    if quote_at.date() != current.date() or age < 0 or age > max(1, int(max_age_seconds)):
        return None
    return value.quote


def resolve_recent_complete_trading_day(now: datetime, calendar: TradingCalendar) -> str:
    """解析上海时区下最近完整交易日；日历不可用时严格失败关闭。"""
    current = as_shanghai(now)
    cutoff = time(15, 0)
    start = current.date() if current.time() >= cutoff else current.date() - timedelta(days=1)
    loader = getattr(calendar, "_loader", None)
    if callable(loader):
        try:
            loaded = set(loader(start - timedelta(days=370), start))
        except Exception as exc:
            raise RuntimeError(f"交易日历不可用：{exc}") from exc
        if not loaded or not all(isinstance(item, date) for item in loaded):
            raise RuntimeError("交易日历不可用：返回为空或结构异常")
        candidates = [item for item in loaded if item <= start]
        if candidates:
            return max(candidates).isoformat()
        raise RuntimeError("交易日历不可用：找不到最近完整交易日")

    candidate = start
    for _ in range(370):
        trading, available = calendar.is_trading_day(candidate, today=current.date())
        if not available:
            raise RuntimeError("交易日历不可用，未使用系统日期强行结算")
        if trading:
            return candidate.isoformat()
        candidate -= timedelta(days=1)
    raise RuntimeError("交易日历不可用：回溯超过 370 天")


class ReviewAutomationCoordinator:
    """观察检查与样本结算的单次协调器。"""

    def __init__(
        self,
        cache: CacheStore,
        review_service: ReviewWorkflowService,
        performance_store: PerformanceStore | None = None,
        *,
        trading_clock: TradingClock | None = None,
        app_version: str = __version__,
    ) -> None:
        self.cache = cache
        self.review_service = review_service
        self.performance_store = performance_store or review_service.performance_store
        self.trading_clock = trading_clock or TradingClock()
        self.app_version = app_version

    def policy(self) -> AutomationPolicy:
        return AutomationPolicy.from_cache(self.cache)

    def _previous_run(self, current_run_id: int) -> Any | None:
        for item in self.cache.list_review_automation_runs(limit=20):
            if item.id != current_run_id and item.status != "running":
                return item
        return None

    @staticmethod
    def _phase_due(previous: Any | None, field: str, now: datetime, minutes: int) -> bool:
        if previous is None:
            return True
        finished = _parse_datetime(getattr(previous, field, ""))
        return finished is None or now >= finished + timedelta(minutes=max(1, int(minutes)))

    def _track_open_tasks(
        self,
        quote_provider: Any,
        actor: str,
        now: datetime,
        policy: AutomationPolicy,
        run_id: int,
        owner_token: str,
        heartbeat: Any,
    ) -> AutomationPhaseReport:
        tasks = self.review_service.list_tracking_tasks({"status": "open", "include_closed": False})
        if not tasks:
            return AutomationPhaseReport(status="success")
        codes = list(dict.fromkeys(task.code for task in tasks))
        try:
            snapshots = dict(quote_provider.quotes_by_codes(codes) or {})
        except Exception as exc:
            return AutomationPhaseReport(
                status="partial", scanned=len(tasks), missing_data=len(tasks), retryable_count=len(tasks), errors=(str(exc),)
            )

        reviewed = closed = missing = retryable = 0
        errors: list[str] = []
        for index, task in enumerate(tasks, 1):
            if index == 1 or index % 25 == 0:
                self.cache.heartbeat_review_automation_run(run_id, owner_token, heartbeat())
            quote = _fresh_quote(snapshots.get(task.code), now, policy.quote_max_age_seconds)
            try:
                result = self.review_service.review_tracking_task(
                    task.id,
                    quote,
                    actor,
                    now=now.date(),
                )
            except Exception as exc:
                errors.append(f"{task.code}: {exc}")
                retryable += 1
                continue
            if result.code in {"missing_quote", "invalid_due_date"}:
                missing += 1 if result.code == "missing_quote" else 0
                retryable += 1 if result.code == "missing_quote" else 0
            elif result.ok:
                reviewed += 1
                if (result.payload or {}).get("status") == "closed":
                    closed += 1
            else:
                errors.append(f"{task.code}: {result.message}")
                retryable += 1
        status = "failed" if errors and reviewed == 0 and missing == 0 else ("partial" if errors or retryable else "success")
        return AutomationPhaseReport(
            status=status,
            scanned=len(tasks),
            reviewed=reviewed,
            closed=closed,
            missing_data=missing,
            retryable_count=retryable,
            errors=tuple(errors[:10]),
        )

    @staticmethod
    def _retry_delay(attempt: int) -> int:
        return {1: 60, 2: 5 * 60, 3: 15 * 60}.get(attempt, 0)

    def _settle_samples(
        self,
        price_source: PriceSource,
        now: datetime,
        effective_asof_date: str,
        policy: AutomationPolicy,
        run_id: int,
        owner_token: str,
        heartbeat: Any,
    ) -> AutomationPhaseReport:
        if self.performance_store is None:
            return AutomationPhaseReport(status="failed", errors=("表现复盘数据库不可用。",))
        samples = self.performance_store.list_due_samples(asof_date=effective_asof_date, limit=policy.batch_size)
        service = ObservationSettlementService(self.performance_store, price_source)
        settled = valid = invalid = retryable = 0
        missing_data = 0
        errors: list[str] = []
        next_retry_at: list[datetime] = []
        for index, sample in enumerate(samples, 1):
            if index == 1 or index % 25 == 0:
                self.cache.heartbeat_review_automation_run(run_id, owner_token, heartbeat())
            retry = self.cache.get_review_automation_retry(sample.id)
            if retry and retry[1]:
                retry_time = _parse_datetime(retry[1])
                if retry_time and retry_time > now:
                    retryable += 1
                    next_retry_at.append(retry_time)
                    continue
            try:
                outcome = service.settle_sample(sample, record_missing_price=False, asof_date=effective_asof_date)
            except sqlite3.Error:
                raise
            except Exception as exc:
                previous_attempt = retry[0] if retry else 0
                attempt = previous_attempt + 1
                delay = self._retry_delay(attempt)
                if delay:
                    due = now + timedelta(seconds=delay)
                    self.cache.save_review_automation_retry(sample.id, attempt, _iso(due), str(exc), _iso(now))
                    next_retry_at.append(due)
                    retryable += 1
                else:
                    errors.append(f"{sample.code}: {exc}")
                    retryable += 1
                continue

            if outcome.data_quality == "missing_price":
                previous_attempt = retry[0] if retry else 0
                attempt = previous_attempt + 1
                delay = self._retry_delay(attempt)
                if delay:
                    due = now + timedelta(seconds=delay)
                    self.cache.save_review_automation_retry(
                        sample.id, attempt, _iso(due), outcome.data_quality_note, _iso(now)
                    )
                    next_retry_at.append(due)
                    retryable += 1
                    missing_data += 1
                    continue
                # 第四次仍没有完整窗口才写终态 missing_price，绝不伪造收益。
                self.performance_store.record_outcome(outcome)
                self.cache.clear_review_automation_retry(sample.id)
                settled += 1
                invalid += 1
                missing_data += 1
                continue

            self.cache.clear_review_automation_retry(sample.id)
            settled += 1
            if outcome.data_quality == "ok":
                valid += 1
            else:
                invalid += 1
                missing_data += 1
        status = "failed" if errors and not settled else ("partial" if errors or retryable else "success")
        return AutomationPhaseReport(
            status=status,
            scanned=len(samples),
            settled=settled,
            valid_settled=valid,
            invalid_settled=invalid,
            missing_data=missing_data,
            retryable_count=retryable,
            errors=tuple(errors[:10]),
            next_retry_at=min(next_retry_at) if next_retry_at else None,
        )

    def run_once(
        self,
        *,
        trigger: AutomationTrigger,
        quote_provider: Any,
        price_source: PriceSource,
        actor: str,
        force_tracking: bool = False,
        force_settlement: bool = False,
        now: datetime | None = None,
    ) -> AutomationRunReport:
        current = as_shanghai(now or datetime.now(SHANGHAI_TZ))
        policy = self.policy()
        try:
            effective_asof_date = resolve_recent_complete_trading_day(current, self.trading_clock.calendar)
        except Exception as exc:
            return AutomationRunReport(None, "failed", trigger, "", error_summary=str(exc))

        owner_token = secrets.token_urlsafe(24)
        run_id = self.cache.claim_review_automation_run(
            run_key=RUN_KEY,
            trigger=trigger,
            owner_token=owner_token,
            business_date=effective_asof_date,
            started_at=_iso(current),
            app_version=self.app_version,
            stale_after_seconds=policy.stale_run_minutes * 60,
        )
        if run_id is None:
            return AutomationRunReport(run_id=None, status="already_running", trigger=trigger, effective_asof_date=effective_asof_date)
        previous = self._previous_run(run_id)
        if not policy.enabled and trigger != "manual":
            self.cache.finish_review_automation_run(
                run_id, owner_token, status="skipped", finished_at=_iso(current),
                values={"tracking_status": "skipped", "settlement_status": "skipped", "error_summary": "自动复盘已暂停。"},
            )
            return AutomationRunReport(run_id, "skipped", trigger, effective_asof_date, error_summary="自动复盘已暂停。")

        tracking_due = force_tracking or trigger == "startup" or self._phase_due(
            previous, "tracking_finished_at", current, policy.tracking_interval_minutes
        )
        settlement_due = force_settlement or trigger == "startup" or self._phase_due(
            previous, "settlement_finished_at", current, policy.settlement_interval_minutes
        )
        tracking = AutomationPhaseReport(status="skipped")
        settlement = AutomationPhaseReport(status="skipped")
        heartbeat = lambda: _iso(current if now is not None else datetime.now(SHANGHAI_TZ))
        try:
            if tracking_due:
                tracking = self._track_open_tasks(
                    quote_provider, actor, current, policy, run_id, owner_token, heartbeat
                )
            if settlement_due:
                settlement = self._settle_samples(
                    price_source, current, effective_asof_date, policy, run_id, owner_token, heartbeat
                )
            errors = tuple(tracking.errors) + tuple(settlement.errors)
            retryable = tracking.retryable_count + settlement.retryable_count
            status = "failed" if (tracking.status == "failed" or settlement.status == "failed") else (
                "partial" if retryable or errors or tracking.status == "partial" or settlement.status == "partial" else "success"
            )
            finished = current if now is not None else as_shanghai(datetime.now(SHANGHAI_TZ))
            tracking_finished_at = (
                _iso(finished)
                if tracking_due
                else (getattr(previous, "tracking_finished_at", "") if previous else "")
            )
            settlement_finished_at = (
                _iso(finished)
                if settlement_due
                else (getattr(previous, "settlement_finished_at", "") if previous else "")
            )
            tracking_base = _parse_datetime(tracking_finished_at) or finished
            settlement_base = _parse_datetime(settlement_finished_at) or finished
            next_tracking = tracking_base + timedelta(minutes=policy.tracking_interval_minutes)
            next_settlement = settlement_base + timedelta(minutes=policy.settlement_interval_minutes)
            next_run = min(next_tracking, next_settlement)
            self.cache.finish_review_automation_run(
                run_id,
                owner_token,
                status=status,
                finished_at=_iso(finished),
                values={
                    "tracking_status": tracking.status,
                    "tracking_finished_at": tracking_finished_at,
                    "tracking_scanned": tracking.scanned,
                    "tracking_reviewed": tracking.reviewed,
                    "tracking_closed": tracking.closed,
                    "settlement_status": settlement.status,
                    "settlement_finished_at": settlement_finished_at,
                    "samples_scanned": settlement.scanned,
                    "samples_settled": settlement.settled,
                    "valid_settled": settlement.valid_settled,
                    "invalid_settled": settlement.invalid_settled,
                    "missing_data_count": tracking.missing_data + settlement.missing_data,
                    "retryable_count": retryable,
                    "error_summary": "；".join(errors)[:2000],
                    "next_retry_at": _iso(settlement.next_retry_at) if settlement.next_retry_at else "",
                },
            )
            return AutomationRunReport(
                run_id, status, trigger, effective_asof_date,
                tracking_scanned=tracking.scanned, tracking_reviewed=tracking.reviewed, tracking_closed=tracking.closed,
                tracking_missing_data=tracking.missing_data, samples_scanned=settlement.scanned,
                samples_settled=settlement.settled, valid_settled=settlement.valid_settled,
                invalid_settled=settlement.invalid_settled, retryable_count=retryable,
                error_summary="；".join(errors), next_tracking_at=next_tracking,
                next_settlement_at=next_settlement, next_run_at=next_run, next_retry_at=settlement.next_retry_at,
                tracking_status=tracking.status, settlement_status=settlement.status,
            )
        except sqlite3.Error as exc:
            message = f"数据库错误：{exc}"
            self.cache.finish_review_automation_run(
                run_id, owner_token, status="failed", finished_at=_iso(current), values={"error_summary": message}
            )
            return AutomationRunReport(run_id, "failed", trigger, effective_asof_date, error_summary=message)
        except Exception as exc:
            message = str(exc)
            self.cache.finish_review_automation_run(
                run_id, owner_token, status="failed", finished_at=_iso(current), values={"error_summary": message}
            )
            return AutomationRunReport(run_id, "failed", trigger, effective_asof_date, error_summary=message)


__all__ = [
    "AutomationPhaseReport",
    "AutomationPolicy",
    "AutomationRunReport",
    "AutomationTrigger",
    "ReviewAutomationCoordinator",
    "resolve_recent_complete_trading_day",
]
