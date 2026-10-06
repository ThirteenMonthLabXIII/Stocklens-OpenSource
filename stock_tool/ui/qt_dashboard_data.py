from __future__ import annotations

"""Pure data helpers for the Qt workbench dashboard.

These functions intentionally avoid Qt imports so dashboard behavior can be
covered by fast offline tests.
"""

import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable


SearchIntent = str

_CODE_RE = re.compile(r"^(?:sh|sz|bj)?\d{1,6}$", re.IGNORECASE)
_FILTER_KEYWORDS = {
    "roe",
    "pe",
    "pb",
    "rsi",
    "kdj",
    "macd",
    "大于",
    "小于",
    "高于",
    "低于",
    "不高于",
    "不低于",
    "超过",
    "低于",
    "负债率",
    "资产负债",
    "净利率",
    "毛利率",
    "营收",
    "利润",
    "增长",
    "市值",
    "换手",
    "量比",
    "回撤",
    "趋势",
    "主力",
    "资金流",
    "排序",
    "筛选",
    "低估",
    "估值",
    "优质",
    "稳健",
    "龙头",
    "放量",
    "近5日",
    "近10日",
    "近20日",
    "近30日",
    "between",
    "sort",
}
_FILTER_SYMBOLS = (">=", "<=", ">", "<", "=", "%")


def classify_search_intent(text: str) -> SearchIntent:
    """Classify the global command box into stock lookup or screener query."""
    normalized = (text or "").strip()
    if not normalized:
        return "stock"
    compact = re.sub(r"\s+", "", normalized)
    lowered = compact.lower()
    if _CODE_RE.match(lowered):
        return "stock"
    if any(symbol in compact for symbol in _FILTER_SYMBOLS):
        return "filter"
    if any(keyword in lowered for keyword in _FILTER_KEYWORDS):
        return "filter"
    return "stock"


def summarize_tracking(tasks: Iterable[Any], *, today: date | None = None) -> dict[str, int]:
    """Summarize observation/review work from tracking task records."""
    today = today or date.today()
    summary = {
        "pool": 0,
        "watching": 0,
        "holding": 0,
        "review": 0,
        "in_progress": 0,
        "done": 0,
    }
    for task in tasks or []:
        status = str(_value(task, "status", "") or "").lower()
        task_type = str(_value(task, "task_type", "") or "").lower()
        is_open = status == "open"
        if is_open:
            summary["pool"] += 1
            summary["in_progress"] += 1
            if task_type == "holding":
                summary["holding"] += 1
            else:
                summary["watching"] += 1
            due = _parse_date(_value(task, "due_date", ""))
            if due is not None and due <= today:
                summary["review"] += 1
        else:
            summary["done"] += 1
    return summary


def summarize_alerts(alerts: Iterable[Any], history_today: Iterable[Any]) -> dict[str, int]:
    monitoring = sum(1 for alert in alerts or [] if bool(_value(alert, "enabled", True)))
    triggered_today = sum(1 for _item in history_today or [])
    return {"monitoring": monitoring, "triggered_today": triggered_today}


def status_tags_for_code(code: str, tasks: Iterable[Any], alerts: Iterable[Any], *, today: date | None = None) -> list[str]:
    today = today or date.today()
    normalized_code = str(code or "").zfill(6)
    tags: list[str] = []
    for task in tasks or []:
        if str(_value(task, "code", "") or "").zfill(6) != normalized_code:
            continue
        if str(_value(task, "status", "") or "").lower() != "open":
            continue
        task_type = str(_value(task, "task_type", "") or "").lower()
        if task_type == "holding":
            tags.append("持仓观察")
        else:
            tags.append("观察中")
        due = _parse_date(_value(task, "due_date", ""))
        if due is not None and due <= today:
            tags.append("待复盘")
    for alert in alerts or []:
        if str(_value(alert, "code", "") or "").zfill(6) == normalized_code and bool(_value(alert, "enabled", True)):
            tags.append("价格预警")
            break
    return _dedupe(tags)


def build_watch_rows(
    watches: Iterable[dict[str, Any]],
    quotes_by_code: dict[str, Any],
    tasks: Iterable[Any],
    alerts: Iterable[Any],
    *,
    updated_at: datetime | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    age_text = _relative_age(updated_at) if updated_at else "-"
    for watch in watches or []:
        code = str(watch.get("code") or "").zfill(6)
        quote = quotes_by_code.get(code)
        name = _value(quote, "name", "") or watch.get("name") or "-"
        price = _value(quote, "price", None)
        change_pct = _value(quote, "change_pct", None)
        tags = status_tags_for_code(code, tasks, alerts, today=today)
        if quote is None or price is None:
            tags.append("数据缺失")
        rows.append(
            {
                "code": code,
                "name": name,
                "price": price,
                "change_pct": change_pct,
                "tags": _dedupe(tags) or ["自选"],
                "updated_at": age_text,
            }
        )
    return rows


def merge_quote_cache_payloads(
    payloads: Iterable[Iterable[dict[str, Any]] | None],
    quote_factory: Callable[..., Any],
) -> dict[str, Any]:
    quotes_by_code: dict[str, Any] = {}
    for payload in payloads:
        for item in payload or []:
            if not isinstance(item, dict):
                continue
            try:
                quote = quote_factory(**item)
            except Exception:
                continue
            code = str(_value(quote, "code", "") or "").zfill(6)
            if code:
                quotes_by_code[code] = quote
    return quotes_by_code


def data_health_rows(cache_or_items: Any, *, now: datetime | None = None) -> list[tuple[str, str, str]]:
    """Return (category, status, relative age) rows from cache metadata."""
    now = now or datetime.now()
    items = _cache_items(cache_or_items)
    groups = [
        ("自选行情", ("watch_quotes",), timedelta(days=1)),
        ("全市场行情", ("spot_quotes",), timedelta(days=1)),
        ("K线", ("history:",), timedelta(days=15)),
        ("财务", ("financial:",), timedelta(days=7)),
        ("事件", ("events:",), timedelta(days=30)),
    ]
    rows: list[tuple[str, str, str]] = []
    for title, prefixes, fresh_for in groups:
        newest = _newest_for_prefixes(items, prefixes)
        if newest is None:
            rows.append((title, "暂不可用", "无可用缓存"))
            continue
        age = now - newest
        if age <= fresh_for:
            status = "正常"
        elif age <= fresh_for * 2:
            status = "偏旧"
        else:
            status = "过旧"
        rows.append((title, status, _relative_age(newest, now=now)))
    return rows


def _cache_items(cache_or_items: Any) -> list[Any]:
    if hasattr(cache_or_items, "list_cache_metadata"):
        return list(cache_or_items.list_cache_metadata(limit=1000))
    return list(cache_or_items or [])


def _newest_for_prefixes(items: Iterable[Any], prefixes: tuple[str, ...]) -> datetime | None:
    candidates = []
    for item in items:
        key = str(_value(item, "key", "") or "")
        if any(key == prefix or key.startswith(prefix) for prefix in prefixes):
            updated_at = _value(item, "updated_at", None)
            if isinstance(updated_at, datetime):
                candidates.append(updated_at)
    return max(candidates) if candidates else None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text[:10], text):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            continue
    return None


def _relative_age(value: datetime | None, *, now: datetime | None = None) -> str:
    if value is None:
        return "-"
    now = now or datetime.now()
    delta = max(timedelta(), now - value)
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "刚刚"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}小时前"
    days = hours // 24
    if days < 30:
        return f"{days}天前"
    return value.strftime("%Y-%m-%d")


def _value(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
