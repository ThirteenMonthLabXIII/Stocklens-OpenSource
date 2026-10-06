from __future__ import annotations

import pandas as pd

from research._testkit import run_tests
from research.adaptive_baseline import classify_market_regime, load_policy, score_adaptive_baseline


def _panel(trend: float, breadth: float) -> pd.DataFrame:
    rows = []
    for index in range(24):
        positive = index < int(24 * breadth)
        rows.append(
            {
                "code": f"{index:06d}",
                "industry": "fixture",
                "momentum_60d": trend if positive else -abs(trend),
                "volatility_20d": 0.02 + index / 10000,
                "pe_ttm": 10 + index,
                "pb_mrq": 1 + index / 10,
                "roe": 0.10 + index / 1000,
                "net_margin": 0.08,
                "gross_margin": 0.30,
                "debt_ratio": 0.40,
                "revenue_growth": 0.10,
                "profit_growth": 0.12,
                "momentum_120d": trend,
                "max_drawdown_60d": -0.10,
            }
        )
    return pd.DataFrame(rows)


def test_policy_is_versioned_and_weights_are_complete():
    policy = load_policy()
    assert policy.version == "adaptive_baseline_v1"
    assert set(policy.regime_weights) == {"up", "range", "down", "unknown"}
    assert all(abs(sum(weights.values()) - 1.0) < 1e-9 for weights in policy.regime_weights.values())


def test_market_state_is_deterministic_and_requires_enough_observations():
    policy = load_policy()
    assert classify_market_regime(_panel(0.12, 0.75), policy)["regime"] == "up"
    assert classify_market_regime(_panel(0.01, 0.50), policy)["regime"] == "range"
    assert classify_market_regime(_panel(-0.12, 0.75), policy)["regime"] == "down"
    assert classify_market_regime(_panel(0.12, 0.75).head(5), policy)["regime"] == "unknown"


def test_scoring_retains_explanations_and_quality_missingness():
    panel = _panel(0.12, 0.75)
    panel.loc[0, "roe_quality"] = "suspect"
    scored = score_adaptive_baseline(panel)
    assert scored.iloc[0]["regime"] == "up"
    assert scored.iloc[0]["policy_version"] == "adaptive_baseline_v1"
    assert "valuation_contribution" in scored.columns
    assert "quality_missing_share" in scored.columns
    bad = scored.loc[scored["code"] == "000000"].iloc[0]
    assert bad["quality_missing_share"] > 0
    contribution_columns = [f"{dimension}_contribution" for dimension in load_policy().dimension_factors]
    assert abs(float(bad["score"]) / float(bad["completeness_factor"]) - sum(float(bad[column]) for column in contribution_columns)) < 1e-8


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
