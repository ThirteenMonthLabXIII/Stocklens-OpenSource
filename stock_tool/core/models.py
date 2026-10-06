from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass
class StockQuote:
    code: str
    name: str
    price: float | None = None
    change_pct: float | None = None
    turnover: float | None = None
    market_cap: float | None = None
    pe: float | None = None
    pb: float | None = None
    pe_ttm: float | None = None
    industry: str = ""
    industry_strength: float | None = None
    industry_rank: int | None = None
    industry_sample_count: int | None = None
    industry_updated_at: str = ""


@dataclass
class FinancialMetrics:
    roe: float | None = None
    gross_margin: float | None = None
    net_margin: float | None = None
    debt_ratio: float | None = None
    revenue_growth: float | None = None
    profit_growth: float | None = None
    operating_cashflow_per_share: float | None = None
    report_date: str = ""


@dataclass
class MoneyFlowSnapshot:
    code: str
    rows: list[dict]
    main_3d: float | None = None
    main_5d: float | None = None
    main_10d: float | None = None
    xlarge_5d: float | None = None
    small_5d: float | None = None
    streak: int = 0
    summary: str = ""


@dataclass
class MarketSentiment:
    up: int = 0
    down: int = 0
    flat: int = 0
    limit_up: int = 0
    limit_down: int = 0
    avg_change_pct: float | None = None
    summary: str = ""


@dataclass
class EventCatalyst:
    title: str
    source: str = ""
    date: str = ""
    url: str = ""


@dataclass
class TradeReference:
    action: str
    level: str
    entry_price: float | None
    fail_price: float | None
    target_price: float | None
    horizon_days: int
    position_hint: str
    thesis: str
    risks: list[str]


def fmt_number(value: float | None, suffix: str = "", digits: int = 2) -> str:
    if value is None:
        return "-"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(numeric):
        return "-"
    return f"{numeric:.{digits}f}{suffix}"
