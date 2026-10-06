from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

import lazy_loader as lazy

# 惰性加载 pandas：仅在首次使用（计算技术面指标时）才导入，避免拖慢启动。
pd = lazy.load("pandas")

DEFAULT_BOX_WINDOW_DAYS = 60


@dataclass
class TechnicalProfile:
    available: bool
    status: str = "low_data"
    setup_name: str = "unknown"
    trend_stage: str = "unknown"
    close: float | None = None
    ma5: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    rsi14: float | None = None
    kdj_j: float | None = None
    atr14: float | None = None
    volume_ratio: float | None = None
    pullback_pct: float | None = None
    max_drawdown_30d: float | None = None
    max_drawdown_60d: float | None = None
    entry_trigger: float | None = None
    fail_level: float | None = None
    target_level: float | None = None
    box_score: float | None = None
    box_upper: float | None = None
    box_lower: float | None = None
    box_range_pct: float | None = None
    box_position_pct: float | None = None
    box_position_label: str = ""
    box_days: int = 0
    box_ma20_slope_pct: float | None = None
    box_boundary_touch_count: int = 0
    box_is_sideways: bool = False
    box_summary: str = ""
    opportunity_label: str = "数据不足"
    risk_flags: list[str] = field(default_factory=list)
    summary: str = ""
    data_gaps: list[str] = field(default_factory=list)


def _num(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    except (TypeError, ValueError):
        return None


def _round(value: Any, digits: int = 2) -> float | None:
    value = _num(value)
    return round(value, digits) if value is not None else None


def _col(df: pd.DataFrame, *names: str) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    return None


def _clean_history(history: pd.DataFrame) -> tuple[pd.DataFrame | None, list[str], bool]:
    df = history.copy()
    close_col = _col(df, "收盘", "close")
    high_col = _col(df, "最高", "high")
    low_col = _col(df, "最低", "low")
    open_col = _col(df, "开盘", "open")
    volume_col = _col(df, "成交量", "volume", "vol")
    date_col = _col(df, "日期", "date", "day", "交易日期")
    if not close_col or not high_col or not low_col:
        return None, ["历史行情缺少收盘/最高/最低字段"], False

    out = pd.DataFrame(
        {
            "收盘": df[close_col].map(_num),
            "最高": df[high_col].map(_num),
            "最低": df[low_col].map(_num),
        }
    )
    out["开盘"] = df[open_col].map(_num) if open_col else out["收盘"]

    gaps: list[str] = []
    if date_col:
        out["日期"] = pd.to_datetime(df[date_col], errors="coerce", format="mixed")
    if volume_col:
        out["成交量"] = df[volume_col].map(_num)
        out.loc[out["成交量"].notna() & (out["成交量"] < 0), "成交量"] = None
    else:
        out["成交量"] = None

    mask = (
        out[["开盘", "最高", "最低", "收盘"]].notna().all(axis=1)
        & (out[["开盘", "最高", "最低", "收盘"]] > 0).all(axis=1)
        & (out["最高"] >= out[["开盘", "收盘"]].max(axis=1))
        & (out["最低"] <= out[["开盘", "收盘"]].min(axis=1))
        & (out["最高"] >= out["最低"])
    )
    if date_col:
        mask &= out["日期"].notna()
    cleaned = out.loc[mask].copy()
    if date_col and not cleaned.empty:
        cleaned = cleaned.sort_values("日期").drop_duplicates(subset=["日期"], keep="last")
    cleaned = cleaned.reset_index(drop=True)

    dropped = len(out) - len(cleaned)
    if dropped:
        gaps.append(f"已忽略{dropped}条日期或OHLC异常记录")

    volume_missing = (not volume_col) or cleaned.empty or cleaned["成交量"].isna().all()
    if volume_missing:
        gaps.append("缺少成交量，机会降级")
    return cleaned, list(dict.fromkeys(gaps)), volume_missing


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    value = 100 - 100 / (1 + rs)
    return _round(value.iloc[-1])


def _kdj_j(df: pd.DataFrame, high_col: str, low_col: str, close_col: str) -> float | None:
    low_n = df[low_col].rolling(9).min()
    high_n = df[high_col].rolling(9).max()
    rsv = (df[close_col] - low_n) / (high_n - low_n).replace(0, pd.NA) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    return _round(j.iloc[-1])


def _atr(df: pd.DataFrame, high_col: str, low_col: str, close_col: str, period: int = 14) -> float | None:
    high = df[high_col].astype(float)
    low = df[low_col].astype(float)
    close = df[close_col].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return _round(tr.rolling(period).mean().iloc[-1])


def _max_drawdown_pct(close: pd.Series, window: int) -> float | None:
    values = close.tail(window).astype(float)
    if len(values) < window or values.isna().any() or (values <= 0).any():
        return None
    running_peak = values.cummax()
    drawdown = (running_peak - values) / running_peak * 100
    return _round(drawdown.max())


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _box_position_label(position_pct: float | None, current_in_box: bool, close: float, lower: float, upper: float) -> str:
    if not current_in_box:
        if close > upper:
            return "上破箱体"
        if close < lower:
            return "跌破箱体"
    if position_pct is None:
        return "位置缺失"
    if position_pct <= 30:
        return "接近下沿"
    if position_pct >= 70:
        return "接近上沿"
    return "箱体中部"


def _boundary_touch_count(reference: pd.DataFrame, upper: float, lower: float) -> int:
    width = upper - lower
    if width <= 0:
        return 0
    tolerance = max(width * 0.06, lower * 0.006)
    high_touches = int((reference["最高"].astype(float) >= upper - tolerance).sum())
    low_touches = int((reference["最低"].astype(float) <= lower + tolerance).sum())
    return high_touches + low_touches


def _box_metrics(
    df: pd.DataFrame,
    close_s: pd.Series,
    close: float,
    max_drawdown_60d: float | None,
    *,
    volume_missing: bool,
    window_days: int,
) -> dict[str, Any]:
    window = max(20, int(window_days or DEFAULT_BOX_WINDOW_DAYS))
    if len(df) < window:
        return {
            "box_score": None,
            "box_upper": None,
            "box_lower": None,
            "box_range_pct": None,
            "box_position_pct": None,
            "box_position_label": "",
            "box_days": len(df),
            "box_ma20_slope_pct": None,
            "box_boundary_touch_count": 0,
            "box_is_sideways": False,
            "box_summary": f"箱体数据不足：有效K线{len(df)}条，少于{window}条",
        }

    reference = df.iloc[-window - 1 : -1].copy() if len(df) > window else df.tail(window).copy()
    box_days = len(reference)
    upper = _num(reference["最高"].astype(float).max())
    lower = _num(reference["最低"].astype(float).min())
    if upper is None or lower is None or upper <= 0 or lower <= 0 or upper <= lower:
        return {
            "box_score": None,
            "box_upper": _round(upper),
            "box_lower": _round(lower),
            "box_range_pct": None,
            "box_position_pct": None,
            "box_position_label": "",
            "box_days": box_days,
            "box_ma20_slope_pct": None,
            "box_boundary_touch_count": 0,
            "box_is_sideways": False,
            "box_summary": "箱体区间异常，无法计算箱体画像",
        }

    range_pct = (upper - lower) / lower * 100
    current_in_box = lower <= close <= upper
    position_pct = (close - lower) / (upper - lower) * 100 if upper > lower else None
    position_label = _box_position_label(position_pct, current_in_box, close, lower, upper)

    ma20_series = close_s.rolling(20).mean()
    ma20_current = _num(ma20_series.iloc[-1])
    ma20_prev = _num(ma20_series.iloc[-11]) if len(ma20_series) >= 11 else None
    ma20_slope = (
        (ma20_current - ma20_prev) / ma20_prev * 100
        if ma20_current is not None and ma20_prev is not None and ma20_prev > 0
        else None
    )
    touch_count = _boundary_touch_count(reference, upper, lower)

    range_score = 35 * (1 - _clamp(range_pct, 0, 25) / 25)
    slope_abs = abs(ma20_slope) if ma20_slope is not None else 5
    slope_score = 20 * (1 - _clamp(slope_abs, 0, 5) / 5)
    drawdown = max_drawdown_60d if max_drawdown_60d is not None else 25
    stability_score = 20 * (1 - _clamp(drawdown, 0, 25) / 25)
    touch_score = 15 * _clamp(touch_count / 6, 0, 1)
    if volume_missing:
        volume_score = 5
        volume_note = "量能缺失"
    else:
        volume = reference["成交量"].dropna().astype(float)
        if len(volume) >= 10 and _num(volume.mean()) not in (None, 0):
            volume_cv = float(volume.std(ddof=0) / volume.mean())
            volume_score = 10 * (1 - _clamp(volume_cv, 0, 1))
            volume_note = "量能温和" if volume_cv <= 0.6 else "量能波动偏大"
        else:
            volume_score = 5
            volume_note = "量能样本不足"
    score = round(_clamp(range_score + slope_score + stability_score + touch_score + volume_score, 0, 100), 1)

    slope_ok = ma20_slope is not None and abs(ma20_slope) <= 5
    drawdown_ok = max_drawdown_60d is not None and max_drawdown_60d <= 25
    box_is_sideways = bool(range_pct <= 25 and current_in_box and slope_ok and drawdown_ok)
    slope_text = f"{ma20_slope:.1f}%" if ma20_slope is not None else "-"
    summary = (
        f"箱体评分{score:.1f}；区间{lower:.2f}-{upper:.2f}，振幅{range_pct:.1f}%，"
        f"位置{position_label}，横盘{box_days}日，MA20斜率{slope_text}"
    )
    summary += f"，边界触达{touch_count}次，{volume_note}"

    return {
        "box_score": score,
        "box_upper": _round(upper),
        "box_lower": _round(lower),
        "box_range_pct": _round(range_pct),
        "box_position_pct": _round(position_pct),
        "box_position_label": position_label,
        "box_days": box_days,
        "box_ma20_slope_pct": _round(ma20_slope),
        "box_boundary_touch_count": touch_count,
        "box_is_sideways": box_is_sideways,
        "box_summary": summary,
    }


def build_technical_profile(history: pd.DataFrame, box_window_days: int = DEFAULT_BOX_WINDOW_DAYS) -> TechnicalProfile:
    if history is None or history.empty:
        return TechnicalProfile(False, summary="K线数据缺失，无法生成技术画像")

    cleaned, data_gaps, volume_missing = _clean_history(history)
    if cleaned is None:
        return TechnicalProfile(False, summary="历史行情缺少收盘/最高/最低字段", data_gaps=data_gaps)
    df = cleaned
    if len(df) < 60:
        summary = f"K线有效样本仅{len(df)}条，少于60条，无法生成可靠技术画像"
        return TechnicalProfile(False, status="low_data", summary=summary, data_gaps=data_gaps)

    close_col = "收盘"
    high_col = "最高"
    low_col = "最低"
    open_col = "开盘"
    volume_col = "成交量"

    close_s = df[close_col].astype(float)
    close = _num(close_s.iloc[-1])
    if close is None:
        return TechnicalProfile(False, summary="最新收盘价异常，无法生成技术画像", data_gaps=data_gaps)
    ma5 = close_s.rolling(5).mean().iloc[-1]
    ma20 = close_s.rolling(20).mean().iloc[-1]
    ma60 = close_s.rolling(60).mean().iloc[-1]
    high20 = df[high_col].astype(float).iloc[:-1].tail(20).max() if len(df) > 20 else None
    low20 = df[low_col].astype(float).iloc[:-1].tail(20).min() if len(df) > 20 else None
    low5 = df[low_col].astype(float).iloc[:-1].tail(5).min() if len(df) > 5 else None
    atr14 = _atr(df, high_col, low_col, close_col)
    rsi14 = _rsi(close_s)
    kdj_j = _kdj_j(df, high_col, low_col, close_col)

    volume_ratio = None
    if volume_col and len(df) >= 21:
        volume = _num(df[volume_col].iloc[-1])
        avg_volume = _num(df[volume_col].iloc[-21:-1].mean())
        if volume is not None and avg_volume is not None and volume > 0 and avg_volume > 0:
            volume_ratio = round(volume / avg_volume, 2)

    ma5_value = _num(ma5)
    ma20_value = _num(ma20)
    ma60_value = _num(ma60)
    ma20_prev = _num(close_s.rolling(20).mean().iloc[-6]) if len(df) >= 66 else None
    ma20_slope = ((ma20_value - ma20_prev) / ma20_prev * 100) if ma20_value is not None and ma20_prev and ma20_prev > 0 else 0
    pullback_pct = (high20 - close) / high20 * 100 if high20 and high20 > 0 else None
    max_drawdown_30d = _max_drawdown_pct(close_s, 30)
    max_drawdown_60d = _max_drawdown_pct(close_s, 60)
    box = _box_metrics(
        df,
        close_s,
        close,
        max_drawdown_60d,
        volume_missing=volume_missing,
        window_days=box_window_days,
    )
    uptrend = bool(
        ma5_value is not None
        and ma20_value is not None
        and ma60_value is not None
        and ma5_value > ma20_value > ma60_value
        and close > ma20_value
        and ma20_slope >= 0
    )
    downtrend = bool(
        ma5_value is not None
        and ma20_value is not None
        and ma60_value is not None
        and ma5_value < ma20_value < ma60_value
        and close < ma20_value
    )
    if uptrend:
        trend_stage = "uptrend"
    elif downtrend:
        trend_stage = "downtrend"
    elif ma20_value is not None and close > ma20_value:
        trend_stage = "recovery"
    else:
        trend_stage = "sideways"

    open_value = _num(df[open_col].iloc[-1]) if open_col else close
    day_high = _num(df[high_col].iloc[-1]) or close
    day_low = _num(df[low_col].iloc[-1]) or close
    body_top = max(open_value or close, close)
    day_range = day_high - day_low
    long_upper = bool(day_range > 0 and (day_high - body_top) / day_range >= 0.45)
    breakout = bool(high20 and close > high20 and (volume_ratio or 0) >= 1.3)
    near_breakout = bool(high20 and close >= high20 * 0.97)
    pullback = bool(uptrend and ma20 * 0.97 <= close <= ma20 * 1.03 and (volume_ratio or 0) <= 1.25)
    oversold = bool((rsi14 is not None and rsi14 < 35) or (kdj_j is not None and kdj_j < 20))

    risk_flags: list[str] = []
    if downtrend:
        risk_flags.append("日线弱势")
    if long_upper:
        risk_flags.append("长上影")
    if rsi14 is not None and rsi14 >= 75:
        risk_flags.append("RSI偏高")
    if kdj_j is not None and kdj_j >= 90:
        risk_flags.append("KDJ高位")
    if volume_missing:
        risk_flags.append("缺少成交量，机会降级")

    if breakout:
        setup_name, status, label = "box_breakout", "triggered", "S 已触发机会"
    elif pullback:
        setup_name, status, label = "pullback_buy", "candidate", "A 回踩候选"
    elif trend_stage in {"uptrend", "recovery"} and near_breakout:
        setup_name, status, label = "trend_breakout", "candidate", "A 临界机会"
    elif oversold:
        setup_name, status, label = "oversold_rebound", "candidate", "B 反弹观察"
    elif downtrend:
        setup_name, status, label = "downtrend_avoid", "avoid", "D 回避"
    else:
        setup_name, status, label = "event_momentum", "candidate", "C 弱观察"

    if label.startswith("S") and risk_flags:
        label = "S 已触发但高风险"
    if volume_missing and label.startswith(("S", "A")):
        setup_name, status, label = "volume_gap_watch", "candidate", "B 量能缺口观察"

    entry = close if status == "triggered" else (high20 if high20 and high20 > close else close)
    fail_candidates = [value for value in [low5, low20, close - 2 * atr14 if atr14 else None] if value and value < close]
    fail = max(fail_candidates) if fail_candidates else None
    target = entry + 2 * (entry - fail) if entry and fail and entry > fail else close * 1.05
    entry = _num(entry)
    fail = _num(fail)
    target = _num(target)
    entry_text = f"{entry:.2f}" if entry is not None else "-"
    ma20_text = f"{ma20_value:.2f}" if ma20_value is not None else "-"
    summary = (
        f"{label}；趋势={trend_stage}，收盘={close:.2f}，MA20={ma20_text}，"
        f"量比={volume_ratio if volume_ratio is not None else '-'}，"
        f"触发={entry_text}"
    )
    if box.get("box_summary"):
        summary += f"；{box['box_summary']}"
    return TechnicalProfile(
        available=True,
        status=status,
        setup_name=setup_name,
        trend_stage=trend_stage,
        close=_round(close),
        ma5=_round(ma5),
        ma20=_round(ma20),
        ma60=_round(ma60),
        rsi14=rsi14,
        kdj_j=kdj_j,
        atr14=atr14,
        volume_ratio=volume_ratio,
        pullback_pct=_round(pullback_pct),
        max_drawdown_30d=max_drawdown_30d,
        max_drawdown_60d=max_drawdown_60d,
        entry_trigger=_round(entry),
        fail_level=_round(fail),
        target_level=_round(target),
        box_score=box["box_score"],
        box_upper=box["box_upper"],
        box_lower=box["box_lower"],
        box_range_pct=box["box_range_pct"],
        box_position_pct=box["box_position_pct"],
        box_position_label=box["box_position_label"],
        box_days=box["box_days"],
        box_ma20_slope_pct=box["box_ma20_slope_pct"],
        box_boundary_touch_count=box["box_boundary_touch_count"],
        box_is_sideways=box["box_is_sideways"],
        box_summary=box["box_summary"],
        opportunity_label=label,
        risk_flags=list(dict.fromkeys(risk_flags)),
        summary=summary,
        data_gaps=data_gaps,
    )
