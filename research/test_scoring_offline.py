from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research._testkit import run_tests
from research.metric_metadata import QUALITY_PASS, QUALITY_SCALE_SUSPECT
from research.scoring_offline import DIMENSION_METRICS, load_preregistered_weights, score_cross_section


def _panel() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"code": "A", "industry": "i1", "roe": 20, "net_margin": 20, "gross_margin": 50, "profit_growth": 30, "revenue_growth": 30, "debt_ratio": 30, "debt_ratio_quality": QUALITY_PASS, "pe_ttm": 10, "pb_mrq": 1, "trend_20d": 0.2, "trend_60d": 0.3},
            {"code": "B", "industry": "i1", "roe": 10, "net_margin": 10, "gross_margin": 30, "profit_growth": 10, "revenue_growth": 10, "debt_ratio": 80, "debt_ratio_quality": QUALITY_PASS, "pe_ttm": 50, "pb_mrq": 5, "trend_20d": -0.1, "trend_60d": -0.2},
            {"code": "C", "industry": "i2", "roe": 15, "net_margin": 15, "gross_margin": 40, "profit_growth": 20, "revenue_growth": 20, "debt_ratio": 50, "debt_ratio_quality": QUALITY_PASS, "pe_ttm": 20, "pb_mrq": 2, "trend_20d": 0.0, "trend_60d": 0.0},
        ]
    )


def test_preregistered_weights_are_small_and_bounded():
    candidates = load_preregistered_weights()
    assert 2 <= len(candidates) <= 5
    assert {c.id for c in candidates} == {"contract_baseline", "value_tilt", "quality_tilt", "balanced_equal", "no_trend"}
    assert all(sum(c.weights.values()) <= 1.0 for c in candidates)


def test_lower_pe_pb_and_debt_are_better():
    weights = {"fundamental": 0, "growth": 0, "safety": 0.5, "valuation": 0.5, "trend": 0}
    scored = score_cross_section(_panel(), weights).set_index("code")
    assert scored.loc["A", "score"] > scored.loc["B", "score"]


def test_debt_ratio_scale_suspect_is_treated_as_missing():
    panel = _panel()
    panel.loc[panel["code"] == "A", "debt_ratio_quality"] = QUALITY_SCALE_SUSPECT
    weights = {"fundamental": 0, "growth": 0, "safety": 1.0, "valuation": 0, "trend": 0}
    scored = score_cross_section(panel, weights).set_index("code")
    assert int(scored.loc["A", "missing_indicator_count"]) == 1
    assert scored.loc["A", "score"] == 0.0


def test_scale_suspect_debt_never_raises_bad_candidate_score():
    panel = _panel()
    bad_panel = panel.copy()
    bad_panel.loc[bad_panel["code"] == "B", "debt_ratio_quality"] = QUALITY_SCALE_SUSPECT
    adversary = {"fundamental": 0, "growth": 0, "safety": 1.0, "valuation": 0, "trend": 0}
    for weights in [c.weights for c in load_preregistered_weights()] + [adversary]:
        baseline = score_cross_section(panel, weights).set_index("code")
        masked = score_cross_section(bad_panel, weights).set_index("code")
        assert masked.loc["B", "score"] <= baseline.loc["B", "score"]


def test_masking_any_bad_indicator_never_raises_score():
    metric_to_dim = {metric: dim for dim, metrics in DIMENSION_METRICS.items() for metric in metrics}
    for metric, dim in metric_to_dim.items():
        panel = _panel()
        baseline = score_cross_section(panel, {d: (1.0 if d == dim else 0.0) for d in DIMENSION_METRICS}).set_index("code")
        worst_code = baseline["score"].idxmin()
        masked_panel = panel.copy()
        masked_panel.loc[masked_panel["code"] == worst_code, metric] = pd.NA
        masked = score_cross_section(masked_panel, {d: (1.0 if d == dim else 0.0) for d in DIMENSION_METRICS}).set_index("code")
        assert masked.loc[worst_code, "score"] <= baseline.loc[worst_code, "score"]


def test_masking_zero_weight_dimension_does_not_raise_score():
    panel = _panel()
    weights = {"fundamental": 0.5, "growth": 0.5, "safety": 0, "valuation": 0, "trend": 0}
    baseline = score_cross_section(panel, weights).set_index("code")
    masked_panel = panel.copy()
    masked_panel.loc[masked_panel["code"] == "B", "debt_ratio"] = pd.NA
    masked = score_cross_section(masked_panel, weights).set_index("code")
    assert masked.loc["B", "score"] <= baseline.loc["B", "score"]


def test_zero_signal_scores_near_50_but_all_missing_scores_zero():
    panel = _panel()
    panel["roe"] = 7.0
    weights = {"fundamental": 1.0, "growth": 0, "safety": 0, "valuation": 0, "trend": 0}
    scored = score_cross_section(panel, weights).set_index("code")
    assert scored["fundamental_score"].between(0, 100).all()

    all_missing = panel.copy()
    for metric in DIMENSION_METRICS["fundamental"]:
        all_missing[metric] = pd.NA
    scored_missing = score_cross_section(all_missing, weights).set_index("code")
    assert (scored_missing["fundamental_score"] == 0.0).all()
    assert (scored_missing["score"] == 0.0).all()


def test_thin_industry_falls_back_to_market_percentile():
    panel = _panel()
    weights = {"fundamental": 1.0, "growth": 0, "safety": 0, "valuation": 0, "trend": 0}
    scored = score_cross_section(panel, weights).set_index("code")
    assert scored.loc["A", "fundamental_score"] > scored.loc["C", "fundamental_score"] > scored.loc["B", "fundamental_score"]


def test_thick_industry_uses_shrunk_industry_percentile():
    panel = pd.DataFrame(
        [
            {"code": f"T{i}", "industry": "thick", "roe": i, "net_margin": i, "gross_margin": i}
            for i in range(1, 6)
        ]
        + [{"code": "M", "industry": "thin", "roe": 100, "net_margin": 100, "gross_margin": 100}]
    )
    weights = {"fundamental": 1.0, "growth": 0, "safety": 0, "valuation": 0, "trend": 0}
    scored = score_cross_section(panel, weights).set_index("code")
    assert scored.loc["T5", "fundamental_score"] < 100.0
    assert scored.loc["T5", "fundamental_score"] > scored.loc["T4", "fundamental_score"]


def test_zeroing_weak_trend_weight_does_not_raise_score():
    panel = _panel()
    baseline = {"fundamental": 0, "growth": 0, "safety": 0, "valuation": 0, "trend": 0.2}
    no_trend = {"fundamental": 0, "growth": 0, "safety": 0, "valuation": 0, "trend": 0.0}
    scored_base = score_cross_section(panel, baseline).set_index("code")
    scored_no = score_cross_section(panel, no_trend).set_index("code")
    assert scored_no.loc["B", "score"] <= scored_base.loc["B", "score"]


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
