from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.models import FinancialMetrics, StockQuote
from stock_tool.core.screener_engine import build_scored_candidate
from stock_tool.core.technical_profile import TechnicalProfile


def test_quality_value_profile_and_grade() -> None:
    candidate = build_scored_candidate(
        StockQuote(
            code="600001",
            name="质量价值样例",
            price=12,
            pe=18,
            pe_ttm=16,
            pb=1.8,
            industry="制造",
            industry_strength=55,
        ),
        FinancialMetrics(
            roe=24,
            gross_margin=58,
            net_margin=24,
            debt_ratio=35,
            revenue_growth=12,
            profit_growth=14,
        ),
        TechnicalProfile(True, close=10.5, ma20=10.0, rsi14=55, box_score=72, max_drawdown_60d=-6),
        extra_fields={"main_5d_yi": 0.2},
    )

    breakdown = candidate.score_breakdown
    assert breakdown["selection_profile"] == "质量价值"
    assert breakdown["selection_grade"] in {"S", "A", "B"}
    assert breakdown["selection_state"] in {"优先观察", "普通观察"}
    assert breakdown["veto_reasons"] == []
    assert breakdown["evidence_gaps"] == []


def test_missing_fields_create_data_gap_state() -> None:
    candidate = build_scored_candidate(
        StockQuote(code="600002", name="缺字段样例"),
        FinancialMetrics(),
        TechnicalProfile(False),
    )

    breakdown = candidate.score_breakdown
    gap_fields = {item["field"] for item in breakdown["evidence_gaps"]}
    assert breakdown["selection_state"] == "数据待补"
    assert "数据质量低" in breakdown["veto_reasons"]
    assert "资金缺失" in breakdown["veto_reasons"]
    assert "行业缺失" in breakdown["veto_reasons"]
    assert {"main_5d_yi", "industry_strength"}.issubset(gap_fields)


def test_extreme_valuation_and_drawdown_are_vetoed() -> None:
    candidate = build_scored_candidate(
        StockQuote(code="600003", name="风险样例", pe=95, pe_ttm=95, pb=7.0, industry_strength=80),
        FinancialMetrics(roe=16, gross_margin=30, net_margin=10, debt_ratio=65, revenue_growth=8, profit_growth=8),
        TechnicalProfile(True, close=8.0, ma20=10.0, rsi14=72, box_score=35, max_drawdown_60d=-26),
        extra_fields={"main_5d_yi": 0.1},
    )

    breakdown = candidate.score_breakdown
    assert breakdown["selection_profile"] == "风险回避"
    assert breakdown["selection_state"] == "风险回避"
    assert "估值极端" in breakdown["veto_reasons"]
    assert "回撤过大" in breakdown["veto_reasons"]


def run_tests() -> None:
    test_quality_value_profile_and_grade()
    test_missing_fields_create_data_gap_state()
    test_extreme_valuation_and_drawdown_are_vetoed()
    print("test_selection_profile passed")


if __name__ == "__main__":
    run_tests()
