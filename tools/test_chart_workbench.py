from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.charting import aggregate_ohlcv, build_chart_payload, calculate_indicators, normalize_ohlcv
from stock_tool.core.models import FinancialMetrics, StockQuote
from stock_tool.ui.app_entry import run_app


def _sample_history(days: int = 90, with_volume: bool = True) -> pd.DataFrame:
    rows = []
    for idx in range(days):
        close = 10 + idx * 0.06
        row = {
            "日期": pd.Timestamp("2025-01-01") + pd.Timedelta(days=idx),
            "开盘": close - 0.03,
            "最高": close + 0.18,
            "最低": close - 0.16,
            "收盘": close,
        }
        if with_volume:
            row["成交量"] = 1000 + idx * 12
        rows.append(row)
    return pd.DataFrame(rows)


def test_normalize_and_aggregate() -> None:
    normalized, quality = normalize_ohlcv(_sample_history(95))
    assert not quality
    assert list(normalized.columns) == ["date", "open", "high", "low", "close", "volume"]
    weekly = aggregate_ohlcv(normalized, "weekly")
    monthly = aggregate_ohlcv(normalized, "monthly")
    assert 10 <= len(weekly) <= 15
    assert 3 <= len(monthly) <= 5
    assert weekly.iloc[0]["open"] == normalized.iloc[0]["open"]


def test_payload_serialization_and_indicators() -> None:
    payload = build_chart_payload(
        StockQuote(code="000001", name="样例", price=12.3, change_pct=1.2),
        _sample_history(90),
        FinancialMetrics(roe=12.5, report_date="2025-12-31"),
        period="daily",
        chart_range="60d",
        mode="candlestick",
    ).to_dict()
    assert payload["version"] == "1.0"
    assert payload["stock"]["code"] == "000001"
    assert len(payload["ohlcv"]) == 60
    assert len(payload["indicators"]["ma"]["MA5"]) == 60
    assert len(payload["indicators"]["macd"]["MACD"]) == 60
    assert payload["intelligence"]["valuation"]["roe"] == 12.5
    assert json.loads(json.dumps(payload, ensure_ascii=False))["stock"]["name"] == "样例"


def test_payload_quality_fallbacks() -> None:
    no_volume = build_chart_payload(
        StockQuote(code="000001", name="样例"),
        _sample_history(90, with_volume=False),
        chart_range="all",
    )
    assert any("成交量" in item for item in no_volume.quality)
    assert any("机会降级" in item for item in no_volume.quality)
    assert not any(item.startswith(("A", "S")) for item in no_volume.intelligence["opportunity_tags"])
    assert any("机会降级" in item for item in no_volume.intelligence["risk_tags"])

    dirty = _sample_history(90)
    dirty["日期"] = dirty["日期"].astype(object)
    dirty.loc[0, "日期"] = "bad-date"
    dirty.loc[1, "最高"] = float("inf")
    dirty.loc[2, "最低"] = dirty.loc[2, "收盘"] + 1
    dirty_payload = build_chart_payload(StockQuote(code="000001", name="样例"), dirty, chart_range="all").to_dict()
    assert len(dirty_payload["ohlcv"]) == 87
    encoded = json.dumps(dirty_payload, ensure_ascii=False)
    assert "Infinity" not in encoded and "NaN" not in encoded and "inf" not in encoded.lower()
    assert any("异常" in item for item in dirty_payload["quality"])

    short_no_volume = build_chart_payload(
        StockQuote(code="000001", name="样例"),
        _sample_history(30, with_volume=False),
        chart_range="all",
    )
    assert any("60" in item for item in short_no_volume.quality)
    empty = build_chart_payload(StockQuote(code="000001", name="样例"), pd.DataFrame()).to_dict()
    assert empty["ohlcv"] == []
    assert empty["quality"]
    indicators = calculate_indicators(pd.DataFrame())
    assert indicators["ma"]["MA5"] == []
    indicators = calculate_indicators(pd.DataFrame({"date": []}))
    assert indicators["macd"]["MACD"] == []


def test_app_entry_is_lazy() -> None:
    assert callable(run_app)


def main() -> int:
    test_normalize_and_aggregate()
    test_payload_serialization_and_indicators()
    test_payload_quality_fallbacks()
    test_app_entry_is_lazy()
    print("[OK] 个股情报工作台图表数据区块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
