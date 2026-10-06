"""Frozen Phase 0.5 LensPolicy and deterministic selection evaluation.

This is the policy boundary for the three initial research Lenses.  It does
not calculate a cross-Lens score and it never emits a trading conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
import math
from typing import Any, Iterable, Mapping

from stock_tool.core.missing_data_policy import MissingDataPolicy
from stock_tool.core.priority_policy import DEFAULT_PRIORITY_POLICY, PriorityResult
from stock_tool.core.screening_contract import stable_hash
from stock_tool.core.universe_policy import (
    DEFAULT_UNIVERSE_PROFILE,
    UniverseDecision,
    UniverseProfile,
    evaluate_universe_member,
)


SELECTION_POLICY_VERSION = "selection.v1"
SELECTION_INPUT_VERSION = "selection_input.v1"
LENS_POLICY_VERSION = "lens_policy.v1"
SELECTION_MODES = frozenset({"formal", "exploratory", "review_only"})
LENS_IDS = ("quality_research", "trend_observation", "event_verification")
LENS_STATUSES = frozenset({"matched", "partial", "insufficient", "stale", "excluded"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _get(item: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(item, Mapping) and name in item:
            value = item.get(name)
        else:
            value = getattr(item, name, None)
        if value is not None and value != "":
            return value
    for container_name in ("financial", "technical", "event", "facts", "values"):
        container = item.get(container_name) if isinstance(item, Mapping) else getattr(item, container_name, None)
        if isinstance(container, Mapping):
            for name in names:
                if name in container and container[name] is not None and container[name] != "":
                    return container[name]
    return default


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _date(value: Any) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, Iterable) and not isinstance(value, Mapping):
        return list(value)
    return []


def _texts(value: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_text(item) for item in _list(value) if _text(item)))


def _markers(item: Any, name: str) -> set[str]:
    value = _get(item, name, default=())
    return {_text(part).casefold() for part in _list(value) if _text(part)}


def _quality_by_field(item: Any) -> dict[str, str]:
    value = _get(item, "quality_status_by_field", "field_quality", default={})
    if not isinstance(value, Mapping):
        return {}
    return {_text(key).casefold(): _text(status).casefold() for key, status in value.items() if _text(status)}


def _field_state(item: Any, field_name: str, *aliases: str) -> tuple[Any, bool, bool, bool]:
    value = _get(item, field_name, *aliases)
    missing = field_name.casefold() in _markers(item, "missing_fields") or (value is None and not isinstance(value, bool))
    stale = field_name.casefold() in _markers(item, "stale_fields")
    status = _quality_by_field(item).get(field_name.casefold(), "")
    unverified = status not in {"", "verified"}
    return value, missing, stale, unverified


@dataclass(frozen=True)
class SelectionPolicyInput:
    """Explicit, PIT-aware input contract for the formal selection policy.

    ``FactorSnapshot`` remains a legacy UI/materialized-factor shape.  Formal
    selection must receive this contract so that missing identity, timing,
    universe, price-adjustment, financial, industry, and event facts remain
    missing instead of being guessed by an adapter.
    """

    code: str = ""
    name: str = ""
    snapshot_id: str = ""
    as_of: str = ""
    available_at: str = ""
    snapshot_quality_status: str = ""
    board_id: str = ""
    exchange: str = ""
    listing_date: str = ""
    first_trade_date: str = ""
    security_status: str = ""
    security_status_provided: bool = False
    is_st: bool | None = None
    is_delisting: bool | None = None
    is_suspended: bool | None = None
    price: float | None = None
    current_price: float | None = None
    price_limit_rule_id: str = ""
    price_limit_rule_version: str = ""
    trading_calendar_version: str = ""
    valid_listing_trading_days: int | None = None
    valid_turnover_days_20d: int | None = None
    valid_price_volume_days: int | None = None
    median_turnover_value_20d: float | None = None
    liquidity_window: str = ""
    raw_price: float | None = None
    adjusted_price: float | None = None
    adjusted_price_series: Any = None
    adjustment_factor: float | None = None
    corporate_action_fact_id: str = ""
    adjustment_method: str = ""
    adjustment_version: str = ""
    financial_reports: tuple[Mapping[str, Any], ...] = ()
    report_date: str = ""
    financial_available_at: str = ""
    financial_quality_status: str = ""
    latest_report_age_days: int | None = None
    report_freshness_days: int | None = None
    roe_ttm: float | None = None
    net_profit_positive_periods: int | None = None
    operating_cashflow_positive_periods: int | None = None
    verified_report_periods: int | None = None
    debt_ratio: float | None = None
    financial_debt_metric: float | None = None
    industry_classification_id: str = ""
    industry_classification_version: str = ""
    industry_sector: str = ""
    industry: str = ""
    industry_code: str = ""
    industry_effective_at: str = ""
    industry_sample_count: int | None = None
    peer_set_digest: str = ""
    debt_sector_percentile: float | None = None
    events: tuple[Mapping[str, Any], ...] = ()
    event_code: str = ""
    event_date: str = ""
    event_available_at: str = ""
    event_source: str = ""
    event_source_tier: int | None = None
    event_source_route: str = ""
    event_dedup_status: str = ""
    valid_price_days_20d: int | None = None
    valid_volume_days_20d: int | None = None
    trend_20d: float | None = None
    trend_60d: float | None = None
    relative_strength_20d: float | None = None
    volatility: float | None = None
    max_drawdown_30d: float | None = None
    price_limit_locked: bool | None = None
    price_limit_direction: str = ""
    technical_fields_quality: str = ""
    adjustment_factor_missing: bool | None = None
    quality_status_by_field: Mapping[str, str] = field(default_factory=dict)
    fact_lineage: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    contract_version: str = SELECTION_INPUT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != SELECTION_INPUT_VERSION:
            raise ValueError(f"不支持的 SelectionPolicyInput 版本：{self.contract_version}")
        code = _text(self.code)
        object.__setattr__(self, "code", code.zfill(6) if code.isdigit() else code)
        object.__setattr__(self, "financial_reports", _mapping_tuple(self.financial_reports))
        object.__setattr__(self, "events", _mapping_tuple(self.events))
        object.__setattr__(
            self,
            "quality_status_by_field",
            {_text(key): _text(value) for key, value in dict(self.quality_status_by_field).items() if _text(key) and _text(value)},
        )
        object.__setattr__(
            self,
            "fact_lineage",
            {str(key): dict(value) for key, value in dict(self.fact_lineage).items() if isinstance(value, Mapping)},
        )

    @classmethod
    def from_mapping(cls, item: Any) -> "SelectionPolicyInput":
        """Convert a legacy mapping/object without inventing missing fields."""

        if isinstance(item, cls):
            return item
        names = tuple(name for name in cls.__dataclass_fields__ if name not in {"financial_reports", "events", "quality_status_by_field", "fact_lineage", "contract_version"})
        if isinstance(item, Mapping):
            raw = dict(item)
        else:
            raw = {name: getattr(item, name, None) for name in names}
        values: dict[str, Any] = {}
        for name in names:
            value = raw.get(name)
            if value is None and name == "snapshot_id":
                value = raw.get("data_snapshot_id")
            if value is None and name == "as_of":
                value = raw.get("data_date")
            if value is None and name == "security_status":
                value = raw.get("status") or raw.get("listing_status")
            if name == "security_status_provided":
                value = bool(raw.get("security_status_provided")) or (
                    "security_status" in raw and _text(raw.get("security_status")) != ""
                )
            if value is None and name == "valid_listing_trading_days":
                value = raw.get("listing_age_trading_days") or raw.get("listing_trading_days")
            if value is None and name == "valid_turnover_days_20d":
                value = raw.get("valid_liquidity_days") or raw.get("effective_turnover_days")
            if value is None and name == "valid_price_volume_days":
                value = raw.get("price_volume_valid_days")
            if value is None and name == "is_suspended":
                value = raw.get("suspended") or raw.get("halted") or raw.get("trading_halted")
            if value is None and name == "current_price":
                value = raw.get("price") or raw.get("last_price")
            if value is None and name == "financial_available_at" and raw.get("report_date") is not None:
                value = raw.get("available_at")
            if value is None and name == "price_limit_locked":
                value = raw.get("limit_locked")
            if value is None and name == "industry_classification_version":
                value = raw.get("industry_version")
            if value is None and name == "industry_classification_id":
                value = raw.get("industry_classification")
            values[name] = value

        reports = raw.get("financial_reports") or raw.get("reports") or ()
        if not reports and any(raw.get(name) is not None for name in ("report_date", "financial_available_at", "roe_ttm", "roe", "net_profit", "operating_cashflow")):
            reports = ({
                "report_date": raw.get("report_date"),
                "available_at": values.get("financial_available_at"),
                "quality_status": raw.get("financial_quality_status"),
                "roe_ttm": raw.get("roe_ttm", raw.get("roe")),
                "net_profit": raw.get("net_profit"),
                "operating_cashflow": raw.get("operating_cashflow"),
            },)
        events = raw.get("events") or raw.get("event_facts") or ()
        if not events and any(raw.get(name) is not None for name in ("event_code", "event_date", "event_available_at", "event_source")):
            events = ({
                "code": raw.get("event_code"),
                "event_date": raw.get("event_date"),
                "available_at": raw.get("event_available_at"),
                "event_source": raw.get("event_source"),
                "source_tier": raw.get("event_source_tier"),
                "source_route": raw.get("event_source_route"),
                "dedup_status": raw.get("event_dedup_status"),
            },)
        return cls(
            **values,
            financial_reports=reports,
            events=events,
            quality_status_by_field=raw.get("quality_status_by_field") or raw.get("field_quality") or {},
            fact_lineage=raw.get("fact_lineage") or {},
        )

    @classmethod
    def from_fact_values(
        cls,
        *,
        code: str,
        values: Mapping[str, Any],
        snapshot_id: str,
        as_of: str,
        snapshot_quality_status: str,
        fact_lineage: Mapping[str, Mapping[str, Any]],
    ) -> "SelectionPolicyInput":
        """Hydrate one input from explicit snapshot facts and field lineage."""

        raw = dict(values)
        raw["code"] = code
        raw["snapshot_id"] = snapshot_id
        raw["as_of"] = as_of
        raw["snapshot_quality_status"] = snapshot_quality_status
        raw["fact_lineage"] = fact_lineage
        raw["quality_status_by_field"] = {
            field_name: str(metadata.get("quality_status") or "")
            for field_name, metadata in fact_lineage.items()
            if isinstance(metadata, Mapping) and str(metadata.get("quality_status") or "")
        }
        if not raw.get("available_at") and raw.get("snapshot_available_at"):
            raw["available_at"] = raw["snapshot_available_at"]
        if not raw.get("financial_available_at"):
            raw["financial_available_at"] = raw.get("financial_report_available_at") or next(
                (
                    metadata.get("available_at")
                    for field_name in ("financial_report_date", "report_date")
                    if isinstance(metadata := dict(fact_lineage.get(field_name) or {}), Mapping)
                    and metadata.get("available_at")
                ),
                None,
            )
        if not raw.get("event_available_at"):
            raw["event_available_at"] = raw.get("event_fact_available_at") or next(
                (
                    metadata.get("available_at")
                    for field_name in ("event_date", "event_title", "event_source")
                    if isinstance(metadata := dict(fact_lineage.get(field_name) or {}), Mapping)
                    and metadata.get("available_at")
                ),
                None,
            )
        return cls.from_mapping(raw)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "code": self.code,
            "name": self.name,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of,
            "available_at": self.available_at,
            "snapshot_quality_status": self.snapshot_quality_status,
            "board_id": self.board_id,
            "exchange": self.exchange,
            "listing_date": self.listing_date,
            "first_trade_date": self.first_trade_date,
            "security_status": self.security_status,
            "is_st": self.is_st,
            "is_delisting": self.is_delisting,
            "is_suspended": self.is_suspended,
            "security_status_provided": self.security_status_provided,
            "price": self.price,
            "current_price": self.current_price,
            "price_limit_rule_id": self.price_limit_rule_id,
            "price_limit_rule_version": self.price_limit_rule_version,
            "trading_calendar_version": self.trading_calendar_version,
            "valid_listing_trading_days": self.valid_listing_trading_days,
            "valid_turnover_days_20d": self.valid_turnover_days_20d,
            "valid_price_volume_days": self.valid_price_volume_days,
            "median_turnover_value_20d": self.median_turnover_value_20d,
            "liquidity_window": self.liquidity_window,
            "raw_price": self.raw_price,
            "adjusted_price": self.adjusted_price,
            "adjusted_price_series": self.adjusted_price_series,
            "adjustment_factor": self.adjustment_factor,
            "corporate_action_fact_id": self.corporate_action_fact_id,
            "adjustment_method": self.adjustment_method,
            "adjustment_version": self.adjustment_version,
            "financial_reports": [dict(item) for item in self.financial_reports],
            "report_date": self.report_date,
            "financial_available_at": self.financial_available_at,
            "financial_quality_status": self.financial_quality_status,
            "latest_report_age_days": self.latest_report_age_days,
            "report_freshness_days": self.report_freshness_days,
            "roe_ttm": self.roe_ttm,
            "net_profit_positive_periods": self.net_profit_positive_periods,
            "operating_cashflow_positive_periods": self.operating_cashflow_positive_periods,
            "verified_report_periods": self.verified_report_periods,
            "debt_ratio": self.debt_ratio,
            "financial_debt_metric": self.financial_debt_metric,
            "industry_classification_id": self.industry_classification_id,
            "industry_classification_version": self.industry_classification_version,
            "industry_sector": self.industry_sector,
            "industry": self.industry,
            "industry_code": self.industry_code,
            "industry_effective_at": self.industry_effective_at,
            "industry_sample_count": self.industry_sample_count,
            "peer_set_digest": self.peer_set_digest,
            "debt_sector_percentile": self.debt_sector_percentile,
            "events": [dict(item) for item in self.events],
            "event_code": self.event_code,
            "event_date": self.event_date,
            "event_available_at": self.event_available_at,
            "event_source": self.event_source,
            "event_source_tier": self.event_source_tier,
            "event_source_route": self.event_source_route,
            "event_dedup_status": self.event_dedup_status,
            "valid_price_days_20d": self.valid_price_days_20d,
            "valid_volume_days_20d": self.valid_volume_days_20d,
            "trend_20d": self.trend_20d,
            "trend_60d": self.trend_60d,
            "relative_strength_20d": self.relative_strength_20d,
            "volatility": self.volatility,
            "max_drawdown_30d": self.max_drawdown_30d,
            "price_limit_locked": self.price_limit_locked,
            "price_limit_direction": self.price_limit_direction,
            "technical_fields_quality": self.technical_fields_quality,
            "adjustment_factor_missing": self.adjustment_factor_missing,
            "quality_status_by_field": dict(self.quality_status_by_field),
            "fact_lineage": {key: dict(value) for key, value in self.fact_lineage.items()},
        }


def _mapping_tuple(value: Any) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        value = (value,)
    if isinstance(value, str) or not isinstance(value, Iterable):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


@dataclass(frozen=True)
class LensPolicy:
    lens_id: str
    lens_version: str
    mode: str
    critical_fields: tuple[str, ...]
    supporting_fields: tuple[str, ...]
    hard_filters: dict[str, Any]
    exclusion_rules: dict[str, Any]
    missing_policy: str
    freshness_policy: dict[str, Any]
    peer_policy: dict[str, Any]
    sort_fields: tuple[str, ...]
    evaluation_metrics: tuple[str, ...]
    purpose: str
    contract_version: str = LENS_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != LENS_POLICY_VERSION:
            raise ValueError(f"不支持的 LensPolicy 版本：{self.contract_version}")
        if self.lens_id not in LENS_IDS:
            raise ValueError(f"未知 Lens：{self.lens_id}")
        if self.mode not in SELECTION_MODES:
            raise ValueError(f"未知 Lens 模式：{self.mode}")
        object.__setattr__(self, "critical_fields", tuple(dict.fromkeys(_text(item) for item in self.critical_fields if _text(item))))
        object.__setattr__(self, "supporting_fields", tuple(dict.fromkeys(_text(item) for item in self.supporting_fields if _text(item))))
        object.__setattr__(self, "sort_fields", tuple(dict.fromkeys(_text(item) for item in self.sort_fields if _text(item))))
        object.__setattr__(self, "evaluation_metrics", tuple(dict.fromkeys(_text(item) for item in self.evaluation_metrics if _text(item))))
        object.__setattr__(self, "hard_filters", dict(self.hard_filters))
        object.__setattr__(self, "exclusion_rules", dict(self.exclusion_rules))
        object.__setattr__(self, "freshness_policy", dict(self.freshness_policy))
        object.__setattr__(self, "peer_policy", dict(self.peer_policy))

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "lens_id": self.lens_id,
            "lens_version": self.lens_version,
            "mode": self.mode,
            "critical_fields": list(self.critical_fields),
            "supporting_fields": list(self.supporting_fields),
            "hard_filters": dict(self.hard_filters),
            "exclusion_rules": dict(self.exclusion_rules),
            "missing_policy": self.missing_policy,
            "freshness_policy": dict(self.freshness_policy),
            "peer_policy": dict(self.peer_policy),
            "sort_fields": list(self.sort_fields),
            "evaluation_metrics": list(self.evaluation_metrics),
            "purpose": self.purpose,
        }


QUALITY_POLICY = LensPolicy(
    lens_id="quality_research", lens_version="v1", mode="formal",
    critical_fields=("snapshot_quality_status", "latest_report_age_days", "verified_report_periods", "roe_ttm", "net_profit_positive_periods", "operating_cashflow_positive_periods"),
    supporting_fields=("debt_ratio", "debt_sector_percentile", "industry_classification_version", "peer_set_digest"),
    hard_filters={"roe_ttm": {"formal": 8, "exploratory": 0}, "verified_report_periods": {"formal": 3, "exploratory": 2}},
    exclusion_rules={"st_status": "excluded", "future_available_at": "invalidated"},
    missing_policy="missing_data.v1", freshness_policy={"max_report_age_days": 365},
    peer_policy={"min_sample_count": 20, "max_percentile": 75},
    sort_fields=("roe_continuity_periods", "cashflow_positive_periods", "roe_ttm", "net_margin_ttm", "debt_sector_percentile", "report_freshness", "code"),
    evaluation_metrics=("roe_continuity_periods", "cashflow_positive_periods", "roe_ttm", "net_margin_ttm", "debt_sector_percentile", "report_freshness"),
    purpose="发现经营质量值得进一步研究的股票，不代表估值合理或未来收益。",
)
TREND_POLICY = LensPolicy(
    lens_id="trend_observation", lens_version="v1", mode="formal",
    critical_fields=("adjusted_price_series", "valid_price_days_20d", "valid_volume_days_20d", "trend_20d", "trend_60d", "relative_strength_20d", "median_turnover_value_20d", "technical_fields_quality"),
    supporting_fields=("volatility", "max_drawdown_30d", "price_limit_direction", "price_limit_rule_id", "price_limit_rule_version"),
    hard_filters={"valid_price_days_20d": {"formal": 15, "exploratory": 10}, "trend_20d": {"formal": 3, "exploratory": 0}},
    exclusion_rules={"st_status": "excluded", "suspended": "excluded"},
    missing_policy="missing_data.v1", freshness_policy={"price_window_days": 20, "limit_locked_days_formal": 3, "limit_locked_days_exploratory": 6},
    peer_policy={"relative_strength_required_formal": True},
    sort_fields=("relative_strength_20d", "trend_20d", "trend_60d", "median_turnover_value_20d", "volatility", "max_drawdown_30d", "code"),
    evaluation_metrics=("relative_strength_20d", "trend_20d", "trend_60d", "median_turnover_value_20d", "volatility", "max_drawdown_30d"),
    purpose="发现价格、成交和行业相对表现值得观察的股票，不输出上涨预测或交易动作。",
)
EVENT_POLICY = LensPolicy(
    lens_id="event_verification", lens_version="v1", mode="formal",
    critical_fields=("event_date", "available_at", "code", "source_route_verified"),
    supporting_fields=("event_title", "event_type", "dedup_digest"),
    hard_filters={"formal_max_age_days": 30, "exploratory_max_age_days": 90, "formal_source_tier": 1},
    exclusion_rules={"future_available_at": "invalidated", "missing_available_at": "insufficient"},
    missing_policy="missing_data.v1", freshness_policy={"formal_days": 30, "exploratory_days": 90},
    peer_policy={},
    sort_fields=("event_age_days", "source_tier", "event_date", "verification_gap_count", "event_type_priority", "code"),
    evaluation_metrics=("event_age_days", "source_tier", "event_date", "verification_gap_count", "event_type_priority"),
    purpose="发现并核验近期公告、监管、公司披露或其他事件资料，不判断事件是否必然带来价格变化。",
)
DEFAULT_LENS_POLICIES = (QUALITY_POLICY, TREND_POLICY, EVENT_POLICY)


@dataclass(frozen=True)
class LensEvaluation:
    lens_id: str
    lens_version: str
    mode: str
    status: str
    matched: bool
    missing_fields: tuple[str, ...] = ()
    stale_fields: tuple[str, ...] = ()
    unverified_fields: tuple[str, ...] = ()
    blocking_fields: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    sort_values: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[dict[str, Any], ...] = ()
    data_quality_status: str = "verified"
    priority: PriorityResult | None = None

    def __post_init__(self) -> None:
        if self.status not in LENS_STATUSES:
            raise ValueError(f"未知 Lens 结果状态：{self.status}")
        if self.mode not in SELECTION_MODES:
            raise ValueError(f"未知筛选模式：{self.mode}")
        for name in ("missing_fields", "stale_fields", "unverified_fields", "blocking_fields", "reason_codes"):
            object.__setattr__(self, name, tuple(dict.fromkeys(_text(item) for item in getattr(self, name) if _text(item))))
        object.__setattr__(self, "sort_values", dict(self.sort_values))
        object.__setattr__(self, "evidence", tuple(dict(item) for item in self.evidence))

    def as_dict(self) -> dict[str, Any]:
        return {
            "lens_id": self.lens_id,
            "lens_version": self.lens_version,
            "mode": self.mode,
            "status": self.status,
            "matched": self.matched,
            "missing_fields": list(self.missing_fields),
            "stale_fields": list(self.stale_fields),
            "unverified_fields": list(self.unverified_fields),
            "blocking_fields": list(self.blocking_fields),
            "reason_codes": list(self.reason_codes),
            "sort_values": dict(self.sort_values),
            "evidence": [dict(item) for item in self.evidence],
            "data_quality_status": self.data_quality_status,
            "priority": self.priority.as_dict() if self.priority else None,
        }


@dataclass(frozen=True)
class SelectionResult:
    code: str
    mode: str
    status: str
    candidate_status: str
    exploratory_only: bool
    universe: UniverseDecision
    lens_results: tuple[LensEvaluation, ...]
    matched_lenses: tuple[str, ...]
    missing_fields: tuple[str, ...]
    stale_fields: tuple[str, ...]
    unverified_fields: tuple[str, ...]
    blocking_fields: tuple[str, ...]
    reason_codes: tuple[str, ...]
    priority_by_lens: dict[str, PriorityResult]
    facts: dict[str, Any]
    candidate_digest: str
    selection_policy_version: str = SELECTION_POLICY_VERSION
    member_status_policy: str = "listed_non_st"
    selection_input: dict[str, Any] = field(default_factory=dict)

    @property
    def eligibility_status(self) -> str:
        return self.candidate_status

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "mode": self.mode,
            "status": self.status,
            "candidate_status": self.candidate_status,
            "eligibility_status": self.candidate_status,
            "exploratory_only": self.exploratory_only,
            "universe": self.universe.as_dict(),
            "lens_results": {item.lens_id: item.as_dict() for item in self.lens_results},
            "matched_lenses": list(self.matched_lenses),
            "missing_fields": list(self.missing_fields),
            "stale_fields": list(self.stale_fields),
            "unverified_fields": list(self.unverified_fields),
            "blocking_fields": list(self.blocking_fields),
            "reason_codes": list(self.reason_codes),
            "priority_by_lens": {key: value.as_dict() for key, value in self.priority_by_lens.items()},
            "facts": dict(self.facts),
            "candidate_digest": self.candidate_digest,
            "selection_policy_version": self.selection_policy_version,
            "member_status_policy": self.member_status_policy,
            "selection_input": dict(self.selection_input),
        }


@dataclass(frozen=True)
class SelectionBatch:
    mode: str
    results: tuple[SelectionResult, ...]
    candidate_set_digest: str
    selection_policy_version: str = SELECTION_POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "results": [item.as_dict() for item in self.results],
            "candidate_set_digest": self.candidate_set_digest,
            "selection_policy_version": self.selection_policy_version,
        }


@dataclass(frozen=True)
class ExploratoryProposal:
    proposal_id: str
    code: str
    proposed_lens_id: str
    source_run_id: str
    evidence_refs: tuple[str, ...]
    proposal_reason: str
    missing_evidence: tuple[str, ...] = ()
    status: str = "pending_human_review"
    contract_version: str = "exploratory_proposal.v1"
    snapshot_id: str = ""

    def __post_init__(self) -> None:
        if self.status != "pending_human_review":
            raise ValueError("ExploratoryProposal 必须等待人工复核")
        if self.proposed_lens_id not in LENS_IDS:
            raise ValueError(f"未知 proposed_lens_id：{self.proposed_lens_id}")
        for name in ("proposal_id", "code", "source_run_id", "proposal_reason"):
            if not _text(getattr(self, name)):
                raise ValueError(f"ExploratoryProposal.{name} 不能为空")
        object.__setattr__(self, "evidence_refs", tuple(dict.fromkeys(_text(item) for item in self.evidence_refs if _text(item))))
        object.__setattr__(self, "missing_evidence", tuple(dict.fromkeys(_text(item) for item in self.missing_evidence if _text(item))))

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "proposal_id": self.proposal_id,
            "code": self.code,
            "proposed_lens_id": self.proposed_lens_id,
            "source_run_id": self.source_run_id,
            "evidence_refs": list(self.evidence_refs),
            "proposal_reason": self.proposal_reason,
            "missing_evidence": list(self.missing_evidence),
            "status": self.status,
            "snapshot_id": self.snapshot_id,
        }


def _reports(item: Any) -> list[Mapping[str, Any]]:
    values = _get(item, "financial_reports", "reports", "financial_periods", default=[])
    result = [value for value in _list(values) if isinstance(value, Mapping)]
    if result:
        return result
    direct = {
        key: _get(item, key)
        for key in ("period_end", "report_date", "available_at", "roe_ttm", "roe", "net_profit", "operating_cashflow", "quality_status")
        if _get(item, key) is not None
    }
    return [direct] if direct else []


def _quality_evaluation(item: Any, policy: LensPolicy, *, mode: str, cutoff: date | None) -> LensEvaluation:
    missing: list[str] = []
    stale: list[str] = []
    unverified: list[str] = []
    reasons: list[str] = []
    reports = _reports(item)
    report_valid = []
    for report in reports:
        report_date = _date(report.get("report_date"))
        available_at = _date(report.get("available_at"))
        if report_date is None:
            missing.append("financial_report_date")
            continue
        if available_at is None:
            missing.append("financial_available_at")
            continue
        if cutoff and available_at and available_at > cutoff:
            reasons.append("FUTURE_AVAILABLE_AT")
        if cutoff and report_date and report_date > cutoff:
            reasons.append("FUTURE_REPORT_DATE")
        if not cutoff or (available_at <= cutoff and report_date <= cutoff):
            quality = _text(report.get("quality_status")).casefold()
            if quality == "verified":
                report_valid.append(report)
            else:
                unverified.append("financial_report_quality_status")
    cutoff = cutoff or _date(_get(item, "as_of", "data_date"))
    age = _number(_get(item, "latest_report_age_days", "report_freshness_days"))
    if age is None and cutoff and report_valid:
        latest = max((_date(report.get("report_date")) for report in report_valid if _date(report.get("report_date"))), default=None)
        age = (cutoff - latest).days if latest else None
    if age is None:
        missing.append("latest_report_age_days")
    elif age > 365:
        stale.append("latest_report_age_days")
        reasons.append("FINANCIAL_REPORT_STALE")
    verified_periods = _number(_get(item, "verified_report_periods", "report_periods_verified"))
    if verified_periods is None:
        verified_periods = float(len(report_valid)) if report_valid else None
    roe = _number(_get(item, "roe_ttm", "roe"))
    net_positive = _number(_get(item, "net_profit_positive_periods", "positive_net_profit_periods"))
    cash_positive = _number(_get(item, "operating_cashflow_positive_periods", "cashflow_positive_periods", "positive_operating_cashflow_periods"))
    if verified_periods is None:
        missing.append("verified_report_periods")
    if roe is None:
        missing.append("roe_ttm")
    if net_positive is None:
        if report_valid:
            values = [_number(report.get("net_profit")) for report in report_valid]
            if any(value is None for value in values):
                missing.append("net_profit_positive_periods")
            else:
                net_positive = float(sum(value > 0 for value in values if value is not None))
        else:
            missing.append("net_profit_positive_periods")
    if cash_positive is None:
        if report_valid:
            values = [_number(report.get("operating_cashflow")) for report in report_valid]
            if any(value is None for value in values):
                missing.append("operating_cashflow_positive_periods")
            else:
                cash_positive = float(sum(value > 0 for value in values if value is not None))
        else:
            missing.append("operating_cashflow_positive_periods")
    snapshot_quality = _text(_get(item, "snapshot_quality_status", "data_quality_status", "quality_status")).casefold()
    if snapshot_quality != "verified":
        unverified.append("snapshot_quality_status")
        reasons.append(f"DATA_QUALITY_{snapshot_quality.upper()}")
    if reasons and any(item in {"FUTURE_AVAILABLE_AT", "FUTURE_REPORT_DATE"} for item in reasons):
        return LensEvaluation(policy.lens_id, policy.lens_version, mode, "excluded", False, tuple(missing), tuple(stale), tuple(unverified), (), tuple(dict.fromkeys(reasons)), data_quality_status=snapshot_quality)
    minimum_periods = 3 if mode == "formal" else 2
    roe_floor = 8 if mode == "formal" else 0
    if verified_periods is not None and verified_periods < minimum_periods:
        reasons.append("REPORT_PERIODS_INSUFFICIENT")
        if "verified_report_periods" not in missing:
            missing.append("verified_report_periods")
    if roe is not None and roe < roe_floor:
        reasons.append("ROE_BELOW_THRESHOLD")
    if net_positive is not None and net_positive < minimum_periods:
        reasons.append("NET_PROFIT_CONTINUITY_INSUFFICIENT")
    if cash_positive is None and mode == "exploratory":
        missing.append("operating_cashflow_positive_periods")
        reasons.append("CASHFLOW_INSUFFICIENT")
    elif cash_positive is not None and cash_positive < minimum_periods:
        reasons.append("CASHFLOW_CONTINUITY_INSUFFICIENT")

    sector = _text(_get(item, "industry_sector", "sector", "industry")).casefold()
    financial = any(token in sector for token in ("银行", "保险", "证券", "bank", "insurance", "broker"))
    property_or_utility = any(token in sector for token in ("地产", "公用事业", "房地产", "property", "utility"))
    percentile = _number(_get(item, "debt_sector_percentile", "industry_debt_percentile"))
    sample_count = _number(_get(item, "industry_sample_count", "peer_sample_count"))
    debt = _number(_get(item, "debt_ratio"))
    for identity_field, aliases in (
        ("industry_classification_id", ("industry_classification_id", "industry_classification")),
        ("industry_classification_version", ("industry_classification_version", "industry_version")),
        ("peer_set_digest", ("peer_set_digest",)),
    ):
        if _get(item, *aliases) in (None, ""):
            missing.append(identity_field)
            reasons.append("INDUSTRY_VERSION_BINDING_MISSING")
    if financial:
        specialist = _get(item, "financial_debt_metric", "financial_debt_ratio", "capital_adequacy_ratio", "solvency_ratio")
        if specialist is None:
            missing.append("financial_debt_metric")
            reasons.append("FINANCIAL_DEBT_FIELD_MISSING")
        elif _bool_false(_get(item, "financial_debt_compliant", default=True)):
            reasons.append("FINANCIAL_DEBT_RULE_FAILED")
    else:
        if sample_count is None:
            missing.append("industry_sample_count")
            reasons.append("INDUSTRY_SAMPLE_COUNT_MISSING")
        elif sample_count < 20 and mode == "formal":
            missing.append("industry_debt_percentile")
            reasons.append("INDUSTRY_SAMPLE_INSUFFICIENT")
        if percentile is None:
            if property_or_utility or mode == "formal":
                missing.append("debt_sector_percentile")
                reasons.append("INDUSTRY_DEBT_PERCENTILE_MISSING")
        elif percentile > 75:
            reasons.append("DEBT_SECTOR_PERCENTILE_ABOVE_THRESHOLD")
        if not property_or_utility and debt is None:
            missing.append("debt_ratio")
        elif not property_or_utility and debt is not None and debt > (85 if mode == "formal" else 95):
            reasons.append("DEBT_RATIO_ABOVE_THRESHOLD")

    hard_fail = any(code in reasons for code in (
        "ROE_BELOW_THRESHOLD", "DEBT_RATIO_ABOVE_THRESHOLD", "DEBT_SECTOR_PERCENTILE_ABOVE_THRESHOLD",
        "FINANCIAL_DEBT_RULE_FAILED", "NET_PROFIT_CONTINUITY_INSUFFICIENT", "CASHFLOW_CONTINUITY_INSUFFICIENT",
    ))
    if hard_fail:
        status = "excluded"
    elif stale:
        status = "stale"
    elif missing or unverified:
        status = "insufficient"
    else:
        status = "matched"
    sort_values = {
        "roe_continuity_periods": verified_periods,
        "cashflow_positive_periods": cash_positive,
        "roe_ttm": roe,
        "net_margin_ttm": _number(_get(item, "net_margin_ttm", "net_margin")),
        "debt_sector_percentile": percentile,
        "report_freshness": age,
    }
    return LensEvaluation(policy.lens_id, policy.lens_version, mode, status, status == "matched", tuple(dict.fromkeys(missing)), tuple(dict.fromkeys(stale)), tuple(dict.fromkeys(unverified)), (), tuple(dict.fromkeys(reasons)), sort_values, data_quality_status=snapshot_quality)


def _bool_false(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return not value
    return _text(value).casefold() in {"false", "0", "no", "不通过", "failed"}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).casefold() in {"1", "true", "yes", "y", "是", "locked", "lock"}


def _trend_evaluation(item: Any, policy: LensPolicy, *, mode: str) -> LensEvaluation:
    missing: list[str] = []
    reasons: list[str] = []
    adjusted = _get(item, "adjusted_price_series", "price_series_adjusted", "adjusted_prices_available")
    mixed = _get(item, "mixed_price_series", "raw_adjusted_mixed", default=False)
    factor_missing = _get(item, "adjustment_factor_missing", default=False)
    if _bool_false(adjusted) or adjusted is None:
        missing.append("adjusted_price_series")
        reasons.append("ADJUSTED_PRICE_REQUIRED")
    if _truthy(mixed) or _truthy(factor_missing):
        missing.append("adjustment_factor")
        reasons.append("PRICE_SERIES_MIXED_OR_FACTOR_MISSING")
    valid_price = _number(_get(item, "valid_price_days_20d", "valid_price_days"))
    valid_volume = _number(_get(item, "valid_volume_days_20d", "valid_volume_days"))
    if valid_price is None:
        prices = _list(_get(item, "prices_20d", "adjusted_prices_20d"))
        valid_price = float(sum(value is not None and _number(value) is not None for value in prices)) if prices else None
    if valid_volume is None:
        volumes = _list(_get(item, "volumes_20d", "volume_20d"))
        valid_volume = float(sum(value is not None and _number(value) is not None for value in volumes)) if volumes else None
    required_days = 15 if mode == "formal" else 10
    if valid_price is None or valid_price < required_days:
        missing.append("valid_price_days_20d")
        reasons.append("TREND_PRICE_WINDOW_INSUFFICIENT")
    if valid_volume is None or valid_volume < required_days:
        missing.append("valid_volume_days_20d")
        reasons.append("TREND_VOLUME_WINDOW_INSUFFICIENT")
    trend20 = _number(_get(item, "trend_20d"))
    trend60 = _number(_get(item, "trend_60d"))
    relative = _number(_get(item, "relative_strength_20d", "relative_industry_strength_20d"))
    median_turnover = _number(_get(item, "median_turnover_value_20d", "turnover_value_median_20d"))
    if trend20 is None:
        missing.append("trend_20d")
    if trend60 is None:
        missing.append("trend_60d")
    if relative is None:
        missing.append("relative_strength_20d")
        if mode == "exploratory":
            reasons.append("RELATIVE_STRENGTH_INSUFFICIENT")
    if median_turnover is None:
        missing.append("median_turnover_value_20d")
    locked_days = _number(_get(item, "price_limit_locked_days_20d", "locked_days_20d", default=0))
    recent_locked = _get(item, "recent_price_limit_locked", "price_limit_locked", default=False)
    max_locked = 3 if mode == "formal" else 6
    if _truthy(recent_locked) or (locked_days is not None and locked_days > max_locked):
        reasons.append("TREND_OBSERVATION_INSUFFICIENT_PRICE_LIMIT")
        missing.append("price_limit_observation")
    tech_quality = _text(_get(item, "technical_fields_quality", "technical_quality_status")).casefold()
    if tech_quality != "verified":
        missing.append("technical_fields_quality")
        reasons.append(f"TECHNICAL_DATA_{tech_quality.upper()}")
    if trend20 is not None and trend20 < (3 if mode == "formal" else 0):
        reasons.append("TREND_20D_BELOW_THRESHOLD")
    if mode == "formal" and trend60 is not None and trend60 < 0:
        reasons.append("TREND_60D_BELOW_THRESHOLD")
    if mode == "formal" and relative is not None and relative < 0:
        reasons.append("RELATIVE_STRENGTH_BELOW_THRESHOLD")
    if median_turnover is not None and median_turnover < (20_000_000 if mode == "formal" else 5_000_000):
        reasons.append("MEDIAN_TURNOVER_BELOW_THRESHOLD")
    hard_fail = any(code in reasons for code in ("TREND_20D_BELOW_THRESHOLD", "TREND_60D_BELOW_THRESHOLD", "RELATIVE_STRENGTH_BELOW_THRESHOLD", "MEDIAN_TURNOVER_BELOW_THRESHOLD"))
    status = "excluded" if hard_fail else ("insufficient" if missing else "matched")
    sort_values = {
        "relative_strength_20d": relative, "trend_20d": trend20, "trend_60d": trend60,
        "median_turnover_value_20d": median_turnover, "volatility": _number(_get(item, "volatility")),
        "max_drawdown_30d": _number(_get(item, "max_drawdown_30d")),
    }
    return LensEvaluation(policy.lens_id, policy.lens_version, mode, status, status == "matched", tuple(dict.fromkeys(missing)), (), (), (), tuple(dict.fromkeys(reasons)), sort_values, data_quality_status=tech_quality)


def _event_rows(item: Any) -> list[Mapping[str, Any]]:
    events = _get(item, "events", "event_facts", default=[])
    rows = [value for value in _list(events) if isinstance(value, Mapping)]
    if rows:
        return rows
    direct = {key: _get(item, key) for key in ("event_title", "event_date", "event_source", "source_tier", "available_at", "code", "source_route_verified", "dedup_digest", "event_type") if _get(item, key) is not None}
    return [direct] if direct else []


def _event_evaluation(item: Any, policy: LensPolicy, *, mode: str, cutoff: date | None) -> LensEvaluation:
    rows = _event_rows(item)
    if not rows:
        return LensEvaluation(policy.lens_id, policy.lens_version, mode, "insufficient", False, ("event_fact",), (), (), (), ("EVENT_FACT_MISSING",), data_quality_status="verified")
    cutoff = cutoff or _date(_get(item, "as_of", "data_date"))
    stale = False
    future = False
    best: Mapping[str, Any] | None = None
    reasons: list[str] = []
    for row in rows:
        code = _text(row.get("code") or _get(item, "code"))
        parent_code = _text(_get(item, "code", "symbol")).zfill(6)
        event_date = _date(row.get("event_date"))
        available = _date(row.get("available_at"))
        if cutoff and available and available > cutoff:
            future = True
            continue
        if cutoff and event_date and event_date > cutoff:
            future = True
            continue
        if not code:
            reasons.append("EVENT_CODE_MISSING")
            continue
        if code.zfill(6) != parent_code:
            reasons.append("EVENT_CODE_MISMATCH")
            continue
        if not available:
            reasons.append("EVENT_AVAILABLE_AT_MISSING")
            continue
        if not event_date:
            reasons.append("EVENT_DATE_MISSING")
            continue
        age = (cutoff - event_date).days if cutoff else 0
        limit = 30 if mode == "formal" else 90
        if age > limit:
            stale = True
            continue
        tier = _number(row.get("source_tier"))
        if tier is None:
            source_name = _text(row.get("event_source") or row.get("source"))
            tier = 1 if any(token in source_name.casefold() for token in ("exchange", "监管", "交易所", "上市公司", "regulator")) else 2
        route_verified = row.get("source_route_verified", row.get("source_route"))
        if route_verified in (None, "", False, "unverified"):
            reasons.append("SOURCE_ROUTE_UNVERIFIED")
            continue
        if row.get("deduped") is False:
            reasons.append("EVENT_NOT_DEDUPED")
            continue
        if mode == "formal" and tier != 1:
            reasons.append("SOURCE_TIER_NOT_FORMAL")
            continue
        if mode == "exploratory" and tier >= 3:
            reasons.append("SOURCE_TIER_UNVERIFIED")
            continue
        best = {**row, "event_age_days": age, "source_tier": int(tier)}
        break
    if future:
        return LensEvaluation(policy.lens_id, policy.lens_version, mode, "excluded", False, (), (), (), (), ("FUTURE_AVAILABLE_AT",), data_quality_status="verified")
    if best is None:
        status = "stale" if stale else "insufficient"
        if stale:
            reasons.append("EVENT_STALE")
        return LensEvaluation(policy.lens_id, policy.lens_version, mode, status, False, ("event_fact",), ("event_date",) if stale else (), (), (), tuple(dict.fromkeys(reasons or ["EVENT_FACT_INSUFFICIENT"])), data_quality_status="verified")
    values = {
        "event_age_days": best.get("event_age_days"), "source_tier": best.get("source_tier"),
        "event_date": _text(best.get("event_date")), "verification_gap_count": len(reasons),
        "event_type_priority": _number(best.get("event_type_priority")) or 0,
    }
    evidence = ({"code": _text(best.get("code") or _get(item, "code")), "event_date": _text(best.get("event_date")), "available_at": _text(best.get("available_at")), "source_tier": best.get("source_tier")},)
    is_partial = mode == "exploratory" and int(best.get("source_tier") or 0) == 2
    if is_partial:
        reasons.append("SOURCE_TIER_PARTIAL")
    return LensEvaluation(policy.lens_id, policy.lens_version, mode, "partial" if is_partial else "matched", not is_partial, (), (), (), (), tuple(dict.fromkeys(reasons)), values, evidence, "partial" if is_partial else "verified")


class SelectionPolicy:
    """Versioned coordinator for Universe, Lens, missing data, and priority."""

    def __init__(self, *, universe_profile: UniverseProfile = DEFAULT_UNIVERSE_PROFILE, lens_policies: Iterable[LensPolicy] = DEFAULT_LENS_POLICIES) -> None:
        self.universe_profile = universe_profile
        self.lens_policies = {item.lens_id: item for item in lens_policies}
        unknown = set(self.lens_policies) - set(LENS_IDS)
        if unknown:
            raise ValueError(f"包含未知 LensPolicy：{', '.join(sorted(unknown))}")
        if set(self.lens_policies) != set(LENS_IDS):
            raise ValueError("SelectionPolicy v1 必须包含三个初始 Lens")

    @property
    def version(self) -> str:
        return SELECTION_POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "universe_profile": self.universe_profile.as_dict(),
            "lens_policies": {key: value.as_dict() for key, value in sorted(self.lens_policies.items())},
        }

    def _evaluate_lens(self, item: Any, lens_id: str, *, mode: str, cutoff: date | None) -> LensEvaluation:
        policy = self.lens_policies[lens_id]
        if lens_id == "quality_research":
            return _quality_evaluation(item, policy, mode=mode, cutoff=cutoff)
        if lens_id == "trend_observation":
            return _trend_evaluation(item, policy, mode=mode)
        return _event_evaluation(item, policy, mode=mode, cutoff=cutoff)

    def evaluate(
        self,
        item: Any,
        *,
        mode: str = "formal",
        lens_ids: Iterable[str] | None = None,
        as_of: str = "",
        availability_cutoff: str = "",
        trading_calendar: Iterable[Any] | None = None,
        manual_focus: bool = False,
        member_status_policy: str = "listed_non_st",
    ) -> SelectionResult:
        if mode not in SELECTION_MODES:
            raise ValueError(f"未知筛选模式：{mode}")
        requested = tuple(dict.fromkeys(_text(value) for value in (lens_ids or LENS_IDS) if _text(value)))
        unknown = set(requested) - set(LENS_IDS)
        if unknown:
            raise KeyError(f"未知 Lens：{', '.join(sorted(unknown))}")
        selection_input = SelectionPolicyInput.from_mapping(item)
        cutoff = _date(availability_cutoff) or _date(as_of)
        universe = evaluate_universe_member(
            selection_input,
            profile=self.universe_profile,
            mode=mode,
            as_of=as_of,
            availability_cutoff=availability_cutoff,
            trading_calendar=trading_calendar,
            member_status_policy=member_status_policy,
        )
        evaluations: list[LensEvaluation] = []
        # Review-only and exploratory runs still evaluate every requested
        # Lens so deterministic matches remain visible even when the Universe
        # layer marks the row stale/insufficient for formal use.  Explicit
        # exclusions are still returned as excluded and are enforced by the
        # caller.
        if universe.status == "eligible" or mode in {"review_only", "exploratory"}:
            evaluations = [self._evaluate_lens(selection_input, lens_id, mode=mode, cutoff=cutoff) for lens_id in requested]
        matched = tuple(item.lens_id for item in evaluations if item.matched)
        # Universe/PIT governance gaps are public selection evidence too.  If
        # they stay nested only under ``universe``, the UI cannot explain why
        # a review-only candidate is not eligible for formal use.
        missing = tuple(
            dict.fromkeys(
                [
                    *universe.missing_fields,
                    *(field for item in evaluations for field in item.missing_fields),
                ]
            )
        )
        stale = tuple(dict.fromkeys(field for item in evaluations for field in item.stale_fields))
        unverified = tuple(dict.fromkeys(field for item in evaluations for field in item.unverified_fields))
        blocking = tuple(
            dict.fromkeys(
                [
                    *universe.blocking_fields,
                    *(field for item in evaluations for field in item.blocking_fields),
                ]
            )
        )
        reasons = list(universe.reason_codes)
        reasons.extend(reason for evaluation in evaluations for reason in evaluation.reason_codes)
        if universe.status != "eligible":
            status = universe.status
        elif mode == "review_only":
            status = "stale"
            reasons.append("REVIEW_ONLY_NOT_FORMAL")
        elif stale:
            status = "stale"
        elif mode == "exploratory":
            status = "exploratory" if matched else ("insufficient" if missing or unverified else "excluded")
        elif matched:
            status = "candidate"
        elif missing or unverified:
            status = "insufficient"
        else:
            status = "excluded"
        record = {
            "code": _text(_get(selection_input, "code", "symbol")),
            "status": status,
            "candidate_status": status,
            "missing_fields": missing,
            "stale_fields": stale,
            "unverified_fields": unverified,
            "blocking_fields": blocking,
            "data_quality_status": _text(_get(selection_input, "snapshot_quality_status", "data_quality_status", "quality_status", default="")),
            "manual_focus": manual_focus,
        }
        priorities: dict[str, PriorityResult] = {}
        with_priorities: list[LensEvaluation] = []
        for evaluation in evaluations:
            priority = DEFAULT_PRIORITY_POLICY.classify({**record, "status": evaluation.status, "sort_values": evaluation.sort_values}, lens_id=evaluation.lens_id, manual_focus=manual_focus)
            priorities[evaluation.lens_id] = priority
            with_priorities.append(LensEvaluation(**{**evaluation.__dict__, "priority": priority}))
        evaluations = with_priorities
        facts = dict(universe.facts)
        for field_name in ("industry_classification_id", "industry_classification_version", "industry_code", "industry_effective_at", "peer_set_digest", "raw_price", "adjusted_price", "adjustment_factor", "corporate_action_fact_id", "adjustment_method", "adjustment_version", "price_limit_locked", "price_limit_direction"):
            value = _get(selection_input, field_name)
            if value is not None:
                facts[field_name] = value
        digest = stable_hash({
            "code": _text(_get(selection_input, "code", "symbol")).zfill(6),
            "mode": mode,
            "universe": universe.as_dict(),
            "lens_results": [evaluation.as_dict() for evaluation in evaluations],
            "selection_policy_version": self.version,
            "member_status_policy": member_status_policy,
            "selection_input": selection_input.as_dict(),
        })
        return SelectionResult(
            code=_text(_get(selection_input, "code", "symbol")).zfill(6), mode=mode, status=status,
            candidate_status=status, exploratory_only=mode != "formal", universe=universe,
            lens_results=tuple(evaluations), matched_lenses=matched, missing_fields=missing,
            stale_fields=stale, unverified_fields=unverified, blocking_fields=blocking,
            reason_codes=tuple(dict.fromkeys(reasons)), priority_by_lens=priorities,
            facts=facts, candidate_digest=digest,
            member_status_policy=member_status_policy,
            selection_input=selection_input.as_dict(),
        )

    def evaluate_many(self, items: Iterable[Any], **kwargs: Any) -> SelectionBatch:
        results = tuple(sorted((self.evaluate(item, **kwargs) for item in items), key=lambda result: result.code))
        return SelectionBatch(
            mode=kwargs.get("mode", "formal"),
            results=results,
            candidate_set_digest=stable_hash([item.candidate_digest for item in results]),
        )

    def evaluate_snapshot(self, item: Any, **kwargs: Any) -> SelectionResult:
        return self.evaluate(item, **kwargs)

    def select(self, items: Iterable[Any], **kwargs: Any) -> SelectionBatch:
        return self.evaluate_many(items, **kwargs)

    def formalize_exploratory_proposal(self, proposal: ExploratoryProposal, item: Any, *, actor: str, **kwargs: Any) -> SelectionResult:
        normalized_actor = _text(actor).casefold()
        if not normalized_actor.startswith(("human:", "user:")):
            raise ValueError("ExploratoryProposal 只能由 human:<id> 或 user:<id> 复核")
        if proposal.status != "pending_human_review":
            raise ValueError("ExploratoryProposal 已不在待复核状态")
        if _text(_get(item, "code", "symbol")).zfill(6) != proposal.code.zfill(6):
            raise ValueError("ExploratoryProposal code 与复核快照不一致")
        return self.evaluate(item, mode="formal", lens_ids=(proposal.proposed_lens_id,), **kwargs)


DEFAULT_SELECTION_POLICY = SelectionPolicy()


def get_lens_policy(lens_id: str, *, mode: str = "formal") -> LensPolicy:
    """Return one immutable Lens policy for the requested execution mode."""

    wanted = _text(lens_id)
    if wanted not in DEFAULT_SELECTION_POLICY.lens_policies:
        raise KeyError(f"未知 Lens：{lens_id}")
    if mode not in SELECTION_MODES:
        raise ValueError(f"未知筛选模式：{mode}")
    return replace(DEFAULT_SELECTION_POLICY.lens_policies[wanted], mode=mode)


def evaluate_candidate_lens(item: Any, lens_id: str, *, mode: str = "formal", **kwargs: Any) -> LensEvaluation:
    result = DEFAULT_SELECTION_POLICY.evaluate(item, mode=mode, lens_ids=(lens_id,), **kwargs)
    return result.lens_results[0]


def evaluate_selection(item: Any, *, mode: str = "formal", **kwargs: Any) -> SelectionResult:
    return DEFAULT_SELECTION_POLICY.evaluate(item, mode=mode, **kwargs)


__all__ = [
    "DEFAULT_LENS_POLICIES",
    "DEFAULT_SELECTION_POLICY",
    "EVENT_POLICY",
    "ExploratoryProposal",
    "LENS_IDS",
    "LENS_POLICY_VERSION",
    "LensEvaluation",
    "LensPolicy",
    "QUALITY_POLICY",
    "SELECTION_MODES",
    "SELECTION_INPUT_VERSION",
    "SELECTION_POLICY_VERSION",
    "TREND_POLICY",
    "SelectionBatch",
    "SelectionPolicy",
    "SelectionPolicyInput",
    "SelectionResult",
    "evaluate_selection",
    "evaluate_candidate_lens",
    "get_lens_policy",
]
