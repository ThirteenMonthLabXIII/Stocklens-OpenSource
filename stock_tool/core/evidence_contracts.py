"""Frozen v1 contracts for facts, candidate lenses, and formal Agent output.

This module is deliberately independent from the legacy scoring and trade-AI
modules.  It is the narrow boundary used by the migration plan: local code
owns data quality and candidate state, while formal Agents may only organize
evidence and identify gaps or conflicts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping


FACT_CONTRACT_VERSION = "fact.v1"
CANDIDATE_LENS_CONTRACT_VERSION = "candidate_lens.v1"
AGENT_RESULT_CONTRACT_VERSION = "agent_result.v1"
LEGACY_COMPATIBILITY_VERSION = "legacy_compat.v1"

AGENT_STATUSES = frozenset({"ok", "degraded", "failed", "skipped"})
CANDIDATE_STATUSES = frozenset(
    {
        "eligible",
        "candidate",
        "watch",
        "insufficient",
        "conflicted",
        "excluded",
        "stale",
        "invalidated",
        "closed",
    }
)
FACT_QUALITY_STATUSES = frozenset({"verified", "partial", "unverified", "blocked"})
AGENT_CLAIM_TYPES = frozenset(
    {
        "supports_lens",
        "opposes_lens",
        "context",
        "insufficient",
        "no_observed_counter_evidence",
    }
)

# These names are rejected recursively in formal Agent payloads.  Legacy
# payloads may still contain them behind their compatibility adapters.
FORBIDDEN_AGENT_FIELDS = frozenset(
    {
        "buy",
        "sell",
        "can_buy",
        "target_price",
        "entry_price",
        "stop_price",
        "fail_price",
        "expected_return",
        "return_rate",
        "position",
        "position_hint",
        "weight",
        "allocation",
        "rank",
        "rank_order",
        "score",
        "ai_score",
        "confidence",
        "final_rating",
        "stance",
        "bullish",
        "bearish",
        "neutral",
    }
)
FORBIDDEN_AGENT_TERMS = (
    "买入",
    "卖出",
    "目标价",
    "止损价",
    "仓位",
    "收益率",
    "bullish",
    "bearish",
    "neutral",
)

FORMAL_AGENT_FIELDS = frozenset(
    {
        "contract_version",
        "agent_id",
        "agent_version",
        "run_id",
        "snapshot_id",
        "prompt_version",
        "model_version",
        "status",
        "coverage",
        "claims",
        "counter_claims",
        "missing_evidence",
        "risk_flags",
        "unresolved_questions",
        "diagnostics",
        "cost_note",
    }
)


class ContractValidationError(ValueError):
    """Raised when a v1 contract payload is incomplete or unsafe."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def contract_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractValidationError(f"{name} 不能为空")
    return text


def _parse_datetime(name: str, value: str) -> None:
    text = _required_text(name, value).replace("Z", "+00:00")
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractValidationError(f"{name} 必须是 ISO 时间") from exc


def _text_tuple(name: str, value: Iterable[Any] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise ContractValidationError(f"{name} 必须是字符串列表")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _copy_mapping(name: str, value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{name} 必须是对象")
    return json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(value).casefold())


def _reject_formal_forbidden(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if normalized in {_normalized_key(item) for item in FORBIDDEN_AGENT_FIELDS}:
                raise ContractValidationError(f"正式 Agent 结果包含禁止字段：{path}.{key}")
            _reject_formal_forbidden(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_formal_forbidden(child, f"{path}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.casefold()
        if any(term.casefold() in lowered for term in FORBIDDEN_AGENT_TERMS):
            raise ContractValidationError(f"正式 Agent 结果包含禁止交易表达：{path}")


@dataclass(frozen=True)
class Fact:
    """One immutable, point-in-time fact visible to a formal Agent."""

    fact_id: str
    code: str
    field_name: str
    value: Any
    unit: str
    source: str
    source_version: str
    effective_at: str
    available_at: str
    snapshot_id: str
    quality_status: str
    raw_hash: str
    contract_version: str = FACT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != FACT_CONTRACT_VERSION:
            raise ContractValidationError(f"不支持的 Fact 契约版本：{self.contract_version}")
        for name in (
            "fact_id",
            "code",
            "field_name",
            "unit",
            "source",
            "source_version",
            "snapshot_id",
            "raw_hash",
        ):
            _required_text(name, getattr(self, name))
        _parse_datetime("effective_at", self.effective_at)
        _parse_datetime("available_at", self.available_at)
        if self.quality_status not in FACT_QUALITY_STATUSES:
            raise ContractValidationError(f"不支持的 Fact quality_status：{self.quality_status}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "code": self.code,
            "field_name": self.field_name,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "source_version": self.source_version,
            "effective_at": self.effective_at,
            "available_at": self.available_at,
            "snapshot_id": self.snapshot_id,
            "quality_status": self.quality_status,
            "raw_hash": self.raw_hash,
            "contract_version": self.contract_version,
        }

    @classmethod
    def from_snapshot_fact(
        cls,
        snapshot_fact: Any,
        *,
        unit: str = "unspecified",
        fact_id: str = "",
    ) -> "Fact":
        """Adapt the immutable snapshot store row without changing its schema."""

        snapshot_id = _required_text("snapshot_id", getattr(snapshot_fact, "snapshot_id", ""))
        code = _required_text("code", getattr(snapshot_fact, "code", ""))
        field_name = _required_text("field_name", getattr(snapshot_fact, "field_name", ""))
        raw_hash = str(
            getattr(snapshot_fact, "raw_hash", "")
            or getattr(snapshot_fact, "resolved_raw_hash", "")
            or ""
        ).strip()
        raw_payload = {
            "code": code,
            "field_name": field_name,
            "value": getattr(snapshot_fact, "value", None),
            "effective_at": getattr(snapshot_fact, "effective_at", ""),
            "available_at": getattr(snapshot_fact, "available_at", ""),
            "source": getattr(snapshot_fact, "source", ""),
            "source_version": getattr(snapshot_fact, "source_version", ""),
        }
        raw_hash = raw_hash or contract_hash(raw_payload)
        resolved_fact_id = fact_id.strip() or f"fact-{contract_hash({'snapshot_id': snapshot_id, 'code': code, 'field_name': field_name})[:24]}"
        return cls(
            fact_id=resolved_fact_id,
            code=code,
            field_name=field_name,
            value=getattr(snapshot_fact, "value", None),
            unit=str(unit or ""),
            source=str(getattr(snapshot_fact, "source", "") or ""),
            source_version=str(getattr(snapshot_fact, "source_version", "") or ""),
            effective_at=str(getattr(snapshot_fact, "effective_at", "") or ""),
            available_at=str(getattr(snapshot_fact, "available_at", "") or ""),
            snapshot_id=snapshot_id,
            quality_status=str(getattr(snapshot_fact, "quality_status", "verified") or "verified"),
            raw_hash=raw_hash,
        )


@dataclass(frozen=True)
class CandidateLens:
    """A named, versioned view of candidates; it never owns candidate state."""

    lens_id: str
    lens_version: str
    name: str
    purpose: str
    required_fields: tuple[str, ...] = ()
    hard_filters: dict[str, Any] = field(default_factory=dict)
    exclusion_rules: dict[str, Any] = field(default_factory=dict)
    evidence_fields: tuple[str, ...] = ()
    observation_conditions: tuple[str, ...] = ()
    contract_version: str = CANDIDATE_LENS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CANDIDATE_LENS_CONTRACT_VERSION:
            raise ContractValidationError(f"不支持的 CandidateLens 契约版本：{self.contract_version}")
        for name in ("lens_id", "lens_version", "name", "purpose"):
            _required_text(name, getattr(self, name))
        object.__setattr__(self, "required_fields", _text_tuple("required_fields", self.required_fields))
        object.__setattr__(self, "evidence_fields", _text_tuple("evidence_fields", self.evidence_fields))
        object.__setattr__(self, "observation_conditions", _text_tuple("observation_conditions", self.observation_conditions))
        object.__setattr__(self, "hard_filters", _copy_mapping("hard_filters", self.hard_filters))
        object.__setattr__(self, "exclusion_rules", _copy_mapping("exclusion_rules", self.exclusion_rules))

    def as_dict(self) -> dict[str, Any]:
        return {
            "lens_id": self.lens_id,
            "lens_version": self.lens_version,
            "name": self.name,
            "purpose": self.purpose,
            "required_fields": list(self.required_fields),
            "hard_filters": dict(self.hard_filters),
            "exclusion_rules": dict(self.exclusion_rules),
            "evidence_fields": list(self.evidence_fields),
            "observation_conditions": list(self.observation_conditions),
            "contract_version": self.contract_version,
        }


DEFAULT_CANDIDATE_LENSES = (
    CandidateLens(
        lens_id="quality_research",
        lens_version="v1",
        name="质量研究",
        purpose="从已通过数据质量和基础资格闸门的股票中整理可复核的质量事实。",
        required_fields=("roe", "debt_ratio", "pe_ttm", "report_date"),
        hard_filters={
            "data_quality": "verified",
            "st_status": "excluded",
            "roe": {">=": 8},
            "debt_ratio": {"<=": 85},
        },
        exclusion_rules={"missing_required_fields": "insufficient"},
        evidence_fields=("roe", "debt_ratio", "pe_ttm", "report_date"),
        observation_conditions=("必填事实保持 verified", "报告期和 available_at 可追溯"),
    ),
    CandidateLens(
        lens_id="trend_observation",
        lens_version="v1",
        name="趋势观察",
        purpose="整理价格、技术状态和趋势窗口事实，不输出方向性预测。",
        required_fields=("price", "trend_20d", "technical_status", "updated_at"),
        hard_filters={
            "data_quality": "verified",
            "st_status": "excluded",
            "trend_20d": {">=": 0},
        },
        exclusion_rules={"missing_required_fields": "insufficient"},
        evidence_fields=("price", "trend_20d", "technical_status", "updated_at"),
        observation_conditions=("趋势窗口与快照截止时间一致", "技术字段缺失时降级观察"),
    ),
    CandidateLens(
        lens_id="event_verification",
        lens_version="v1",
        name="事件核验",
        purpose="整理公告或事件资料的时间、来源和待人工核查项。",
        required_fields=("event_title", "event_date", "event_source", "available_at"),
        hard_filters={"data_quality": "verified"},
        exclusion_rules={"future_available_at": "invalidated"},
        evidence_fields=("event_title", "event_date", "event_source", "available_at"),
        observation_conditions=("外部文本仅作为资料区块", "无法核验时间时标记 insufficient"),
    ),
)


def get_default_candidate_lens(lens_id: str) -> CandidateLens:
    for lens in DEFAULT_CANDIDATE_LENSES:
        if lens.lens_id == str(lens_id).strip():
            return lens
    raise KeyError(f"未知 CandidateLens：{lens_id}")


def _normalize_claim(name: str, value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{name}[{index}] 必须是包含事实引用的对象")
    allowed = {"claim_id", "type", "text", "evidence_refs", "caveats", "code"}
    unknown = set(value) - allowed
    if unknown:
        raise ContractValidationError(f"{name}[{index}] 包含未知字段：{', '.join(sorted(map(str, unknown)))}")
    required = {"claim_id", "type", "text", "evidence_refs", "caveats"}
    missing = required - set(value)
    if missing:
        raise ContractValidationError(f"{name}[{index}] 缺少字段：{', '.join(sorted(missing))}")
    claim_type = _required_text(f"{name}[{index}].type", value.get("type"))
    if claim_type not in AGENT_CLAIM_TYPES:
        raise ContractValidationError(f"{name}[{index}].type 不支持：{claim_type}")
    text = _required_text(f"{name}[{index}].text", value.get("text"))
    result = {
        "claim_id": str(value.get("claim_id") or f"{name}-{index + 1:03d}").strip(),
        "type": claim_type,
        "text": text,
        "evidence_refs": list(_text_tuple(f"{name}[{index}].evidence_refs", value.get("evidence_refs", []))),
        "caveats": list(_text_tuple(f"{name}[{index}].caveats", value.get("caveats", []))),
    }
    if value.get("code") not in (None, ""):
        result["code"] = _required_text(f"{name}[{index}].code", value.get("code"))
    return result


def _normalize_claims(name: str, value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ContractValidationError(f"{name} 必须是列表")
    return tuple(_normalize_claim(name, item, index) for index, item in enumerate(value))


@dataclass(frozen=True)
class AgentResult:
    """Formal Agent output: evidence organization only, never candidate action."""

    agent_id: str
    agent_version: str
    run_id: str
    snapshot_id: str
    prompt_version: str
    model_version: str
    status: str
    coverage: Any
    claims: tuple[dict[str, Any], ...] = ()
    counter_claims: tuple[dict[str, Any], ...] = ()
    missing_evidence: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)
    cost_note: str = ""
    contract_version: str = AGENT_RESULT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != AGENT_RESULT_CONTRACT_VERSION:
            raise ContractValidationError(f"不支持的 AgentResult 契约版本：{self.contract_version}")
        for name in ("agent_id", "agent_version", "run_id", "snapshot_id", "prompt_version"):
            _required_text(name, getattr(self, name))
        if self.status not in AGENT_STATUSES:
            raise ContractValidationError(f"不支持的 Agent status：{self.status}")
        object.__setattr__(self, "claims", _normalize_claims("claims", list(self.claims)))
        object.__setattr__(self, "counter_claims", _normalize_claims("counter_claims", list(self.counter_claims)))
        for name in ("missing_evidence", "risk_flags", "unresolved_questions"):
            object.__setattr__(self, name, _text_tuple(name, getattr(self, name)))
        object.__setattr__(self, "diagnostics", _copy_mapping("diagnostics", self.diagnostics))

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "prompt_version": self.prompt_version,
            "model_version": self.model_version,
            "status": self.status,
            "coverage": self.coverage,
            "claims": [dict(item) for item in self.claims],
            "counter_claims": [dict(item) for item in self.counter_claims],
            "missing_evidence": list(self.missing_evidence),
            "risk_flags": list(self.risk_flags),
            "unresolved_questions": list(self.unresolved_questions),
            "diagnostics": dict(self.diagnostics),
            "cost_note": self.cost_note,
        }

    @property
    def result_hash(self) -> str:
        return contract_hash(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AgentResult":
        return validate_agent_result_payload(payload)


def validate_agent_result_payload(payload: Mapping[str, Any]) -> AgentResult:
    """Reject unknown or trading-oriented fields before formal persistence."""

    if not isinstance(payload, Mapping):
        raise ContractValidationError("AgentResult 必须是对象")
    _reject_formal_forbidden(payload)
    unknown = set(payload) - FORMAL_AGENT_FIELDS
    if unknown:
        raise ContractValidationError(f"AgentResult 包含未知字段：{', '.join(sorted(map(str, unknown)))}")
    required = FORMAL_AGENT_FIELDS - {"contract_version"}
    missing = required - set(payload)
    if missing:
        raise ContractValidationError(f"AgentResult 缺少字段：{', '.join(sorted(missing))}")
    result = AgentResult(
        contract_version=str(payload.get("contract_version") or AGENT_RESULT_CONTRACT_VERSION),
        agent_id=str(payload.get("agent_id") or ""),
        agent_version=str(payload.get("agent_version") or ""),
        run_id=str(payload.get("run_id") or ""),
        snapshot_id=str(payload.get("snapshot_id") or ""),
        prompt_version=str(payload.get("prompt_version") or ""),
        model_version=str(payload.get("model_version") or ""),
        status=str(payload.get("status") or ""),
        coverage=payload.get("coverage"),
        claims=_normalize_claims("claims", payload.get("claims")),
        counter_claims=_normalize_claims("counter_claims", payload.get("counter_claims")),
        missing_evidence=_text_tuple("missing_evidence", payload.get("missing_evidence")),
        risk_flags=_text_tuple("risk_flags", payload.get("risk_flags")),
        unresolved_questions=_text_tuple("unresolved_questions", payload.get("unresolved_questions")),
        diagnostics=_copy_mapping("diagnostics", payload.get("diagnostics")),
        cost_note=str(payload.get("cost_note") or ""),
    )
    return result


LEGACY_FIELD_MAPPING = {
    "formula_version": "selection_policy_version",
    "local_score": "legacy.local_score",
    "ai_score": "legacy.ai_score",
    "rank": "legacy.rank",
    "rank_order": "legacy.rank_order",
    "buy": "legacy.buy",
    "sell": "legacy.sell",
    "target_price": "legacy.target_price",
    "entry_price": "legacy.entry_price",
    "stop_price": "legacy.stop_price",
    "expected_return": "legacy.expected_return",
    "position": "legacy.position",
    "confidence": "legacy.confidence",
    "final_rating": "legacy.final_rating",
}


def selection_policy_version_from_legacy(formula_version: str) -> str:
    """Give legacy formula rows an explicit, non-formal policy identity."""

    version = str(formula_version or "").strip() or "unknown"
    return f"legacy_formula.{version}"


def legacy_compatibility_identity(
    *,
    formula_version: str = "",
    lens_version: str = "",
) -> dict[str, Any]:
    """Describe how old rows are read without making them formal Agent data."""

    return {
        "compatibility_version": LEGACY_COMPATIBILITY_VERSION,
        "legacy_formula_version": str(formula_version or ""),
        "selection_policy_version": selection_policy_version_from_legacy(formula_version),
        "lens_version": str(lens_version or ""),
        "formal_agent_allowed": False,
        "field_mapping": dict(LEGACY_FIELD_MAPPING),
    }
