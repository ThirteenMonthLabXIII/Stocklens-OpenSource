"""Explainable, regime-aware cross-sectional scoring baseline.

This module is intentionally research-only.  It uses a frozen JSON policy,
cross-sectional percentiles, and deterministic market-state rules.  It does
not learn weights from the same returns it later evaluates.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from research.metric_metadata import QUALITY_PASS
from research.scoring_offline import _shrunk_percentile

POLICY_PATH = Path(__file__).resolve().parent / "adaptive_baseline_policy.json"
REGIMES = ("up", "range", "down", "unknown")


@dataclass(frozen=True)
class AdaptiveBaselinePolicy:
    version: str
    regime_rules: Mapping[str, Any]
    dimension_factors: Mapping[str, Mapping[str, str]]
    regime_weights: Mapping[str, Mapping[str, float]]
    missing_penalty: float


def load_policy(path: Path = POLICY_PATH) -> AdaptiveBaselinePolicy:
    payload = json.loads(path.read_text(encoding="utf-8"))
    dimensions = {str(k): {str(f): str(d) for f, d in v.items()} for k, v in payload["dimension_factors"].items()}
    weights = {
        str(regime): {str(dim): float(value) for dim, value in values.items()}
        for regime, values in payload["regime_weights"].items()
    }
    if tuple(weights) != REGIMES:
        raise ValueError(f"policy regimes must be {REGIMES}")
    expected_dimensions = tuple(dimensions)
    for regime, regime_weights in weights.items():
        if tuple(regime_weights) != expected_dimensions:
            raise ValueError(f"policy {regime} dimensions do not match dimension_factors")
        if any(value < 0 for value in regime_weights.values()):
            raise ValueError(f"policy {regime} contains negative weights")
        if not math.isclose(sum(regime_weights.values()), 1.0, abs_tol=1e-9):
            raise ValueError(f"policy {regime} weights must sum to 1")
    for dimension, factors in dimensions.items():
        if not factors:
            raise ValueError(f"policy dimension {dimension} is empty")
        if any(direction not in {"high", "low"} for direction in factors.values()):
            raise ValueError(f"policy dimension {dimension} has an invalid direction")
    penalty = float(payload["missing_penalty"])
    if not 0 <= penalty <= 1:
        raise ValueError("missing_penalty must be between 0 and 1")
    return AdaptiveBaselinePolicy(
        version=str(payload["version"]),
        regime_rules=payload["regime_rules"],
        dimension_factors=dimensions,
        regime_weights=weights,
        missing_penalty=penalty,
    )


def classify_market_regime(panel: pd.DataFrame, policy: AdaptiveBaselinePolicy | None = None) -> dict[str, Any]:
    """Classify market state from point-in-time panel fields only."""
    policy = policy or load_policy()
    rules = policy.regime_rules
    min_observations = int(rules.get("min_observations", 20))
    trend, trend_column = _first_median(panel, rules.get("trend_column_preference", ()))
    breadth, breadth_column = _first_median(panel, rules.get("breadth_column_preference", ()))
    if breadth is None:
        trend_source = _first_series(panel, rules.get("trend_column_preference", ()))
        if trend_source is not None:
            breadth = float((trend_source > 0).mean())
            breadth_column = f"sign({trend_column})" if trend_column else "sign(trend)"
    volatility, volatility_column = _first_median(panel, rules.get("volatility_column_preference", ()))
    missing_inputs = []
    if trend is None:
        missing_inputs.append("trend")
    if breadth is None:
        missing_inputs.append("breadth")
    regime = "unknown"
    if len(panel) >= min_observations and not missing_inputs:
        if trend >= float(rules["up"]["trend_at_least"]) and breadth >= float(rules["up"]["breadth_at_least"]):
            regime = "up"
        elif trend <= float(rules["down"]["trend_at_most"]) or breadth <= float(rules["down"]["breadth_at_most"]):
            regime = "down"
        else:
            regime = "range"
    return {
        "regime": regime,
        "policy_version": policy.version,
        "n_observations": int(len(panel)),
        "trend": trend,
        "trend_column": trend_column,
        "breadth": breadth,
        "breadth_column": breadth_column,
        "volatility": volatility,
        "volatility_column": volatility_column,
        "missing_inputs": missing_inputs,
    }


def score_adaptive_baseline(
    panel: pd.DataFrame,
    *,
    policy: AdaptiveBaselinePolicy | None = None,
    regime: str | None = None,
    industry_col: str = "industry",
) -> pd.DataFrame:
    """Score a cross-section and retain factor/dimension explanations."""
    policy = policy or load_policy()
    if panel.empty:
        return pd.DataFrame(columns=["code", "score", "regime", "policy_version"])
    if "code" not in panel.columns:
        raise ValueError("panel must contain code")
    state = classify_market_regime(panel, policy)
    selected_regime = regime or state["regime"]
    if selected_regime not in REGIMES:
        raise ValueError(f"unknown regime {selected_regime}")
    weights = policy.regime_weights[selected_regime]
    factor_scores: dict[str, pd.Series] = {}
    factor_missing: dict[str, pd.Series] = {}
    missing_count = pd.Series(0, index=panel.index, dtype="int64")
    for dimension, factors in policy.dimension_factors.items():
        for factor, direction in factors.items():
            values = _factor_values(panel, factor)
            missing = values.isna()
            factor_missing[factor] = missing
            missing_count = missing_count + missing.astype("int64")
            factor_scores[factor] = _shrunk_percentile(values, direction, panel.get(industry_col))

    dimension_scores: dict[str, pd.Series] = {}
    dimension_missing: dict[str, pd.Series] = {}
    for dimension, factors in policy.dimension_factors.items():
        frame = pd.concat([factor_scores[factor] for factor in factors], axis=1)
        missing_frame = pd.concat([factor_missing[factor] for factor in factors], axis=1)
        dimension_scores[dimension] = frame.fillna(0.0).mean(axis=1)
        dimension_missing[dimension] = missing_frame.mean(axis=1)

    weighted_missing = pd.Series(0.0, index=panel.index)
    score = pd.Series(0.0, index=panel.index)
    for dimension, weight in weights.items():
        score = score + dimension_scores[dimension] * float(weight)
        weighted_missing = weighted_missing + dimension_missing[dimension] * float(weight)
    completeness = (1.0 - policy.missing_penalty * weighted_missing).clip(lower=0.0, upper=1.0)
    score = score * completeness

    result = pd.DataFrame({"code": panel["code"].astype(str), "score": score})
    result["regime"] = selected_regime
    result["policy_version"] = policy.version
    result["regime_observations"] = state["n_observations"]
    result["regime_trend"] = state["trend"]
    result["regime_breadth"] = state["breadth"]
    for dimension, values in dimension_scores.items():
        result[f"{dimension}_score"] = values
        result[f"{dimension}_weight"] = float(weights[dimension])
        result[f"{dimension}_contribution"] = values * float(weights[dimension])
        result[f"{dimension}_missing_share"] = dimension_missing[dimension]
    result["missing_factor_count"] = missing_count
    result["weighted_missing_share"] = weighted_missing
    result["completeness_factor"] = completeness
    return result.sort_values(["score", "code"], ascending=[False, True]).reset_index(drop=True)


def _factor_values(panel: pd.DataFrame, factor: str) -> pd.Series:
    if factor not in panel.columns:
        return pd.Series(math.nan, index=panel.index, dtype="float64")
    values = pd.to_numeric(panel[factor], errors="coerce")
    values = values.where(values.map(math.isfinite))
    if factor in {"pe_ttm", "pb_mrq"}:
        values = values.where(values > 0)
    quality = f"{factor}_quality"
    if quality in panel.columns:
        values = values.where(panel[quality].fillna(QUALITY_PASS).astype(str).eq(QUALITY_PASS))
    return values


def _first_series(panel: pd.DataFrame, columns: Any) -> pd.Series | None:
    for column in columns:
        if column in panel.columns:
            values = pd.to_numeric(panel[column], errors="coerce").dropna()
            if not values.empty:
                return values
    return None


def _first_median(panel: pd.DataFrame, columns: Any) -> tuple[float | None, str | None]:
    for column in columns:
        if column in panel.columns:
            values = pd.to_numeric(panel[column], errors="coerce").dropna()
            if not values.empty:
                return float(values.median()), str(column)
    return None, None
