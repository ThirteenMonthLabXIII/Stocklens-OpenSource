"""Explainable, within-Lens priority ordering for Phase 0.5.

There is deliberately no numeric score in this module.  Priority is a
lexicographic ordering of discrete data-quality states followed by a fixed
Lens-specific whitelist and the normalized stock code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cmp_to_key
import math
from typing import Any, Iterable, Mapping


PRIORITY_POLICY_VERSION = "priority.v1"
PRIORITY_TIERS = ("重点研究", "一般候选", "证据不足", "存在冲突", "过期候选")
PRIORITY_TIER_ORDER = {name: index for index, name in enumerate(PRIORITY_TIERS)}
FORBIDDEN_PRIORITY_FIELDS = frozenset({"combined_score", "overall_score", "cross_lens_rank", "ai_confidence", "final_rating"})

LENS_SORT_FIELDS: dict[str, tuple[str, ...]] = {
    "quality_research": (
        "roe_continuity_periods", "cashflow_positive_periods", "roe_ttm", "net_margin_ttm",
        "debt_sector_percentile", "report_freshness", "code",
    ),
    "trend_observation": (
        "relative_strength_20d", "trend_20d", "trend_60d", "median_turnover_value_20d",
        "volatility", "max_drawdown_30d", "code",
    ),
    "event_verification": (
        "event_age_days", "source_tier", "event_date", "verification_gap_count",
        "event_type_priority", "code",
    ),
}
LENS_SORT_DIRECTIONS: dict[str, dict[str, str]] = {
    "quality_research": {"report_freshness": "asc", "debt_sector_percentile": "asc"},
    "trend_observation": {"volatility": "asc", "max_drawdown_30d": "asc"},
    "event_verification": {"event_age_days": "asc", "source_tier": "asc", "event_date": "desc", "verification_gap_count": "asc"},
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _get(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping) and name in record:
        return record.get(name)
    value = getattr(record, name, None)
    if value is not None:
        return value
    for container_name in ("sort_values", "facts", "values", "score_breakdown"):
        container = record.get(container_name) if isinstance(record, Mapping) else getattr(record, container_name, None)
        if isinstance(container, Mapping) and name in container:
            return container.get(name)
    return default


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _code(record: Any) -> str:
    return _text(_get(record, "code", _get(record, "symbol", ""))).zfill(6)


def _fields(record: Any, name: str) -> tuple[str, ...]:
    value = _get(record, name, ())
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, Iterable) or isinstance(value, Mapping):
        return ()
    return tuple(dict.fromkeys(_text(item) for item in value if _text(item)))


@dataclass(frozen=True)
class PriorityResult:
    lens_id: str
    priority_tier: str
    priority_explanation: tuple[str, ...]
    priority_key: tuple[Any, ...]
    data_quality_order: int
    completeness_order: int
    freshness_order: int
    conflict_order: int
    missing_field_count: int
    manual_focus: bool = False
    contract_version: str = PRIORITY_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.priority_tier not in PRIORITY_TIERS:
            raise ValueError(f"未知候选优先级层：{self.priority_tier}")
        if self.contract_version != PRIORITY_POLICY_VERSION:
            raise ValueError(f"不支持的优先级策略版本：{self.contract_version}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "lens_id": self.lens_id,
            "priority_tier": self.priority_tier,
            "priority_explanation": list(self.priority_explanation),
            "priority_key": list(self.priority_key),
            "data_quality_order": self.data_quality_order,
            "completeness_order": self.completeness_order,
            "freshness_order": self.freshness_order,
            "conflict_order": self.conflict_order,
            "missing_field_count": self.missing_field_count,
            "manual_focus": self.manual_focus,
        }


@dataclass(frozen=True)
class PriorityPolicy:
    lens_sort_fields: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(LENS_SORT_FIELDS))
    version: str = PRIORITY_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.version != PRIORITY_POLICY_VERSION:
            raise ValueError(f"不支持的优先级策略版本：{self.version}")
        normalized = {str(key): tuple(value) for key, value in self.lens_sort_fields.items()}
        for lens_id, fields in normalized.items():
            forbidden = set(fields) & FORBIDDEN_PRIORITY_FIELDS
            if forbidden:
                raise ValueError(f"排序白名单包含禁止字段：{', '.join(sorted(forbidden))}")
        object.__setattr__(self, "lens_sort_fields", normalized)

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "lens_sort_fields": {key: list(value) for key, value in self.lens_sort_fields.items()}}

    def validate_sort_fields(self, lens_id: str, sort_fields: Iterable[str] | None = None) -> tuple[str, ...]:
        allowed = self.lens_sort_fields.get(str(lens_id))
        if allowed is None:
            raise KeyError(f"未知 Lens：{lens_id}")
        requested = tuple(_text(item) for item in (sort_fields or ()) if _text(item))
        unknown = set(requested) - set(allowed)
        if unknown:
            raise ValueError(f"{lens_id} 不允许使用排序字段：{', '.join(sorted(unknown))}")
        return requested or allowed

    def classify(self, record: Any, *, lens_id: str, manual_focus: bool | None = None) -> PriorityResult:
        missing = _fields(record, "missing_fields")
        stale = _fields(record, "stale_fields")
        unverified = _fields(record, "unverified_fields")
        blocking = _fields(record, "blocking_fields")
        conflicts = _fields(record, "conflicts")
        status = _text(_get(record, "status", _get(record, "candidate_status", ""))).casefold()
        quality = _text(_get(record, "data_quality_status", _get(record, "quality_status", "verified"))).casefold()
        focus = bool(_get(record, "manual_focus", False)) if manual_focus is None else bool(manual_focus)
        if conflicts or status in {"conflicted", "conflict"}:
            tier = "存在冲突"
            explanation = ("存在互相冲突且有效的证据",)
            conflict_order = 0
        elif stale or status in {"stale", "expired"}:
            tier = "过期候选"
            explanation = ("事实超过当前 Lens freshness window",)
            conflict_order = 1
        elif missing or unverified or blocking or status in {"insufficient", "review_only", "exploratory", "partial"} or quality not in {"verified", ""}:
            tier = "证据不足"
            explanation = ("关键字段、样本或事实质量不足",)
            conflict_order = 2
        elif focus:
            tier = "重点研究"
            explanation = ("Formal、数据完整且满足人工重点条件",)
            conflict_order = 3
        else:
            tier = "一般候选"
            explanation = ("Formal Lens matched，但未满足人工重点条件",)
            conflict_order = 3
        quality_order = {"verified": 0, "partial": 1, "unverified": 2, "blocked": 3}.get(quality, 2)
        completeness_order = 0 if not (missing or unverified or blocking) else 1
        freshness_order = 0 if not stale else 1
        key = (
            PRIORITY_TIER_ORDER[tier],
            quality_order,
            completeness_order,
            freshness_order,
            conflict_order,
            len(set((*missing, *stale, *unverified, *blocking))),
            0 if focus else 1,
            _code(record),
        )
        return PriorityResult(
            lens_id=str(lens_id),
            priority_tier=tier,
            priority_explanation=explanation,
            priority_key=key,
            data_quality_order=quality_order,
            completeness_order=completeness_order,
            freshness_order=freshness_order,
            conflict_order=conflict_order,
            missing_field_count=len(set((*missing, *stale, *unverified, *blocking))),
            manual_focus=focus,
        )

    def sort_key(self, record: Any, *, lens_id: str, sort_fields: Iterable[str] | None = None) -> tuple[Any, ...]:
        """Return a stable key; missing values are always placed last."""

        fields = self.validate_sort_fields(lens_id, sort_fields)
        priority = self.classify(record, lens_id=lens_id)
        values: list[Any] = [priority.priority_key[:-1]]
        for field_name in fields:
            if field_name == "code":
                values.append((0, _code(record)))
                continue
            value = _get(record, field_name)
            number = _number(value)
            if number is not None:
                # Sort-field direction is applied by sort_records; this key
                # only supplies a total ordering and missing-last marker.
                descending = LENS_SORT_DIRECTIONS.get(lens_id, {}).get(field_name, "desc") == "desc"
                values.append((0, -number if descending else number))
            else:
                values.append((1, _text(value)))
        values.append(_code(record))
        return tuple(values)

    def sort_records(self, records: Iterable[Any], *, lens_id: str, sort_fields: Iterable[str] | None = None) -> list[Any]:
        fields = self.validate_sort_fields(lens_id, sort_fields)

        def compare(left: Any, right: Any) -> int:
            left_priority = self.classify(left, lens_id=lens_id).priority_key
            right_priority = self.classify(right, lens_id=lens_id).priority_key
            # The normalized code is the final tie-breaker, after the
            # user-visible Lens fields.  Do not let it short-circuit the
            # Lens-specific ordering below.
            if left_priority[:-1] != right_priority[:-1]:
                return -1 if left_priority[:-1] < right_priority[:-1] else 1
            for field_name in fields:
                if field_name == "code":
                    left_value, right_value = _code(left), _code(right)
                    if left_value != right_value:
                        return -1 if left_value < right_value else 1
                    continue
                left_value, right_value = _get(left, field_name), _get(right, field_name)
                left_number, right_number = _number(left_value), _number(right_value)
                left_missing, right_missing = left_number is None, right_number is None
                if left_missing != right_missing:
                    return 1 if left_missing else -1
                if left_missing:
                    left_text, right_text = _text(left_value), _text(right_value)
                    if left_text != right_text:
                        return -1 if left_text < right_text else 1
                    continue
                if left_number != right_number:
                    descending = LENS_SORT_DIRECTIONS.get(lens_id, {}).get(field_name, "desc") == "desc"
                    return (-1 if left_number > right_number else 1) if descending else (-1 if left_number < right_number else 1)
            left_code, right_code = _code(left), _code(right)
            return (left_code > right_code) - (left_code < right_code)

        return sorted(list(records), key=cmp_to_key(compare))

    def sort_by_lens(self, records: Iterable[Any], *, sort_fields_by_lens: Mapping[str, Iterable[str]] | None = None) -> dict[str, list[Any]]:
        values = list(records)
        return {
            lens_id: self.sort_records(
                [record for record in values if lens_id in set(_fields(record, "lens_ids")) or _text(_get(record, "lens_id")) == lens_id],
                lens_id=lens_id,
                sort_fields=(sort_fields_by_lens or {}).get(lens_id),
            )
            for lens_id in self.lens_sort_fields
        }


DEFAULT_PRIORITY_POLICY = PriorityPolicy()


def priority_for(record: Any, *, lens_id: str, manual_focus: bool | None = None) -> PriorityResult:
    return DEFAULT_PRIORITY_POLICY.classify(record, lens_id=lens_id, manual_focus=manual_focus)


__all__ = [
    "DEFAULT_PRIORITY_POLICY",
    "FORBIDDEN_PRIORITY_FIELDS",
    "LENS_SORT_FIELDS",
    "PRIORITY_POLICY_VERSION",
    "PRIORITY_TIERS",
    "PriorityPolicy",
    "PriorityResult",
    "priority_for",
]
