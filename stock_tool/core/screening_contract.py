"""Auditable execution contracts shared by screening and research surfaces."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from typing import Any, Iterable


SCREENING_STATUSES = {
    "verified",
    "partial",
    "unverified",
    "blocked",
    "review_only",
    "exploratory",
}
MISSING_DATA_POLICY_VERSION = "v1"


@dataclass(frozen=True)
class DataQualityAssessment:
    """The data contract result for one screening input snapshot."""

    status: str
    required_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    blocking_fields: tuple[str, ...] = ()
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EligibilityDecision:
    """Auditable result of the qualification gate before ranking."""

    eligibility_status: str
    required_fields: tuple[str, ...] = ()
    missing_required_fields: tuple[str, ...] = ()
    risk_vetoes: tuple[str, ...] = ()
    data_quality_status: str = "verified"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_eligibility_decision(
    *,
    required_fields: Iterable[str] = (),
    available_fields: Iterable[str] = (),
    risk_vetoes: Iterable[str] = (),
    data_quality_status: str = "verified",
) -> EligibilityDecision:
    required = tuple(dict.fromkeys(str(item) for item in required_fields if str(item).strip()))
    available = {str(item) for item in available_fields}
    missing = tuple(field for field in required if field not in available)
    vetoes = tuple(dict.fromkeys(str(item) for item in risk_vetoes if str(item).strip()))
    status = "eligible" if not missing and not vetoes and data_quality_status == "verified" else "ineligible"
    return EligibilityDecision(status, required, missing, vetoes, data_quality_status)


def stable_hash(payload: Any) -> str:
    """Return a deterministic hash for an auditable JSON-compatible payload."""

    if is_dataclass(payload):
        payload = asdict(payload)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def condition_hash(spec: Any) -> str:
    """Hash executable conditions without making display text part of semantics."""

    payload = asdict(spec) if is_dataclass(spec) else spec
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.pop("raw_text", None)
    return stable_hash(payload)


def snapshot_asof(snapshots: Iterable[Any]) -> str:
    values = [str(getattr(item, "updated_at", "") or "") for item in snapshots]
    return max(values, default="")


def snapshot_id(snapshots: Iterable[Any], *, data_source: str = "") -> str:
    rows = []
    for item in snapshots:
        if is_dataclass(item):
            payload = asdict(item)
        elif hasattr(item, "__dict__"):
            payload = dict(vars(item))
        else:
            payload = {
                "updated_at": str(getattr(item, "updated_at", "") or ""),
                "data_date": str(getattr(item, "data_date", "") or ""),
            }
        rows.append({"code": str(getattr(item, "code", "") or "").zfill(6), "snapshot": payload})
    rows.sort(key=lambda row: row["code"])
    return stable_hash({"data_source": data_source, "snapshots": rows})


def _snapshot_has_field(snapshot: Any, field: str) -> bool:
    if field == "main_5d_yi":
        # Factor snapshots intentionally do not carry money-flow data.
        return False
    value = getattr(snapshot, field, None)
    missing = {
        part.strip()
        for part in str(getattr(snapshot, "missing_fields", "") or "").split(",")
        if part.strip()
    }
    return value is not None and field not in missing


def required_fields_for_filter(filters: Any) -> tuple[str, ...]:
    fields: list[str] = []
    if getattr(filters, "max_pe", None) is not None and not getattr(filters, "allow_missing_pe", True):
        fields.append("pe")
    if getattr(filters, "max_pe_ttm", None) is not None and not getattr(filters, "allow_missing_pe", True):
        fields.append("pe_ttm")
    if getattr(filters, "min_roe", None) is not None:
        fields.append("roe")
    if getattr(filters, "max_debt_ratio", None) is not None:
        fields.append("debt_ratio")
    if getattr(filters, "min_net_margin", None) is not None:
        fields.append("net_margin")
    if getattr(filters, "min_gross_margin", None) is not None:
        fields.append("gross_margin")
    if getattr(filters, "min_revenue_growth", None) is not None:
        fields.append("revenue_growth")
    if getattr(filters, "min_profit_growth", None) is not None:
        fields.append("profit_growth")
    if getattr(filters, "max_pb", None) is not None:
        fields.append("pb")
    if getattr(filters, "min_market_cap_yi", None) is not None or getattr(filters, "max_market_cap_yi", None) is not None:
        fields.append("market_cap_yi")

    if getattr(filters, "min_main_5d_yi", None) is not None:
        fields.append("main_5d_yi")
    if getattr(filters, "min_industry_strength", None) is not None:
        fields.append("industry_strength")
    if getattr(filters, "min_volume_ratio", None) is not None:
        fields.append("volume_ratio")
    if getattr(filters, "min_turnover", None) is not None or getattr(filters, "max_turnover", None) is not None:
        fields.append("turnover")
    if getattr(filters, "min_trend_20d", None) is not None or getattr(filters, "require_ma_bullish", False):
        fields.extend(("technical_status", "trend_20d"))
    if getattr(filters, "require_box_sideways", False) or any(
        getattr(filters, name, None) is not None
        for name in ("min_box_score", "max_box_range_pct")
    ):
        fields.extend(("technical_status", "box_score", "box_range_pct"))
    if any(
        getattr(filters, name, None) is not None
        for name in (
            "min_volume_ratio",
            "max_pullback_pct",
            "max_max_drawdown_30d",
            "max_max_drawdown_60d",
            "min_turnover",
            "max_turnover",
            "min_rsi",
            "max_rsi",
        )
    ):
        fields.append("technical_status")
    return tuple(dict.fromkeys(fields))


def assess_snapshot_quality(
    snapshots: list[Any],
    filters: Any,
    *,
    execution_mode: str = "formal",
) -> DataQualityAssessment:
    """Classify missing data before a formal or historical screening run."""

    required = required_fields_for_filter(filters)
    missing_required = [
        field
        for field in required
        if not snapshots or any(not _snapshot_has_field(item, field) for item in snapshots)
    ]
    blocking = [
        field
        for field in missing_required
        if not snapshots or all(not _snapshot_has_field(item, field) for item in snapshots)
    ]
    optional_missing = any(str(getattr(item, "missing_fields", "") or "").strip() for item in snapshots)

    if execution_mode == "review_only":
        return DataQualityAssessment(
            status="review_only",
            required_fields=required,
            missing_fields=tuple(dict.fromkeys(missing_required)),
            blocking_fields=tuple(dict.fromkeys(blocking)),
            note="当前输入为过期或历史缓存，仅允许历史复核，不得视为正式筛选。",
        )
    if execution_mode == "exploratory":
        return DataQualityAssessment(
            status="exploratory",
            required_fields=required,
            missing_fields=tuple(dict.fromkeys(missing_required)),
            blocking_fields=tuple(dict.fromkeys(blocking)),
            note="当前输入为探索性研究结果，不得进入正式筛选或绩效统计。",
        )
    if blocking:
        return DataQualityAssessment(
            status="blocked",
            required_fields=required,
            missing_fields=tuple(dict.fromkeys(missing_required)),
            blocking_fields=tuple(dict.fromkeys(blocking)),
            note=f"正式筛选被数据有效性闸门阻断：缺少 {', '.join(blocking)}。",
        )
    if missing_required:
        return DataQualityAssessment(
            status="unverified",
            required_fields=required,
            missing_fields=tuple(dict.fromkeys(missing_required)),
            blocking_fields=(),
            note=f"关键字段在部分样本缺失，结果只可作为未验证研究结果：{', '.join(missing_required)}。",
        )
    if optional_missing:
        return DataQualityAssessment(
            status="partial",
            required_fields=required,
            missing_fields=(),
            blocking_fields=(),
            note="非关键字段存在缺失，结果可研究但不完整。",
        )
    return DataQualityAssessment(status="verified", required_fields=required)


def parse_snapshot_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
