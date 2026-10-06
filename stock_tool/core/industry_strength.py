from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import median
from typing import Any

from stock_tool.core.pit_database import PITDatabase

MIN_INDUSTRY_SAMPLE_COUNT = 10


@dataclass
class IndustryStrengthRow:
    industry: str
    score: float | None
    rank: int | None
    sample_count: int
    low_coverage: bool
    median_return_20d: float | None
    median_return_60d: float | None
    up_ratio_20d: float | None
    raw_score: float | None
    updated_at: str
    status: str


@dataclass(frozen=True)
class StockIndustryStrength:
    code: str
    industry: str
    strength: float | None
    rank: int | None
    sample_count: int
    updated_at: str
    low_coverage: bool = False


@dataclass(frozen=True)
class IndustryStrengthReport:
    asof_date: str
    by_industry: dict[str, IndustryStrengthRow]
    by_code: dict[str, StockIndustryStrength]
    available: bool
    status_message: str


def _compact_date(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits if len(digits) == 8 else ""


def _iso_date(value: Any) -> str:
    digits = _compact_date(value)
    if not digits:
        return ""
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"


def _finite_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pct_return(prices: list[dict[str, Any]], window: int) -> float | None:
    if len(prices) <= window:
        return None
    latest = _finite_float(prices[-1].get("close"))
    base = _finite_float(prices[-window - 1].get("close"))
    if latest is None or base is None or base <= 0:
        return None
    return round((latest - base) / base * 100, 4)


def _latest_industries(db: PITDatabase, asof_date: str, source: str | None = None) -> dict[str, dict[str, Any]]:
    asof = _compact_date(asof_date)
    if not asof:
        return {}
    if source:
        sql = """
            SELECT code, industry, industry_level, asof_date, source, fetched_at
            FROM pit_industry
            WHERE TRIM(industry) <> '' AND REPLACE(asof_date, '-', '') <= ? AND source = ?
            ORDER BY code, REPLACE(asof_date, '-', '') DESC, source
        """
        params: tuple[Any, ...] = (asof, source)
    else:
        sql = """
            SELECT code, industry, industry_level, asof_date, source, fetched_at
            FROM pit_industry
            WHERE TRIM(industry) <> '' AND REPLACE(asof_date, '-', '') <= ?
            ORDER BY code, REPLACE(asof_date, '-', '') DESC, source
        """
        params = (asof,)
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row["code"] or "").split(".")[-1].zfill(6)
        if code and code not in latest:
            latest[code] = dict(row)
    return latest


def _latest_pit_asof(db: PITDatabase) -> str:
    with db.connect() as conn:
        row = conn.execute("SELECT MAX(trade_date) AS day FROM pit_prices").fetchone()
        day = str(row["day"] or "") if row else ""
        if day:
            return _iso_date(day)
        row = conn.execute("SELECT MAX(asof_date) AS day FROM pit_universe").fetchone()
        day = str(row["day"] or "") if row else ""
    return _iso_date(day)


def build_latest_industry_strength_report(
    db: PITDatabase,
    *,
    source: str | None = None,
    min_sample_count: int = MIN_INDUSTRY_SAMPLE_COUNT,
) -> IndustryStrengthReport:
    asof = _latest_pit_asof(db)
    if not asof:
        return IndustryStrengthReport("", {}, {}, False, "PIT 价格或股票池数据为空，无法计算行业强弱。")
    return build_industry_strength_report(db, asof, source=source, min_sample_count=min_sample_count)


def build_industry_strength_report(
    db: PITDatabase,
    asof_date: str,
    *,
    source: str | None = None,
    min_sample_count: int = MIN_INDUSTRY_SAMPLE_COUNT,
) -> IndustryStrengthReport:
    query_asof = _iso_date(asof_date)
    if not query_asof:
        return IndustryStrengthReport("", {}, {}, False, "行业强弱 as-of 日期无效。")

    universe = db.universe_asof(query_asof, source=source)
    if not universe:
        return IndustryStrengthReport(query_asof, {}, {}, False, "PIT 股票池为空，无法计算行业强弱。")
    industries_by_code = _latest_industries(db, query_asof, source=source)
    if not industries_by_code:
        return IndustryStrengthReport(query_asof, {}, {}, False, "PIT 行业表为空，请先更新 PIT 行业数据。")

    grouped: dict[str, list[tuple[str, float | None, float | None]]] = {}
    latest_price_date = ""
    for member in universe:
        code = str(member.get("code") or "").split(".")[-1].zfill(6)
        industry_row = industries_by_code.get(code)
        industry = str((industry_row or {}).get("industry") or "").strip()
        if not code or not industry:
            continue
        prices = db.prices_visible_asof(code, query_asof, lookback_rows=61, source=source)
        if prices:
            latest_price_date = max(latest_price_date, str(prices[-1].get("trade_date") or ""))
        ret20 = _pct_return(prices, 20)
        ret60 = _pct_return(prices, 60)
        grouped.setdefault(industry, []).append((code, ret20, ret60))

    if not grouped:
        return IndustryStrengthReport(query_asof, {}, {}, False, "行业映射存在，但对应股票缺少可见价格数据。")

    rows: dict[str, IndustryStrengthRow] = {}
    for industry, samples in grouped.items():
        returns20 = [value for _code, value, _ret60 in samples if value is not None]
        returns60 = [value for _code, _ret20, value in samples if value is not None]
        sample_count = len(returns20)
        med20 = round(float(median(returns20)), 4) if returns20 else None
        med60 = round(float(median(returns60)), 4) if returns60 else None
        up_ratio = round(sum(1 for value in returns20 if value > 0) / sample_count, 4) if sample_count else None
        low_coverage = sample_count < max(1, int(min_sample_count))
        raw_score = None
        status = "low_coverage" if low_coverage else "ok"
        if not low_coverage and med20 is not None and up_ratio is not None:
            raw_score = round(med20 * 0.45 + (med60 or 0.0) * 0.30 + up_ratio * 100 * 0.25, 4)
        rows[industry] = IndustryStrengthRow(
            industry=industry,
            score=None,
            rank=None,
            sample_count=sample_count,
            low_coverage=low_coverage,
            median_return_20d=med20,
            median_return_60d=med60,
            up_ratio_20d=up_ratio,
            raw_score=raw_score,
            updated_at=latest_price_date or query_asof,
            status=status,
        )

    eligible = [row for row in rows.values() if row.raw_score is not None and not row.low_coverage]
    eligible.sort(key=lambda row: row.raw_score if row.raw_score is not None else -10**9, reverse=True)
    total = len(eligible)
    for rank, row in enumerate(eligible, start=1):
        row.rank = rank
        row.score = 100.0 if total == 1 else round((total - rank) / (total - 1) * 100, 1)

    by_code: dict[str, StockIndustryStrength] = {}
    for industry, samples in grouped.items():
        row = rows[industry]
        for code, _ret20, _ret60 in samples:
            by_code[code] = StockIndustryStrength(
                code=code,
                industry=industry,
                strength=row.score,
                rank=row.rank,
                sample_count=row.sample_count,
                updated_at=row.updated_at,
                low_coverage=row.low_coverage,
            )

    available = any(row.score is not None for row in rows.values())
    if available:
        message = f"行业强弱已按 {query_asof} 计算，可覆盖 {len(by_code)} 只股票。"
    else:
        message = "行业强弱样本覆盖不足，所有行业均少于可过滤样本数。"
    return IndustryStrengthReport(query_asof, rows, by_code, available, message)
