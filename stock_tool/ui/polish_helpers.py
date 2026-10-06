from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from stock_tool.core.cache import CacheMetadata, FetchLogRecord
from stock_tool.core.query_schema import AMBIGUOUS_TERMS, EXECUTABLE_RULES, FIELD_LABELS, FIELD_UNITS, SORTABLE_FIELDS


@dataclass(frozen=True)
class AcceptanceReportSummary:
    time_text: str
    conclusion: str
    live_enabled: bool
    elapsed_seconds: float | None
    failed_blocks: list[str]
    txt_path: Path
    json_path: Path
    error: str = ""


@dataclass(frozen=True)
class DataSourceHealthRow:
    action: str
    sources: str
    direct_success_rate: str
    latest_status: str
    cache_fallback: str
    cache_age: str
    last_failure: str
    level: str


@dataclass(frozen=True)
class QueryHelpRow:
    field: str
    label: str
    unit: str
    operators: str
    example: str


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

CACHE_GROUPS = [
    ("全市场行情", ("spot_quotes",)),
    ("财务指标", ("financial:",)),
    ("K线历史", ("history:",)),
    ("主力资金", ("money_flow:",)),
    ("市场情绪", ("market_sentiment",)),
    ("事件催化", ("events:",)),
]

STATUS_LABELS = {
    "success": "成功",
    "failed": "失败",
    "cache": "缓存",
    "info": "信息",
}

FIELD_EXAMPLES = {
    "roe": "ROE >= 15",
    "net_margin": "净利率 >= 10",
    "debt_ratio": "资产负债率 <= 60",
    "pe": "PE <= 40",
    "pb": "PB <= 2",
    "gross_margin": "毛利率 >= 30",
    "revenue_growth": "营收增长率 >= 20",
    "profit_growth": "净利润增长率 >= 20",
    "market_cap_yi": "总市值 between 100 到 1000",
    "trend_20d": "近20日趋势 > 0",
    "volume_ratio": "量比 >= 1.5",
    "pullback_pct": "回撤 <= 15",
    "max_drawdown_30d": "最大回撤 <= 20",
    "max_drawdown_60d": "60日回撤 <= 20",
    "turnover": "换手率 between 3 到 15",
    "main_5d_yi": "近5日主力净流入 > 0",
    "rsi": "RSI between 30 到 70",
    "box_score": "箱体评分 >= 60",
    "box_range_pct": "箱体振幅 <= 25",
    "industry_strength": "行业强度 >= 60",
}

CLARIFICATION_TEMPLATES = [
    ("估值低", "估值低默认 PE <= 30；可改成 PE <= 20 或补充 PB <= 2。"),
    ("低估", "低估默认 PE <= 30；可改成 PE <= 20、PB <= 2。"),
    ("便宜", "便宜默认 PE <= 30；可改成 PE <= 25。"),
    ("回撤小", "回撤小默认 距20日前高回撤 <= 15%；可改成 回撤 <= 10。"),
    ("最大回撤小", "最大回撤小默认 近30日最大回撤 <= 20%；可改成 最大回撤 <= 15。"),
    ("30日回撤小", "30日回撤小默认 近30日最大回撤 <= 15%；可改成 30日回撤 <= 10。"),
    ("60日回撤小", "60日回撤小默认 近60日最大回撤 <= 20%；可改成 60日回撤 <= 15。"),
    ("放量", "放量默认 量比 >= 1.5；可改成 量比 >= 2。"),
    ("趋势好", "趋势好默认 近20日趋势 > 0；可改成 站上20日线。"),
    ("走势好", "走势好默认 近20日趋势 > 0。"),
    ("换手活跃", "换手活跃默认 换手率 between 3 到 15；可改成 换手率 between 5 到 12。"),
    ("主力进场", "主力进场默认 近5日主力净流入 > 0；资金接口不可用时会忽略该条件并提示。"),
    ("成长好", "成长好默认 营收增长率 >= 20；可补充 净利润增长率 >= 20。"),
    ("高成长", "高成长默认 营收增长率 >= 20；可改成 营收增长率 >= 30。"),
    ("稳健", "稳健默认 ROE >= 10、资产负债率 <= 60。"),
    ("龙头", "龙头默认 总市值 >= 500；可改成 总市值 >= 1000。"),
    ("优质", "优质默认 ROE >= 15、毛利率 >= 30。"),
    ("箱体横盘", "箱体横盘默认 箱体评分 >= 60 且仍在箱体内；可补充 箱体振幅 <= 25。"),
    ("横盘蓄势", "横盘蓄势默认 箱体评分 >= 60；可补充 行业强度 >= 60。"),
    ("震荡整理", "震荡整理默认 箱体评分 >= 60；可改成 箱体振幅 <= 20。"),
    ("行业强", "行业强默认 行业强度 >= 60；行业数据依赖 PIT/筛选数据，缺失时会提示更新。"),
    ("强势行业", "强势行业默认 行业强度 >= 60；行业数据依赖 PIT/筛选数据。"),
]


def clean_export_filename(name: str, fallback: str = "export") -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", name).strip(" ._")
    return cleaned[:120] if cleaned else fallback


def supported_query_help_rows() -> list[QueryHelpRow]:
    rows = []
    for field_name in sorted(EXECUTABLE_RULES):
        rows.append(
            QueryHelpRow(
                field=field_name,
                label=FIELD_LABELS.get(field_name, field_name),
                unit=FIELD_UNITS.get(field_name, ""),
                operators=", ".join(sorted(EXECUTABLE_RULES[field_name])),
                example=FIELD_EXAMPLES.get(field_name, ""),
            )
        )
    sort_labels = [
        FIELD_LABELS.get(field_name, "主评分/专业分" if field_name == "local_score" else field_name)
        for field_name in sorted(SORTABLE_FIELDS)
    ]
    rows.append(QueryHelpRow("sort", "排序字段", "-", "asc, desc", "可排序：" + "、".join(sort_labels)))
    return rows


def _template_matches(text: str, term: str) -> bool:
    if term != "回撤小":
        return term in text
    narrowed = text
    for explicit_term in ("最大回撤小", "30日回撤小", "60日回撤小"):
        narrowed = narrowed.replace(explicit_term, "")
    return term in narrowed


def clarification_template_for_text(text: str) -> str:
    normalized = (text or "").strip()
    matched = [
        template
        for term, template in CLARIFICATION_TEMPLATES
        if _template_matches(normalized, term)
    ]
    if not matched:
        matched = [template for term, template in CLARIFICATION_TEMPLATES if term in AMBIGUOUS_TERMS][:4]
    return "\n".join(matched[:4])


def format_relative_age(value: datetime | None, *, now: datetime | None = None) -> str:
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


def _cache_group_for_key(key: str) -> str:
    for action, prefixes in CACHE_GROUPS:
        if any(key == prefix or key.startswith(prefix) for prefix in prefixes):
            return action
    return "其他缓存"


def cache_age_overview(cache_items: list[CacheMetadata], *, now: datetime | None = None) -> str:
    if not cache_items:
        return "当前没有行情/财务缓存。"
    newest = max(item.updated_at for item in cache_items)
    oldest = min(item.updated_at for item in cache_items)
    groups = sorted({_cache_group_for_key(item.key) for item in cache_items})
    group_text = "、".join(groups[:5])
    if len(groups) > 5:
        group_text += f"等{len(groups)}类"
    return (
        f"缓存 {len(cache_items)} 条；最新 {format_relative_age(newest, now=now)}，"
        f"最旧 {format_relative_age(oldest, now=now)}；覆盖 {group_text}。"
    )


def build_data_source_health_rows(
    logs: list[FetchLogRecord],
    cache_items: list[CacheMetadata],
    *,
    now: datetime | None = None,
) -> list[DataSourceHealthRow]:
    now = now or datetime.now()
    actions = [action for action, _prefixes in CACHE_GROUPS]
    for log in logs:
        if log.action not in actions:
            actions.append(log.action)

    cache_by_action: dict[str, list[CacheMetadata]] = {}
    for item in cache_items:
        cache_by_action.setdefault(_cache_group_for_key(item.key), []).append(item)

    rows: list[DataSourceHealthRow] = []
    for action in actions:
        action_logs = [log for log in logs if log.action == action]
        if not action_logs and action not in cache_by_action:
            continue
        success = sum(1 for log in action_logs if log.status == "success")
        failed = sum(1 for log in action_logs if log.status == "failed")
        cache_hit = sum(1 for log in action_logs if log.status == "cache")
        direct_total = success + failed
        direct_rate = f"{success / direct_total * 100:.1f}%" if direct_total else "-"
        latest = max(action_logs, key=lambda log: log.created_at) if action_logs else None
        latest_status = STATUS_LABELS.get(latest.status, latest.status) if latest else "-"
        if latest and latest.status == "cache" and "限流" in (latest.message or ""):
            latest_status = "限流缓存"
        sources = sorted({log.source for log in action_logs if log.source and log.source != "cache"})
        source_text = "、".join(sources[:3]) if sources else "-"
        if len(sources) > 3:
            source_text += f"等{len(sources)}个"
        failures = [log for log in action_logs if log.status == "failed"]
        last_failure = max(failures, key=lambda log: log.created_at).message if failures else "-"
        if latest and latest.status == "cache" and "限流" in (latest.message or ""):
            last_failure = latest.message
        action_cache = cache_by_action.get(action, [])
        newest_cache = max((item.updated_at for item in action_cache), default=None)
        cache_age = f"{format_relative_age(newest_cache, now=now)}（{len(action_cache)}条）" if action_cache else "-"
        cache_fallback = f"{cache_hit}次" if cache_hit else "无"

        if latest and latest.status == "failed" and not action_cache:
            level = "failed"
        elif latest and latest.status == "failed":
            level = "warning"
        elif latest and latest.status == "cache":
            level = "warning" if "限流" in (latest.message or "") else "cache"
        elif newest_cache and now - newest_cache > timedelta(hours=72):
            level = "warning"
        else:
            level = "success" if success else "info"

        rows.append(
            DataSourceHealthRow(
                action=action,
                sources=source_text,
                direct_success_rate=direct_rate,
                latest_status=latest_status,
                cache_fallback=cache_fallback,
                cache_age=cache_age,
                last_failure=last_failure[:160],
                level=level,
            )
        )
    return rows


def comparison_items_for_filter(comparison: Any, filter_label: str):
    if filter_label == "新增":
        return comparison.added
    if filter_label == "消失":
        return comparison.removed
    if filter_label == "保留":
        return comparison.kept
    if filter_label == "排名变化":
        return [item for item in comparison.kept if item.rank_delta not in (None, 0)]
    if filter_label == "指标变化":
        return [item for item in comparison.kept if item.metric_changes]
    return comparison.items


def load_acceptance_report_summaries(report_dir: Path, limit: int = 10) -> list[AcceptanceReportSummary]:
    if not report_dir.exists():
        return []
    summaries: list[AcceptanceReportSummary] = []
    for json_path in report_dir.glob("*.json"):
        txt_path = json_path.with_suffix(".txt")
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
            blocks = payload.get("blocks") if isinstance(payload, dict) else []
            failed_blocks = [
                str(block.get("name") or "未命名区块")
                for block in blocks
                if isinstance(block, dict) and str(block.get("status") or "").upper() == "FAIL"
            ]
            elapsed = payload.get("elapsed_seconds")
            if elapsed is None and isinstance(blocks, list):
                elapsed = sum(float(block.get("elapsed_seconds") or 0) for block in blocks if isinstance(block, dict))
            conclusion = str(payload.get("conclusion") or ("不可发版" if payload.get("failed") else "可发版"))
            report_txt = Path(str(payload.get("txt_report") or txt_path))
            summaries.append(
                AcceptanceReportSummary(
                    time_text=str(payload.get("finished_at") or payload.get("started_at") or ""),
                    conclusion=conclusion,
                    live_enabled=bool(payload.get("live_enabled")),
                    elapsed_seconds=round(float(elapsed), 3) if elapsed is not None else None,
                    failed_blocks=failed_blocks,
                    txt_path=report_txt,
                    json_path=json_path,
                )
            )
        except Exception as exc:
            mtime = datetime.fromtimestamp(json_path.stat().st_mtime).isoformat(timespec="seconds")
            summaries.append(
                AcceptanceReportSummary(
                    time_text=mtime,
                    conclusion="JSON损坏",
                    live_enabled=False,
                    elapsed_seconds=None,
                    failed_blocks=[],
                    txt_path=txt_path,
                    json_path=json_path,
                    error=str(exc),
                )
            )
    summaries.sort(key=lambda item: item.time_text or "", reverse=True)
    return summaries[: max(1, int(limit))]
