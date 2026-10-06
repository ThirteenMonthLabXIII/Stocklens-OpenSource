from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.factor_store import FactorSnapshot
from stock_tool.core.professional_formula import PROFESSIONAL_DIMENSIONS
from stock_tool.core.screener_engine import ScreenFilter, screen_factor_snapshots


def test_local_score_is_fixed_baseline_and_active_formula_is_shadow_only() -> None:
    snapshots = [
        FactorSnapshot(
            code="600010",
            name="旧分更高",
            price=10,
            pe=20,
            pe_ttm=20,
            pb=2.0,
            roe=5,
            gross_margin=10,
            net_margin=2,
            debt_ratio=40,
            revenue_growth=5,
            profit_growth=5,
            trend_20d=12,
            box_score=100,
            industry_strength=100,
            technical_status="triggered",
            technical_label="S级机会",
        ),
        FactorSnapshot(
            code="600011",
            name="专业分更高",
            price=10,
            pe=120,
            pe_ttm=120,
            pb=5.0,
            roe=25,
            gross_margin=60,
            net_margin=25,
            debt_ratio=90,
            revenue_growth=-10,
            profit_growth=-10,
            trend_20d=-2,
            box_score=0,
            industry_strength=10,
            technical_status="candidate",
        ),
    ]
    formula = {
        "dimension_weights": {dimension: (1.0 if dimension == "quality" else 0.0) for dimension in PROFESSIONAL_DIMENSIONS},
        "field_weights": {"quality": {"roe": 1.0, "gross_margin": 0.0, "net_margin": 0.0}},
    }
    report = screen_factor_snapshots(
        snapshots,
        ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None, result_limit=2),
        professional_formula=formula,
    )

    assert [item.quote.code for item in report.results] == ["600010", "600011"]
    for item in report.results:
        assert item.local_score == item.score_breakdown["professional_formula"]["score"]
        assert "shadow_formula" in item.score_breakdown
        assert item.legacy_score is not None
        assert item.score_breakdown["legacy_score"] == item.legacy_score
    assert report.results[0].legacy_score > report.results[1].legacy_score


def run_tests() -> None:
    test_local_score_is_fixed_baseline_and_active_formula_is_shadow_only()
    print("test_professional_scoring_contract passed")


if __name__ == "__main__":
    run_tests()
