from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import lazy_loader as lazy

# 惰性加载 pandas：仅在首次绘图/计算时才导入，避免拖慢启动。
pd = lazy.load("pandas")

from stock_tool.core.models import FinancialMetrics, StockQuote
from stock_tool.core.technical_profile import TechnicalProfile, build_technical_profile


ChartPeriod = Literal["daily", "weekly", "monthly"]
ChartRange = Literal["60d", "120d", "240d", "all"]
ChartMode = Literal["candlestick", "line"]


def empty_indicators() -> dict[str, dict[str, list[float | None]]]:
    return {
        "ma": {"MA5": [], "MA10": [], "MA20": [], "MA60": []},
        "volume": {"VOL5": [], "VOL10": []},
        "macd": {"DIF": [], "DEA": [], "MACD": []},
        "rsi": {"RSI6": [], "RSI12": [], "RSI24": []},
        "kdj": {"K": [], "D": [], "J": []},
        "boll": {"MID": [], "UPPER": [], "LOWER": []},
    }


@dataclass
class ChartDataPayload:
    version: str
    stock: dict[str, Any]
    period: ChartPeriod
    range: ChartRange
    mode: ChartMode
    ohlcv: list[dict[str, Any]]
    indicators: dict[str, dict[str, list[float | None]]]
    signals: list[dict[str, Any]] = field(default_factory=list)
    quality: list[str] = field(default_factory=list)
    intelligence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _num(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        text = str(value).replace(",", "").strip()
        if text in {"", "-", "--", "nan", "None"}:
            return None
        value = float(text)
        if math.isfinite(value):
            return value
    except (TypeError, ValueError):
        return None
    return None


def _round_or_none(value: Any, digits: int = 4) -> float | None:
    value = _num(value)
    return round(value, digits) if value is not None else None


def _first_col(df: pd.DataFrame, names: list[str]) -> str | None:
    lowered = {str(col).lower(): col for col in df.columns}
    for name in names:
        if name in df.columns:
            return name
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def normalize_ohlcv(history: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    quality: list[str] = []
    if history is None or history.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"]), ["K线数据为空"]

    df = history.copy()
    date_col = _first_col(df, ["日期", "date", "day", "交易日期"])
    open_col = _first_col(df, ["开盘", "open"])
    high_col = _first_col(df, ["最高", "high"])
    low_col = _first_col(df, ["最低", "low"])
    close_col = _first_col(df, ["收盘", "close"])
    volume_col = _first_col(df, ["成交量", "volume", "vol"])

    missing = [
        label
        for label, col in [("日期", date_col), ("开盘", open_col), ("最高", high_col), ("最低", low_col), ("收盘", close_col)]
        if not col
    ]
    if missing:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"]), [f"历史行情缺少字段：{','.join(missing)}"]

    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df[date_col], errors="coerce", format="mixed"),
            "open": df[open_col].map(_num),
            "high": df[high_col].map(_num),
            "low": df[low_col].map(_num),
            "close": df[close_col].map(_num),
        }
    )
    if volume_col:
        out["volume"] = df[volume_col].map(_num)
    else:
        out["volume"] = None
        quality.append("缺少成交量字段，成交量副图将隐藏")

    before = len(out)
    valid_mask = (
        out[["date", "open", "high", "low", "close"]].notna().all(axis=1)
        & (out[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (out["high"] >= out[["open", "close"]].max(axis=1))
        & (out["low"] <= out[["open", "close"]].min(axis=1))
        & (out["high"] >= out["low"])
    )
    out = out.loc[valid_mask].sort_values("date")
    out = out.drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    dropped = before - len(out)
    if dropped:
        quality.append(f"已忽略{dropped}条日期或OHLC异常记录")
    if len(out) < 60:
        quality.append(f"K线样本仅{len(out)}条，部分指标样本不足")
    return out, quality


def aggregate_ohlcv(df: pd.DataFrame, period: ChartPeriod) -> pd.DataFrame:
    if period == "daily" or df.empty:
        return df.copy().reset_index(drop=True)
    rule = "W-FRI" if period == "weekly" else "ME"
    tmp = df.copy()
    tmp["_date"] = pd.to_datetime(tmp["date"], errors="coerce")
    tmp = tmp.dropna(subset=["_date"]).set_index("_date")
    try:
        grouped = tmp.resample(rule)
    except Exception:
        grouped = tmp.resample("M" if period == "monthly" else rule)
    agg = grouped.agg(
        {
            "date": "last",
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    agg = agg.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    agg["date"] = pd.to_datetime(agg["date"], errors="coerce")
    return agg.reset_index(drop=True)


def apply_range(df: pd.DataFrame, chart_range: ChartRange) -> pd.DataFrame:
    if chart_range == "all":
        return df.copy().reset_index(drop=True)
    size = int(chart_range.removesuffix("d"))
    return df.tail(size).copy().reset_index(drop=True)


def _series(values: pd.Series, digits: int = 4) -> list[float | None]:
    return [_round_or_none(value, digits) for value in values.tolist()]


def calculate_indicators(df: pd.DataFrame) -> dict[str, dict[str, list[float | None]]]:
    required = {"close", "high", "low"}
    if df is None or df.empty or not required.issubset(set(df.columns)):
        return empty_indicators()
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = pd.to_numeric(df["volume"], errors="coerce") if "volume" in df else pd.Series([pd.NA] * len(df))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd = (dif - dea) * 2

    def rsi(period: int) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.replace(0, pd.NA)
        return 100 - 100 / (1 + rs)

    low_n = low.rolling(9).min()
    high_n = high.rolling(9).max()
    rsv = (close - low_n) / (high_n - low_n).replace(0, pd.NA) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d

    boll_mid = close.rolling(20).mean()
    boll_std = close.rolling(20).std()
    return {
        "ma": {
            "MA5": _series(close.rolling(5).mean()),
            "MA10": _series(close.rolling(10).mean()),
            "MA20": _series(close.rolling(20).mean()),
            "MA60": _series(close.rolling(60).mean()),
        },
        "volume": {
            "VOL5": _series(volume.rolling(5).mean(), 2),
            "VOL10": _series(volume.rolling(10).mean(), 2),
        },
        "macd": {
            "DIF": _series(dif),
            "DEA": _series(dea),
            "MACD": _series(macd),
        },
        "rsi": {
            "RSI6": _series(rsi(6)),
            "RSI12": _series(rsi(12)),
            "RSI24": _series(rsi(24)),
        },
        "kdj": {
            "K": _series(k),
            "D": _series(d),
            "J": _series(j),
        },
        "boll": {
            "MID": _series(boll_mid),
            "UPPER": _series(boll_mid + 2 * boll_std),
            "LOWER": _series(boll_mid - 2 * boll_std),
        },
    }


def build_signals(df: pd.DataFrame, indicators: dict[str, dict[str, list[float | None]]]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    if len(df) < 2:
        return signals
    dates = df["date"].dt.strftime("%Y-%m-%d").tolist()
    close = df["close"].tolist()
    ma20 = indicators["ma"]["MA20"]
    ma60 = indicators["ma"]["MA60"]
    dif = indicators["macd"]["DIF"]
    dea = indicators["macd"]["DEA"]
    for idx in range(1, len(df)):
        if ma20[idx - 1] is not None and ma60[idx - 1] is not None and ma20[idx] is not None and ma60[idx] is not None:
            if ma20[idx - 1] <= ma60[idx - 1] and ma20[idx] > ma60[idx]:
                signals.append({"date": dates[idx], "type": "ma_golden", "label": "MA20上穿MA60", "price": _round_or_none(close[idx])})
            elif ma20[idx - 1] >= ma60[idx - 1] and ma20[idx] < ma60[idx]:
                signals.append({"date": dates[idx], "type": "ma_dead", "label": "MA20下穿MA60", "price": _round_or_none(close[idx])})
        if dif[idx - 1] is not None and dea[idx - 1] is not None and dif[idx] is not None and dea[idx] is not None:
            if dif[idx - 1] <= dea[idx - 1] and dif[idx] > dea[idx]:
                signals.append({"date": dates[idx], "type": "macd_golden", "label": "MACD金叉", "price": _round_or_none(close[idx])})
            elif dif[idx - 1] >= dea[idx - 1] and dif[idx] < dea[idx]:
                signals.append({"date": dates[idx], "type": "macd_dead", "label": "MACD死叉", "price": _round_or_none(close[idx])})
    return signals[-40:]


def _last(values: list[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def build_intelligence(
    quote: StockQuote,
    metrics: FinancialMetrics | None,
    technical: TechnicalProfile,
    indicators: dict[str, dict[str, list[float | None]]],
    quality: list[str],
) -> dict[str, Any]:
    close = technical.close or quote.price
    ma5 = _last(indicators["ma"]["MA5"])
    ma20 = _last(indicators["ma"]["MA20"])
    ma60 = _last(indicators["ma"]["MA60"])
    rsi6 = _last(indicators["rsi"]["RSI6"])
    macd = _last(indicators["macd"]["MACD"])
    risks = list(technical.risk_flags)
    opportunities: list[str] = []
    if technical.opportunity_label:
        opportunities.append(technical.opportunity_label)
    if close is not None and ma20 is not None and close > ma20:
        opportunities.append("收盘价位于MA20上方")
    if macd is not None and macd > 0:
        opportunities.append("MACD柱线偏多")
    if rsi6 is not None and rsi6 >= 80:
        risks.append("RSI6短线过热")
    if close is not None and ma60 is not None and close < ma60:
        risks.append("价格低于MA60")

    valuation = {}
    if metrics:
        valuation = {
            "roe": metrics.roe,
            "gross_margin": metrics.gross_margin,
            "net_margin": metrics.net_margin,
            "debt_ratio": metrics.debt_ratio,
            "revenue_growth": metrics.revenue_growth,
            "profit_growth": metrics.profit_growth,
            "report_date": metrics.report_date,
        }
    return {
        "quote": {
            "price": quote.price,
            "change_pct": quote.change_pct,
            "turnover": quote.turnover,
            "market_cap": quote.market_cap,
            "pe": quote.pe,
            "pb": quote.pb,
        },
        "valuation": valuation,
        "trend": {
            "stage": technical.trend_stage,
            "status": technical.status,
            "summary": technical.summary,
        },
        "moving_average": {
            "ma5": ma5,
            "ma20": ma20,
            "ma60": ma60,
            "state": "多头排列" if ma5 and ma20 and ma60 and ma5 > ma20 > ma60 else "非多头排列",
        },
        "momentum": {
            "rsi6": rsi6,
            "macd": macd,
            "kdj_j": _last(indicators["kdj"]["J"]),
        },
        "risk_tags": risks or ["暂未识别到显著技术风险"],
        "opportunity_tags": opportunities or ["等待更明确量价信号"],
        "data_gaps": quality or ["暂无明显数据缺口"],
    }


def build_chart_payload(
    quote: StockQuote,
    history: pd.DataFrame,
    metrics: FinancialMetrics | None = None,
    period: ChartPeriod = "daily",
    chart_range: ChartRange = "240d",
    mode: ChartMode = "candlestick",
) -> ChartDataPayload:
    normalized, quality = normalize_ohlcv(history)
    aggregated = aggregate_ohlcv(normalized, period)
    ranged = apply_range(aggregated, chart_range)
    indicators = calculate_indicators(ranged)
    profile_input = aggregated.rename(columns={"date": "日期", "open": "开盘", "high": "最高", "low": "最低", "close": "收盘", "volume": "成交量"})
    technical = build_technical_profile(profile_input)
    if len(ranged) < 60:
        quality.append("当前范围少于60条，MA60等指标样本不足")
    if not ranged.empty and ranged["volume"].isna().all():
        quality.append("当前范围无有效成交量")
    quality.extend(technical.data_gaps)
    quality = list(dict.fromkeys(quality))
    ohlcv = []
    for _, row in ranged.iterrows():
        ohlcv.append(
            {
                "date": pd.to_datetime(row["date"]).strftime("%Y-%m-%d"),
                "open": _round_or_none(row["open"]),
                "high": _round_or_none(row["high"]),
                "low": _round_or_none(row["low"]),
                "close": _round_or_none(row["close"]),
                "volume": _round_or_none(row["volume"], 2),
            }
        )
    signals = build_signals(ranged, indicators) if not ranged.empty else []
    return ChartDataPayload(
        version="1.0",
        stock={
            "code": quote.code,
            "name": quote.name,
            "price": quote.price,
            "change_pct": quote.change_pct,
        },
        period=period,
        range=chart_range,
        mode=mode,
        ohlcv=ohlcv,
        indicators=indicators,
        signals=signals,
        quality=quality,
        intelligence=build_intelligence(quote, metrics, technical, indicators, quality),
    )
