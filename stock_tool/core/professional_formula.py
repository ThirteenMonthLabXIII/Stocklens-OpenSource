"""Legacy formula compatibility layer.

Historical formula rows remain readable.  New formal candidate identity uses
selection policies and CandidateLens versions rather than treating a formula
score as an Agent decision.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


PROFESSIONAL_FORMULA_VERSION = "professional_formula_v1"
PROMPT_PROFESSIONAL_WEIGHT_CANDIDATE_V1 = "professional_weight_candidate_v1"

PROFESSIONAL_DIMENSIONS = (
    "quality",
    "growth",
    "valuation",
    "safety",
    "trend",
    "industry",
    "money_flow",
)

PROFESSIONAL_DIMENSION_LABELS = {
    "quality": "质量",
    "growth": "成长",
    "valuation": "估值",
    "safety": "安全",
    "trend": "趋势",
    "industry": "行业",
    "money_flow": "资金",
}

PROFESSIONAL_FIELD_LABELS = {
    "roe": "ROE",
    "gross_margin": "毛利率",
    "net_margin": "净利率",
    "revenue_growth": "营收增长",
    "profit_growth": "利润增长",
    "pe_ttm": "PE-TTM",
    "pb": "PB",
    "debt_ratio": "负债率",
    "max_drawdown": "最大回撤",
    "trend_20d": "趋势",
    "box_score": "箱体",
    "rsi14": "RSI",
    "industry_strength": "行业强度",
    "main_5d_yi": "近5日主力净流入",
}

PROFESSIONAL_DIMENSION_FIELDS = {
    "quality": ("roe", "gross_margin", "net_margin"),
    "growth": ("revenue_growth", "profit_growth"),
    "valuation": ("pe_ttm", "pb"),
    "safety": ("debt_ratio", "max_drawdown"),
    "trend": ("trend_20d", "box_score", "rsi14"),
    "industry": ("industry_strength",),
    "money_flow": ("main_5d_yi",),
}

PROFESSIONAL_DEFAULT_DIMENSION_WEIGHTS = {
    "quality": 0.20,
    "growth": 0.16,
    "valuation": 0.14,
    "safety": 0.16,
    "trend": 0.16,
    "industry": 0.08,
    "money_flow": 0.10,
}

PROFESSIONAL_DEFAULT_FIELD_WEIGHTS = {
    "quality": {"roe": 0.45, "gross_margin": 0.25, "net_margin": 0.30},
    "growth": {"revenue_growth": 0.45, "profit_growth": 0.55},
    "valuation": {"pe_ttm": 0.55, "pb": 0.45},
    "safety": {"debt_ratio": 0.55, "max_drawdown": 0.45},
    "trend": {"trend_20d": 0.40, "box_score": 0.35, "rsi14": 0.25},
    "industry": {"industry_strength": 1.00},
    "money_flow": {"main_5d_yi": 1.00},
}

PROFESSIONAL_OBSERVATION_PERIOD_WEIGHTS = {3: 0.10, 5: 0.15, 10: 0.25, 20: 0.50}
PROFESSIONAL_PERFORMANCE_WEIGHTS = {
    "benchmark_hit_rate": 0.35,
    "direction_hit_rate": 0.20,
    "avg_excess_vs_hs300_pct": 0.20,
    "drawdown_control": 0.15,
    "strong_hit_rate": 0.10,
}

PROFESSIONAL_MIN_SETTLED_SAMPLES = 30
PROFESSIONAL_MIN_DATA_QUALITY_SCORE = 80.0
PROFESSIONAL_MIN_DIRECTION_HIT_RATE = 0.50
PROFESSIONAL_MIN_BENCHMARK_HIT_RATE = 0.50
PROFESSIONAL_MIN_AVG_EXCESS_VS_HS300_PCT = 0.0
PROFESSIONAL_MIN_AVG_MAX_DRAWDOWN_PCT = -18.0
PROFESSIONAL_MAX_DIMENSION_DELTA = 0.05
PROFESSIONAL_MAX_FIELD_DELTA = 0.08

FORBIDDEN_INVESTMENT_TERMS = (
    "买入",
    "卖出",
    "必涨",
    "稳赚",
    "收益承诺",
    "准确率承诺",
    "保证准确率",
    "保证收益",
    "稳赚不赔",
)


@dataclass(frozen=True)
class ProfessionalValidationResult:
    accepted: bool
    status: str
    suggest_enable: bool
    reason: str
    dimension_weights: dict[str, float] = field(default_factory=dict)
    field_weights: dict[str, dict[str, float]] = field(default_factory=dict)
    active_score: float | None = None
    candidate_score: float | None = None
    checks: dict[str, Any] = field(default_factory=dict)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def json_loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        payload = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return payload if payload is not None else default


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def normalize_weight_map(
    weights: dict[str, Any] | None,
    keys: tuple[str, ...],
    fallback: dict[str, float],
) -> dict[str, float]:
    raw = weights or {}
    cleaned = {key: max(0.0, float(raw.get(key, fallback.get(key, 0.0)) or 0.0)) for key in keys}
    total = sum(cleaned.values())
    if total <= 0:
        cleaned = {key: max(0.0, float(fallback.get(key, 0.0) or 0.0)) for key in keys}
        total = sum(cleaned.values())
    if total <= 0:
        share = round(1.0 / len(keys), 6)
        return {key: share for key in keys}
    normalized = {key: round(value / total, 6) for key, value in cleaned.items()}
    delta = round(1.0 - sum(normalized.values()), 6)
    if normalized and abs(delta) > 0:
        first = keys[0]
        normalized[first] = round(normalized[first] + delta, 6)
    return normalized


def normalize_dimension_weights(weights: dict[str, Any] | None = None) -> dict[str, float]:
    return normalize_weight_map(weights, PROFESSIONAL_DIMENSIONS, PROFESSIONAL_DEFAULT_DIMENSION_WEIGHTS)


def normalize_field_weights(weights: dict[str, Any] | None = None) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    raw = weights or {}
    for dimension, fields in PROFESSIONAL_DIMENSION_FIELDS.items():
        dimension_raw = raw.get(dimension) if isinstance(raw.get(dimension), dict) else None
        result[dimension] = normalize_weight_map(
            dimension_raw,
            fields,
            PROFESSIONAL_DEFAULT_FIELD_WEIGHTS[dimension],
        )
    return result


def build_default_professional_formula() -> dict[str, Any]:
    return {
        "version": PROFESSIONAL_FORMULA_VERSION,
        "dimension_weights": normalize_dimension_weights(),
        "field_weights": normalize_field_weights(),
    }


def _linear_score(value: float, bad: float, good: float, *, higher_is_better: bool = True) -> float:
    if bad == good:
        return 100.0
    if higher_is_better:
        score = (value - bad) / (good - bad) * 100.0
    else:
        score = (bad - value) / (bad - good) * 100.0
    return max(0.0, min(100.0, score))


def _band_score(value: float, low_good: float, high_good: float, low_bad: float, high_bad: float) -> float:
    if low_good <= value <= high_good:
        return 100.0
    if value < low_good:
        return _linear_score(value, low_bad, low_good)
    return _linear_score(value, high_bad, high_good, higher_is_better=False)


def _score_drawdown(value: float) -> float:
    drawdown = value if value <= 0 else -abs(value)
    return _linear_score(drawdown, -30.0, -5.0)


def _score_money_flow(value: float) -> float:
    amount_yi = value / 100000000.0 if abs(value) > 1000000 else value
    return _linear_score(amount_yi, -2.0, 2.0)


def score_professional_field(field_name: str, value: Any) -> float | None:
    numeric = to_float(value)
    if numeric is None:
        return None
    if field_name == "roe":
        return _linear_score(numeric, 0.0, 25.0)
    if field_name == "gross_margin":
        return _linear_score(numeric, 5.0, 60.0)
    if field_name == "net_margin":
        return _linear_score(numeric, -5.0, 25.0)
    if field_name == "revenue_growth":
        return _linear_score(numeric, -15.0, 45.0)
    if field_name == "profit_growth":
        return _linear_score(numeric, -20.0, 55.0)
    if field_name == "pe_ttm":
        return _band_score(numeric, 5.0, 28.0, 0.0, 95.0)
    if field_name == "pb":
        return _band_score(numeric, 0.5, 2.8, 0.0, 8.0)
    if field_name == "debt_ratio":
        return _linear_score(numeric, 88.0, 30.0, higher_is_better=False)
    if field_name == "max_drawdown":
        return _score_drawdown(numeric)
    if field_name == "trend_20d":
        return _linear_score(numeric, -10.0, 18.0)
    if field_name == "box_score":
        return max(0.0, min(100.0, numeric))
    if field_name == "rsi14":
        return _band_score(numeric, 42.0, 68.0, 18.0, 88.0)
    if field_name == "industry_strength":
        return max(0.0, min(100.0, numeric))
    if field_name == "main_5d_yi":
        return _score_money_flow(numeric)
    return None


def _extra_value(extra_fields: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in extra_fields:
            return extra_fields[name]
    return None


def professional_field_value(
    field_name: str,
    quote: Any,
    metrics: Any,
    technical: Any,
    extra_fields: dict[str, Any] | None = None,
) -> Any:
    extra = extra_fields or {}
    if field_name in extra:
        return extra[field_name]
    if field_name in {"roe", "gross_margin", "net_margin", "debt_ratio", "revenue_growth", "profit_growth"}:
        return getattr(metrics, field_name, None)
    if field_name == "pe_ttm":
        return getattr(quote, "pe_ttm", None) if getattr(quote, "pe_ttm", None) is not None else getattr(quote, "pe", None)
    if field_name == "pb":
        return getattr(quote, "pb", None)
    if field_name == "max_drawdown":
        return (
            _extra_value(extra, "max_drawdown", "max_drawdown_60d", "max_drawdown_30d")
            if extra
            else None
        ) or getattr(technical, "max_drawdown_60d", None) or getattr(technical, "max_drawdown_30d", None)
    if field_name == "trend_20d":
        value = _extra_value(extra, "trend_20d")
        if value is not None:
            return value
        close = to_float(getattr(technical, "close", None))
        ma20 = to_float(getattr(technical, "ma20", None))
        if close is not None and ma20 not in (None, 0):
            return (close / ma20 - 1.0) * 100.0
        return None
    if field_name == "box_score":
        return getattr(technical, "box_score", None)
    if field_name == "rsi14":
        return getattr(technical, "rsi14", None)
    if field_name == "industry_strength":
        return getattr(quote, "industry_strength", None)
    if field_name == "main_5d_yi":
        return _extra_value(extra, "main_5d_yi", "main_5d", "money_flow_5d_yi")
    return None


def build_professional_score_breakdown(
    quote: Any,
    metrics: Any,
    technical: Any,
    *,
    extra_fields: dict[str, Any] | None = None,
    dimension_weights: dict[str, Any] | None = None,
    field_weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dimensions = normalize_dimension_weights(dimension_weights)
    fields = normalize_field_weights(field_weights)
    dimension_scores: dict[str, float] = {}
    field_scores: dict[str, dict[str, dict[str, Any]]] = {}
    missing_fields: list[str] = []
    explanations: list[str] = []

    for dimension, dimension_fields in PROFESSIONAL_DIMENSION_FIELDS.items():
        field_scores[dimension] = {}
        dimension_score = 0.0
        present_count = 0
        for field_name in dimension_fields:
            value = professional_field_value(field_name, quote, metrics, technical, extra_fields)
            score = score_professional_field(field_name, value)
            weight = fields[dimension].get(field_name, 0.0)
            if score is None:
                missing_fields.append(field_name)
                contribution = 0.0
            else:
                present_count += 1
                contribution = round(score * weight, 6)
                dimension_score += contribution
            field_scores[dimension][field_name] = {
                "label": PROFESSIONAL_FIELD_LABELS.get(field_name, field_name),
                "value": value,
                "score": None if score is None else round(score, 4),
                "weight": weight,
                "contribution": contribution,
            }
        dimension_scores[dimension] = round(max(0.0, min(100.0, dimension_score)), 4)
        if present_count == 0:
            explanations.append(f"{PROFESSIONAL_DIMENSION_LABELS[dimension]}维度缺字段，按0分计入。")
        elif dimension_scores[dimension] >= 75:
            explanations.append(f"{PROFESSIONAL_DIMENSION_LABELS[dimension]}维度较强。")
        elif dimension_scores[dimension] <= 35:
            explanations.append(f"{PROFESSIONAL_DIMENSION_LABELS[dimension]}维度偏弱或字段不足。")

    total = sum(dimensions[dimension] * dimension_scores[dimension] for dimension in PROFESSIONAL_DIMENSIONS)
    # A template may intentionally disable a dimension.  Its unavailable
    # fields are still retained in evidence_gaps, but must not lower the
    # quality score of the fields the template actually declares relevant.
    relevant_fields = [
        field_name
        for dimension, dimension_fields in PROFESSIONAL_DIMENSION_FIELDS.items()
        if dimensions.get(dimension, 0.0) > 0
        for field_name in dimension_fields
        if fields[dimension].get(field_name, 0.0) > 0
    ]
    relevant_missing = sum(1 for field_name in missing_fields if field_name in relevant_fields)
    field_count = len(relevant_fields)
    data_quality_score = round((field_count - relevant_missing) / field_count * 100.0, 2) if field_count else 100.0
    return {
        "version": PROFESSIONAL_FORMULA_VERSION,
        "score": round(max(0.0, min(100.0, total)), 4),
        "dimension_weights": dimensions,
        "field_weights": fields,
        "dimension_scores": dimension_scores,
        "field_scores": field_scores,
        "missing_fields": missing_fields,
        "data_quality_score": data_quality_score,
        "explanations": explanations[:6],
    }


def extract_professional_breakdown(score_breakdown: Any) -> dict[str, Any] | None:
    payload = json_loads(score_breakdown, {})
    if not isinstance(payload, dict):
        return None
    if payload.get("version") == PROFESSIONAL_FORMULA_VERSION:
        return payload
    item = payload.get("professional_formula")
    return item if isinstance(item, dict) else None


def _valid_settled_count(stats: dict[str, Any]) -> int:
    if "valid_settled_sample_count" in stats:
        return max(0, int(stats.get("valid_settled_sample_count") or 0))
    return max(0, int(stats.get("settled_sample_count") or stats.get("settled_count") or 0))


def robust_performance_score(stats: dict[str, Any]) -> float:
    settled = _valid_settled_count(stats)
    if settled <= 0:
        return 0.0
    benchmark = float(stats.get("benchmark_hit_rate") or 0.0)
    direction = float(stats.get("direction_hit_rate") or 0.0)
    excess = float(stats.get("avg_excess_vs_hs300_pct") or 0.0)
    drawdown = float(stats.get("avg_max_drawdown_pct") or 0.0)
    strong = float(stats.get("strong_hit_rate") or 0.0)

    excess_score = max(0.0, min(1.0, (excess + 5.0) / 10.0))
    drawdown_score = max(0.0, min(1.0, (drawdown + 18.0) / 18.0))
    score = (
        benchmark * PROFESSIONAL_PERFORMANCE_WEIGHTS["benchmark_hit_rate"]
        + direction * PROFESSIONAL_PERFORMANCE_WEIGHTS["direction_hit_rate"]
        + excess_score * PROFESSIONAL_PERFORMANCE_WEIGHTS["avg_excess_vs_hs300_pct"]
        + drawdown_score * PROFESSIONAL_PERFORMANCE_WEIGHTS["drawdown_control"]
        + strong * PROFESSIONAL_PERFORMANCE_WEIGHTS["strong_hit_rate"]
    )
    return round(score * 100.0, 4)


def summarize_performance_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [
        row
        for row in rows
        if row.get("return_pct") is not None or str(row.get("data_quality") or "").strip()
    ]
    valid = [row for row in settled if str(row.get("data_quality") or "ok") == "ok"]
    invalid = [row for row in settled if str(row.get("data_quality") or "ok") != "ok"]
    if not settled:
        return {
            "sample_count": len(rows),
            "settled_sample_count": 0,
            "valid_settled_sample_count": 0,
            "invalid_settled_sample_count": 0,
            "data_quality_score": 0.0,
            "robust_score": 0.0,
        }
    direction_values = [bool(row.get("direction_hit")) for row in valid if row.get("direction_hit") is not None]
    benchmark_values = [bool(row.get("benchmark_hit")) for row in valid if row.get("benchmark_hit") is not None]
    excess_values = [to_float(row.get("excess_vs_hs300_pct")) for row in valid]
    drawdown_values = [to_float(row.get("max_drawdown_pct")) for row in valid]
    strong_count = sum(1 for row in valid if str(row.get("rating") or "") == "A")
    ok_count = len(valid)

    def avg(values: list[float | None]) -> float | None:
        clean = [value for value in values if value is not None]
        return round(sum(clean) / len(clean), 6) if clean else None

    stats = {
        "sample_count": len(rows),
        "settled_sample_count": len(settled),
        "valid_settled_sample_count": len(valid),
        "invalid_settled_sample_count": len(invalid),
        "direction_hit_rate": round(sum(direction_values) / len(direction_values), 6) if direction_values else None,
        "benchmark_hit_rate": round(sum(benchmark_values) / len(benchmark_values), 6) if benchmark_values else None,
        "avg_excess_vs_hs300_pct": avg(excess_values),
        "avg_max_drawdown_pct": avg(drawdown_values),
        "strong_hit_rate": round(strong_count / len(valid), 6) if valid else None,
        "data_quality_score": round(ok_count / len(settled) * 100.0, 4),
    }
    stats["robust_score"] = robust_performance_score(stats)
    return stats


def _row_professional_scores(row: dict[str, Any]) -> dict[str, float]:
    breakdown = extract_professional_breakdown(row.get("score_breakdown_json") or row.get("score_breakdown"))
    scores = breakdown.get("dimension_scores", {}) if breakdown else {}
    return {dimension: float(scores.get(dimension) or 0.0) for dimension in PROFESSIONAL_DIMENSIONS}


def derive_professional_weight_ranges(
    rows: list[dict[str, Any]],
    *,
    active_dimension_weights: dict[str, Any] | None = None,
    active_field_weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active_dimensions = normalize_dimension_weights(active_dimension_weights)
    active_fields = normalize_field_weights(active_field_weights)
    settled = [
        row for row in rows
        if (row.get("return_pct") is not None or row.get("data_quality"))
        and str(row.get("data_quality") or "ok") == "ok"
    ]
    sample_count = len(settled)
    confidence = min(1.0, sample_count / 120.0)
    spread = 0.10 - 0.04 * confidence
    dimensions: dict[str, dict[str, Any]] = {}
    fields: dict[str, dict[str, dict[str, Any]]] = {}
    overall_excess = _average(row.get("excess_vs_hs300_pct") for row in settled)

    for dimension in PROFESSIONAL_DIMENSIONS:
        high_rows = [row for row in settled if _row_professional_scores(row).get(dimension, 0.0) >= 65.0]
        high_excess = _average(row.get("excess_vs_hs300_pct") for row in high_rows)
        lift = 0.0
        if high_excess is not None and overall_excess is not None:
            lift = max(-0.025, min(0.025, (high_excess - overall_excess) / 200.0))
        center = active_dimensions[dimension] + lift * confidence
        lower = max(0.01, center - spread)
        upper = min(0.45, center + spread)
        lower = min(lower, active_dimensions[dimension])
        upper = max(upper, active_dimensions[dimension])
        dimensions[dimension] = {
            "label": PROFESSIONAL_DIMENSION_LABELS[dimension],
            "min": round(lower, 6),
            "max": round(upper, 6),
            "center": round(center, 6),
            "sample_count": sample_count,
            "evidence_lift": round(lift, 6),
        }
        fields[dimension] = {}
        field_spread = min(0.22, 0.14 - 0.04 * confidence)
        for field_name, weight in active_fields[dimension].items():
            fields[dimension][field_name] = {
                "label": PROFESSIONAL_FIELD_LABELS.get(field_name, field_name),
                "min": round(max(0.01, weight - field_spread), 6),
                "max": round(min(1.0, weight + field_spread), 6),
                "center": round(weight, 6),
                "sample_count": sample_count,
            }
    return {
        "version": PROFESSIONAL_FORMULA_VERSION,
        "generated_at": now_iso(),
        "sample_count": sample_count,
        "dimensions": dimensions,
        "fields": fields,
    }


def derive_recent_precise_weights(
    rows: list[dict[str, Any]],
    *,
    active_dimension_weights: dict[str, Any] | None = None,
    active_field_weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dimensions = normalize_dimension_weights(active_dimension_weights)
    fields = normalize_field_weights(active_field_weights)
    settled = [
        row for row in rows
        if (row.get("return_pct") is not None or row.get("data_quality"))
        and str(row.get("data_quality") or "ok") == "ok"
    ]
    if len(settled) < 1:
        return {"dimension_weights": dimensions, "field_weights": fields}
    overall_excess = _average(row.get("excess_vs_hs300_pct") for row in settled) or 0.0
    target_dimensions = dict(dimensions)
    for dimension in PROFESSIONAL_DIMENSIONS:
        high_rows = [row for row in settled if _row_professional_scores(row).get(dimension, 0.0) >= 65.0]
        high_excess = _average(row.get("excess_vs_hs300_pct") for row in high_rows)
        if high_excess is None:
            continue
        shift = max(-0.04, min(0.04, (high_excess - overall_excess) / 120.0))
        target_dimensions[dimension] = max(0.01, dimensions[dimension] + shift)
    return {
        "dimension_weights": normalize_dimension_weights(target_dimensions),
        "field_weights": fields,
    }


def _average(values: Any) -> float | None:
    clean = [to_float(value) for value in values]
    numbers = [value for value in clean if value is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 6)


def build_professional_weight_candidate_prompt(evidence_package: dict[str, Any]) -> str:
    return f"""
你是 A 股候选池“专业公式闭环”复盘助手。你只能基于证据包和结构化记忆提出 shadow 候选权重；不能直接启用公式，也不能给买入、卖出或收益承诺。

输出严格 JSON，不要 Markdown，不要解释性正文。字段：
{{
  "should_adjust": true,
  "candidate_name": "professional_shadow_YYYYMMDD",
  "dimension_weights": {{}},
  "field_weights": {{}},
  "adjustment_reasons": [],
  "risks": [],
  "sample_gaps": [],
  "next_validation_conditions": []
}}

硬约束：
1. 样本不足 {PROFESSIONAL_MIN_SETTLED_SAMPLES} 或数据质量不足 {PROFESSIONAL_MIN_DATA_QUALITY_SCORE:.0f}% 时，should_adjust 必须为 false。
2. 3/5/10/20 日周期必须都有已结算样本，且方向命中、跑赢沪深300、平均超额和回撤控制全部过线。
3. 维度权重只允许这些键：{", ".join(PROFESSIONAL_DIMENSIONS)}，总和必须为 1。
4. 字段权重必须按维度内归一，总和为 1。
5. 单维度相对 active 调整不得超过 {PROFESSIONAL_MAX_DIMENSION_DELTA:.2f}，单字段不得超过 {PROFESSIONAL_MAX_FIELD_DELTA:.2f}。
6. 候选必须落在 historical_weight_ranges 内，并说明下一轮验证条件。
7. 禁止出现买入、卖出、必涨、稳赚、收益承诺或准确率承诺。

版本：{PROMPT_PROFESSIONAL_WEIGHT_CANDIDATE_V1}
证据包：
{json_dumps(evidence_package)}
""".strip()


def validate_professional_candidate_payload(
    payload: dict[str, Any],
    evidence_package: dict[str, Any],
    *,
    active_dimension_weights: dict[str, Any] | None = None,
    active_field_weights: dict[str, Any] | None = None,
) -> ProfessionalValidationResult:
    if not isinstance(payload, dict):
        raise ValueError("AI 专业公式候选必须是 JSON 对象")
    raw_text = json_dumps(payload)
    if any(word in raw_text for word in FORBIDDEN_INVESTMENT_TERMS):
        raise ValueError("专业公式候选包含禁止的投资建议或收益/准确率承诺表述")

    long_term = evidence_package.get("long_term") or {}
    settled_count = _valid_settled_count(long_term)
    total_settled_count = int(long_term.get("settled_sample_count") or long_term.get("settled_count") or 0)
    data_quality_score = float(long_term.get("data_quality_score") or 0.0)
    if settled_count < PROFESSIONAL_MIN_SETTLED_SAMPLES:
        return ProfessionalValidationResult(
            accepted=False,
            status="rejected",
            suggest_enable=False,
            reason=f"已结算样本不足 {PROFESSIONAL_MIN_SETTLED_SAMPLES}，继续观察。",
            checks={"settled_sample_count": total_settled_count, "valid_settled_sample_count": settled_count, "data_quality_score": data_quality_score},
        )
    if data_quality_score < PROFESSIONAL_MIN_DATA_QUALITY_SCORE:
        return ProfessionalValidationResult(
            accepted=False,
            status="rejected",
            suggest_enable=False,
            reason=f"数据质量 {data_quality_score:.1f}% 不足 {PROFESSIONAL_MIN_DATA_QUALITY_SCORE:.0f}%。",
            checks={"settled_sample_count": total_settled_count, "valid_settled_sample_count": settled_count, "data_quality_score": data_quality_score},
        )
    gate_issues = _professional_enable_gate_issues(long_term)
    if gate_issues:
        return ProfessionalValidationResult(
            accepted=False,
            status="rejected",
            suggest_enable=False,
            reason="；".join(gate_issues[:3]) + "，继续观察。",
            checks={
                "settled_sample_count": total_settled_count,
                "valid_settled_sample_count": settled_count,
                "data_quality_score": data_quality_score,
                "gate_issues": gate_issues,
            },
        )
    if payload.get("should_adjust") is False:
        return ProfessionalValidationResult(
            accepted=False,
            status="rejected",
            suggest_enable=False,
            reason=str(payload.get("reason") or "AI 判断暂不调权。"),
            checks={"settled_sample_count": total_settled_count, "valid_settled_sample_count": settled_count, "data_quality_score": data_quality_score},
        )

    active_dimensions = normalize_dimension_weights(
        active_dimension_weights
        or (evidence_package.get("active") or {}).get("dimension_weights")
    )
    active_fields = normalize_field_weights(
        active_field_weights
        or (evidence_package.get("active") or {}).get("field_weights")
    )
    candidate_dimensions = _strict_dimension_weights(payload.get("dimension_weights"))
    candidate_fields = _strict_field_weights(payload.get("field_weights"))
    ranges = evidence_package.get("historical_weight_ranges") or {}

    range_issues = _candidate_range_issues(candidate_dimensions, candidate_fields, active_dimensions, active_fields, ranges)
    if range_issues:
        return ProfessionalValidationResult(
            accepted=False,
            status="rejected",
            suggest_enable=False,
            reason="；".join(range_issues[:3]),
            dimension_weights=candidate_dimensions,
            field_weights=candidate_fields,
            checks={"range_issues": range_issues, "settled_sample_count": total_settled_count, "valid_settled_sample_count": settled_count, "data_quality_score": data_quality_score},
        )

    precise = evidence_package.get("recent_precise_weights") or {}
    target_dimensions = normalize_dimension_weights(precise.get("dimension_weights") or active_dimensions)
    target_fields = normalize_field_weights(precise.get("field_weights") or active_fields)
    active_score = _closeness_score(active_dimensions, active_fields, target_dimensions, target_fields)
    candidate_score = _closeness_score(candidate_dimensions, candidate_fields, target_dimensions, target_fields)
    shadow_stats = _candidate_shadow_stats(payload, evidence_package)
    shadow_issues = _candidate_shadow_validation_issues(shadow_stats, long_term)
    suggest_enable = not shadow_issues and candidate_score >= active_score + 0.1
    status = "suggest_enable" if suggest_enable else "shadow"
    if shadow_issues:
        reason = "候选通过权重硬约束，但缺少不劣于 active 的 shadow 结算证据，建议继续 shadow 观察。"
    elif suggest_enable:
        reason = "候选通过硬约束，并且 shadow 结算证据不劣于当前 active。"
    else:
        reason = "候选通过硬约束和 shadow 结算证据，但相对 active 权重目标优势不足，建议继续 shadow 观察。"
    return ProfessionalValidationResult(
        accepted=True,
        status=status,
        suggest_enable=suggest_enable,
        reason=reason,
        dimension_weights=candidate_dimensions,
        field_weights=candidate_fields,
        active_score=round(active_score, 4),
        candidate_score=round(candidate_score, 4),
        checks={
            "settled_sample_count": total_settled_count,
            "valid_settled_sample_count": settled_count,
            "data_quality_score": data_quality_score,
            "max_dimension_delta": PROFESSIONAL_MAX_DIMENSION_DELTA,
            "max_field_delta": PROFESSIONAL_MAX_FIELD_DELTA,
            "shadow_validation_issues": shadow_issues,
            "shadow_validation": shadow_stats,
        },
    )


def _professional_enable_gate_issues(stats: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    by_horizon = stats.get("by_horizon")
    if isinstance(by_horizon, dict):
        missing_horizons = [
            str(horizon)
            for horizon in PROFESSIONAL_OBSERVATION_PERIOD_WEIGHTS
            if _valid_settled_count(by_horizon.get(str(horizon)) or {}) <= 0
        ]
        if missing_horizons:
            issues.append("缺少已结算周期：" + "/".join(missing_horizons))
    direction = to_float(stats.get("direction_hit_rate"))
    if direction is None or direction < PROFESSIONAL_MIN_DIRECTION_HIT_RATE:
        issues.append(f"方向命中率不足 {PROFESSIONAL_MIN_DIRECTION_HIT_RATE:.0%}")
    benchmark = to_float(stats.get("benchmark_hit_rate"))
    if benchmark is None or benchmark < PROFESSIONAL_MIN_BENCHMARK_HIT_RATE:
        issues.append(f"跑赢沪深300比例不足 {PROFESSIONAL_MIN_BENCHMARK_HIT_RATE:.0%}")
    excess = to_float(stats.get("avg_excess_vs_hs300_pct"))
    if excess is None or excess < PROFESSIONAL_MIN_AVG_EXCESS_VS_HS300_PCT:
        issues.append("平均超额收益未跑赢沪深300")
    drawdown = to_float(stats.get("avg_max_drawdown_pct"))
    if drawdown is None or drawdown < PROFESSIONAL_MIN_AVG_MAX_DRAWDOWN_PCT:
        issues.append(f"平均最大回撤低于 {PROFESSIONAL_MIN_AVG_MAX_DRAWDOWN_PCT:.0f}%")
    return issues


def _candidate_shadow_stats(payload: dict[str, Any], evidence_package: dict[str, Any]) -> dict[str, Any]:
    for key in ("shadow_validation", "validation_metrics"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    value = evidence_package.get("shadow_validation")
    return value if isinstance(value, dict) else {}


def _candidate_shadow_validation_issues(candidate: dict[str, Any], active: dict[str, Any]) -> list[str]:
    if not candidate:
        return ["缺少 shadow 已结算验证指标"]
    issues = _professional_enable_gate_issues(candidate)
    if not isinstance(candidate.get("by_horizon"), dict):
        issues.append("缺少 shadow 3/5/10/20 周期验证")
    settled = _valid_settled_count(candidate)
    if settled < PROFESSIONAL_MIN_SETTLED_SAMPLES:
        issues.append(f"shadow 已结算样本不足 {PROFESSIONAL_MIN_SETTLED_SAMPLES}")
    data_quality = to_float(candidate.get("data_quality_score"))
    if data_quality is None or data_quality < PROFESSIONAL_MIN_DATA_QUALITY_SCORE:
        issues.append(f"shadow 数据质量不足 {PROFESSIONAL_MIN_DATA_QUALITY_SCORE:.0f}%")

    comparisons = (
        ("direction_hit_rate", "方向命中率", 1.0),
        ("benchmark_hit_rate", "跑赢沪深300比例", 1.0),
        ("avg_excess_vs_hs300_pct", "平均超额", 1.0),
        ("avg_max_drawdown_pct", "最大回撤控制", 1.0),
    )
    for field, label, direction in comparisons:
        candidate_value = to_float(candidate.get(field))
        active_value = to_float(active.get(field))
        if active_value is None or candidate_value is None:
            issues.append(f"缺少{label}对比指标")
            continue
        if direction > 0 and candidate_value + 0.000001 < active_value:
            issues.append(f"{label}劣于 active")
    return list(dict.fromkeys(issues))


def propose_professional_shadow_candidate(evidence_package: dict[str, Any]) -> dict[str, Any]:
    active = evidence_package.get("active") or {}
    precise = evidence_package.get("recent_precise_weights") or {}
    active_dimensions = normalize_dimension_weights(active.get("dimension_weights"))
    active_fields = normalize_field_weights(active.get("field_weights"))
    target_dimensions = normalize_dimension_weights(precise.get("dimension_weights") or active_dimensions)
    target_fields = normalize_field_weights(precise.get("field_weights") or active_fields)
    candidate_dimensions = {
        dimension: active_dimensions[dimension]
        + max(-PROFESSIONAL_MAX_DIMENSION_DELTA, min(PROFESSIONAL_MAX_DIMENSION_DELTA, target_dimensions[dimension] - active_dimensions[dimension]))
        for dimension in PROFESSIONAL_DIMENSIONS
    }
    candidate_fields: dict[str, dict[str, float]] = {}
    for dimension, fields in PROFESSIONAL_DIMENSION_FIELDS.items():
        candidate_fields[dimension] = {}
        for field_name in fields:
            current = active_fields[dimension][field_name]
            target = target_fields[dimension][field_name]
            candidate_fields[dimension][field_name] = current + max(
                -PROFESSIONAL_MAX_FIELD_DELTA,
                min(PROFESSIONAL_MAX_FIELD_DELTA, target - current),
            )
    return {
        "should_adjust": True,
        "candidate_name": "professional_shadow_local",
        "dimension_weights": normalize_dimension_weights(candidate_dimensions),
        "field_weights": normalize_field_weights(candidate_fields),
        "adjustment_reasons": ["本地证据目标生成的 shadow 候选，仍需复盘验证。"],
        "risks": ["样本分布和公开数据缺口可能影响权重稳定性。"],
        "sample_gaps": list((evidence_package.get("data_gaps") or [])[:5]),
        "next_validation_conditions": ["继续完成下一轮 3/5/10/20 日观察后复核。"],
    }


def build_professional_memory_entries(evidence_package: dict[str, Any]) -> list[dict[str, Any]]:
    cases = evidence_package.get("cases") or {}
    gaps = evidence_package.get("data_gaps") or []
    entries: list[dict[str, Any]] = []
    if cases.get("strong_success"):
        entries.append(
            {
                "memory_type": "effective_experience",
                "title": "强成功样本共性",
                "payload": {"cases": cases["strong_success"][:5]},
            }
        )
    if cases.get("failures"):
        entries.append(
            {
                "memory_type": "failure_pattern",
                "title": "失败样本特征",
                "payload": {"cases": cases["failures"][:5]},
            }
        )
    if gaps:
        entries.append(
            {
                "memory_type": "data_gap",
                "title": "下一轮优先修补的数据缺口",
                "payload": {"gaps": gaps[:8]},
            }
        )
    next_conditions = (evidence_package.get("recent_round") or {}).get("next_validation_conditions") or []
    if next_conditions:
        entries.append(
            {
                "memory_type": "next_validation",
                "title": "下一轮验证条件",
                "payload": {"conditions": next_conditions[:6]},
            }
        )
    return entries


def _strict_dimension_weights(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError("候选必须包含 dimension_weights")
    extra = sorted(set(value) - set(PROFESSIONAL_DIMENSIONS))
    missing = [key for key in PROFESSIONAL_DIMENSIONS if key not in value]
    if extra or missing:
        raise ValueError(f"维度权重键不匹配：缺少 {missing or '-'}，多余 {extra or '-'}")
    weights = {key: _strict_positive_number(value[key], f"dimension_weights.{key}") for key in PROFESSIONAL_DIMENSIONS}
    if abs(sum(weights.values()) - 1.0) > 0.01:
        raise ValueError("维度权重总和必须接近 1")
    return normalize_dimension_weights(weights)


def _strict_field_weights(value: Any) -> dict[str, dict[str, float]]:
    if not isinstance(value, dict):
        raise ValueError("候选必须包含 field_weights")
    result: dict[str, dict[str, float]] = {}
    for dimension, fields in PROFESSIONAL_DIMENSION_FIELDS.items():
        raw = value.get(dimension)
        if not isinstance(raw, dict):
            raise ValueError(f"字段权重缺少维度 {dimension}")
        extra = sorted(set(raw) - set(fields))
        missing = [field for field in fields if field not in raw]
        if extra or missing:
            raise ValueError(f"{dimension} 字段权重键不匹配：缺少 {missing or '-'}，多余 {extra or '-'}")
        weights = {field: _strict_positive_number(raw[field], f"field_weights.{dimension}.{field}") for field in fields}
        if abs(sum(weights.values()) - 1.0) > 0.01:
            raise ValueError(f"{dimension} 字段权重总和必须接近 1")
        result[dimension] = normalize_weight_map(weights, fields, PROFESSIONAL_DEFAULT_FIELD_WEIGHTS[dimension])
    return result


def _strict_positive_number(value: Any, label: str) -> float:
    number = to_float(value)
    if number is None or number < 0:
        raise ValueError(f"{label} 必须是非负数字")
    return number


def _candidate_range_issues(
    candidate_dimensions: dict[str, float],
    candidate_fields: dict[str, dict[str, float]],
    active_dimensions: dict[str, float],
    active_fields: dict[str, dict[str, float]],
    ranges: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    dimension_ranges = ranges.get("dimensions") if isinstance(ranges, dict) else {}
    field_ranges = ranges.get("fields") if isinstance(ranges, dict) else {}
    for dimension in PROFESSIONAL_DIMENSIONS:
        delta = abs(candidate_dimensions[dimension] - active_dimensions[dimension])
        if delta > PROFESSIONAL_MAX_DIMENSION_DELTA + 0.000001:
            issues.append(f"{dimension} 单维度变化 {delta:.4f} 超过 {PROFESSIONAL_MAX_DIMENSION_DELTA:.2f}")
        limits = dimension_ranges.get(dimension, {}) if isinstance(dimension_ranges, dict) else {}
        lower = to_float(limits.get("min")) if isinstance(limits, dict) else None
        upper = to_float(limits.get("max")) if isinstance(limits, dict) else None
        if lower is not None and candidate_dimensions[dimension] < lower - 0.000001:
            issues.append(f"{dimension} 低于历史权重范围")
        if upper is not None and candidate_dimensions[dimension] > upper + 0.000001:
            issues.append(f"{dimension} 高于历史权重范围")
        for field_name in PROFESSIONAL_DIMENSION_FIELDS[dimension]:
            field_delta = abs(candidate_fields[dimension][field_name] - active_fields[dimension][field_name])
            if field_delta > PROFESSIONAL_MAX_FIELD_DELTA + 0.000001:
                issues.append(f"{dimension}.{field_name} 单字段变化 {field_delta:.4f} 超过 {PROFESSIONAL_MAX_FIELD_DELTA:.2f}")
            limits = (
                field_ranges.get(dimension, {}).get(field_name, {})
                if isinstance(field_ranges, dict) and isinstance(field_ranges.get(dimension), dict)
                else {}
            )
            lower = to_float(limits.get("min")) if isinstance(limits, dict) else None
            upper = to_float(limits.get("max")) if isinstance(limits, dict) else None
            if lower is not None and candidate_fields[dimension][field_name] < lower - 0.000001:
                issues.append(f"{dimension}.{field_name} 低于历史权重范围")
            if upper is not None and candidate_fields[dimension][field_name] > upper + 0.000001:
                issues.append(f"{dimension}.{field_name} 高于历史权重范围")
    return issues


def _closeness_score(
    dimensions: dict[str, float],
    fields: dict[str, dict[str, float]],
    target_dimensions: dict[str, float],
    target_fields: dict[str, dict[str, float]],
) -> float:
    dimension_distance = sum(abs(dimensions[dimension] - target_dimensions[dimension]) for dimension in PROFESSIONAL_DIMENSIONS)
    field_distance = 0.0
    field_count = 0
    for dimension, dimension_fields in PROFESSIONAL_DIMENSION_FIELDS.items():
        for field_name in dimension_fields:
            field_count += 1
            field_distance += abs(fields[dimension][field_name] - target_fields[dimension][field_name])
    field_distance = field_distance / max(1, field_count)
    return max(0.0, 100.0 - dimension_distance * 60.0 - field_distance * 40.0)
