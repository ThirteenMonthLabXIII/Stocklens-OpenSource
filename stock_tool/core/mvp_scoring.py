from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from stock_tool.core.mvp_registry import ProductRegistry, TemplateDefinition, load_product_registry


MISSING_NEUTRAL_SCORE = 50.0
MISSING_DISCOUNT = 0.97
FIELD_LABELS = {
    "roe": "ROE",
    "net_margin": "净利率",
    "gross_margin": "毛利率",
    "revenue_growth": "营收增长率",
    "profit_growth": "净利润增长率",
    "debt_ratio": "资产负债率",
    "pe_ttm": "PE-TTM",
    "pb": "PB",
    "trend_20d": "近20日趋势",
    "technical_profile_score": "技术画像",
}


@dataclass(frozen=True)
class ScoringConfig:
    template_id: str
    template_version: str
    field_weights: dict[str, float]
    default_top_n: int
    missing_neutral_score: float = MISSING_NEUTRAL_SCORE
    missing_discount_per_field: float = MISSING_DISCOUNT
    mode: str = "shadow"


@dataclass(frozen=True)
class ScoreBreakdown:
    template_id: str
    template_version: str
    score: float
    base_score: float
    completeness_discount: float
    raw_values: dict[str, float | None]
    field_scores: dict[str, float]
    weighted_contributions: dict[str, float]
    missing_fields: tuple[str, ...]
    field_timestamps: dict[str, str]
    explanation: str


@dataclass(frozen=True)
class ScoredSnapshot:
    snapshot: Any
    breakdown: ScoreBreakdown


def scoring_config_for(template: TemplateDefinition) -> ScoringConfig:
    return ScoringConfig(
        template_id=template.template_id,
        template_version=template.version,
        field_weights=dict(template.field_weights),
        default_top_n=template.default_top_n,
        mode=template.status,
    )


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _value(snapshot: Any, field_name: str) -> float | None:
    if field_name == "technical_profile_score":
        label = str(getattr(snapshot, "technical_label", "") or "")
        status = str(getattr(snapshot, "technical_status", "") or "")
        return technical_profile_score(label, status)
    value = _number(getattr(snapshot, field_name, None))
    if field_name in {"pe_ttm", "pb"} and value is not None and value <= 0:
        return None
    return value


def technical_profile_score(label: str, status: str = "") -> float | None:
    text = (label or "").strip().upper()
    if not text or text == "技术画像缺失" or status == "missing":
        return None
    if text.startswith("S"):
        return 65.0 if "高风险" in text else 100.0
    if text.startswith("A"):
        return 80.0
    if text.startswith("B"):
        return 60.0
    if text.startswith("C"):
        return 40.0
    if text.startswith("D"):
        return 0.0
    return None


def _percentile_scores(values_by_code: dict[str, float], *, higher_better: bool) -> dict[str, float]:
    if not values_by_code:
        return {}
    ordered = sorted(values_by_code.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    if count == 1:
        return {ordered[0][0]: 50.0}
    scores: dict[str, float] = {}
    index = 0
    while index < count:
        end = index + 1
        while end < count and ordered[end][1] == ordered[index][1]:
            end += 1
        average_rank = (index + end - 1) / 2
        percentile = average_rank / (count - 1) * 100
        score = percentile if higher_better else 100 - percentile
        for pos in range(index, end):
            scores[ordered[pos][0]] = round(score, 10)
        index = end
    return scores


def _quantile(sorted_values: list[float], quantile: float) -> float:
    if not sorted_values:
        raise ValueError("不能对空数据计算分位")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


def _interpolate_piecewise(value: float, anchors: list[tuple[float, float]]) -> float:
    if value <= anchors[0][0]:
        return anchors[0][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if value <= x1:
            if math.isclose(x0, x1):
                return max(y0, y1)
            fraction = (value - x0) / (x1 - x0)
            return y0 + fraction * (y1 - y0)
    return anchors[-1][1]


def _interval_scores(values_by_code: dict[str, float]) -> dict[str, float]:
    if not values_by_code:
        return {}
    values = sorted(values_by_code.values())
    quantile_anchors = [(0.0, 0.0), (0.01, 20.0), (0.05, 70.0), (0.10, 100.0), (0.40, 100.0), (0.75, 50.0), (0.90, 0.0)]
    anchors = [(_quantile(values, quantile), score) for quantile, score in quantile_anchors]
    return {code: round(max(0.0, min(100.0, _interpolate_piecewise(value, anchors))), 10) for code, value in values_by_code.items()}


def normalized_field_scores(
    reference_snapshots: Iterable[Any],
    registry: ProductRegistry | None = None,
) -> dict[str, dict[str, float]]:
    registry = registry or load_product_registry()
    snapshots = list(reference_snapshots)
    all_fields = {field for dimension in registry.dimensions.values() for field in dimension.eligible_fields}
    directions = {field: direction for dimension in registry.dimensions.values() for field, direction in dimension.field_directions.items()}
    values: dict[str, dict[str, float]] = {field: {} for field in all_fields}
    for snapshot in snapshots:
        if _is_excluded_name(snapshot):
            continue
        code = str(getattr(snapshot, "code", "") or "")
        if not code:
            continue
        for field_name in all_fields:
            value = _value(snapshot, field_name)
            if value is not None:
                values[field_name][code] = value
    normalized: dict[str, dict[str, float]] = {}
    for field_name, field_values in values.items():
        direction = directions[field_name]
        if field_name == "technical_profile_score":
            normalized[field_name] = dict(field_values)
        elif direction == "interval_optimal":
            normalized[field_name] = _interval_scores(field_values)
        else:
            normalized[field_name] = _percentile_scores(field_values, higher_better=direction == "higher_better")
    return normalized


def _is_excluded_name(snapshot: Any) -> bool:
    name = str(getattr(snapshot, "name", "") or "").strip().upper()
    return not name or name.startswith(("退", "ST", "*ST"))


def _explanation(
    template: TemplateDefinition,
    universe_name: str,
    score: float,
    contributions: dict[str, float],
    missing_fields: list[str],
    discount: float,
) -> str:
    ordered = sorted(contributions.items(), key=lambda item: (item[1] - template.field_weights[item[0]] * 50, item[0]), reverse=True)
    top = "、".join(f"{FIELD_LABELS.get(field, field)} {value:.1f}" for field, value in ordered[:2]) or "无"
    drags = "、".join(f"{FIELD_LABELS.get(field, field)} {value:.1f}" for field, value in reversed(ordered[-2:])) or "无"
    missing = "、".join(FIELD_LABELS.get(field, field) for field in missing_fields) or "无"
    return template.explanation_template.format(
        template_name=template.display_name,
        template_version=template.version,
        universe_name=universe_name,
        score=f"{score:.1f}",
        top_contributions=top,
        top_drags=drags,
        missing_fields=missing,
        completeness_discount=f"{discount:.4f}",
    )


def score_snapshot(
    snapshot: Any,
    template: TemplateDefinition,
    normalized: dict[str, dict[str, float]],
    *,
    universe_name: str,
) -> ScoreBreakdown | None:
    if _is_excluded_name(snapshot):
        return None
    code = str(getattr(snapshot, "code", "") or "")
    raw_values: dict[str, float | None] = {}
    field_scores: dict[str, float] = {}
    contributions: dict[str, float] = {}
    missing_fields: list[str] = []
    present = 0
    for field_name, weight in template.field_weights.items():
        raw = _value(snapshot, field_name)
        raw_values[field_name] = raw
        normalized_value = normalized.get(field_name, {}).get(code) if raw is not None else None
        if normalized_value is None:
            normalized_value = MISSING_NEUTRAL_SCORE
            missing_fields.append(field_name)
        else:
            present += 1
        field_scores[field_name] = round(normalized_value, 6)
        contributions[field_name] = normalized_value * weight
    if present == 0:
        return None
    base_score = sum(contributions.values())
    discount = MISSING_DISCOUNT ** len(missing_fields)
    score = round(max(0.0, min(100.0, base_score * discount)), 1)
    financial_fields = {"roe", "net_margin", "gross_margin", "revenue_growth", "profit_growth", "debt_ratio"}
    field_timestamps: dict[str, str] = {}
    for field_name in template.field_weights:
        if field_name == "pe_ttm":
            stamp = str(getattr(snapshot, "pe_ttm_updated_at", "") or "")
        elif field_name == "pb":
            stamp = str(getattr(snapshot, "pb_updated_at", "") or getattr(snapshot, "quote_updated_at", "") or "")
        elif field_name in financial_fields:
            stamp = str(getattr(snapshot, "financial_updated_at", "") or "")
        else:
            stamp = str(getattr(snapshot, "history_updated_at", "") or "")
        field_timestamps[field_name] = stamp
    return ScoreBreakdown(
        template_id=template.template_id,
        template_version=template.version,
        score=score,
        base_score=round(base_score, 6),
        completeness_discount=round(discount, 10),
        raw_values=raw_values,
        field_scores=field_scores,
        weighted_contributions={field: round(value, 6) for field, value in contributions.items()},
        missing_fields=tuple(missing_fields),
        field_timestamps=field_timestamps,
        explanation=_explanation(template, universe_name, score, contributions, missing_fields, discount),
    )


def score_template_snapshots(
    reference_snapshots: Iterable[Any],
    candidate_snapshots: Iterable[Any],
    template_id: str,
    *,
    universe_name: str,
    top_n: int | None = None,
    registry: ProductRegistry | None = None,
) -> list[ScoredSnapshot]:
    registry = registry or load_product_registry()
    template = registry.template(template_id)
    normalized = normalized_field_scores(reference_snapshots, registry)
    scored: list[ScoredSnapshot] = []
    for snapshot in candidate_snapshots:
        breakdown = score_snapshot(snapshot, template, normalized, universe_name=universe_name)
        if breakdown is not None:
            scored.append(ScoredSnapshot(snapshot=snapshot, breakdown=breakdown))
    scored.sort(key=lambda item: (-item.breakdown.score, str(getattr(item.snapshot, "code", ""))))
    return scored[: max(1, top_n or template.default_top_n)]


def required_field_coverage(snapshots: Iterable[Any], template: TemplateDefinition) -> dict[str, float]:
    items = [item for item in snapshots if not _is_excluded_name(item)]
    total = len(items)
    if total == 0:
        return {field: 0.0 for field in template.required_fields}
    return {
        field: sum(_value(item, field) is not None for item in items) / total
        for field in template.required_fields
    }
