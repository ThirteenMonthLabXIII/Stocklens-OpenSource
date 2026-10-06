"""Qt 工作台使用的价格预警业务服务。

UI 只负责异步取得行情、呈现结果和执行通知；预警的校验、状态转换、历史和
审计全部由本模块完成。这样两个界面不会因为实现细节不同而得到不同结论。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import math
import threading
from typing import Any, Callable, Mapping

from stock_tool.core.cache import AlertHistoryRecord, CacheStore, PriceAlertRecord
from stock_tool.core.models import StockQuote
from stock_tool.core.price_alert import ALERT_COOLDOWN_MIN, ALERT_KINDS, decide_alert_action, describe_alert, evaluate_alert


MONITOR_INTERVALS = frozenset({1, 5, 10, 30})


@dataclass(frozen=True)
class AlertDraft:
    code: str
    name: str
    kind: str
    threshold: float
    note: str = ""
    repeat: bool = False


@dataclass(frozen=True)
class AlertOperationResult:
    ok: bool
    code: str
    message: str
    alert: PriceAlertRecord | None = None


@dataclass(frozen=True)
class AlertTrigger:
    alert_id: int
    code: str
    message: str


@dataclass(frozen=True)
class AlertMatch:
    """条件当前满足；是否真正触发由边沿、冷却与原子认领共同决定。"""

    alert_id: int
    code: str
    message: str


@dataclass(frozen=True)
class AlertCheckResult:
    checked_count: int
    failed_codes: tuple[str, ...] = ()
    triggers: tuple[AlertTrigger, ...] = ()
    state_updates: int = 0
    code: str = "ok"
    message: str = ""
    matches: tuple[AlertMatch, ...] = ()
    stale_codes: tuple[str, ...] = ()

    @property
    def triggered_count(self) -> int:
        return len(self.triggers)


def _record_payload(record: PriceAlertRecord | None) -> dict[str, Any]:
    if record is None:
        return {}
    payload = asdict(record)
    payload["created_at"] = record.created_at.isoformat(timespec="seconds")
    return payload


class AlertService:
    """唯一的预警状态机；实例级互斥防止重复检查写入两次。"""

    def __init__(self, cache: CacheStore, *, cooldown_minutes: int = ALERT_COOLDOWN_MIN) -> None:
        self.cache = cache
        self.cooldown_minutes = max(0, int(cooldown_minutes))
        self._check_lock = threading.Lock()

    @staticmethod
    def _validate_draft(draft: AlertDraft) -> AlertDraft:
        code = str(draft.code or "").strip()
        try:
            threshold = float(draft.threshold)
        except (TypeError, ValueError) as exc:
            raise ValueError("阈值必须是大于 0 的数字") from exc
        if not (code.isdigit() and len(code) == 6):
            raise ValueError("股票代码必须为 6 位数字")
        if draft.kind not in ALERT_KINDS:
            raise ValueError("预警类型无效")
        if not math.isfinite(threshold) or threshold <= 0:
            raise ValueError("阈值必须是大于 0 的数字")
        return AlertDraft(code, str(draft.name or code).strip() or code, draft.kind, threshold, str(draft.note or "").strip(), bool(draft.repeat))

    def create_alert(self, draft: AlertDraft, actor: str) -> AlertOperationResult:
        try:
            clean = self._validate_draft(draft)
        except ValueError as exc:
            return AlertOperationResult(False, "validation_error", str(exc))
        alert_id = self.cache.add_price_alert(clean.code, clean.name, clean.kind, clean.threshold, clean.note, clean.repeat)
        record = self.cache.get_price_alert(alert_id)
        self.cache.add_audit("alert", alert_id, "create", actor, after=_record_payload(record))
        return AlertOperationResult(True, "created", f"已新建预警：{clean.name} {describe_alert(clean.kind, clean.threshold)}", record)

    def update_alert(self, alert_id: int, draft: AlertDraft, actor: str) -> AlertOperationResult:
        before = self.cache.get_price_alert(alert_id)
        if before is None:
            return AlertOperationResult(False, "not_found", "预警已不存在，请刷新后重试。")
        try:
            clean = self._validate_draft(draft)
        except ValueError as exc:
            return AlertOperationResult(False, "validation_error", str(exc), before)
        updated = self.cache.update_price_alert(
            alert_id, code=clean.code, name=clean.name, kind=clean.kind, threshold=clean.threshold,
            note=clean.note, repeat=clean.repeat,
        )
        record = self.cache.get_price_alert(alert_id)
        if not updated or record is None:
            return AlertOperationResult(False, "not_found", "预警已不存在，请刷新后重试。")
        self.cache.add_audit("alert", alert_id, "update", actor, before=_record_payload(before), after=_record_payload(record))
        return AlertOperationResult(True, "updated", "预警已编辑；历史记录已保留，触发状态已重新计算。", record)

    def set_enabled(self, alert_id: int, enabled: bool, actor: str) -> AlertOperationResult:
        before = self.cache.get_price_alert(alert_id)
        if before is None:
            return AlertOperationResult(False, "not_found", "预警已不存在，请刷新后重试。")
        self.cache.set_price_alert_enabled(alert_id, bool(enabled))
        record = self.cache.get_price_alert(alert_id)
        self.cache.add_audit("alert", alert_id, "enable" if enabled else "disable", actor, before=_record_payload(before), after=_record_payload(record))
        return AlertOperationResult(True, "enabled" if enabled else "disabled", "预警已" + ("启用" if enabled else "停用"), record)

    def delete_alert(self, alert_id: int, actor: str, reason: str) -> AlertOperationResult:
        before = self.cache.get_price_alert(alert_id)
        if before is None:
            return AlertOperationResult(False, "not_found", "预警已不存在，请刷新后重试。")
        self.cache.add_audit("alert", alert_id, "delete", actor, before=_record_payload(before), reason=reason)
        self.cache.delete_price_alert(alert_id)
        return AlertOperationResult(True, "deleted", "预警已删除；触发历史已保留。")

    def list_alerts(self, enabled_only: bool = False) -> list[PriceAlertRecord]:
        return self.cache.list_price_alerts(enabled_only=enabled_only)

    def match_alerts(
        self,
        alerts: list[PriceAlertRecord],
        quotes: Mapping[str, StockQuote | None],
    ) -> tuple[AlertMatch, ...]:
        """只判断条件，不读取或推进边沿状态，供协调器安全地做预览。"""
        matches: list[AlertMatch] = []
        for alert in alerts:
            quote = quotes.get(alert.code)
            if quote is None:
                continue
            if not quote.name:
                quote.name = alert.name
            hit, message = evaluate_alert(alert.kind, alert.threshold, quote)
            if hit:
                matches.append(AlertMatch(alert.id, alert.code, message or describe_alert(alert.kind, alert.threshold)))
        return tuple(matches)

    def check_alerts(
        self,
        quote_provider: Callable[[str], StockQuote | None] | Mapping[str, StockQuote | None],
        now: datetime | None = None,
        *,
        quote_sources: Mapping[str, str] | None = None,
        quote_times: Mapping[str, datetime | str | None] | None = None,
    ) -> AlertCheckResult:
        if not self._check_lock.acquire(blocking=False):
            return AlertCheckResult(0, code="already_running", message="预警检查仍在运行。")
        try:
            alerts = self.list_alerts(enabled_only=True)
            quotes: dict[str, StockQuote | None] = {}
            if isinstance(quote_provider, Mapping):
                quotes = dict(quote_provider)
            else:
                for code in {item.code for item in alerts}:
                    try:
                        quotes[code] = quote_provider(code)
                    except Exception:
                        quotes[code] = None
            return apply_alert_quotes(
                self.cache,
                alerts,
                quotes,
                now=now,
                cooldown_minutes=self.cooldown_minutes,
                quote_sources=quote_sources,
                quote_times=quote_times,
            )
        finally:
            self._check_lock.release()

    def get_monitor_policy(self) -> dict[str, int | bool]:
        value = self.cache.get_setting("alert_interval_min", "5")
        try:
            interval = int(value)
        except (TypeError, ValueError):
            interval = 5
        return {"enabled": self.cache.get_setting("alert_monitor_enabled", "0") == "1", "interval_minutes": interval if interval in MONITOR_INTERVALS else 5}

    def update_monitor_policy(self, enabled: bool, interval_minutes: int) -> dict[str, int | bool]:
        if int(interval_minutes) not in MONITOR_INTERVALS:
            raise ValueError("监控间隔只允许 1、5、10、30 分钟")
        self.cache.set_setting("alert_monitor_enabled", "1" if enabled else "0")
        self.cache.set_setting("alert_interval_min", str(int(interval_minutes)))
        return self.get_monitor_policy()

    def list_history(self, scope: str = "all") -> list[AlertHistoryRecord]:
        if scope == "today":
            return self.cache.list_alert_history(since=datetime.combine(datetime.now().date(), datetime.min.time()))
        return self.cache.list_alert_history()

    def clear_history(self, actor: str, reason: str) -> AlertOperationResult:
        count = len(self.cache.list_alert_history(limit=100000))
        self.cache.clear_alert_history()
        self.cache.add_audit("alert_history", "all", "clear", actor, before={"count": count}, reason=reason)
        return AlertOperationResult(True, "cleared", f"已清空 {count} 条预警记录。")

    def export_history_rows(self, scope: str = "all") -> list[dict[str, Any]]:
        return [
            {
                "时间": item.created_at.strftime("%Y-%m-%d %H:%M:%S"), "代码": item.code, "名称": item.name,
                "条件": describe_alert(item.kind, item.threshold), "价格": item.price,
                "涨跌幅": item.change_pct, "数据源": item.quote_source,
                "行情时间": item.quote_at, "说明": item.message,
            }
            for item in self.list_history(scope)
        ]


def apply_alert_quotes(
    cache: CacheStore,
    alerts: list[PriceAlertRecord],
    quotes: Mapping[str, StockQuote | None],
    *,
    now: datetime | None = None,
    cooldown_minutes: int = ALERT_COOLDOWN_MIN,
    quote_sources: Mapping[str, str] | None = None,
    quote_times: Mapping[str, datetime | str | None] | None = None,
) -> AlertCheckResult:
    """兼容旧调用方的批量状态转换；行情失败不会改变预警状态。"""
    checked, failed_codes, triggers, matches, state_updates = 0, [], [], [], 0
    timestamp = (now or datetime.now()).replace(microsecond=0)
    timestamp_text = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    for alert in alerts:
        quote = quotes.get(alert.code)
        if quote is None:
            failed_codes.append(alert.code)
            continue
        checked += 1
        if not quote.name:
            quote.name = alert.name
        hit, message = evaluate_alert(alert.kind, alert.threshold, quote)
        rendered = message or describe_alert(alert.kind, alert.threshold)
        if hit:
            matches.append(AlertMatch(alert.id, alert.code, rendered))
        notify, new_state = decide_alert_action(hit, alert.last_state, alert.last_triggered_at, cooldown_minutes, now=timestamp)
        if notify:
            if cache.claim_price_alert_trigger(alert.id, timestamp_text, disable=not alert.repeat):
                quote_at = (quote_times or {}).get(alert.code)
                if isinstance(quote_at, datetime):
                    quote_at = quote_at.isoformat(timespec="seconds")
                cache.add_alert_history(
                    alert.id,
                    alert.code,
                    alert.name,
                    alert.kind,
                    alert.threshold,
                    quote.price,
                    quote.change_pct,
                    rendered,
                    created_at=timestamp_text,
                    quote_source=(quote_sources or {}).get(alert.code, ""),
                    quote_at=str(quote_at or ""),
                )
                triggers.append(AlertTrigger(alert.id, alert.code, rendered))
        elif new_state != alert.last_state:
            if cache.set_price_alert_state(alert.id, new_state, expected_state=alert.last_state):
                state_updates += 1
    return AlertCheckResult(
        checked,
        tuple(dict.fromkeys(failed_codes)),
        tuple(triggers),
        state_updates,
        matches=tuple(matches),
    )
