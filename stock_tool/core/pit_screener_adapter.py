from __future__ import annotations

import json
import math
from typing import Any

import lazy_loader as lazy

from stock_tool.core.factor_store import FactorSnapshot, _missing_fields, _pct_from_ma
from stock_tool.core.industry_strength import build_industry_strength_report
from stock_tool.core.pit_database import PITDatabase
from stock_tool.core.screener_engine import ScreenFilter, ScreenReport, screen_factor_snapshots
from stock_tool.core.screening_contract import snapshot_id
from stock_tool.core.technical_profile import DEFAULT_BOX_WINDOW_DAYS, TechnicalProfile, build_technical_profile


pd = lazy.load("pandas")


FINANCIAL_METRIC_FIELDS = {
    "roe",
    "gross_margin",
    "net_margin",
    "debt_ratio",
    "revenue_growth",
    "profit_growth",
}


def build_pit_snapshots(
    db: PITDatabase,
    asof_date: str,
    *,
    source: str | None = None,
    scan_limit: int = 300,
    box_window_days: int = DEFAULT_BOX_WINDOW_DAYS,
) -> list[FactorSnapshot]:
    """Build deterministic read-only FactorSnapshot rows from PIT data."""
    query_asof = _iso_date(asof_date)
    if not query_asof:
        return []
    universe = sorted(db.universe_asof(query_asof, source=source), key=lambda row: str(row.get("code") or ""))
    industry_report = build_industry_strength_report(db, query_asof, source=source)
    snapshots: list[FactorSnapshot] = []
    for member in universe[: max(1, int(scan_limit))]:
        code = str(member.get("code") or "")
        name = str(member.get("name") or "")
        if not code or not name:
            continue
        financial_rows = db.financials_visible_asof(code, query_asof)
        financials = _latest_visible_financials(financial_rows)
        financial_available_at = _latest_visible_financial_pub_date(financial_rows)
        prices = db.prices_visible_asof(code, query_asof, lookback_rows=max(61, int(box_window_days or 60) + 1), source=source)
        snapshot = _snapshot_from_pit_rows(
            code,
            name,
            query_asof,
            financials,
            financial_available_at,
            prices,
            industry_report.by_code.get(code.zfill(6)),
            box_window_days=box_window_days,
        )
        snapshots.append(snapshot)
    return snapshots


def screen_pit_asof(
    db: PITDatabase,
    asof_date: str,
    filters: ScreenFilter,
    *,
    source: str | None = None,
    professional_formula: dict[str, Any] | None = None,
    legacy: bool = False,
) -> ScreenReport:
    if not legacy:
        raise ValueError("PIT 公式筛选仅支持 legacy 兼容入口；新生产流程必须使用 CandidateLens")
    query_asof = _iso_date(asof_date)
    snapshots = build_pit_snapshots(
        db,
        query_asof,
        source=source,
        scan_limit=filters.scan_limit,
        box_window_days=filters.box_window_days,
    )
    data_source = f"pit:{source or 'default'}"
    report = screen_factor_snapshots(snapshots, filters, professional_formula=professional_formula)
    report.data_source = data_source
    report.snapshot_asof = query_asof
    report.data_snapshot_id = snapshot_id(snapshots, data_source=data_source)
    return report


def _latest_visible_financials(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for row in rows:
        metric = str(row.get("metric") or "")
        if metric in FINANCIAL_METRIC_FIELDS:
            values[metric] = _finite_float(row.get("value"))
    return values


def _latest_visible_financial_pub_date(rows: list[dict[str, Any]]) -> str:
    """Return the latest publication date among the financial facts used."""

    dates = [
        _iso_date(row.get("pub_date"))
        for row in rows
        if str(row.get("metric") or "") in FINANCIAL_METRIC_FIELDS
    ]
    return max((value for value in dates if value), default="")


def _snapshot_from_pit_rows(
    code: str,
    name: str,
    asof_date: str,
    financials: dict[str, float | None],
    financial_available_at: str,
    prices: list[dict[str, Any]],
    industry_info: Any | None = None,
    *,
    box_window_days: int = DEFAULT_BOX_WINDOW_DAYS,
) -> FactorSnapshot:
    last_price = prices[-1] if prices else {}
    last_trade_date = _iso_date(last_price.get("trade_date")) or asof_date
    last_available_at = _iso_date(last_price.get("asof_date")) or last_trade_date
    closes = [_finite_float(row.get("close")) for row in prices]
    close = closes[-1] if closes else None
    technical = _technical_from_pit_prices(prices, box_window_days=box_window_days)
    snapshot = FactorSnapshot(
        code=code,
        name=name,
        price=_finite_float(last_price.get("close_raw")),
        pe=None,
        pe_ttm=_finite_float(last_price.get("pe_ttm")),
        pb=_finite_float(last_price.get("pb_mrq")),
        market_cap_yi=None,
        roe=financials.get("roe"),
        gross_margin=financials.get("gross_margin"),
        net_margin=financials.get("net_margin"),
        debt_ratio=financials.get("debt_ratio"),
        revenue_growth=financials.get("revenue_growth"),
        profit_growth=financials.get("profit_growth"),
        trend_20d=_pct_from_ma(technical.close, technical.ma20) if technical.available else _pct_from_ma(close, _mean_last(closes, 20)),
        trend_60d=_pct_from_ma(technical.close, technical.ma60) if technical.available else _pct_from_ma(close, _mean_last(closes, 60)),
        box_score=technical.box_score,
        box_upper=technical.box_upper,
        box_lower=technical.box_lower,
        box_range_pct=technical.box_range_pct,
        box_position_pct=technical.box_position_pct,
        box_position_label=technical.box_position_label,
        box_days=technical.box_days,
        box_ma20_slope_pct=technical.box_ma20_slope_pct,
        box_boundary_touch_count=technical.box_boundary_touch_count,
        box_is_sideways=technical.box_is_sideways,
        box_summary=technical.box_summary,
        industry=str(getattr(industry_info, "industry", "") or ""),
        industry_strength=getattr(industry_info, "strength", None),
        industry_rank=getattr(industry_info, "rank", None),
        industry_sample_count=getattr(industry_info, "sample_count", None),
        industry_updated_at=str(getattr(industry_info, "updated_at", "") or ""),
        technical_label=technical.opportunity_label or "技术画像缺失",
        technical_status=technical.status or "missing",
        technical_summary=technical.summary or "技术画像缺失",
        risk_flags_json=json.dumps(technical.risk_flags, ensure_ascii=False),
        data_date=last_trade_date,
        quote_updated_at=last_available_at,
        financial_updated_at=financial_available_at,
        history_updated_at=last_available_at,
        pe_ttm_updated_at=last_available_at,
        pe_ttm_source=str(last_price.get("source") or ""),
        pb_updated_at=last_available_at,
        pb_source=str(last_price.get("source") or ""),
        updated_at=f"{asof_date}T00:00:00",
    )
    snapshot.missing_fields = _missing_fields(snapshot)
    return snapshot


def _technical_from_pit_prices(prices: list[dict[str, Any]], *, box_window_days: int) -> TechnicalProfile:
    if not prices:
        return TechnicalProfile(False, status="missing", opportunity_label="技术画像缺失", summary="技术画像缺失")
    rows = []
    for row in prices:
        close = _finite_float(row.get("close"))
        if close is None:
            continue
        high = _finite_float(row.get("high")) or close
        low = _finite_float(row.get("low")) or close
        open_price = _finite_float(row.get("open")) or close
        rows.append(
            {
                "日期": row.get("trade_date"),
                "开盘": open_price,
                "最高": max(high, open_price, close),
                "最低": min(low, open_price, close),
                "收盘": close,
                "成交量": _finite_float(row.get("volume")),
            }
        )
    if len(rows) < 60:
        return TechnicalProfile(False, status="low_data", opportunity_label="技术画像缺失", summary="PIT 价格少于60条，技术画像缺失")
    try:
        return build_technical_profile(pd.DataFrame(rows), box_window_days=box_window_days)
    except Exception as exc:
        return TechnicalProfile(False, status="missing", opportunity_label="技术画像缺失", summary=f"PIT 技术画像失败：{exc}")


def _mean_last(values: list[float | None], window: int) -> float | None:
    if len(values) < window:
        return None
    tail = values[-window:]
    if any(value is None for value in tail):
        return None
    return sum(value for value in tail if value is not None) / window


def _finite_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _iso_date(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) != 8:
        return ""
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
