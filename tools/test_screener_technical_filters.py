from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.models import StockQuote
from stock_tool.core.screener_engine import (
    ScreenFilter,
    passes_non_trend_technical_filter,
    requires_realtime_screening,
    requires_technical_filter,
)
from stock_tool.core.technical_profile import TechnicalProfile
from stock_tool.core.technical_profile import build_technical_profile


def _quote() -> StockQuote:
    return StockQuote(code="000001", name="样例", turnover=6)


def _history_from_close(close: list[float], *, with_volume: bool = True) -> pd.DataFrame:
    rows = {
        "日期": pd.date_range("2026-01-01", periods=len(close), freq="D"),
        "开盘": [value * 0.998 for value in close],
        "最高": [value * 1.01 for value in close],
        "最低": [value * 0.99 for value in close],
        "收盘": close,
    }
    if with_volume:
        rows["成交量"] = [1000 + (idx % 7) * 12 for idx in range(len(close))]
    return pd.DataFrame(rows)


def test_ma_bullish_and_rsi_filter() -> None:
    filters = ScreenFilter(
        require_ma_bullish=True,
        min_rsi=30,
        max_rsi=70,
    )
    assert passes_non_trend_technical_filter(
        TechnicalProfile(True, ma5=12, ma20=10, ma60=8, rsi14=50),
        _quote(),
        filters,
    )
    assert not passes_non_trend_technical_filter(
        TechnicalProfile(True, ma5=None, ma20=10, ma60=8, rsi14=50),
        _quote(),
        filters,
    )
    assert not passes_non_trend_technical_filter(
        TechnicalProfile(True, ma5=9, ma20=10, ma60=8, rsi14=50),
        _quote(),
        filters,
    )
    assert not passes_non_trend_technical_filter(
        TechnicalProfile(True, ma5=12, ma20=10, ma60=8, rsi14=29.9),
        _quote(),
        filters,
    )
    assert not passes_non_trend_technical_filter(
        TechnicalProfile(True, ma5=12, ma20=10, ma60=8, rsi14=70.1),
        _quote(),
        filters,
    )


def test_strict_rsi_boundaries() -> None:
    min_filters = ScreenFilter(min_rsi=30, strict_min_rsi=True)
    assert not passes_non_trend_technical_filter(TechnicalProfile(True, rsi14=30), _quote(), min_filters)
    assert passes_non_trend_technical_filter(TechnicalProfile(True, rsi14=30.1), _quote(), min_filters)

    max_filters = ScreenFilter(max_rsi=70, strict_max_rsi=True)
    assert not passes_non_trend_technical_filter(TechnicalProfile(True, rsi14=70), _quote(), max_filters)
    assert passes_non_trend_technical_filter(TechnicalProfile(True, rsi14=69.9), _quote(), max_filters)


def test_max_drawdown_filter_boundaries() -> None:
    filters = ScreenFilter(max_max_drawdown_30d=15, max_max_drawdown_60d=20)
    assert passes_non_trend_technical_filter(
        TechnicalProfile(True, max_drawdown_30d=12, max_drawdown_60d=18),
        _quote(),
        filters,
    )
    assert not passes_non_trend_technical_filter(
        TechnicalProfile(True, max_drawdown_30d=16, max_drawdown_60d=18),
        _quote(),
        filters,
    )
    assert not passes_non_trend_technical_filter(
        TechnicalProfile(True, max_drawdown_30d=12, max_drawdown_60d=21),
        _quote(),
        filters,
    )
    assert not passes_non_trend_technical_filter(
        TechnicalProfile(True, max_drawdown_30d=None, max_drawdown_60d=18),
        _quote(),
        filters,
    )

    strict_filters = ScreenFilter(max_max_drawdown_30d=15, strict_max_max_drawdown_30d=True)
    assert not passes_non_trend_technical_filter(
        TechnicalProfile(True, max_drawdown_30d=15),
        _quote(),
        strict_filters,
    )
    assert passes_non_trend_technical_filter(
        TechnicalProfile(True, max_drawdown_30d=14.9),
        _quote(),
        strict_filters,
    )


def test_build_technical_profile_max_drawdown() -> None:
    close = [100.0] * 10 + [120.0] + [110.0] * 29 + [80.0] + [100.0] * 9 + [90.0] + [95.0] * 19
    history = pd.DataFrame(
        {
            "日期": pd.date_range("2026-01-01", periods=len(close), freq="D"),
            "开盘": close,
            "最高": [value + 1 for value in close],
            "最低": [value - 1 for value in close],
            "收盘": close,
            "成交量": [1000 + idx for idx in range(len(close))],
        }
    )
    profile = build_technical_profile(history)
    assert profile.available
    assert profile.max_drawdown_30d == 10
    assert profile.max_drawdown_60d == 33.33
    assert profile.pullback_pct is not None


def test_build_technical_profile_box_sideways() -> None:
    close = [10 + ((idx % 9) - 4) * 0.08 for idx in range(80)]
    profile = build_technical_profile(_history_from_close(close))
    assert profile.available
    assert profile.box_is_sideways, profile.box_summary
    assert profile.box_score is not None and profile.box_score >= 70, profile.box_summary
    assert profile.box_upper is not None and profile.box_lower is not None
    assert profile.box_range_pct is not None and profile.box_range_pct <= 25
    assert profile.box_position_label in {"接近下沿", "箱体中部", "接近上沿"}
    assert passes_non_trend_technical_filter(
        profile,
        _quote(),
        ScreenFilter(require_box_sideways=True, min_box_score=60, max_box_range_pct=25),
    )


def test_build_technical_profile_box_rejects_trends_and_breaks() -> None:
    uptrend = [10 + idx * 0.18 for idx in range(90)]
    downtrend = [24 - idx * 0.14 for idx in range(90)]
    reference_box = [10 + ((idx % 8) - 4) * 0.06 for idx in range(70)]
    break_up = reference_box + [12.2]
    break_down = reference_box + [8.0]
    filters = ScreenFilter(require_box_sideways=True, min_box_score=60, max_box_range_pct=25)

    for close in [uptrend, downtrend, break_up, break_down]:
        profile = build_technical_profile(_history_from_close(close))
        assert not profile.box_is_sideways, profile.box_summary
        assert not passes_non_trend_technical_filter(profile, _quote(), filters)


def test_build_technical_profile_box_low_data_and_missing_volume() -> None:
    low_data_profile = build_technical_profile(_history_from_close([10.0] * 59))
    assert not low_data_profile.available
    assert low_data_profile.box_score is None
    assert "少于60条" in low_data_profile.summary

    no_volume = [10 + ((idx % 6) - 3) * 0.05 for idx in range(80)]
    profile = build_technical_profile(_history_from_close(no_volume, with_volume=False))
    assert profile.available
    assert profile.box_is_sideways
    assert profile.box_score is not None
    assert "量能缺失" in profile.box_summary


def test_realtime_requirements() -> None:
    for filters in [
        ScreenFilter(require_ma_bullish=True),
        ScreenFilter(min_rsi=30),
        ScreenFilter(max_rsi=70),
        ScreenFilter(max_max_drawdown_30d=15),
        ScreenFilter(max_max_drawdown_60d=20),
        ScreenFilter(require_box_sideways=True),
        ScreenFilter(min_box_score=60),
        ScreenFilter(sort=[("rsi", "desc")]),
        ScreenFilter(sort=[("max_drawdown_30d", "asc")]),
        ScreenFilter(sort=[("box_score", "desc")]),
    ]:
        assert requires_technical_filter(filters)
        if filters.box_window_days == 60 and (
            filters.require_box_sideways
            or filters.min_box_score is not None
            or any(field == "box_score" for field, _direction in filters.sort)
        ):
            assert not requires_realtime_screening(filters)
        else:
            assert requires_realtime_screening(filters)


def main() -> None:
    test_ma_bullish_and_rsi_filter()
    test_strict_rsi_boundaries()
    test_max_drawdown_filter_boundaries()
    test_build_technical_profile_max_drawdown()
    test_build_technical_profile_box_sideways()
    test_build_technical_profile_box_rejects_trends_and_breaks()
    test_build_technical_profile_box_low_data_and_missing_volume()
    test_realtime_requirements()
    print("[OK] 技术因子筛选专项测试通过")


if __name__ == "__main__":
    main()
