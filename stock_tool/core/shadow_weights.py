from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ShadowWeightResult:
    production_prior: dict[str, float]
    evidence_weights: dict[str, float]
    shadow_weights: dict[str, float]
    used_evidence: bool
    reason: str


def _normalize(values: dict[str, float], fields: tuple[str, ...]) -> dict[str, float] | None:
    cleaned: dict[str, float] = {}
    for field in fields:
        value = values.get(field)
        if value is None or not math.isfinite(float(value)) or float(value) <= 0:
            cleaned[field] = 0.0
        else:
            cleaned[field] = float(value)
    total = sum(cleaned.values())
    if total <= 0:
        return None
    return {field: value / total for field, value in cleaned.items()}


def _bounded_simplex_projection(
    target: dict[str, float], lower: dict[str, float], upper: dict[str, float]
) -> dict[str, float]:
    """Euclidean projection onto sum(w)=1 with per-field bounds."""
    fields = tuple(target)
    if sum(lower.values()) > 1.0 + 1e-12 or sum(upper.values()) < 1.0 - 1e-12:
        raise ValueError("权重边界与总和为1约束不相容")
    lo = min(lower[field] - target[field] for field in fields) - 1.0
    hi = max(upper[field] - target[field] for field in fields) + 1.0
    for _ in range(100):
        shift = (lo + hi) / 2.0
        total = sum(min(upper[field], max(lower[field], target[field] + shift)) for field in fields)
        if total < 1.0:
            lo = shift
        else:
            hi = shift
    shift = (lo + hi) / 2.0
    result = {field: min(upper[field], max(lower[field], target[field] + shift)) for field in fields}
    residual = 1.0 - sum(result.values())
    for field in fields:
        if abs(residual) <= 1e-12:
            break
        room = (upper[field] - result[field]) if residual > 0 else (result[field] - lower[field])
        delta = math.copysign(min(abs(residual), room), residual)
        result[field] += delta
        residual -= delta
    return result


def derive_shadow_weights(
    production_prior: dict[str, float],
    positive_stable_evidence: dict[str, float] | None,
) -> ShadowWeightResult:
    """Apply the preregistered 75/25 shrinkage and ±25% prior bounds.

    The caller must pass only evidence established on the 2019–2024 training window.
    Holdout diagnostics are deliberately absent from this interface.
    """
    fields = tuple(production_prior)
    prior = _normalize(production_prior, fields)
    if prior is None or any(field not in production_prior for field in fields):
        raise ValueError("生产先验必须包含正权重")
    evidence = _normalize(positive_stable_evidence or {}, fields)
    if evidence is None:
        return ShadowWeightResult(prior, {}, dict(prior), False, "无稳定正证据，退回生产先验")
    target = {field: 0.75 * prior[field] + 0.25 * evidence[field] for field in fields}
    lower = {field: prior[field] * 0.75 for field in fields}
    upper = {field: prior[field] * 1.25 for field in fields}
    projected = _bounded_simplex_projection(target, lower, upper)
    return ShadowWeightResult(
        production_prior=prior,
        evidence_weights=evidence,
        shadow_weights={field: round(projected[field], 12) for field in fields},
        used_evidence=True,
        reason="2019–2024训练证据按75%先验+25%证据收缩，并投影到±25%边界",
    )
