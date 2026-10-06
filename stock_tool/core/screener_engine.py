from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from stock_tool.core.candidate_lens import candidate_digest, evaluate_candidate_lenses
from stock_tool.core.data_fetcher import DataFetcher
from stock_tool.core.evidence_contracts import DEFAULT_CANDIDATE_LENSES
from stock_tool.core.factor_store import FactorSnapshot
from stock_tool.core.models import FinancialMetrics, MoneyFlowSnapshot, StockQuote
from stock_tool.core.professional_formula import PROFESSIONAL_FIELD_LABELS, build_professional_score_breakdown
from stock_tool.core.screening_contract import (
    build_eligibility_decision,
    required_fields_for_filter,
    stable_hash,
)
from stock_tool.core.selection_policy import SelectionPolicy, SelectionPolicyInput
from stock_tool.core.selection_policy import DEFAULT_SELECTION_POLICY
from stock_tool.core.priority_policy import DEFAULT_PRIORITY_POLICY
from stock_tool.core.technical_profile import DEFAULT_BOX_WINDOW_DAYS, TechnicalProfile, build_technical_profile


ProgressCallback = Callable[[int, int, str], None]

_NON_FORMAL_POLICY_HARD_EXCLUSIONS = frozenset(
    {
        "INVALID_CODE",
        "EXCLUDED_ST",
        "EXCLUDED_DELISTING",
        "EXCLUDED_STOCK_STATUS",
        "FUTURE_AVAILABLE_AT",
        "SUSPENDED_NO_VALID_PRICE",
    }
)


def _selection_policy_input(
    snapshot: FactorSnapshot,
    *,
    selection_inputs_by_code: Mapping[str, SelectionPolicyInput | Mapping[str, Any]] | None,
) -> SelectionPolicyInput:
    """Resolve the explicit formal input; legacy snapshots remain conservative."""

    code = str(snapshot.code or "").strip().zfill(6)
    provided = (selection_inputs_by_code or {}).get(code)
    if provided is None:
        return SelectionPolicyInput.from_mapping(snapshot)
    return SelectionPolicyInput.from_mapping(provided)


@dataclass
class ScreenFilter:
    min_roe: float | None = 8
    min_net_margin: float | None = None
    max_debt_ratio: float | None = 85
    max_pe: float | None = 80
    max_pe_ttm: float | None = None
    max_pb: float | None = None
    min_gross_margin: float | None = None
    min_revenue_growth: float | None = None
    min_profit_growth: float | None = None
    min_market_cap_yi: float | None = None
    max_market_cap_yi: float | None = None
    min_trend_20d: float | None = None
    min_volume_ratio: float | None = None
    max_pullback_pct: float | None = None
    max_max_drawdown_30d: float | None = None
    max_max_drawdown_60d: float | None = None
    min_turnover: float | None = None
    max_turnover: float | None = None
    min_main_5d_yi: float | None = None
    require_ma_bullish: bool = False
    min_rsi: float | None = None
    max_rsi: float | None = None
    require_box_sideways: bool = False
    min_box_score: float | None = None
    max_box_range_pct: float | None = None
    box_window_days: int = DEFAULT_BOX_WINDOW_DAYS
    include_industries: list[str] = field(default_factory=list)
    exclude_industries: list[str] = field(default_factory=list)
    min_industry_strength: float | None = None
    scan_limit: int = 300
    result_limit: int = 80
    allow_missing_pe: bool = True
    with_technical: bool = True
    exclude: list[str] = field(default_factory=list)
    sort: list[tuple[str, str]] = field(default_factory=list)
    strict_min_roe: bool = False
    strict_min_net_margin: bool = False
    strict_max_debt_ratio: bool = False
    strict_max_pe: bool = False
    strict_max_pe_ttm: bool = False
    strict_max_pb: bool = False
    strict_min_gross_margin: bool = False
    strict_min_revenue_growth: bool = False
    strict_min_profit_growth: bool = False
    strict_min_market_cap_yi: bool = False
    strict_max_market_cap_yi: bool = False
    strict_min_trend_20d: bool = False
    strict_min_volume_ratio: bool = False
    strict_max_pullback_pct: bool = False
    strict_max_max_drawdown_30d: bool = False
    strict_max_max_drawdown_60d: bool = False
    strict_min_turnover: bool = False
    strict_max_turnover: bool = False
    strict_min_main_5d_yi: bool = False
    strict_min_rsi: bool = False
    strict_max_rsi: bool = False
    strict_min_box_score: bool = False
    strict_max_box_range_pct: bool = False
    strict_min_industry_strength: bool = False


@dataclass
class ScreenCandidate:
    quote: StockQuote
    metrics: FinancialMetrics
    technical: TechnicalProfile
    local_score: float | None
    reasons: list[str] = field(default_factory=list)
    sort_values: dict[str, float | str | None] = field(default_factory=dict)
    legacy_score: float | None = None
    template_score: float | None = None
    experimental_score: float | None = None
    score_breakdown: dict = field(default_factory=dict)
    data_status: str = "verified"
    missing_fields: list[str] = field(default_factory=list)
    lens_ids: tuple[str, ...] = ()
    eligibility_status: str = "eligible"
    reason_codes: list[str] = field(default_factory=list)
    stale_fields: list[str] = field(default_factory=list)
    data_snapshot_id: str = ""
    candidate_digest: str = ""
    fact_sources: dict[str, str] = field(default_factory=dict)
    lens_evaluations: dict[str, dict[str, Any]] = field(default_factory=dict)
    mode: str = "formal"
    exploratory_only: bool = False
    universe_id: str = ""
    universe_version: str = ""
    board_profile_id: str = ""
    priority_tier: str = ""
    priority_explanation: list[str] = field(default_factory=list)
    selection_policy_version: str = ""


@dataclass
class ScreenReport:
    results: list[ScreenCandidate]
    scanned: int
    skipped: int
    failed: int
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    status: str = "verified"
    missing_fields: list[str] = field(default_factory=list)
    data_source: str = ""
    snapshot_asof: str = ""
    data_snapshot_id: str = ""
    lens_results: dict[str, list[ScreenCandidate]] = field(default_factory=dict)
    candidate_set_digest: str = ""
    mode: str = "formal"
    universe_id: str = ""
    universe_version: str = ""
    selection_policy_version: str = ""


def _professional_formula_weights(
    professional_formula: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(professional_formula, dict):
        return None, None
    dimension_weights = professional_formula.get("dimension_weights")
    field_weights = professional_formula.get("field_weights")
    return (
        dimension_weights if isinstance(dimension_weights, dict) else None,
        field_weights if isinstance(field_weights, dict) else None,
    )


def _numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric


def _professional_field_score(
    professional_breakdown: dict[str, Any],
    dimension: str,
    field_name: str,
) -> dict[str, Any]:
    field_scores = professional_breakdown.get("field_scores")
    if not isinstance(field_scores, dict):
        return {}
    dimension_scores = field_scores.get(dimension)
    if not isinstance(dimension_scores, dict):
        return {}
    item = dimension_scores.get(field_name)
    return item if isinstance(item, dict) else {}


def _selection_grade(score: float, data_quality: float, veto_reasons: list[str]) -> str:
    if data_quality < 60 or "数据质量低" in veto_reasons:
        return "D"
    if "回撤过大" in veto_reasons or "估值极端" in veto_reasons:
        return "C" if score >= 65 else "D"
    if score >= 85 and data_quality >= 90:
        return "S"
    if score >= 75 and data_quality >= 80:
        return "A"
    if score >= 60:
        return "B"
    if score >= 45:
        return "C"
    return "D"


def _selection_state(grade: str, data_quality: float, veto_reasons: list[str]) -> str:
    if data_quality < 80:
        return "数据待补"
    if "回撤过大" in veto_reasons or "估值极端" in veto_reasons:
        return "风险回避"
    if grade in {"S", "A"} and not veto_reasons:
        return "优先观察"
    return "普通观察"


def _selection_profile(
    professional_breakdown: dict[str, Any],
    veto_reasons: list[str],
) -> str:
    dimension_scores = professional_breakdown.get("dimension_scores")
    scores = dimension_scores if isinstance(dimension_scores, dict) else {}
    score = lambda name: _numeric(scores.get(name)) or 0.0
    if "回撤过大" in veto_reasons or "估值极端" in veto_reasons:
        return "风险回避"
    if score("money_flow") >= 70:
        return "资金确认"
    if score("industry") >= 70 and score("industry") >= max(score("quality"), score("growth"), score("trend")):
        return "行业轮动"
    if score("growth") >= 68 and score("quality") >= 50:
        return "成长改善"
    if score("quality") >= 68 and score("valuation") >= 50:
        return "质量价值"
    if score("trend") >= 70:
        return "趋势突破"
    if score("safety") >= 70:
        return "风险回避"
    top_dimension = max(scores, key=lambda name: score(str(name)), default="")
    return {
        "quality": "质量价值",
        "growth": "成长改善",
        "trend": "趋势突破",
        "industry": "行业轮动",
        "money_flow": "资金确认",
        "safety": "风险回避",
    }.get(str(top_dimension), "质量价值")


def build_selection_contract(professional_breakdown: dict[str, Any]) -> dict[str, Any]:
    missing_fields = [str(item) for item in professional_breakdown.get("missing_fields") or []]
    data_quality = _numeric(professional_breakdown.get("data_quality_score")) or 0.0
    score = _numeric(professional_breakdown.get("score")) or 0.0
    dimension_weights = professional_breakdown.get("dimension_weights")
    if not isinstance(dimension_weights, dict):
        dimension_weights = {}
    veto_reasons: list[str] = []
    # Professional fields outside an active formula dimension are evidence
    # gaps, not eligibility vetoes.  Required-field exclusion is applied below
    # from the executable ScreenFilter; hard risk vetoes remain non-compensable.
    if "main_5d_yi" in missing_fields and float(dimension_weights.get("money_flow") or 0.0) > 0:
        veto_reasons.append("资金缺失")
    if "industry_strength" in missing_fields and float(dimension_weights.get("industry") or 0.0) > 0:
        veto_reasons.append("行业缺失")
    if data_quality < 80:
        veto_reasons.append("数据质量低")

    drawdown = _professional_field_score(professional_breakdown, "safety", "max_drawdown")
    drawdown_value = _numeric(drawdown.get("value"))
    drawdown_score = _numeric(drawdown.get("score"))
    if (drawdown_value is not None and drawdown_value <= -20.0) or (drawdown_score is not None and drawdown_score < 35.0):
        veto_reasons.append("回撤过大")

    valuation_score = _numeric((professional_breakdown.get("dimension_scores") or {}).get("valuation"))
    pe_ttm = _numeric(_professional_field_score(professional_breakdown, "valuation", "pe_ttm").get("value"))
    pb = _numeric(_professional_field_score(professional_breakdown, "valuation", "pb").get("value"))
    if (pe_ttm is not None and pe_ttm >= 80.0) or (pb is not None and pb >= 6.0) or (valuation_score is not None and valuation_score < 25.0):
        veto_reasons.append("估值极端")

    veto_reasons = list(dict.fromkeys(veto_reasons))
    grade = _selection_grade(score, data_quality, veto_reasons)
    profile = _selection_profile(professional_breakdown, veto_reasons)
    state = _selection_state(grade, data_quality, veto_reasons)
    evidence_gaps = [
        {
            "field": field_name,
            "label": PROFESSIONAL_FIELD_LABELS.get(field_name, field_name),
            "reason": "missing",
        }
        for field_name in missing_fields
    ]
    return {
        "selection_profile": profile,
        "selection_grade": grade,
        "selection_state": state,
        "veto_reasons": veto_reasons,
        "evidence_gaps": evidence_gaps,
        "eligibility_status": "eligible" if not veto_reasons else "ineligible",
        "required_fields": [],
        "missing_required_fields": [],
        "data_quality_status": "verified" if data_quality >= 80 else "partial",
    }


def _ok_min(value: float | None, threshold: float | None, strict: bool = False, allow_missing: bool = False) -> bool:
    if threshold is None:
        return True
    if value is None:
        return allow_missing
    return value > threshold if strict else value >= threshold


def _ok_max(value: float | None, threshold: float | None, allow_missing: bool = False, strict: bool = False) -> bool:
    if threshold is None:
        return True
    if value is None:
        return allow_missing
    return value < threshold if strict else value <= threshold


def _passes_exclude(quote: StockQuote, exclude: list[str]) -> bool:
    code = (quote.code or "").upper()
    name = (quote.name or "").upper()
    for item in exclude:
        token = str(item or "").strip().upper()
        if not token:
            continue
        if token == "ST":
            if name.startswith(("ST", "*ST")):
                return False
            continue
        if token in code or token in name:
            return False
    return True


def passes_filter(
    quote: StockQuote,
    metrics: FinancialMetrics,
    filters: ScreenFilter,
    *,
    allow_missing_required: bool = False,
) -> bool:
    if not _passes_exclude(quote, filters.exclude):
        return False
    market_cap_yi = quote.market_cap / 100000000 if quote.market_cap else None
    return all(
        [
            _ok_min(metrics.roe, filters.min_roe, strict=filters.strict_min_roe, allow_missing=allow_missing_required),
            _ok_min(metrics.net_margin, filters.min_net_margin, strict=filters.strict_min_net_margin, allow_missing=allow_missing_required),
            _ok_min(metrics.gross_margin, filters.min_gross_margin, strict=filters.strict_min_gross_margin, allow_missing=allow_missing_required),
            _ok_max(metrics.debt_ratio, filters.max_debt_ratio, strict=filters.strict_max_debt_ratio, allow_missing=allow_missing_required),
            _ok_max(quote.pe, filters.max_pe, allow_missing=filters.allow_missing_pe or allow_missing_required, strict=filters.strict_max_pe),
            _ok_max(quote.pe_ttm, filters.max_pe_ttm, allow_missing=filters.allow_missing_pe or allow_missing_required, strict=filters.strict_max_pe_ttm),
            _ok_max(quote.pb, filters.max_pb, strict=filters.strict_max_pb, allow_missing=allow_missing_required),
            _ok_min(metrics.revenue_growth, filters.min_revenue_growth, strict=filters.strict_min_revenue_growth, allow_missing=allow_missing_required),
            _ok_min(metrics.profit_growth, filters.min_profit_growth, strict=filters.strict_min_profit_growth, allow_missing=allow_missing_required),
            _ok_min(market_cap_yi, filters.min_market_cap_yi, strict=filters.strict_min_market_cap_yi, allow_missing=allow_missing_required),
            _ok_max(market_cap_yi, filters.max_market_cap_yi, strict=filters.strict_max_market_cap_yi, allow_missing=allow_missing_required),
        ]
    )


def _trend_20d_from_technical(technical: TechnicalProfile) -> float | None:
    if technical.close is None or technical.ma20 in (None, 0):
        return None
    return round((technical.close - technical.ma20) / technical.ma20 * 100, 2)


def _passes_trend_20d(value: float | None, filters: ScreenFilter) -> bool:
    threshold = filters.min_trend_20d
    if threshold is None:
        return True
    if value is None:
        return False
    return value > threshold if filters.strict_min_trend_20d else value >= threshold


def requires_technical_filter(filters: ScreenFilter) -> bool:
    if any(
        field_name in {"rsi", "max_drawdown_30d", "max_drawdown_60d", "box_score", "box_range_pct", "box_position_pct"}
        for field_name, _direction in filters.sort
    ):
        return True
    return filters.require_ma_bullish or any(
        value is not None
        for value in (
            filters.min_trend_20d,
            filters.min_volume_ratio,
            filters.max_pullback_pct,
            filters.max_max_drawdown_30d,
            filters.max_max_drawdown_60d,
            filters.min_turnover,
            filters.max_turnover,
            filters.min_rsi,
            filters.max_rsi,
            filters.min_box_score,
            filters.max_box_range_pct,
        )
    ) or filters.require_box_sideways


def requires_money_flow_filter(filters: ScreenFilter) -> bool:
    return filters.min_main_5d_yi is not None


def requires_industry_filter(filters: ScreenFilter) -> bool:
    return bool(filters.include_industries or filters.exclude_industries or filters.min_industry_strength is not None)


def requires_industry_strength_filter(filters: ScreenFilter) -> bool:
    return filters.min_industry_strength is not None


def requires_realtime_screening(filters: ScreenFilter) -> bool:
    if any(field_name in {"rsi", "max_drawdown_30d", "max_drawdown_60d"} for field_name, _direction in filters.sort):
        return True
    if filters.box_window_days != DEFAULT_BOX_WINDOW_DAYS and (
        filters.require_box_sideways or filters.min_box_score is not None or filters.max_box_range_pct is not None
    ):
        return True
    return filters.require_ma_bullish or any(
        value is not None
        for value in (
            filters.min_volume_ratio,
            filters.max_pullback_pct,
            filters.max_max_drawdown_30d,
            filters.max_max_drawdown_60d,
            filters.min_turnover,
            filters.max_turnover,
            filters.min_main_5d_yi,
            filters.min_rsi,
            filters.max_rsi,
        )
    )


def passes_technical_filter(technical: TechnicalProfile, quote: StockQuote, filters: ScreenFilter) -> bool:
    if not _passes_trend_20d(_trend_20d_from_technical(technical), filters):
        return False
    return passes_non_trend_technical_filter(technical, quote, filters)


def passes_non_trend_technical_filter(technical: TechnicalProfile, quote: StockQuote, filters: ScreenFilter) -> bool:
    if filters.require_ma_bullish and not (
        technical.ma5 is not None
        and technical.ma20 is not None
        and technical.ma60 is not None
        and technical.ma5 > technical.ma20 > technical.ma60
    ):
        return False
    if not _ok_min(technical.volume_ratio, filters.min_volume_ratio, strict=filters.strict_min_volume_ratio):
        return False
    if not _ok_max(technical.pullback_pct, filters.max_pullback_pct, strict=filters.strict_max_pullback_pct):
        return False
    if not _ok_max(technical.max_drawdown_30d, filters.max_max_drawdown_30d, strict=filters.strict_max_max_drawdown_30d):
        return False
    if not _ok_max(technical.max_drawdown_60d, filters.max_max_drawdown_60d, strict=filters.strict_max_max_drawdown_60d):
        return False
    if not _ok_min(quote.turnover, filters.min_turnover, strict=filters.strict_min_turnover):
        return False
    if not _ok_max(quote.turnover, filters.max_turnover, strict=filters.strict_max_turnover):
        return False
    if not _ok_min(technical.rsi14, filters.min_rsi, strict=filters.strict_min_rsi):
        return False
    if not _ok_max(technical.rsi14, filters.max_rsi, strict=filters.strict_max_rsi):
        return False
    if filters.require_box_sideways and not technical.box_is_sideways:
        return False
    if not _ok_min(technical.box_score, filters.min_box_score, strict=filters.strict_min_box_score):
        return False
    if not _ok_max(technical.box_range_pct, filters.max_box_range_pct, strict=filters.strict_max_box_range_pct):
        return False
    return True


def _industry_match(industry: str, token: str) -> bool:
    token = str(token or "").strip().lower()
    if not token:
        return False
    return token in str(industry or "").strip().lower()


def passes_industry_filter(quote: StockQuote, filters: ScreenFilter) -> bool:
    industry = getattr(quote, "industry", "") or ""
    if filters.include_industries and not any(_industry_match(industry, item) for item in filters.include_industries):
        return False
    if filters.exclude_industries and any(_industry_match(industry, item) for item in filters.exclude_industries):
        return False
    if not _ok_min(
        getattr(quote, "industry_strength", None),
        filters.min_industry_strength,
        strict=filters.strict_min_industry_strength,
    ):
        return False
    return True


def _money_flow_yi(value: float | None) -> float | None:
    return round(value / 100000000, 4) if value is not None else None


def passes_money_flow_filter(money_flow: MoneyFlowSnapshot | None, filters: ScreenFilter) -> bool:
    threshold = filters.min_main_5d_yi
    if threshold is None:
        return True
    value = _money_flow_yi(money_flow.main_5d) if money_flow else None
    if value is None:
        return True
    return value > threshold if filters.strict_min_main_5d_yi else value >= threshold


def _add_note_once(notes: list[str], note: str) -> None:
    if note and note not in notes:
        notes.append(note)


def _score(quote: StockQuote, metrics: FinancialMetrics, technical: TechnicalProfile) -> tuple[float, list[str]]:
    score = 50.0
    reasons: list[str] = []

    if metrics.roe is not None:
        score += min(metrics.roe, 30) * 0.8
        if metrics.roe >= 15:
            reasons.append("ROE较好")
    if metrics.net_margin is not None:
        score += min(max(metrics.net_margin, -10), 30) * 0.35
        if metrics.net_margin >= 10:
            reasons.append("净利率健康")
    if metrics.gross_margin is not None:
        score += min(max(metrics.gross_margin, 0), 60) * 0.12
    if metrics.revenue_growth is not None:
        score += min(max(metrics.revenue_growth, -30), 50) * 0.2
    if metrics.profit_growth is not None:
        score += min(max(metrics.profit_growth, -30), 50) * 0.2
    if metrics.debt_ratio is not None:
        if metrics.debt_ratio <= 50:
            score += 8
            reasons.append("负债率较稳")
        elif metrics.debt_ratio >= 80:
            score -= 10
            reasons.append("负债率偏高")
    if quote.pe is not None:
        if 0 < quote.pe <= 30:
            score += 8
            reasons.append("PE较温和")
        elif quote.pe > 80:
            score -= 8

    label = technical.opportunity_label if technical else ""
    if label.startswith("S"):
        score += 12
        reasons.append(label)
    elif label.startswith("A"):
        score += 9
        reasons.append(label)
    elif label.startswith("B"):
        score += 4
        reasons.append(label)
    elif label.startswith("D"):
        score -= 12
        reasons.append(label)

    if technical.box_score is not None:
        score += min(max(technical.box_score, 0), 100) * 0.08
        if technical.box_score >= 60:
            reasons.append(f"箱体评分{technical.box_score:.0f}")
    industry_strength = getattr(quote, "industry_strength", None)
    if industry_strength is not None:
        score += min(max(industry_strength, 0), 100) * 0.05
        industry = getattr(quote, "industry", "") or "行业"
        if industry_strength >= 60:
            reasons.append(f"{industry}强度{industry_strength:.0f}")

    return round(max(0, min(score, 100)), 1), reasons[:5]


def build_scored_candidate(
    quote: StockQuote,
    metrics: FinancialMetrics,
    technical: TechnicalProfile,
    *,
    extra_fields: dict[str, Any] | None = None,
    sort_values: dict[str, float | str | None] | None = None,
    score_breakdown: dict[str, Any] | None = None,
    dimension_weights: dict[str, Any] | None = None,
    field_weights: dict[str, Any] | None = None,
    reasons: list[str] | None = None,
    template_score: float | None = None,
    experimental_score: float | None = None,
    shadow_formula: dict[str, Any] | None = None,
) -> ScreenCandidate:
    legacy_score, legacy_reasons = _score(quote, metrics, technical)
    professional_breakdown = build_professional_score_breakdown(
        quote,
        metrics,
        technical,
        extra_fields=extra_fields,
        dimension_weights=dimension_weights,
        field_weights=field_weights,
    )
    breakdown = dict(score_breakdown or {})
    breakdown["professional_formula"] = professional_breakdown
    breakdown["legacy_score"] = legacy_score
    breakdown["missing_fields"] = list(professional_breakdown.get("missing_fields") or [])
    selection_contract = build_selection_contract(professional_breakdown)
    breakdown.update(selection_contract)
    primary_score = float(professional_breakdown.get("score") or 0.0)
    if isinstance(shadow_formula, dict):
        shadow_dimension_weights, shadow_field_weights = _professional_formula_weights(shadow_formula)
        shadow_breakdown = build_professional_score_breakdown(
            quote,
            metrics,
            technical,
            extra_fields=extra_fields,
            dimension_weights=shadow_dimension_weights,
            field_weights=shadow_field_weights,
        )
        breakdown["shadow_formula"] = {
            "id": shadow_formula.get("id"),
            "version": shadow_formula.get("version", ""),
            "score": shadow_breakdown.get("score"),
            "dimension_weights": shadow_breakdown.get("dimension_weights", {}),
            "field_weights": shadow_breakdown.get("field_weights", {}),
            "missing_fields": list(shadow_breakdown.get("missing_fields") or []),
        }
        experimental_score = float(shadow_breakdown.get("score") or 0.0)
    explanation_reasons = [str(item) for item in professional_breakdown.get("explanations", []) if str(item).strip()]
    candidate_reasons = list(reasons or [])
    candidate_reasons.extend(explanation_reasons)
    if legacy_reasons:
        candidate_reasons.append("旧分参考：" + "、".join(legacy_reasons[:2]))
    return ScreenCandidate(
        quote=quote,
        metrics=metrics,
        technical=technical,
        local_score=round(primary_score, 4),
        reasons=candidate_reasons[:5],
        sort_values=sort_values or {},
        legacy_score=legacy_score,
        template_score=template_score,
        experimental_score=experimental_score,
        score_breakdown=breakdown,
        data_status="partial" if breakdown["missing_fields"] else "verified",
        missing_fields=list(breakdown["missing_fields"]),
    )


def _candidate_sort_value(candidate: ScreenCandidate, field_name: str) -> float | str | None:
    if field_name in candidate.sort_values:
        return candidate.sort_values[field_name]
    quote = candidate.quote
    metrics = candidate.metrics
    if field_name == "price":
        return quote.price
    if field_name == "pe":
        return quote.pe
    if field_name == "pe_ttm":
        return quote.pe_ttm
    if field_name == "pb":
        return quote.pb
    if field_name == "market_cap_yi":
        return quote.market_cap / 100000000 if quote.market_cap is not None else None
    if field_name == "roe":
        return metrics.roe
    if field_name == "net_margin":
        return metrics.net_margin
    if field_name == "debt_ratio":
        return metrics.debt_ratio
    if field_name == "gross_margin":
        return metrics.gross_margin
    if field_name == "revenue_growth":
        return metrics.revenue_growth
    if field_name == "profit_growth":
        return metrics.profit_growth
    if field_name == "trend_20d":
        tech = candidate.technical
        return _trend_20d_from_technical(tech)
    if field_name == "volume_ratio":
        return candidate.technical.volume_ratio
    if field_name == "pullback_pct":
        return candidate.technical.pullback_pct
    if field_name == "max_drawdown_30d":
        return candidate.technical.max_drawdown_30d
    if field_name == "max_drawdown_60d":
        return candidate.technical.max_drawdown_60d
    if field_name == "rsi":
        return candidate.technical.rsi14
    if field_name == "box_score":
        return candidate.technical.box_score
    if field_name == "box_range_pct":
        return candidate.technical.box_range_pct
    if field_name == "box_position_pct":
        return candidate.technical.box_position_pct
    if field_name == "industry_strength":
        return getattr(quote, "industry_strength", None)
    if field_name == "turnover":
        return quote.turnover
    if field_name == "main_5d_yi":
        return candidate.sort_values.get("main_5d_yi")
    if field_name == "local_score":
        return candidate.local_score
    return None


def _sort_results(results: list[ScreenCandidate], filters: ScreenFilter) -> None:
    if not filters.sort:
        results.sort(key=lambda item: item.local_score, reverse=True)
        return

    for field_name, direction in reversed(filters.sort):
        descending = direction == "desc"

        def key(item: ScreenCandidate):
            value = _candidate_sort_value(item, field_name)
            missing_rank = 1 if value is None else 0
            if isinstance(value, (int, float)):
                value_key = -float(value) if descending else float(value)
            else:
                value_key = value if value is not None else ""
            return missing_rank, value_key

        results.sort(key=key)


def _candidate_pool(quotes: list[StockQuote], limit: int) -> list[StockQuote]:
    items = [
        quote for quote in quotes
        if quote.code
        and quote.name
        and not quote.name.upper().startswith(("退", "ST", "*ST"))
    ]
    items.sort(key=lambda item: item.market_cap or 0, reverse=True)
    return items[:limit]


def _value_from_industry_info(info: Any, attr: str) -> Any:
    if info is None:
        return None
    if isinstance(info, dict):
        return info.get(attr)
    if attr == "industry_strength" and hasattr(info, "strength"):
        return getattr(info, "strength")
    return getattr(info, attr, None)


def _apply_industry_info(quote: StockQuote, info: Any) -> None:
    if info is None:
        return
    industry = _value_from_industry_info(info, "industry")
    if industry:
        quote.industry = str(industry)
    strength = _value_from_industry_info(info, "industry_strength")
    if strength is None:
        strength = _value_from_industry_info(info, "strength")
    if strength is not None:
        quote.industry_strength = strength
    rank = _value_from_industry_info(info, "industry_rank")
    if rank is None:
        rank = _value_from_industry_info(info, "rank")
    if rank is not None:
        quote.industry_rank = int(rank)
    sample_count = _value_from_industry_info(info, "industry_sample_count")
    if sample_count is None:
        sample_count = _value_from_industry_info(info, "sample_count")
    if sample_count is not None:
        quote.industry_sample_count = int(sample_count)
    updated_at = _value_from_industry_info(info, "industry_updated_at")
    if updated_at is None:
        updated_at = _value_from_industry_info(info, "updated_at")
    if updated_at:
        quote.industry_updated_at = str(updated_at)


def _industry_unavailable_error() -> str:
    return "行业强弱数据不可用：请先在“更新筛选数据”或 PIT 数据更新中生成行业和价格数据，再执行行业强度筛选。"


def screen_market(
    fetcher: DataFetcher,
    filters: ScreenFilter,
    progress: ProgressCallback | None = None,
    industry_by_code: dict[str, Any] | None = None,
    professional_formula: dict[str, Any] | None = None,
) -> ScreenReport:
    quotes, _ = fetcher.spot_quotes()
    candidates = _candidate_pool(quotes, max(1, filters.scan_limit))
    results: list[ScreenCandidate] = []
    errors: list[str] = []
    notes: list[str] = []
    failed = 0
    skipped = 0
    total = len(candidates)
    if requires_industry_strength_filter(filters) and not industry_by_code:
        return ScreenReport(
            results=[],
            scanned=0,
            skipped=0,
            failed=0,
            errors=[_industry_unavailable_error()],
            notes=[],
            status="blocked",
            missing_fields=["industry_strength"],
        )
    flow_map: dict[str, float] = {}
    money_flow_available = False
    if requires_money_flow_filter(filters):
        try:
            if progress:
                progress(0, total, "正在获取全市场资金流...")
            flow_map = fetcher.market_money_flow()
            money_flow_available = True
            source_note = str(getattr(fetcher, "last_market_money_flow_note", "") or "")
            _add_note_once(notes, source_note or "主力进场使用公开批量资金流，当前已获取；无需 Tushare token 或 AI token。")
            if progress:
                progress(0, total, "资金流就绪，开始筛选")
        except Exception:
            source_note = str(getattr(fetcher, "last_market_money_flow_note", "") or "")
            _add_note_once(notes, source_note or "资金数据暂不可用，可能是公开接口限流或网络波动；已忽略主力进场条件，其余条件照常，无需配置 Tushare token。")

    for idx, quote in enumerate(candidates, start=1):
        if progress:
            progress(idx, total, f"正在筛选 {quote.name} {quote.code}")
        try:
            if industry_by_code:
                _apply_industry_info(quote, industry_by_code.get(quote.code.zfill(6)))
            if not passes_industry_filter(quote, filters):
                skipped += 1
                continue
            metrics, _ = fetcher.financial_metrics(quote.code)
            if not passes_filter(quote, metrics, filters):
                skipped += 1
                continue
            technical = TechnicalProfile(False, summary="未计算技术画像")
            if filters.with_technical or requires_technical_filter(filters):
                try:
                    history, _ = fetcher.history(quote.code, days=180)
                    technical = build_technical_profile(history, box_window_days=filters.box_window_days)
                except Exception as exc:
                    technical = TechnicalProfile(False, summary=f"技术画像失败：{exc}")
            if not passes_technical_filter(technical, quote, filters):
                skipped += 1
                continue
            main_5d_yi = None
            money_flow_missing = False
            if requires_money_flow_filter(filters) and money_flow_available:
                main_5d_raw = flow_map.get(quote.code.zfill(6))
                main_5d_yi = _money_flow_yi(main_5d_raw)
                money_flow_missing = main_5d_raw is None
                if money_flow_missing:
                    skipped += 1
                    continue
                money_flow = MoneyFlowSnapshot(code=quote.code, rows=[], main_5d=main_5d_raw) if main_5d_raw is not None else None
                if not passes_money_flow_filter(money_flow, filters):
                    skipped += 1
                    continue
            reasons: list[str] = []
            if main_5d_yi is not None:
                reasons.append(f"近5日主力{main_5d_yi:+.2f}亿")
            elif money_flow_missing:
                reasons.append("资金数据缺失")
            results.append(
                build_scored_candidate(
                    quote,
                    metrics,
                    technical,
                    extra_fields={"main_5d_yi": main_5d_yi},
                    reasons=reasons,
                    sort_values={
                        "trend_20d": _trend_20d_from_technical(technical),
                        "volume_ratio": technical.volume_ratio,
                        "pullback_pct": technical.pullback_pct,
                        "max_drawdown_30d": technical.max_drawdown_30d,
                        "max_drawdown_60d": technical.max_drawdown_60d,
                        "rsi": technical.rsi14,
                        "box_score": technical.box_score,
                        "box_range_pct": technical.box_range_pct,
                        "box_position_pct": technical.box_position_pct,
                        "industry_strength": getattr(quote, "industry_strength", None),
                        "turnover": quote.turnover,
                        "main_5d_yi": main_5d_yi,
                    },
                    shadow_formula=professional_formula,
                )
            )
        except Exception as exc:
            failed += 1
            if len(errors) < 8:
                errors.append(f"{quote.code} {quote.name}: {exc}")

    _sort_results(results, filters)
    return ScreenReport(
        results=results[: max(1, filters.result_limit)],
        scanned=total,
        skipped=skipped,
        failed=failed,
        errors=errors,
        notes=notes,
        status=("verified" if money_flow_available or not requires_money_flow_filter(filters) else "unverified"),
        missing_fields=[] if money_flow_available or not requires_money_flow_filter(filters) else ["main_5d_yi"],
    )


def _field_markers(snapshot: Any, name: str) -> set[str]:
    value = getattr(snapshot, name, "")
    if isinstance(value, str):
        return {str(item).strip() for item in value.split(",") if str(item).strip()}
    if isinstance(value, Iterable):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _active_missing_fields(snapshot: FactorSnapshot, filters: ScreenFilter) -> list[str]:
    markers = {str(item).strip() for item in (snapshot.missing_fields or "").split(",") if str(item).strip()}
    missing: list[str] = []
    for field_name in required_fields_for_filter(filters):
        if field_name == "main_5d_yi":
            missing.append(field_name)
            continue
        value = getattr(snapshot, field_name, None)
        if value is None or field_name in markers:
            missing.append(field_name)
    return list(dict.fromkeys(missing))


def _candidate_fact_sources(
    snapshot: FactorSnapshot,
    *,
    fact_lineage_by_code: Mapping[str, Mapping[str, Mapping[str, Any]]] | None,
    data_source: str,
) -> dict[str, str]:
    code = str(snapshot.code or "").zfill(6)
    lineage = (fact_lineage_by_code or {}).get(code, {})
    sources: dict[str, str] = {}
    for field_name, metadata in lineage.items():
        source = str(metadata.get("source") or data_source or "").strip()
        version = str(metadata.get("source_version") or "").strip()
        route = str(metadata.get("source_route") or "primary").strip()
        if version:
            source = f"{source}@{version}"
        if route and route != "primary":
            source = f"{source}[{route}]"
        if source:
            sources[str(field_name)] = source
    if not sources and data_source:
        sources["snapshot"] = str(data_source)
    return dict(sorted(sources.items()))


def _deterministic_candidate(
    snapshot: FactorSnapshot,
    *,
    filters: ScreenFilter,
    quote: StockQuote,
    metrics: FinancialMetrics,
    technical: TechnicalProfile,
    data_snapshot_id: str,
    availability_cutoff: str,
    data_quality_status: str,
    data_source: str,
    lens_ids: Iterable[str] | None,
    fact_lineage_by_code: Mapping[str, Mapping[str, Mapping[str, Any]]] | None,
    lens_values_by_code: Mapping[str, Mapping[str, Mapping[str, Any]]] | None,
) -> ScreenCandidate:
    code = str(snapshot.code or "").zfill(6)
    values_by_lens = (lens_values_by_code or {}).get(code, {})
    evaluations = evaluate_candidate_lenses(
        snapshot,
        lens_ids=lens_ids,
        data_quality_status=data_quality_status,
        values_by_lens=values_by_lens,
        availability_cutoff=availability_cutoff,
    )
    active_missing = _active_missing_fields(snapshot, filters)
    stale_fields = sorted(_field_markers(snapshot, "stale_fields"))
    sort_values: dict[str, float | str | None] = {
        "price": quote.price,
        "pe": quote.pe,
        "pe_ttm": quote.pe_ttm,
        "pb": quote.pb,
        "market_cap_yi": snapshot.market_cap_yi,
        "roe": metrics.roe,
        "net_margin": metrics.net_margin,
        "debt_ratio": metrics.debt_ratio,
        "revenue_growth": metrics.revenue_growth,
        "profit_growth": metrics.profit_growth,
        "trend_20d": snapshot.trend_20d,
        "trend_60d": snapshot.trend_60d,
        "volume_ratio": technical.volume_ratio,
        "pullback_pct": technical.pullback_pct,
        "box_score": technical.box_score,
        "box_range_pct": technical.box_range_pct,
        "industry_strength": quote.industry_strength,
        "event_date": values_by_lens.get("event_verification", {}).get("event_date"),
        "event_available_at": values_by_lens.get("event_verification", {}).get("available_at"),
    }
    matched = tuple(item.lens_id for item in evaluations if item.status == "matched")
    all_missing = list(active_missing)
    all_stale = list(stale_fields)
    reason_codes: list[str] = ["ACTIVE_FILTERS_PASSED"]
    lens_evaluations: dict[str, dict[str, Any]] = {}
    for evaluation in evaluations:
        lens_evaluations[evaluation.lens_id] = evaluation.as_dict()
        reason_codes.extend(evaluation.reason_codes)
        all_missing.extend(evaluation.missing_fields)
        all_stale.extend(evaluation.stale_fields)
    all_missing = list(dict.fromkeys(all_missing))
    all_stale = list(dict.fromkeys(all_stale))
    if active_missing:
        reason_codes.extend(f"MISSING_{str(item).upper()}" for item in active_missing)
    if stale_fields:
        reason_codes.extend(f"STALE_{str(item).upper()}" for item in stale_fields)
    if not matched:
        if any(item.status == "insufficient" for item in evaluations):
            eligibility_status = "insufficient"
            reason_codes.append("CANDIDATE_INSUFFICIENT")
        else:
            eligibility_status = "excluded"
            reason_codes.append("NO_LENS_MATCH")
    elif active_missing or stale_fields:
        eligibility_status = "insufficient"
        reason_codes.append("CANDIDATE_INSUFFICIENT")
    else:
        eligibility_status = "eligible"
        reason_codes.append("CANDIDATE_ELIGIBLE")
    reason_codes = list(dict.fromkeys(reason_codes))
    fact_sources = _candidate_fact_sources(
        snapshot,
        fact_lineage_by_code=fact_lineage_by_code,
        data_source=data_source,
    )
    digest = candidate_digest(
        snapshot=snapshot,
        data_snapshot_id=data_snapshot_id,
        lens_ids=matched,
        eligibility_status=eligibility_status,
        reason_codes=reason_codes,
        missing_fields=all_missing,
        stale_fields=all_stale,
        fact_sources=fact_sources,
        sort_values=sort_values,
        lens_versions={item.lens_id: item.lens_version for item in evaluations},
    )
    human_reasons = []
    if matched:
        names = {
            lens.lens_id: lens.name
            for lens in DEFAULT_CANDIDATE_LENSES
        }
        human_reasons.append("候选 Lens：" + "、".join(names.get(item, item) for item in matched))
    else:
        human_reasons.append("暂无满足条件的候选 Lens")
    if all_missing:
        human_reasons.append("数据缺口：" + "、".join(all_missing[:5]))
    if all_stale:
        human_reasons.append("过期字段：" + "、".join(all_stale[:5]))
    if fact_sources:
        human_reasons.append("事实来源：" + "、".join(sorted(set(fact_sources.values()))[:3]))
    return ScreenCandidate(
        quote=quote,
        metrics=metrics,
        technical=technical,
        local_score=None,
        reasons=human_reasons[:5],
        sort_values=sort_values,
        score_breakdown={
            "lens_ids": list(matched),
            "eligibility_status": eligibility_status,
            "reason_codes": reason_codes,
            "missing_fields": all_missing,
            "stale_fields": all_stale,
            "data_snapshot_id": data_snapshot_id,
            "candidate_digest": digest,
            "fact_sources": fact_sources,
            "candidate_contract": {
                "lens_ids": list(matched),
                "eligibility_status": eligibility_status,
                "reason_codes": reason_codes,
                "missing_fields": all_missing,
                "stale_fields": all_stale,
                "data_snapshot_id": data_snapshot_id,
                "candidate_digest": digest,
                "fact_sources": fact_sources,
            },
            "lens_evaluations": lens_evaluations,
        },
        data_status=(
            "partial"
            if all_missing or all_stale or any(item.status != "matched" for item in evaluations)
            else "verified"
        ),
        missing_fields=all_missing,
        lens_ids=matched,
        eligibility_status=eligibility_status,
        reason_codes=reason_codes,
        stale_fields=all_stale,
        data_snapshot_id=data_snapshot_id,
        candidate_digest=digest,
        fact_sources=fact_sources,
        lens_evaluations=lens_evaluations,
    )


def _refresh_candidate_digest(candidate: ScreenCandidate, snapshot: FactorSnapshot) -> None:
    """Keep the candidate digest aligned with post-policy evidence changes."""

    lens_versions = {
        str(lens_id): str(details.get("lens_version") or "")
        for lens_id, details in (candidate.lens_evaluations or {}).items()
        if isinstance(details, Mapping)
    }
    digest = candidate_digest(
        snapshot=snapshot,
        data_snapshot_id=candidate.data_snapshot_id,
        lens_ids=candidate.lens_ids,
        eligibility_status=candidate.eligibility_status,
        reason_codes=candidate.reason_codes,
        missing_fields=candidate.missing_fields,
        stale_fields=candidate.stale_fields,
        fact_sources=candidate.fact_sources,
        sort_values=candidate.sort_values,
        lens_versions=lens_versions,
    )
    candidate.candidate_digest = digest
    candidate.score_breakdown.update(
        {
            "lens_ids": list(candidate.lens_ids),
            "eligibility_status": candidate.eligibility_status,
            "reason_codes": list(candidate.reason_codes),
            "missing_fields": list(candidate.missing_fields),
            "stale_fields": list(candidate.stale_fields),
            "candidate_digest": digest,
        }
    )
    contract = candidate.score_breakdown.get("candidate_contract")
    if isinstance(contract, dict):
        contract.update(
            {
                "lens_ids": list(candidate.lens_ids),
                "eligibility_status": candidate.eligibility_status,
                "reason_codes": list(candidate.reason_codes),
                "missing_fields": list(candidate.missing_fields),
                "stale_fields": list(candidate.stale_fields),
                "data_snapshot_id": candidate.data_snapshot_id,
                "candidate_digest": digest,
                "fact_sources": dict(candidate.fact_sources),
            }
        )


def _candidate_priority_record(candidate: ScreenCandidate) -> dict[str, Any]:
    """Adapt a candidate to the PriorityPolicy's objective-field contract."""

    values = dict(getattr(candidate, "sort_values", {}) or {})
    metrics = getattr(candidate, "metrics", None)
    technical = getattr(candidate, "technical", None)
    aliases = {
        "roe_ttm": values.get("roe", getattr(metrics, "roe", None)),
        "net_margin_ttm": values.get("net_margin", getattr(metrics, "net_margin", None)),
        "relative_strength_20d": values.get("relative_strength_20d", values.get("trend_20d")),
        "trend_20d": values.get("trend_20d", getattr(candidate, "trend_20d", None)),
        "trend_60d": values.get("trend_60d", getattr(candidate, "trend_60d", None)),
        "median_turnover_value_20d": values.get("median_turnover_value_20d", values.get("turnover")),
        "volatility": values.get("volatility", getattr(technical, "volatility", None)),
        "max_drawdown_30d": values.get("max_drawdown_30d", getattr(technical, "max_drawdown_30d", None)),
        "event_age_days": values.get("event_age_days"),
        "event_date": values.get("event_date"),
        "source_tier": values.get("source_tier"),
        "verification_gap_count": values.get("verification_gap_count"),
        "event_type_priority": values.get("event_type_priority"),
        "debt_sector_percentile": values.get("debt_sector_percentile"),
        "report_freshness": values.get("report_freshness"),
    }
    values.update({key: value for key, value in aliases.items() if value is not None})
    status = getattr(candidate, "eligibility_status", "eligible") or "eligible"
    return {
        "code": str(getattr(getattr(candidate, "quote", None), "code", "")).zfill(6),
        "status": status,
        "candidate_status": status,
        "data_quality_status": getattr(candidate, "data_status", "verified") or "verified",
        "missing_fields": list(getattr(candidate, "missing_fields", ()) or ()),
        "stale_fields": list(getattr(candidate, "stale_fields", ()) or ()),
        "unverified_fields": [],
        "blocking_fields": [],
        "sort_values": values,
    }


def _candidate_priority_sort_key(candidate: ScreenCandidate, lens_id: str) -> tuple[Any, ...]:
    record = _candidate_priority_record(candidate)
    priority = DEFAULT_PRIORITY_POLICY.classify(record, lens_id=lens_id)
    persisted = (getattr(candidate, "score_breakdown", {}) or {}).get("priority_by_lens", {})
    persisted_priority = persisted.get(lens_id) if isinstance(persisted, Mapping) else None
    if isinstance(persisted_priority, Mapping) and isinstance(persisted_priority.get("priority_key"), (list, tuple)):
        priority_key = tuple(persisted_priority["priority_key"][:-1])
    else:
        priority_key = tuple(priority.priority_key[:-1])
    # PriorityPolicy supplies the stable Lens-specific objective fields.  The
    # fixed Lens order is only a tie-breaker between equal tiers and evidence.
    objective_key = DEFAULT_PRIORITY_POLICY.sort_key(record, lens_id=lens_id)[1:]
    fixed_lens_order = tuple(item.lens_id for item in DEFAULT_CANDIDATE_LENSES)
    lens_order = fixed_lens_order.index(lens_id) if lens_id in fixed_lens_order else len(fixed_lens_order)
    return (*priority_key, lens_order, *objective_key)


def _sort_candidate_group(group: list[ScreenCandidate], filters: ScreenFilter, lens_id: str) -> None:
    del filters
    group.sort(key=lambda item: _candidate_priority_sort_key(item, lens_id))


def _sort_candidate_results(
    results: list[ScreenCandidate],
    filters: ScreenFilter,
    lens_ids: Iterable[str] | None = None,
) -> tuple[list[ScreenCandidate], dict[str, list[ScreenCandidate]]]:
    requested = set(str(item).strip() for item in lens_ids or () if str(item).strip())
    groups: dict[str, list[ScreenCandidate]] = {
        lens.lens_id: []
        for lens in DEFAULT_CANDIDATE_LENSES
        if not requested or lens.lens_id in requested
    }
    for item in results:
        for lens_id in item.lens_ids:
            groups.setdefault(lens_id, []).append(item)
    for lens_id, group in groups.items():
        _sort_candidate_group(group, filters, lens_id)
    def primary_key(item: ScreenCandidate) -> tuple[Any, ...]:
        item_lenses = [
            lens_id
            for lens_id in item.lens_ids
            if not requested or lens_id in requested
        ]
        if not item_lenses:
            item_lenses = [
                lens.lens_id
                for lens in DEFAULT_CANDIDATE_LENSES
                if not requested or lens.lens_id in requested
            ]
        return min(_candidate_priority_sort_key(item, lens_id) for lens_id in item_lenses)

    unique: dict[str, ScreenCandidate] = {}
    for item in results:
        unique.setdefault(str(item.quote.code).zfill(6), item)
    ordered = sorted(unique.values(), key=primary_key)
    return ordered, groups


def screen_factor_snapshots(
    snapshots: list[FactorSnapshot],
    filters: ScreenFilter,
    progress: ProgressCallback | None = None,
    professional_formula: dict[str, Any] | None = None,
    *,
    candidate_mode: bool = False,
    lens_ids: Iterable[str] | None = None,
    data_snapshot_id: str = "",
    availability_cutoff: str = "",
    data_quality_status: str = "verified",
    data_source: str = "",
    fact_lineage_by_code: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    lens_values_by_code: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    selection_inputs_by_code: Mapping[str, SelectionPolicyInput | Mapping[str, Any]] | None = None,
    selection_policy: SelectionPolicy | None = None,
    policy_mode: str | None = None,
    trading_calendar: Iterable[Any] | None = None,
    member_status_policy: str = "listed_non_st",
) -> ScreenReport:
    if candidate_mode and professional_formula is not None:
        raise ValueError("CandidateLens 新流程禁止传入 professional_formula")
    candidates = [
        snapshot for snapshot in snapshots
        if snapshot.code
        and snapshot.name
        and not snapshot.name.upper().startswith(("退", "ST", "*ST"))
    ]
    if candidate_mode:
        candidates.sort(
            key=lambda item: (
                item.market_cap_yi is None,
                -(item.market_cap_yi or 0),
                str(item.code).zfill(6),
            )
        )
    candidates = candidates[: max(1, filters.scan_limit)]
    results: list[ScreenCandidate] = []
    notes: list[str] = []
    skipped = 0
    formal_policy_blocked = False
    total = len(candidates)
    if candidate_mode and data_quality_status == "blocked":
        return ScreenReport(
            results=[],
            scanned=0,
            skipped=0,
            failed=0,
            errors=["数据质量状态为 blocked，确定性候选层未执行。"],
            notes=notes,
            status="blocked",
            data_snapshot_id=data_snapshot_id,
        )
    if candidate_mode and requires_money_flow_filter(filters):
        _add_note_once(
            notes,
            "因子快照不含资金流，已忽略主力进场条件；筛选结果按其余已验证条件生成。",
        )
    elif requires_money_flow_filter(filters):
        _add_note_once(
            notes,
            "因子快照不含资金流，已忽略主力进场条件；实时智能筛选可使用公开批量资金流，无需 Tushare token。",
        )
    if requires_industry_strength_filter(filters) and not any(snapshot.industry_strength is not None for snapshot in candidates):
        return ScreenReport(
            results=[],
            scanned=0,
            skipped=0,
            failed=0,
            errors=[_industry_unavailable_error()],
            notes=[],
            status="blocked",
            data_snapshot_id=data_snapshot_id,
        )

    for idx, snapshot in enumerate(candidates, start=1):
        if progress:
            progress(idx, total, f"正在筛选本地快照 {snapshot.name} {snapshot.code}")
        quote = snapshot.to_quote()
        metrics = snapshot.to_metrics()
        if not passes_industry_filter(quote, filters):
            skipped += 1
            continue
        if not passes_filter(
            quote,
            metrics,
            filters,
            allow_missing_required=policy_mode in {"review_only", "exploratory"},
        ):
            skipped += 1
            continue
        technical = snapshot.to_technical() if (filters.with_technical or requires_technical_filter(filters)) else TechnicalProfile(False, summary="未计算技术画像")
        if not _passes_trend_20d(snapshot.trend_20d, filters):
            skipped += 1
            continue
        if not passes_non_trend_technical_filter(technical, quote, filters):
            skipped += 1
            continue
        if candidate_mode:
            candidate = _deterministic_candidate(
                    snapshot,
                    filters=filters,
                    quote=quote,
                    metrics=metrics,
                    technical=technical,
                    data_snapshot_id=data_snapshot_id,
                    availability_cutoff=availability_cutoff,
                    data_quality_status=data_quality_status,
                    data_source=data_source,
                    lens_ids=lens_ids,
                    fact_lineage_by_code=fact_lineage_by_code,
                    lens_values_by_code=lens_values_by_code,
                )
            if selection_policy is not None:
                evaluated_mode = policy_mode or ("exploratory" if data_quality_status == "exploratory" else "formal")
                policy_result = selection_policy.evaluate(
                    _selection_policy_input(
                        snapshot,
                        selection_inputs_by_code=selection_inputs_by_code,
                    ),
                    mode=evaluated_mode,
                    lens_ids=lens_ids,
                    availability_cutoff=availability_cutoff,
                    trading_calendar=trading_calendar,
                    member_status_policy=member_status_policy,
                )
                deterministic_lenses = tuple(candidate.lens_ids)
                if (
                    evaluated_mode in {"review_only", "exploratory"}
                    and not deterministic_lenses
                    and candidate.eligibility_status == "insufficient"
                ):
                    # A partial materialized snapshot can pass the user's
                    # active filters while lacking enough facts to mark one
                    # Lens as matched.  Keep the evaluated Lens scope visible
                    # in non-formal research, with insufficient status and
                    # explicit gaps; formal mode still rejects the row.
                    deterministic_lenses = tuple(
                        str(item).strip()
                        for item in (lens_ids or ())
                        if str(item).strip()
                    ) or tuple(item.lens_id for item in DEFAULT_CANDIDATE_LENSES)
                    candidate.lens_ids = deterministic_lenses
                    candidate.reason_codes = list(
                        dict.fromkeys(
                            [*candidate.reason_codes, "CANDIDATE_LENS_INPUT_INCOMPLETE"]
                        )
                    )
                    candidate.reasons = list(
                        dict.fromkeys(
                            [*candidate.reasons, "CandidateLens 证据不足，仅保留研究复核"]
                        )
                    )[:5]
                hard_exclusion = _NON_FORMAL_POLICY_HARD_EXCLUSIONS.intersection(
                    policy_result.reason_codes
                )
                if evaluated_mode == "formal" and policy_result.status != "candidate":
                    # Formal mode is record-strict: missing PIT/governance
                    # evidence blocks the row instead of turning it into a
                    # formal candidate.
                    formal_policy_blocked = True
                    skipped += 1
                    continue
                if policy_result.status == "excluded":
                    # review_only/exploratory may run on a materialized factor
                    # snapshot that does not yet carry the full PIT governance
                    # input.  Keep the already-passed deterministic CandidateLens
                    # row visible, but never turn a real universe exclusion into
                    # a candidate.  Formal mode remains a hard gate.
                    if (
                        evaluated_mode == "formal"
                        or
                        hard_exclusion
                        or not deterministic_lenses
                    ):
                        skipped += 1
                        continue
                    candidate.eligibility_status = "insufficient"
                    candidate.data_status = "partial"
                    candidate.lens_ids = deterministic_lenses
                    candidate.reason_codes = list(
                        dict.fromkeys(
                            [
                                *candidate.reason_codes,
                                "SELECTION_POLICY_INPUT_INCOMPLETE",
                                *policy_result.reason_codes,
                            ]
                        )
                    )
                    candidate.missing_fields = list(
                        dict.fromkeys([*candidate.missing_fields, *policy_result.missing_fields])
                    )
                    candidate.stale_fields = list(
                        dict.fromkeys([*candidate.stale_fields, *policy_result.stale_fields])
                    )
                    candidate.score_breakdown["reason_codes"] = list(candidate.reason_codes)
                    candidate.score_breakdown["missing_fields"] = list(candidate.missing_fields)
                    candidate.score_breakdown["stale_fields"] = list(candidate.stale_fields)
                    candidate.selection_policy_version = policy_result.selection_policy_version
                    candidate.score_breakdown.update(
                        {
                            "selection_policy": policy_result.as_dict(),
                            "selection_policy_fallback": "incomplete_input_review_only",
                        }
                    )
                    _refresh_candidate_digest(candidate, snapshot)
                    results.append(candidate)
                    continue
                # For non-formal runs the deterministic CandidateLens match
                # is the candidate-set authority.  SelectionPolicy only adds
                # PIT, governance and missing-data evidence; it must not erase
                # a reviewable row merely because the selection input is absent.
                if policy_mode in {"review_only", "exploratory"} and deterministic_lenses:
                    candidate.lens_ids = deterministic_lenses
                else:
                    candidate.lens_ids = policy_result.matched_lenses
                candidate.eligibility_status = policy_result.candidate_status
                candidate.reason_codes = list(policy_result.reason_codes)
                candidate.missing_fields = list(policy_result.missing_fields)
                candidate.stale_fields = list(policy_result.stale_fields)
                candidate.data_status = (
                    "exploratory"
                    if policy_mode == "exploratory"
                    else (
                        "review_only"
                        if policy_mode == "review_only" and policy_result.status == "stale"
                        else ("partial" if policy_result.status != "candidate" else "verified")
                    )
                )
                candidate.data_snapshot_id = data_snapshot_id
                candidate.candidate_digest = policy_result.candidate_digest
                candidate.mode = policy_result.mode
                candidate.exploratory_only = policy_result.exploratory_only
                candidate.universe_id = policy_result.universe.universe_id
                candidate.universe_version = policy_result.universe.universe_version
                candidate.board_profile_id = policy_result.universe.board_profile.profile_id if policy_result.universe.board_profile else ""
                # Keep the detailed policy contract on the candidate; the
                # persisted run-level metadata is normalized to ``v1`` by the
                # workflow boundary.
                candidate.selection_policy_version = policy_result.selection_policy_version
                priority_values = {
                    key: value.as_dict() for key, value in policy_result.priority_by_lens.items()
                }
                best_priority_lens = ""
                if priority_values:
                    priority_candidates = [
                        key for key in priority_values
                        if not candidate.lens_ids or key in set(candidate.lens_ids)
                    ] or list(priority_values)
                    best_priority_lens = min(
                        priority_candidates,
                        key=lambda key: (
                            tuple(priority_values[key].get("priority_key") or ()),
                            tuple(item.lens_id for item in DEFAULT_CANDIDATE_LENSES).index(key)
                            if key in {item.lens_id for item in DEFAULT_CANDIDATE_LENSES}
                            else len(DEFAULT_CANDIDATE_LENSES),
                        ),
                    )
                best_priority = priority_values.get(best_priority_lens, {})
                candidate.priority_tier = best_priority.get("priority_tier", "")
                candidate.priority_explanation = best_priority.get("priority_explanation", [])
                candidate.score_breakdown.update(
                    {
                        "candidate_lens_mode": True,
                        "selection_policy": policy_result.as_dict(),
                        "priority_by_lens": priority_values,
                        "priority_lens_id": best_priority_lens,
                        "mode": policy_result.mode,
                        "exploratory_only": policy_result.exploratory_only,
                    }
                )
                if policy_mode in {"review_only", "exploratory"} and deterministic_lenses and not policy_result.matched_lenses:
                    candidate.reason_codes = list(
                        dict.fromkeys([*candidate.reason_codes, "SELECTION_POLICY_INPUT_INCOMPLETE"])
                    )
                    candidate.score_breakdown["selection_policy_fallback"] = "incomplete_input_review_only"
                    if policy_mode == "review_only":
                        candidate.eligibility_status = "insufficient"
                    candidate.data_status = "partial"
                candidate.score_breakdown["reason_codes"] = list(candidate.reason_codes)
                candidate.score_breakdown["missing_fields"] = list(candidate.missing_fields)
                candidate.score_breakdown["stale_fields"] = list(candidate.stale_fields)
                _refresh_candidate_digest(candidate, snapshot)
            elif candidate.eligibility_status == "excluded":
                skipped += 1
                continue
            results.append(candidate)
            continue
        reasons: list[str] = []
        if snapshot.missing_fields:
            reasons.append("部分因子缺失")
        candidate = build_scored_candidate(
                quote,
                metrics,
                technical,
                extra_fields={"trend_20d": snapshot.trend_20d, "main_5d_yi": None},
                reasons=reasons,
                sort_values={
                    "trend_20d": snapshot.trend_20d,
                    "volume_ratio": technical.volume_ratio,
                    "pullback_pct": technical.pullback_pct,
                    "max_drawdown_30d": technical.max_drawdown_30d,
                    "max_drawdown_60d": technical.max_drawdown_60d,
                    "rsi": technical.rsi14,
                    "box_score": technical.box_score,
                    "box_range_pct": technical.box_range_pct,
                    "box_position_pct": technical.box_position_pct,
                    "industry_strength": getattr(quote, "industry_strength", None),
                    "turnover": quote.turnover,
                    "main_5d_yi": None,
                },
                shadow_formula=professional_formula,
            )
        required_fields = required_fields_for_filter(filters)
        available_fields = {
            field
            for field in required_fields
            if getattr(snapshot, field, None) is not None
            and field not in {
                item.strip()
                for item in (snapshot.missing_fields or "").split(",")
                if item.strip()
            }
        }
        optional_vetoes = {
            "数据质量低": True,
            "资金缺失": "main_5d_yi" not in required_fields,
            "行业缺失": "industry_strength" not in required_fields,
        }
        eligibility_vetoes = [
            reason
            for reason in candidate.score_breakdown.get("veto_reasons", [])
            if not optional_vetoes.get(reason, False)
        ]
        eligibility = build_eligibility_decision(
            required_fields=required_fields,
            available_fields=available_fields,
            risk_vetoes=eligibility_vetoes,
            data_quality_status="verified" if not set(required_fields) - available_fields else "partial",
        )
        candidate.score_breakdown.update(eligibility.as_dict())
        results.append(candidate)

    lens_results: dict[str, list[ScreenCandidate]] = {}
    candidate_set_digest = ""
    if candidate_mode:
        if any(field == "local_score" for field, _direction in filters.sort):
            _add_note_once(notes, "正式候选层忽略 local_score 排序；结果按 Lens 内客观字段和代码稳定排序。")
        results, lens_results = _sort_candidate_results(results, filters, lens_ids)
        candidate_set_digest = stable_hash(
            [item.candidate_digest for item in results[: max(1, filters.result_limit)]]
        )
    else:
        _sort_results(results, filters)
    return ScreenReport(
        results=results[: max(1, filters.result_limit)],
        scanned=total,
        skipped=skipped,
        failed=0,
        errors=[],
        notes=notes,
        status=(
            "blocked"
            if candidate_mode and (policy_mode or "formal") == "formal" and formal_policy_blocked and not results
            else data_quality_status
            if candidate_mode and data_quality_status != "verified"
            else (
                "unverified"
                if requires_money_flow_filter(filters)
                else (
                    "partial"
                    if (not candidate_mode and any(item.missing_fields for item in results))
                    else "verified"
                )
            )
        ),
        missing_fields=["main_5d_yi"] if requires_money_flow_filter(filters) else [],
        data_snapshot_id=data_snapshot_id,
        lens_results=lens_results,
        candidate_set_digest=candidate_set_digest,
        mode=policy_mode or ("exploratory" if data_quality_status == "exploratory" else "formal"),
        universe_id="",
        universe_version="",
        selection_policy_version=selection_policy.version if selection_policy is not None else "",
    )


def run_candidate_lens_screening(
    snapshots: list[FactorSnapshot],
    filters: ScreenFilter,
    progress: ProgressCallback | None = None,
    *,
    mode: str = "formal",
    lens_ids: Iterable[str] | None = None,
    data_snapshot_id: str = "",
    availability_cutoff: str = "",
    data_quality_status: str = "verified",
    data_source: str = "",
    fact_lineage_by_code: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    lens_values_by_code: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    selection_inputs_by_code: Mapping[str, SelectionPolicyInput | Mapping[str, Any]] | None = None,
    selection_policy: SelectionPolicy | None = None,
    trading_calendar: Iterable[Any] | None = None,
    member_status_policy: str = "listed_non_st",
    professional_formula: dict[str, Any] | None = None,
) -> ScreenReport:
    """Run the only supported new screening path.

    The legacy ``screen_factor_snapshots`` scoring path remains available for
    reading and historical compatibility.  This seam is intentionally strict:
    every new execution mode uses the deterministic CandidateLens layer and a
    formula can never become an implicit fallback.
    """

    if professional_formula is not None:
        raise ValueError("run_candidate_lens_screening 不接受 professional_formula")
    if mode not in {"formal", "review_only", "exploratory"}:
        raise ValueError(f"未知 CandidateLens 运行模式：{mode}")
    return screen_factor_snapshots(
        snapshots,
        filters,
        progress,
        professional_formula=None,
        candidate_mode=True,
        lens_ids=lens_ids,
        data_snapshot_id=data_snapshot_id,
        availability_cutoff=availability_cutoff,
        data_quality_status=data_quality_status,
        data_source=data_source,
        fact_lineage_by_code=fact_lineage_by_code,
        lens_values_by_code=lens_values_by_code,
        selection_inputs_by_code=selection_inputs_by_code,
        selection_policy=selection_policy or DEFAULT_SELECTION_POLICY,
        policy_mode=mode,
        trading_calendar=trading_calendar,
        member_status_policy=member_status_policy,
    )
