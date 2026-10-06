from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Literal

import pandas as pd

from stock_tool.core.cache import CacheStore
from stock_tool.core.data_fetcher import DataFetcher
from stock_tool.core.models import StockQuote
from stock_tool.core.technical_profile import build_technical_profile


RefreshTrigger = Literal["startup", "manual", "data_page", "feature", "auto"]
RefreshStage = Literal[
    "cache_snapshot",
    "watch_quotes",
    "current_detail",
    "watch_details",
    "market_context",
    "v1_watch_snapshots",
    "pit_status",
    "quotes",
    "research_detail",
    "factor_snapshot",
    "pit_incremental",
]

FINANCIAL_REFRESH_TTL = timedelta(days=1)
WATCH_QUOTES_CACHE_KEY = "watch_quotes"


@dataclass
class RefreshStepResult:
    stage: RefreshStage
    status: str
    done: int = 0
    total: int = 0
    fresh: int = 0
    cache: int = 0
    failed: int = 0
    message: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class RefreshRunSummary:
    trigger: RefreshTrigger
    started_at: datetime
    current_code: str = ""
    finished_at: datetime | None = None
    status: str = "running"
    message: str = ""
    stages: list[RefreshStepResult] = field(default_factory=list)

    @property
    def fresh(self) -> int:
        return sum(step.fresh for step in self.stages)

    @property
    def cache(self) -> int:
        return sum(step.cache for step in self.stages)

    @property
    def failed(self) -> int:
        return sum(step.failed for step in self.stages)

    def stage_result(self, stage: RefreshStage) -> RefreshStepResult | None:
        for step in reversed(self.stages):
            if step.stage == stage:
                return step
        return None


ProgressCallback = Callable[[RefreshStepResult], None]


class DataRefreshCoordinator:
    """Coordinate user-visible data refresh without leaking UI concerns into core."""

    def __init__(
        self,
        cache: CacheStore,
        fetcher: DataFetcher,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.cache = cache
        self.fetcher = fetcher
        self._now = now_fn or datetime.now

    def run(
        self,
        trigger: RefreshTrigger,
        current_code: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> RefreshRunSummary:
        trigger = trigger if trigger in {"startup", "manual", "data_page", "feature", "auto"} else "manual"
        started_at = self._now()
        normalized_current = self._normalize_code(current_code)
        summary = RefreshRunSummary(trigger=trigger, started_at=started_at, current_code=normalized_current)

        def emit(step: RefreshStepResult) -> None:
            if step.status != "running":
                summary.stages.append(step)
            if progress is not None:
                progress(step)

        emit(self._cache_snapshot())
        ordered_codes = self.prioritized_watch_codes(normalized_current)
        if trigger == "auto":
            emit(self._refresh_auto_quotes(ordered_codes, started_at))
            summary.finished_at = self._now()
            auto_step = summary.stage_result("watch_quotes")
            if auto_step is None or auto_step.status == "skipped":
                summary.status = "success"
                summary.message = "自动行情刷新跳过：当前没有自选或当前个股。"
            elif auto_step.failed and not auto_step.fresh and not auto_step.cache:
                summary.status = "failed"
                summary.message = "自动行情刷新失败，已保留上一份数据。"
            elif auto_step.failed or auto_step.cache:
                summary.status = "partial"
                summary.message = "自动行情部分更新，已保留不可用代码的上一份数据。"
            else:
                summary.status = "success"
                summary.message = "自动行情已更新。"
            return summary
        emit(self._refresh_watch_quotes(ordered_codes, started_at, progress))

        refreshed_current = False
        if normalized_current:
            emit(self._refresh_current_detail(normalized_current, trigger, started_at))
            refreshed_current = True

        if trigger != "startup":
            detail_codes = [code for code in ordered_codes if code != normalized_current or not refreshed_current]
            emit(self._refresh_watch_details(detail_codes, trigger, started_at, progress))
            emit(self._refresh_market_context(trigger, started_at))
            emit(self._manual_entry_stage("v1_watch_snapshots"))
            emit(self._manual_entry_stage("pit_status"))

        summary.finished_at = self._now()
        core_steps = [
            step
            for step in summary.stages
            if step.stage in {"watch_quotes", "current_detail", "watch_details"}
        ]
        core_failed = sum(step.failed for step in core_steps)
        core_cache = sum(step.cache for step in core_steps)
        core_fresh = sum(step.fresh for step in core_steps)
        core_done = core_fresh + core_cache
        if core_failed and not core_done:
            summary.status = "failed"
            summary.message = "核心自选数据刷新失败，已保留旧缓存。"
        elif core_failed:
            summary.status = "partial"
            summary.message = "部分数据刷新失败或使用缓存，详情见刷新中心。"
        elif core_cache:
            summary.status = "partial"
            summary.message = "自选优先刷新完成，部分数据来自缓存；详情见刷新中心。"
        elif core_fresh:
            summary.status = "success"
            summary.message = "自选优先刷新完成，实时数据已更新。"
        else:
            summary.status = "success"
            summary.message = "暂无需要刷新的自选数据。"
        return summary

    def prioritized_watch_codes(self, current_code: str | None = None) -> list[str]:
        current = self._normalize_code(current_code)
        watches = list(self._safe_call_list("list_watch"))
        tasks = list(self._safe_call_list("list_tracking_tasks", include_closed=True))
        alerts = list(self._safe_call_list("list_price_alerts", enabled_only=False))
        today_start = self._now().replace(hour=0, minute=0, second=0, microsecond=0)
        history_today = list(self._safe_call_list("list_alert_history", limit=500, since=today_start))

        watch_order: dict[str, int] = {}
        codes: list[str] = []
        for index, watch in enumerate(watches):
            code = self._normalize_code(self._value(watch, "code", ""))
            if not code:
                continue
            watch_order.setdefault(code, index)
            if code not in codes:
                codes.append(code)
        if current and current not in codes:
            codes.insert(0, current)

        holding_codes: set[str] = set()
        open_task_codes: set[str] = set()
        for task in tasks:
            if str(self._value(task, "status", "") or "").lower() != "open":
                continue
            code = self._normalize_code(self._value(task, "code", ""))
            if not code:
                continue
            task_type = str(self._value(task, "task_type", "") or "").lower()
            if task_type == "holding":
                holding_codes.add(code)
            else:
                open_task_codes.add(code)

        enabled_alert_codes = {
            self._normalize_code(self._value(alert, "code", ""))
            for alert in alerts
            if bool(self._value(alert, "enabled", True))
        }
        enabled_alert_codes.discard("")
        triggered_today_codes = {
            self._normalize_code(self._value(item, "code", ""))
            for item in history_today
        }
        triggered_today_codes.discard("")

        def priority(code: str) -> tuple[int, int, str]:
            if current and code == current:
                return (0, 0, code)
            if code in holding_codes:
                return (1, watch_order.get(code, 999_999), code)
            if code in enabled_alert_codes:
                return (2, watch_order.get(code, 999_999), code)
            if code in triggered_today_codes:
                return (3, watch_order.get(code, 999_999), code)
            if code in open_task_codes:
                return (4, watch_order.get(code, 999_999), code)
            return (5, watch_order.get(code, 999_999), code)

        return sorted(dict.fromkeys(codes), key=priority)

    def _cache_snapshot(self) -> RefreshStepResult:
        metadata = list(self._safe_call_list("list_cache_metadata", limit=1000))
        watches = list(self._safe_call_list("list_watch"))
        return RefreshStepResult(
            stage="cache_snapshot",
            status="success",
            done=len(watches),
            total=len(watches),
            cache=len(metadata),
            message=f"已读取旧缓存 {len(metadata)} 项，自选 {len(watches)} 只；先显示缓存，再刷新实时数据。",
        )

    def _refresh_watch_quotes(
        self,
        codes: list[str],
        started_at: datetime,
        progress: ProgressCallback | None,
    ) -> RefreshStepResult:
        total = len(codes)
        if total == 0:
            return RefreshStepResult(
                stage="watch_quotes",
                status="skipped",
                message="暂无自选股；未触发行情刷新。",
            )

        if total > 20:
            try:
                quotes, updated_at = self.fetcher.spot_quotes(force=True)
                source_is_fresh = self._is_fresh_timestamp(updated_at, started_at)
                quote_map = {self._normalize_code(getattr(quote, "code", "")): quote for quote in quotes}
                matched_quotes = [quote_map[code] for code in codes if code in quote_map]
                matched = len(matched_quotes)
                self._write_watch_quotes(matched_quotes)
                failed = total - matched
                fresh = matched if source_is_fresh else 0
                cache = 0 if source_is_fresh else matched
                status = self._status_for(total, fresh, cache, failed)
                return RefreshStepResult(
                    stage="watch_quotes",
                    status=status,
                    done=total,
                    total=total,
                    fresh=fresh,
                    cache=cache,
                    failed=failed,
                    message=f"自选超过20只，已用全市场行情映射 {matched}/{total} 只。",
                    errors=[] if failed == 0 else [f"{failed} 只自选未在全市场行情中匹配到。"],
                )
            except Exception as exc:
                return RefreshStepResult(
                    stage="watch_quotes",
                    status="failed",
                    done=0,
                    total=total,
                    failed=total,
                    message="全市场行情刷新失败，保留旧自选缓存。",
                    errors=[str(exc)],
                )

        fresh = 0
        failed = 0
        quotes: list[StockQuote] = []
        errors: list[str] = []
        done = 0
        max_workers = min(4, max(1, total))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(self._quote_by_code_safe, code): code for code in codes}
            for future in as_completed(future_map):
                code = future_map[future]
                done += 1
                quote, error = future.result()
                if quote is not None:
                    fresh += 1
                    quotes.append(quote)
                else:
                    failed += 1
                    errors.append(error or f"{code} 实时行情不可用")
                if progress is not None:
                    progress(
                        RefreshStepResult(
                            stage="watch_quotes",
                            status="running",
                            done=done,
                            total=total,
                            fresh=fresh,
                            failed=failed,
                            message=f"自选刷新中 {done}/{total}",
                            errors=errors[-3:],
                        )
                    )
        self._write_watch_quotes(quotes)
        status = self._status_for(total, fresh, 0, failed)
        return RefreshStepResult(
            stage="watch_quotes",
            status=status,
            done=done,
            total=total,
            fresh=fresh,
            failed=failed,
            message=f"自选实时行情已刷新 {fresh}/{total} 只。",
            errors=errors[:8],
        )

    def _refresh_auto_quotes(
        self,
        codes: list[str],
        started_at: datetime,
    ) -> RefreshStepResult:
        """Refresh only live quotes for the short automatic polling loop.

        Automatic polling must stay cheap enough for a 15/30 second cadence;
        financials, history, money flow and events remain on the feature/manual
        refresh paths with their own source and freshness rules.
        """
        total = len(codes)
        if total == 0:
            return RefreshStepResult(
                stage="watch_quotes",
                status="skipped",
                message="暂无自选股或当前个股；未触发行情轮询。",
            )
        try:
            quotes, updated_at = self.fetcher.spot_quotes(force=True)
            quote_map = {
                self._normalize_code(getattr(quote, "code", "")): quote
                for quote in quotes
            }
            matched_quotes = [quote_map[code] for code in codes if code in quote_map]
            matched = len(matched_quotes)
            self._write_watch_quotes(matched_quotes)
            source_is_fresh = self._is_fresh_timestamp(updated_at, started_at)
            fresh = matched if source_is_fresh else 0
            cache = 0 if source_is_fresh else matched
            failed = total - matched
            return RefreshStepResult(
                stage="watch_quotes",
                status=self._status_for(total, fresh, cache, failed),
                done=total,
                total=total,
                fresh=fresh,
                cache=cache,
                failed=failed,
                message=f"自动行情已更新 {matched}/{total} 只。",
                errors=[] if failed == 0 else [f"{failed} 只股票未获得最新行情。"],
            )
        except Exception as exc:
            return RefreshStepResult(
                stage="watch_quotes",
                status="failed",
                done=0,
                total=total,
                failed=total,
                message="自动行情源暂不可用，保留上一份数据。",
                errors=[str(exc)],
            )

    def _refresh_current_detail(
        self,
        code: str,
        trigger: RefreshTrigger,
        started_at: datetime,
    ) -> RefreshStepResult:
        force_detail = trigger in {"manual", "data_page", "feature"}
        return self._refresh_code_detail(
            code,
            "current_detail",
            started_at,
            force_detail=force_detail,
            include_quote=True,
        )

    def _refresh_watch_details(
        self,
        codes: list[str],
        trigger: RefreshTrigger,
        started_at: datetime,
        progress: ProgressCallback | None,
    ) -> RefreshStepResult:
        total = len(codes)
        if total == 0:
            return RefreshStepResult(
                stage="watch_details",
                status="skipped",
                message="当前没有需要后台补齐的自选详情。",
            )
        force_detail = trigger in {"manual", "data_page", "feature"}
        done = 0
        fresh = 0
        cache = 0
        failed = 0
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=min(2, total)) as executor:
            future_map = {
                executor.submit(
                    self._refresh_code_detail,
                    code,
                    "watch_details",
                    started_at,
                    force_detail=force_detail,
                    include_quote=False,
                ): code
                for code in codes
            }
            for future in as_completed(future_map):
                done += 1
                result = future.result()
                fresh += result.fresh
                cache += result.cache
                failed += result.failed
                errors.extend(result.errors)
                if progress is not None:
                    progress(
                        RefreshStepResult(
                            stage="watch_details",
                            status="running",
                            done=done,
                            total=total,
                            fresh=fresh,
                            cache=cache,
                            failed=failed,
                            message=f"自选详情补齐中 {done}/{total}",
                            errors=errors[-3:],
                        )
                    )
        status = self._status_for(total, fresh, cache, failed)
        return RefreshStepResult(
            stage="watch_details",
            status=status,
            done=done,
            total=total,
            fresh=fresh,
            cache=cache,
            failed=failed,
            message=f"自选详情后台补齐完成 {done}/{total} 只。",
            errors=errors[:10],
        )

    def _refresh_market_context(self, trigger: RefreshTrigger, started_at: datetime) -> RefreshStepResult:
        force = trigger in {"manual", "data_page", "feature"}
        total = 2
        fresh = 0
        cache = 0
        failed = 0
        errors: list[str] = []
        try:
            sentiment, updated_at = self.fetcher.market_sentiment(force=force)
            if updated_at is None:
                failed += 1
                errors.append(getattr(sentiment, "summary", "") or "市场情绪暂不可用")
            elif self._is_fresh_timestamp(updated_at, started_at):
                fresh += 1
            else:
                cache += 1
        except Exception as exc:
            failed += 1
            errors.append(f"市场情绪：{exc}")
        try:
            self.fetcher.market_money_flow(force=force)
            fresh += 1
        except Exception as exc:
            failed += 1
            errors.append(f"市场资金流：{exc}")
        status = self._status_for(total, fresh, cache, failed)
        return RefreshStepResult(
            stage="market_context",
            status=status,
            done=total,
            total=total,
            fresh=fresh,
            cache=cache,
            failed=failed,
            message="市场辅助数据已刷新；失败不会阻断自选数据。",
            errors=errors[:6],
        )

    def _manual_entry_stage(self, stage: RefreshStage) -> RefreshStepResult:
        message = (
            "未自动更新全A筛选快照；请在刷新中心手动确认后运行。"
            if stage == "v1_watch_snapshots"
            else "未自动运行 PIT 全量任务；日增量/全量仍保留预检和确认流程。"
        )
        return RefreshStepResult(stage=stage, status="skipped", message=message)

    def _refresh_code_detail(
        self,
        code: str,
        stage: RefreshStage,
        started_at: datetime,
        *,
        force_detail: bool,
        include_quote: bool,
    ) -> RefreshStepResult:
        total = 5 if include_quote else 4
        fresh = 0
        cache = 0
        failed = 0
        errors: list[str] = []
        history_frame = pd.DataFrame()

        if include_quote:
            quote, error = self._quote_by_code_safe(code)
            if quote is not None:
                fresh += 1
            else:
                failed += 1
                errors.append(error or f"{code} 实时行情不可用")

        try:
            history_frame, updated_at = self.fetcher.history(code, days=365, force=force_detail)
            if self._is_fresh_timestamp(updated_at, started_at):
                fresh += 1
            else:
                cache += 1
            try:
                build_technical_profile(history_frame)
            except Exception as exc:
                errors.append(f"{code} 技术画像计算失败：{exc}")
        except Exception as exc:
            failed += 1
            errors.append(f"{code} K线暂不可用：{exc}")

        # A user-triggered feature refresh must try the public source even when
        # the one-day financial TTL has not expired. Startup can still use the
        # TTL to avoid unnecessary requests.
        financial_force = force_detail or self._financial_needs_refresh(code)
        try:
            _metrics, updated_at = self.fetcher.financial_metrics(code, force=financial_force)
            if self._is_fresh_timestamp(updated_at, started_at):
                fresh += 1
            else:
                cache += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{code} 财务暂不可用：{exc}")

        try:
            _money_flow, updated_at = self.fetcher.money_flow(code, days=20, force=force_detail)
            if self._is_fresh_timestamp(updated_at, started_at):
                fresh += 1
            else:
                cache += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{code} 资金流暂不可用：{exc}")

        try:
            _events, updated_at = self.fetcher.event_catalysts(code, limit=6, force=force_detail)
            if updated_at is None:
                cache += 1
            elif self._is_fresh_timestamp(updated_at, started_at):
                fresh += 1
            else:
                cache += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{code} 事件暂不可用：{exc}")

        status = self._status_for(total, fresh, cache, failed)
        return RefreshStepResult(
            stage=stage,
            status=status,
            done=1,
            total=1,
            fresh=fresh,
            cache=cache,
            failed=failed,
            message=f"{code} 详情刷新完成。",
            errors=errors,
        )

    def _quote_by_code_safe(self, code: str) -> tuple[StockQuote | None, str]:
        try:
            quote = self.fetcher.quote_by_code(code)
            if quote is None:
                return None, f"{code} 实时行情为空"
            return quote, ""
        except Exception as exc:
            return None, f"{code} 实时行情失败：{exc}"

    def _write_watch_quotes(self, quotes: list[StockQuote]) -> None:
        if not quotes:
            return
        payload = [dict(quote.__dict__) for quote in quotes]
        try:
            self.cache.set_json(WATCH_QUOTES_CACHE_KEY, payload)
        except Exception:
            return

    def _financial_needs_refresh(self, code: str) -> bool:
        record = None
        try:
            record = self.cache.get_json(f"financial:{code}")
        except Exception:
            return True
        if record is None:
            return True
        return self._now() - record.updated_at > FINANCIAL_REFRESH_TTL

    def _safe_call_list(self, method_name: str, **kwargs: Any) -> list[Any]:
        method = getattr(self.cache, method_name, None)
        if method is None:
            return []
        try:
            return list(method(**kwargs))
        except Exception:
            return []

    def _is_fresh_timestamp(self, updated_at: datetime | None, started_at: datetime) -> bool:
        if updated_at is None:
            return False
        return updated_at >= started_at - timedelta(seconds=1)

    def _status_for(self, total: int, fresh: int, cache: int, failed: int) -> str:
        if total <= 0:
            return "skipped"
        if failed and not fresh and not cache:
            return "failed"
        if failed or cache:
            return "partial"
        if fresh:
            return "success"
        return "failed"

    def _normalize_code(self, code: Any) -> str:
        text = str(code or "").strip()
        if not text:
            return ""
        return text[-6:].zfill(6)

    def _value(self, record: Any, name: str, default: Any = None) -> Any:
        if isinstance(record, dict):
            return record.get(name, default)
        return getattr(record, name, default)
