"""Frozen missing-data rules for Phase 0.5 selection.

The policy is intentionally small and deterministic.  It records the reason
for a data gap instead of manufacturing a neutral numeric value.  The
selection layer owns the domain-specific field lists; this module owns the
common consequence of a gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


MISSING_DATA_POLICY_VERSION = "missing_data.v1"
MISSING_STATUSES = frozenset({"matched", "insufficient", "stale", "excluded"})


def _texts(values: Iterable[object] | None) -> tuple[str, ...]:
    result: list[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


@dataclass(frozen=True)
class MissingDataDecision:
    """Consequence of missing, stale, or unverified fields for one Lens."""

    mode: str
    status: str
    missing_fields: tuple[str, ...] = ()
    stale_fields: tuple[str, ...] = ()
    unverified_fields: tuple[str, ...] = ()
    blocking_fields: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    priority_downgraded: bool = False
    contract_version: str = MISSING_DATA_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.status not in MISSING_STATUSES:
            raise ValueError(f"未知缺失数据状态：{self.status}")
        if self.mode not in {"formal", "exploratory", "review_only"}:
            raise ValueError(f"未知筛选模式：{self.mode}")
        if self.contract_version != MISSING_DATA_POLICY_VERSION:
            raise ValueError(f"不支持的缺失数据策略版本：{self.contract_version}")
        for name in ("missing_fields", "stale_fields", "unverified_fields", "blocking_fields", "reason_codes"):
            object.__setattr__(self, name, _texts(getattr(self, name)))

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "mode": self.mode,
            "status": self.status,
            "missing_fields": list(self.missing_fields),
            "stale_fields": list(self.stale_fields),
            "unverified_fields": list(self.unverified_fields),
            "blocking_fields": list(self.blocking_fields),
            "reason_codes": list(self.reason_codes),
            "priority_downgraded": self.priority_downgraded,
        }


@dataclass(frozen=True)
class MissingDataPolicy:
    """Field-class policy shared by all Lens implementations."""

    critical_fields: tuple[str, ...] = ()
    supporting_fields: tuple[str, ...] = ()
    optional_fields: tuple[str, ...] = ()
    version: str = MISSING_DATA_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.version != MISSING_DATA_POLICY_VERSION:
            raise ValueError(f"不支持的缺失数据策略版本：{self.version}")
        for name in ("critical_fields", "supporting_fields", "optional_fields"):
            object.__setattr__(self, name, _texts(getattr(self, name)))

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "critical_fields": list(self.critical_fields),
            "supporting_fields": list(self.supporting_fields),
            "optional_fields": list(self.optional_fields),
        }

    def classify(
        self,
        *,
        mode: str,
        missing_fields: Iterable[object] = (),
        stale_fields: Iterable[object] = (),
        unverified_fields: Iterable[object] = (),
        reason_codes: Iterable[object] = (),
    ) -> MissingDataDecision:
        """Classify gaps without changing their values.

        Critical gaps block a Formal match.  Exploratory and review-only
        results remain displayable, but retain the gap status and never become
        a Formal candidate through this function.
        """

        if mode not in {"formal", "exploratory", "review_only"}:
            raise ValueError(f"未知筛选模式：{mode}")
        missing = _texts(missing_fields)
        stale = _texts(stale_fields)
        unverified = _texts(unverified_fields)
        reasons = list(_texts(reason_codes))
        critical = set(self.critical_fields)
        supporting = set(self.supporting_fields)
        optional = set(self.optional_fields)
        blocking = tuple(item for item in (*missing, *stale, *unverified) if item in critical)
        material_missing = tuple(item for item in missing if item not in optional)
        material_unverified = tuple(item for item in unverified if item not in optional)
        material_stale = tuple(item for item in stale if item not in optional)
        if blocking:
            status = "stale" if any(item in stale for item in blocking) else "insufficient"
            reasons.append("CRITICAL_DATA_GAP")
        elif material_stale:
            status = "stale"
            reasons.append("STALE_SUPPORTING_DATA")
        elif material_missing or material_unverified:
            status = "insufficient"
            if any(item in supporting for item in (*material_missing, *material_unverified)):
                reasons.append("SUPPORTING_DATA_GAP")
            else:
                reasons.append("DATA_GAP")
        else:
            status = "matched"
        return MissingDataDecision(
            mode=mode,
            status=status,
            missing_fields=missing,
            stale_fields=stale,
            unverified_fields=unverified,
            blocking_fields=blocking,
            reason_codes=tuple(dict.fromkeys(reasons)),
            priority_downgraded=bool(material_missing or material_stale or material_unverified),
        )


DEFAULT_MISSING_DATA_POLICY = MissingDataPolicy()


def classify_missing_data(
    *,
    mode: str,
    critical_fields: Iterable[object] = (),
    supporting_fields: Iterable[object] = (),
    optional_fields: Iterable[object] = (),
    missing_fields: Iterable[object] = (),
    stale_fields: Iterable[object] = (),
    unverified_fields: Iterable[object] = (),
    reason_codes: Iterable[object] = (),
) -> MissingDataDecision:
    """Convenience wrapper used by policy adapters and tests."""

    return MissingDataPolicy(
        critical_fields=_texts(critical_fields),
        supporting_fields=_texts(supporting_fields),
        optional_fields=_texts(optional_fields),
    ).classify(
        mode=mode,
        missing_fields=missing_fields,
        stale_fields=stale_fields,
        unverified_fields=unverified_fields,
        reason_codes=reason_codes,
    )


__all__ = [
    "DEFAULT_MISSING_DATA_POLICY",
    "MISSING_DATA_POLICY_VERSION",
    "MissingDataDecision",
    "MissingDataPolicy",
    "classify_missing_data",
]
