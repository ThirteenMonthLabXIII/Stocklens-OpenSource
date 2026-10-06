"""Deterministic Candidate Lens matching for the Phase 1 screening layer.

This module deliberately knows nothing about Agents, prompts, models, or
trade decisions.  A Lens is only an auditable view over the same immutable
snapshot that the local eligibility gate receives.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from stock_tool.core.evidence_contracts import (
    CandidateLens,
    DEFAULT_CANDIDATE_LENSES,
)
from stock_tool.core.screening_contract import stable_hash


LENS_MATCH_STATUSES = frozenset({"matched", "insufficient", "excluded"})


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _field_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _as_text(value).casefold()).strip("_")


def _reason_for_field(prefix: str, field_name: str) -> str:
    return f"{prefix}_{_field_key(field_name).upper()}"


def _mapping_value(snapshot: Any, field_name: str, overrides: Mapping[str, Any]) -> Any:
    if field_name in overrides:
        return overrides[field_name]
    if isinstance(snapshot, Mapping):
        value = snapshot.get(field_name)
    else:
        value = getattr(snapshot, field_name, None)
    if value is not None:
        return value
    # report_date belongs to financial evidence.  It is kept separate from
    # data_date so a missing financial report period is not silently replaced
    # by the quote date.
    aliases = {
        "event_available_at": ("available_at",),
        "event_source": ("source",),
    }
    for alias in aliases.get(field_name, ()):
        if alias in overrides:
            return overrides[alias]
        if isinstance(snapshot, Mapping):
            value = snapshot.get(alias)
        else:
            value = getattr(snapshot, alias, None)
        if value is not None:
            return value
    return None


def _missing_markers(snapshot: Any) -> set[str]:
    value = snapshot.get("missing_fields") if isinstance(snapshot, Mapping) else getattr(snapshot, "missing_fields", "")
    if isinstance(value, str):
        return {_field_key(item) for item in value.split(",") if _as_text(item)}
    if isinstance(value, Iterable):
        return {_field_key(item) for item in value if _as_text(item)}
    return set()


def _stale_markers(snapshot: Any, overrides: Mapping[str, Any]) -> set[str]:
    value = overrides.get("stale_fields")
    if value is None:
        value = snapshot.get("stale_fields") if isinstance(snapshot, Mapping) else getattr(snapshot, "stale_fields", "")
    if isinstance(value, str):
        return {_field_key(item) for item in value.split(",") if _as_text(item)}
    if isinstance(value, Iterable):
        return {_field_key(item) for item in value if _as_text(item)}
    return set()


def _quality_markers(snapshot: Any, overrides: Mapping[str, Any]) -> dict[str, str]:
    value = overrides.get("__quality_status_by_field")
    if not isinstance(value, Mapping):
        return {}
    return {
        _field_key(field_name): _as_text(status).casefold()
        for field_name, status in value.items()
        if _as_text(status) and _as_text(status).casefold() != "verified"
    }


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(value: Any) -> datetime | None:
    text = _as_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _compare(value: Any, op: str, expected: Any) -> bool:
    left = _numeric(value)
    right = _numeric(expected)
    if left is not None and right is not None:
        if op == ">=":
            return left >= right
        if op == ">":
            return left > right
        if op == "<=":
            return left <= right
        if op == "<":
            return left < right
        if op == "==":
            return left == right
    if op == "==":
        return value == expected
    if op == "in":
        return value in expected if isinstance(expected, (list, tuple, set, frozenset)) else False
    return False


def _hard_filter_items(hard_filters: Mapping[str, Any]) -> Iterable[tuple[str, str, Any]]:
    for field_name, rule in hard_filters.items():
        if field_name in {"data_quality", "st_status"}:
            continue
        if isinstance(rule, Mapping):
            for op, expected in rule.items():
                yield str(field_name), str(op), expected
        else:
            yield str(field_name), "==", rule


@dataclass(frozen=True)
class LensEvaluation:
    """The local, deterministic result of evaluating one Lens."""

    lens_id: str
    lens_version: str
    status: str
    required_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    stale_fields: tuple[str, ...] = ()
    evidence_fields: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in LENS_MATCH_STATUSES:
            raise ValueError(f"不支持的 Lens 评价状态：{self.status}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "lens_id": self.lens_id,
            "lens_version": self.lens_version,
            "status": self.status,
            "required_fields": list(self.required_fields),
            "missing_fields": list(self.missing_fields),
            "stale_fields": list(self.stale_fields),
            "evidence_fields": list(self.evidence_fields),
            "reason_codes": list(self.reason_codes),
        }


def evaluate_candidate_lens(
    snapshot: Any,
    lens: CandidateLens,
    *,
    data_quality_status: str = "verified",
    values: Mapping[str, Any] | None = None,
    availability_cutoff: str = "",
) -> LensEvaluation:
    """Evaluate one Lens without coercing a missing value to zero."""

    overrides = values or {}
    missing_markers = _missing_markers(snapshot)
    stale_markers = _stale_markers(snapshot, overrides)
    quality_markers = _quality_markers(snapshot, overrides)
    required = tuple(lens.required_fields)
    missing_values = [
        field
        for field in required
        if _field_key(field) in missing_markers or _is_missing(_mapping_value(snapshot, field, overrides))
    ]
    stale = tuple(field for field in required if _field_key(field) in stale_markers)
    unverified = tuple(field for field in required if _field_key(field) in quality_markers)
    reasons: list[str] = []
    if data_quality_status != "verified":
        reasons.append(f"DATA_QUALITY_{_field_key(data_quality_status).upper()}")

    name = _mapping_value(snapshot, "name", overrides)
    st_status = _as_text(name).upper().startswith(("ST", "*ST", "退"))
    if st_status:
        reasons.append("EXCLUDED_ST")
        return LensEvaluation(
            lens.lens_id,
            lens.lens_version,
            "excluded",
            required,
            tuple(missing_values),
            stale,
            tuple(lens.evidence_fields),
            tuple(dict.fromkeys(reasons)),
        )

    if missing_values:
        reasons.extend(_reason_for_field("MISSING", field) for field in missing_values)
    if stale:
        reasons.extend(_reason_for_field("STALE", field) for field in stale)
    if unverified:
        reasons.extend(
            f"FIELD_QUALITY_{quality_markers[_field_key(field)].upper()}_{_field_key(field).upper()}"
            for field in unverified
        )

    configured_quality = lens.hard_filters.get("data_quality")
    if configured_quality and data_quality_status != configured_quality:
        reasons.append("DATA_QUALITY_NOT_VERIFIED")

    failed_rule = False
    for field_name, op, expected in _hard_filter_items(lens.hard_filters):
        value = _mapping_value(snapshot, field_name, overrides)
        if _is_missing(value) or _field_key(field_name) in missing_markers:
            if field_name not in missing_values:
                missing_values.append(field_name)
                reasons.append(_reason_for_field("MISSING", field_name))
            continue
        if not _compare(value, op, expected):
            failed_rule = True
            reasons.append(f"LENS_{_field_key(lens.lens_id).upper()}_FILTER_{_field_key(field_name).upper()}")

    if lens.exclusion_rules.get("future_available_at") == "invalidated":
        available_at = _parse_time(_mapping_value(snapshot, "available_at", overrides))
        cutoff = _parse_time(availability_cutoff)
        if available_at is not None and cutoff is not None and available_at > cutoff:
            failed_rule = True
            reasons.append("FUTURE_AVAILABLE_AT")

    missing = tuple(dict.fromkeys(missing_values))
    if (
        missing
        or stale
        or unverified
        or data_quality_status != "verified"
        or configured_quality not in (None, data_quality_status)
    ):
        status = "insufficient"
    elif failed_rule:
        status = "excluded"
    else:
        status = "matched"
        reasons.append(f"LENS_{_field_key(lens.lens_id).upper()}_MATCH")
    return LensEvaluation(
        lens.lens_id,
        lens.lens_version,
        status,
        required,
        tuple(dict.fromkeys(missing)),
        tuple(dict.fromkeys(stale)),
        tuple(lens.evidence_fields),
        tuple(dict.fromkeys(reasons)),
    )


def evaluate_candidate_lenses(
    snapshot: Any,
    *,
    lens_ids: Iterable[str] | None = None,
    data_quality_status: str = "verified",
    values_by_lens: Mapping[str, Mapping[str, Any]] | None = None,
    availability_cutoff: str = "",
) -> tuple[LensEvaluation, ...]:
    """Evaluate the stable default Lens order for one snapshot."""

    requested = set(str(item).strip() for item in lens_ids or () if str(item).strip())
    lenses = tuple(
        lens for lens in DEFAULT_CANDIDATE_LENSES
        if not requested or lens.lens_id in requested
    )
    unknown = requested - {lens.lens_id for lens in lenses}
    if unknown:
        raise KeyError(f"未知 CandidateLens：{', '.join(sorted(unknown))}")
    values_by_lens = values_by_lens or {}
    return tuple(
        evaluate_candidate_lens(
            snapshot,
            lens,
            data_quality_status=data_quality_status,
            values=values_by_lens.get(lens.lens_id),
            availability_cutoff=availability_cutoff,
        )
        for lens in lenses
    )


def _sort_number(value: Any, *, descending: bool) -> float:
    numeric = _numeric(value)
    if numeric is None:
        return math.inf
    return -numeric if descending else numeric


def lens_sort_key(candidate: Any, lens_id: str) -> tuple[Any, ...]:
    """Stable within-Lens sort; ties always resolve by normalized code."""

    values = getattr(candidate, "sort_values", {}) or {}
    code = _as_text(getattr(getattr(candidate, "quote", None), "code", "")).zfill(6)
    if lens_id == "quality_research":
        return (
            _sort_number(values.get("roe"), descending=True),
            _sort_number(values.get("net_margin"), descending=True),
            _sort_number(values.get("debt_ratio"), descending=False),
            code,
        )
    if lens_id == "trend_observation":
        return (
            _sort_number(values.get("trend_20d"), descending=True),
            _sort_number(values.get("trend_60d"), descending=True),
            _sort_number(values.get("volume_ratio"), descending=True),
            code,
        )
    if lens_id == "event_verification":
        # ISO timestamps sort lexicographically when normalized.  Reverse
        # order is achieved by keeping the value in a separate group sort.
        event_at = _as_text(values.get("event_date") or values.get("event_available_at"))
        return (event_at, code)
    return (code,)


def candidate_digest(
    *,
    snapshot: Any,
    data_snapshot_id: str,
    lens_ids: Iterable[str],
    eligibility_status: str,
    reason_codes: Iterable[str],
    missing_fields: Iterable[str],
    stale_fields: Iterable[str],
    fact_sources: Mapping[str, str],
    sort_values: Mapping[str, Any],
    lens_versions: Mapping[str, str] | None = None,
) -> str:
    """Create a replay digest from observable candidate evidence only."""

    payload = {
        "code": _as_text(getattr(snapshot, "code", "") if not isinstance(snapshot, Mapping) else snapshot.get("code")).zfill(6),
        "name": _as_text(getattr(snapshot, "name", "") if not isinstance(snapshot, Mapping) else snapshot.get("name")),
        "data_snapshot_id": str(data_snapshot_id or ""),
        "lens_ids": sorted(str(item) for item in lens_ids),
        "eligibility_status": str(eligibility_status),
        "reason_codes": sorted(str(item) for item in reason_codes),
        "missing_fields": sorted(str(item) for item in missing_fields),
        "stale_fields": sorted(str(item) for item in stale_fields),
        "fact_sources": dict(sorted((str(k), str(v)) for k, v in fact_sources.items())),
        "sort_values": dict(sorted((str(k), v) for k, v in sort_values.items())),
        "lens_versions": dict(sorted((str(k), str(v)) for k, v in (lens_versions or {}).items())),
    }
    return stable_hash(payload)


__all__ = [
    "LENS_MATCH_STATUSES",
    "LensEvaluation",
    "candidate_digest",
    "evaluate_candidate_lens",
    "evaluate_candidate_lenses",
    "lens_sort_key",
]
