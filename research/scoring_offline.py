"""Offline Phase C cross-sectional scoring.

The implementation mirrors the research contract only. It reads point-in-time
rows assembled from ``research.pit_join`` and never imports production scoring.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from research.metric_metadata import QUALITY_PASS

WEIGHTS_PATH = Path(__file__).resolve().parent / "weights_preregister.json"

DIMENSION_METRICS: dict[str, tuple[str, ...]] = {
    "fundamental": ("roe", "net_margin", "gross_margin"),
    "growth": ("revenue_growth", "profit_growth"),
    "safety": ("debt_ratio",),
    "valuation": ("pe_ttm", "pb_mrq"),
    "trend": ("trend_20d", "trend_60d"),
}

METRIC_DIRECTIONS: dict[str, str] = {
    "roe": "high",
    "net_margin": "high",
    "gross_margin": "high",
    "revenue_growth": "high",
    "profit_growth": "high",
    "debt_ratio": "low",
    "pe_ttm": "low",
    "pb_mrq": "low",
    "trend_20d": "high",
    "trend_60d": "high",
}


@dataclass(frozen=True)
class CandidateWeights:
    id: str
    reason: str
    weights: dict[str, float]


def load_preregistered_weights(path: Path = WEIGHTS_PATH) -> list[CandidateWeights]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates: list[CandidateWeights] = []
    dims = tuple(payload["dimensions"])
    for item in payload["candidates"]:
        weights = {str(k): float(v) for k, v in item["weights"].items()}
        if tuple(weights.keys()) != dims:
            raise ValueError(f"candidate {item['id']} dimensions do not match registry")
        total = sum(weights.values())
        if total > 1.000001:
            raise ValueError(f"candidate {item['id']} weights sum to {total}, expected <= 1")
        if any(v < 0 for v in weights.values()):
            raise ValueError(f"candidate {item['id']} has a negative weight")
        candidates.append(CandidateWeights(str(item["id"]), str(item["reason"]), weights))
    return candidates


def score_cross_section(
    panel: pd.DataFrame,
    weights: Mapping[str, float],
    *,
    industry_col: str = "industry",
) -> pd.DataFrame:
    """Return one score row per code using industry-shrunk percentiles.

    Missing raw indicators contribute 0 to their fixed metric slot. Cross-
    sectional no-signal cases, such as every non-missing value being identical,
    still score near 50. Rows marked with non-pass metric quality are treated as
    missing, matching the Phase B quality finding for ``debt_ratio`` scale
    suspects.
    """
    if panel.empty:
        return pd.DataFrame(columns=["code", "score"])
    out = panel.copy()
    if "code" not in out.columns:
        raise ValueError("panel must contain code")

    raw = _prepare_raw_metrics(out)
    indicator_scores: dict[str, pd.Series] = {}
    indicator_missing: dict[str, pd.Series] = {}
    missing_count = pd.Series(0, index=out.index, dtype="int64")
    for metric, direction in METRIC_DIRECTIONS.items():
        values = raw.get(metric, pd.Series(math.nan, index=out.index, dtype="float64"))
        missing = values.isna()
        indicator_missing[metric] = missing
        missing_count = missing_count + missing.astype("int64")
        scored = _shrunk_percentile(values, direction, out.get(industry_col))
        indicator_scores[metric] = scored

    dim_scores: dict[str, pd.Series] = {}
    dim_missing_shares: dict[str, pd.Series] = {}
    for dim, metrics in DIMENSION_METRICS.items():
        metric_frame = pd.concat([indicator_scores[m] for m in metrics], axis=1)
        dim_scores[dim] = metric_frame.fillna(0.0).sum(axis=1) / len(metrics)
        missing_frame = pd.concat([indicator_missing[m] for m in metrics], axis=1)
        dim_missing_shares[dim] = missing_frame.astype(float).sum(axis=1) / len(metrics)

    score = pd.Series(0.0, index=out.index)
    weighted_missing_share = pd.Series(0.0, index=out.index)
    for dim, weight in weights.items():
        if dim not in dim_scores:
            raise ValueError(f"unknown dimension {dim}")
        w = float(weight)
        score = score + dim_scores[dim] * w
        weighted_missing_share = weighted_missing_share + dim_missing_shares[dim] * w
    completeness_factor = (1.0 - 0.03 * weighted_missing_share).clip(lower=0.0, upper=1.0)
    score = score * completeness_factor

    result = pd.DataFrame({"code": out["code"].astype(str), "score": score})
    for dim, values in dim_scores.items():
        result[f"{dim}_score"] = values
    result["missing_indicator_count"] = missing_count
    result["weighted_missing_share"] = weighted_missing_share
    result["completeness_factor"] = completeness_factor
    return result.sort_values(["score", "code"], ascending=[False, True]).reset_index(drop=True)


def append_trend_features(prices: pd.DataFrame) -> pd.DataFrame:
    """Add 20/60-day close-to-close trend columns to a price history frame."""
    if prices.empty:
        return prices.copy()
    required = {"code", "date", "close"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"price frame missing columns: {sorted(missing)}")
    out = prices.copy()
    out["date"] = pd.to_datetime(out["date"])
    out = out.sort_values(["code", "date"])
    close = pd.to_numeric(out["close"], errors="coerce")
    out["trend_20d"] = close / close.groupby(out["code"]).shift(20) - 1.0
    out["trend_60d"] = close / close.groupby(out["code"]).shift(60) - 1.0
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out


def score_with_candidate(panel: pd.DataFrame, candidate_id: str) -> pd.DataFrame:
    candidates = {c.id: c for c in load_preregistered_weights()}
    if candidate_id not in candidates:
        raise KeyError(candidate_id)
    return score_cross_section(panel, candidates[candidate_id].weights)


def _prepare_raw_metrics(panel: pd.DataFrame) -> dict[str, pd.Series]:
    raw: dict[str, pd.Series] = {}
    for metric in METRIC_DIRECTIONS:
        if metric in panel.columns:
            raw[metric] = pd.to_numeric(panel[metric], errors="coerce")
        else:
            raw[metric] = pd.Series(math.nan, index=panel.index, dtype="float64")

    pe = raw["pe_ttm"]
    raw["pe_ttm"] = pe.where(pe > 0)
    pb = raw["pb_mrq"]
    raw["pb_mrq"] = pb.where(pb > 0)

    for metric in METRIC_DIRECTIONS:
        quality_col = f"{metric}_quality"
        if quality_col in panel.columns:
            quality = panel[quality_col].fillna(QUALITY_PASS).astype(str)
            raw[metric] = raw[metric].where(quality.eq(QUALITY_PASS))
    return raw


def _shrunk_percentile(
    values: pd.Series,
    direction: str,
    industry: pd.Series | None,
    *,
    shrink_k: int = 30,
) -> pd.Series:
    market = _percentile(values, direction)
    if industry is None:
        return market
    ind = pd.Series(industry, index=values.index).fillna("__missing__").astype(str)
    group_size = ind.groupby(ind).transform("size")
    ind_score = values.groupby(ind, group_keys=False).apply(lambda s: _percentile(s, direction))
    shrunk = (group_size * ind_score + shrink_k * market) / (group_size + shrink_k)
    return shrunk.where(group_size >= 5, market)


def _percentile(values: pd.Series, direction: str) -> pd.Series:
    clean = pd.to_numeric(values, errors="coerce")
    valid = clean.dropna()
    out = pd.Series(math.nan, index=clean.index, dtype="float64")
    n = len(valid)
    if n == 0:
        return out
    if n == 1:
        out.loc[valid.index] = 50.0
        return out
    ranks = valid.rank(method="average", ascending=True)
    if direction == "high":
        score = (ranks - 1.0) / (n - 1.0) * 100.0
    elif direction == "low":
        score = (n - ranks) / (n - 1.0) * 100.0
    else:
        raise ValueError(f"unknown direction {direction}")
    out.loc[valid.index] = score
    return out
