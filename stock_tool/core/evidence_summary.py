"""Phase 5 F layer: deterministic aggregation of validated Agent evidence.

The F layer deliberately has no candidate-decision vocabulary.  It binds
already validated claim IDs, preserves opposing evidence, and reports data
gaps for later human review.  Candidate membership and candidate state remain
owned by the local deterministic layers.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any, Iterable, Mapping

from stock_tool.core.evidence_contracts import (
    AGENT_STATUSES,
    AgentResult,
    Fact,
    FORBIDDEN_AGENT_FIELDS,
    contract_hash,
    validate_agent_result_payload,
)
from stock_tool.core.research_discussion import (
    validate_claim_evidence,
    validate_evidence_package,
)
from stock_tool.core.multi_agent_orchestrator import FIVE_AGENT_IDS


F_SUMMARY_STATUSES = frozenset({"ok", "partial", "failed"})
F_FORBIDDEN_FIELDS = frozenset(
    set(FORBIDDEN_AGENT_FIELDS)
    | {
        "candidate_status",
    }
)
_FAILOVER_ROUTES = frozenset({"fallback", "failover", "backup", "secondary"})
_FORMAL_AGENT_IDS = frozenset(FIVE_AGENT_IDS)


class EvidenceAggregationError(ValueError):
    """Raised when the F layer cannot prove its input is safe to aggregate."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Fact):
        return value.as_dict()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(child) for child in value]
    return value


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise EvidenceAggregationError("证据汇总中的问题列表必须是字符串列表")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _normalized_key(value: Any) -> str:
    return "".join(char for char in str(value).casefold() if char.isalnum() or char == "_")


def _assert_safe_output(value: Any, path: str = "summary") -> None:
    if isinstance(value, Mapping):
        forbidden = {_normalized_key(item) for item in F_FORBIDDEN_FIELDS}
        for key, child in value.items():
            if _normalized_key(key) in forbidden:
                raise EvidenceAggregationError(f"F 层输出包含禁止字段：{path}.{key}")
            _assert_safe_output(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe_output(child, f"{path}[{index}]")


def _required_text(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise EvidenceAggregationError(f"{name} 不能为空")
    return text


def _candidate_id(record: Mapping[str, Any]) -> str:
    return _required_text("candidate_records[].code", record.get("candidate_id") or record.get("code"))


def _candidate_code(record: Mapping[str, Any]) -> str:
    return _required_text("candidate_records[].code", record.get("code"))


def _records_from_package(package: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    records = package.get("candidate_records")
    if not isinstance(records, list):
        raise EvidenceAggregationError("正式证据包 candidate_records 必须是列表")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(records):
        if not isinstance(item, Mapping):
            raise EvidenceAggregationError(f"candidate_records[{index}] 必须是对象")
        record = deepcopy(dict(item))
        code = _candidate_code(record)
        candidate_id = _candidate_id(record)
        if candidate_id in seen:
            raise EvidenceAggregationError(f"candidate_records 包含重复 candidate_id：{candidate_id}")
        seen.add(candidate_id)
        # A candidate ID is an audit identity, not an AI-generated field.
        record.setdefault("candidate_id", candidate_id)
        record["code"] = code
        result.append(record)
    return tuple(result)


def _records_for_compare(records: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(records, (list, tuple)):
        raise EvidenceAggregationError("Agent 编排结果 candidate_records 必须是列表")
    normalized: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, Mapping):
            raise EvidenceAggregationError("Agent 编排结果 candidate_records 必须只包含对象")
        record = deepcopy(dict(item))
        record.setdefault("candidate_id", _candidate_id(record))
        normalized.append(record)
    return tuple(normalized)


def _execution_items(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Mapping) and "executions" in value:
        items = value.get("executions")
    elif hasattr(value, "executions"):
        items = getattr(value, "executions")
    else:
        items = value
    if not isinstance(items, (list, tuple)):
        raise EvidenceAggregationError("F 层必须接收 Agent 执行记录列表")
    return tuple(items)


def _execution_metadata(item: Any) -> dict[str, str]:
    """Read optional outer execution identity fields for cross-checking."""

    names = (
        "run_id",
        "snapshot_id",
        "candidate_set_digest",
        "evidence_package_hash",
    )
    if isinstance(item, Mapping):
        return {
            name: str(item.get(name) or "").strip()
            for name in names
            if name in item
        }
    return {
        name: str(getattr(item, name) or "").strip()
        for name in names
        if hasattr(item, name)
    }


def _execution_parts(item: Any) -> tuple[str, str, str, Mapping[str, Any] | None]:
    """Extract the stable execution envelope without trusting diagnostics."""

    if isinstance(item, AgentResult):
        result = item
        return result.agent_id, result.status, str(result.diagnostics.get("outcome") or result.status), result.as_dict()
    if isinstance(item, Mapping):
        if "result" in item:
            result_payload = item.get("result")
            agent_id = str(item.get("agent_id") or "")
            status = str(item.get("status") or "failed")
            outcome = str(item.get("outcome") or status)
        else:
            result_payload = item
            agent_id = str(item.get("agent_id") or "")
            status = str(item.get("status") or "failed")
            outcome = str(item.get("diagnostics", {}).get("outcome") or status) if isinstance(item.get("diagnostics"), Mapping) else status
        if not isinstance(result_payload, Mapping):
            return agent_id, status, outcome, None
        try:
            result = validate_agent_result_payload(result_payload)
        except Exception as exc:
            raise EvidenceAggregationError(f"Agent 结果无法通过 v1 校验：{exc}") from exc
        if agent_id and agent_id != result.agent_id:
            raise EvidenceAggregationError("Agent execution 与 AgentResult 的 agent_id 不一致")
        return result.agent_id, status, outcome, result.as_dict()
    result = getattr(item, "result", None)
    agent_id = str(getattr(item, "agent_id", "") or "")
    status = str(getattr(item, "status", "failed") or "failed")
    outcome = str(getattr(item, "outcome", "") or status)
    if result is None:
        return agent_id, status, outcome, None
    if isinstance(result, AgentResult):
        payload = result.as_dict()
    elif isinstance(result, Mapping):
        try:
            payload = validate_agent_result_payload(result).as_dict()
        except Exception as exc:
            raise EvidenceAggregationError(f"Agent 结果无法通过 v1 校验：{exc}") from exc
    else:
        raise EvidenceAggregationError("Agent execution.result 类型不合法")
    if agent_id and agent_id != str(payload.get("agent_id") or ""):
        raise EvidenceAggregationError("Agent execution 与 AgentResult 的 agent_id 不一致")
    return str(payload.get("agent_id") or agent_id), status, outcome, payload


def _market_validation_package(package: Mapping[str, Any], result: Mapping[str, Any]) -> Mapping[str, Any]:
    """Allow batch market facts without making them candidate records."""

    if str(result.get("agent_id") or "") != "market_industry":
        return package
    records = list(package.get("candidate_records") or [])
    known_codes = {str(item.get("code") or "") for item in records if isinstance(item, Mapping)}
    facts = package.get("facts") or []
    for raw_fact in facts:
        if not isinstance(raw_fact, Mapping):
            continue
        code = str(raw_fact.get("code") or "")
        if code and code not in known_codes:
            records.append({"code": code, "name": "market_snapshot_validation_only"})
            known_codes.add(code)
    if len(records) == len(package.get("candidate_records") or []):
        return package
    augmented = deepcopy(dict(package))
    augmented["candidate_records"] = records
    augmented.pop("evidence_package_hash", None)
    augmented["evidence_package_hash"] = contract_hash(augmented)
    return augmented


def _fact_index(package: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for item in package.get("facts") or []:
        if isinstance(item, Mapping):
            fact_id = str(item.get("fact_id") or "")
            if fact_id:
                result[fact_id] = item
    return result


def _is_failover_fact(fact: Mapping[str, Any], package: Mapping[str, Any]) -> bool:
    route = str(fact.get("source_route") or "").casefold()
    source = str(fact.get("source") or "").casefold()
    quality_summary = package.get("data_quality_summary")
    return (
        route in _FAILOVER_ROUTES
        or any(marker in source for marker in ("failover", "fallback", "backup", "secondary"))
        or (isinstance(quality_summary, Mapping) and bool(quality_summary.get("failover")))
    )


def _claim_code(claim: Mapping[str, Any]) -> str:
    return str(claim.get("code") or "").strip()


@dataclass(frozen=True)
class EvidenceConflict:
    code: str
    supporting_claim_ids: tuple[str, ...]
    opposing_claim_ids: tuple[str, ...]
    reason: str = "同一候选存在支持 Lens 与反向 Lens 证据，不能平均或抹平。"

    def as_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": contract_hash(
                {
                    "code": self.code,
                    "supporting_claim_ids": list(self.supporting_claim_ids),
                    "opposing_claim_ids": list(self.opposing_claim_ids),
                }
            )[:24],
            "code": self.code,
            "supporting_claim_ids": list(self.supporting_claim_ids),
            "opposing_claim_ids": list(self.opposing_claim_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EvidenceSummary:
    """F output for one deterministic candidate; it contains no decision."""

    candidate_id: str
    code: str
    status: str
    supporting_claim_ids: tuple[str, ...] = ()
    opposing_claim_ids: tuple[str, ...] = ()
    conflicts: tuple[dict[str, Any], ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    data_quality_summary: dict[str, Any] = field(default_factory=dict)
    candidate_type_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in F_SUMMARY_STATUSES:
            raise EvidenceAggregationError(f"不支持的 F 层 status：{self.status}")
        _required_text("candidate_id", self.candidate_id)
        _required_text("code", self.code)
        object.__setattr__(self, "supporting_claim_ids", tuple(_text_list(self.supporting_claim_ids)))
        object.__setattr__(self, "opposing_claim_ids", tuple(_text_list(self.opposing_claim_ids)))
        object.__setattr__(self, "unresolved_questions", tuple(_text_list(self.unresolved_questions)))
        object.__setattr__(self, "candidate_type_labels", tuple(_text_list(self.candidate_type_labels)))
        object.__setattr__(self, "conflicts", tuple(_jsonable(list(self.conflicts))))
        object.__setattr__(self, "data_quality_summary", _jsonable(dict(self.data_quality_summary or {})))
        _assert_safe_output(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "code": self.code,
            "status": self.status,
            "supporting_claim_ids": list(self.supporting_claim_ids),
            "opposing_claim_ids": list(self.opposing_claim_ids),
            "conflicts": [dict(item) for item in self.conflicts],
            "unresolved_questions": list(self.unresolved_questions),
            "data_quality_summary": dict(self.data_quality_summary),
            "candidate_type_labels": list(self.candidate_type_labels),
        }


@dataclass(frozen=True)
class EvidenceAggregationResult:
    """Complete F output for one five-Agent run."""

    run_id: str
    snapshot_id: str
    candidate_set_digest: str
    status: str
    summaries: tuple[EvidenceSummary, ...]
    agent_statuses: dict[str, str]
    agent_outcomes: dict[str, str]
    candidate_set_preserved: bool = True
    invalid_claim_count: int = 0
    created_at: str = ""

    def __post_init__(self) -> None:
        if self.status not in F_SUMMARY_STATUSES:
            raise EvidenceAggregationError(f"不支持的 F 层 status：{self.status}")
        if not self.candidate_set_preserved:
            raise EvidenceAggregationError("F 层发现候选集合发生变化，已拒绝汇总")
        if self.invalid_claim_count < 0:
            raise EvidenceAggregationError("invalid_claim_count 不能为负数")
        _assert_safe_output(self.as_dict())

    @property
    def summary_hash(self) -> str:
        return contract_hash(self._payload())

    def _payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "candidate_set_digest": self.candidate_set_digest,
            "status": self.status,
            "summaries": [item.as_dict() for item in self.summaries],
            "agent_statuses": dict(self.agent_statuses),
            "agent_outcomes": dict(self.agent_outcomes),
            "candidate_set_preserved": self.candidate_set_preserved,
            "invalid_claim_count": self.invalid_claim_count,
            "created_at": self.created_at,
        }

    def as_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["summary_hash"] = contract_hash(payload)
        return payload

    def payload_with_hash(self) -> dict[str, Any]:
        return self.as_dict()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvidenceAggregationResult":
        """Load and verify a persisted F-layer result without calling an Agent."""

        if not isinstance(payload, Mapping):
            raise EvidenceAggregationError("F 层回放结果必须是对象")
        summaries: list[EvidenceSummary] = []
        for index, item in enumerate(payload.get("summaries") or ()):
            if not isinstance(item, Mapping):
                raise EvidenceAggregationError(f"F 层回放 summaries[{index}] 必须是对象")
            summaries.append(
                EvidenceSummary(
                    candidate_id=str(item.get("candidate_id") or ""),
                    code=str(item.get("code") or ""),
                    status=str(item.get("status") or "failed"),
                    supporting_claim_ids=tuple(item.get("supporting_claim_ids") or ()),
                    opposing_claim_ids=tuple(item.get("opposing_claim_ids") or ()),
                    conflicts=tuple(item.get("conflicts") or ()),
                    unresolved_questions=tuple(item.get("unresolved_questions") or ()),
                    data_quality_summary=dict(item.get("data_quality_summary") or {}),
                    candidate_type_labels=tuple(item.get("candidate_type_labels") or ()),
                )
            )
        result = cls(
            run_id=str(payload.get("run_id") or ""),
            snapshot_id=str(payload.get("snapshot_id") or ""),
            candidate_set_digest=str(payload.get("candidate_set_digest") or ""),
            status=str(payload.get("status") or "failed"),
            summaries=tuple(summaries),
            agent_statuses={str(key): str(value) for key, value in dict(payload.get("agent_statuses") or {}).items()},
            agent_outcomes={str(key): str(value) for key, value in dict(payload.get("agent_outcomes") or {}).items()},
            candidate_set_preserved=bool(payload.get("candidate_set_preserved", True)),
            invalid_claim_count=int(payload.get("invalid_claim_count") or 0),
            created_at=str(payload.get("created_at") or ""),
        )
        supplied_hash = str(payload.get("summary_hash") or "")
        if supplied_hash and supplied_hash != result.summary_hash:
            raise EvidenceAggregationError("F 层回放结果哈希不匹配")
        return result


def aggregate_agent_evidence(
    evidence_package: Mapping[str, Any],
    executions: Any,
    *,
    run_id: str = "",
    created_at: str = "",
) -> EvidenceAggregationResult:
    """Aggregate only already validated Agent claims for the current package.

    ``executions`` may be a ``FiveAgentOrchestrator`` result, execution
    mappings, ``AgentExecution`` objects, or a plain sequence of
    ``AgentResult`` objects.  No input path can add, remove, rank, score, or
    change a candidate record.
    """

    try:
        package = validate_evidence_package(deepcopy(dict(evidence_package)))
    except Exception as exc:
        raise EvidenceAggregationError(f"F 层证据包校验失败：{exc}") from exc
    records = _records_from_package(package)
    expected_records = _records_for_compare(records)
    top_level_metadata = executions if isinstance(executions, Mapping) else None
    if top_level_metadata is not None:
        for field_name, expected in (
            ("snapshot_id", str(package["data_snapshot_id"])),
            ("candidate_set_digest", str(package["candidate_set_digest"])),
        ):
            supplied = str(top_level_metadata.get(field_name) or "").strip()
            if supplied and supplied != expected:
                raise EvidenceAggregationError(
                    f"Agent 编排结果的 {field_name} 与证据包不一致"
                )
        if "candidate_set_preserved" in top_level_metadata and not bool(
            top_level_metadata.get("candidate_set_preserved")
        ):
            raise EvidenceAggregationError("Agent 编排结果报告候选集合已发生变化")
    elif hasattr(executions, "candidate_set_preserved") and not bool(
        getattr(executions, "candidate_set_preserved")
    ):
        raise EvidenceAggregationError("Agent 编排结果报告候选集合已发生变化")
    else:
        for field_name, expected in (
            ("snapshot_id", str(package["data_snapshot_id"])),
            ("candidate_set_digest", str(package["candidate_set_digest"])),
        ):
            supplied = str(getattr(executions, field_name, "") or "").strip()
            if supplied and supplied != expected:
                raise EvidenceAggregationError(
                    f"Agent 编排结果的 {field_name} 与证据包不一致"
                )
    if isinstance(executions, Mapping) and "candidate_records" in executions:
        supplied = executions.get("candidate_records")
        if _records_for_compare(supplied or ()) != expected_records:
            raise EvidenceAggregationError("Agent 编排结果中的 candidate_records 与证据包不一致")
    elif hasattr(executions, "candidate_records"):
        supplied = getattr(executions, "candidate_records")
        if _records_for_compare(supplied or ()) != expected_records:
            raise EvidenceAggregationError("Agent 编排结果中的 candidate_records 与证据包不一致")

    items = _execution_items(executions)
    agent_statuses: dict[str, str] = {}
    agent_outcomes: dict[str, str] = {}
    resolved_run_id = str(
        run_id
        or (executions.get("run_id", "") if isinstance(executions, Mapping) else getattr(executions, "run_id", ""))
        or ""
    )
    raw_claims: list[tuple[str, str, dict[str, Any]]] = []
    missing_by_agent: dict[str, list[str]] = {}
    questions_by_agent: dict[str, list[str]] = {}
    invalid_claim_count = 0
    fact_index = _fact_index(package)

    for item in items:
        metadata = _execution_metadata(item)
        for field_name, expected in (
            ("run_id", resolved_run_id),
            ("snapshot_id", str(package["data_snapshot_id"])),
            ("candidate_set_digest", str(package["candidate_set_digest"])),
            ("evidence_package_hash", str(package["evidence_package_hash"])),
        ):
            supplied = metadata.get(field_name)
            if supplied and expected and supplied != expected:
                raise EvidenceAggregationError(
                    f"Agent execution 的 {field_name} 与证据包不一致"
                )
        envelope_run_id = metadata.get("run_id")
        if envelope_run_id:
            if resolved_run_id and envelope_run_id != resolved_run_id:
                raise EvidenceAggregationError("Agent execution 的 run_id 不一致")
            resolved_run_id = resolved_run_id or envelope_run_id
        agent_id, envelope_status, outcome, result_payload = _execution_parts(item)
        agent_id = _required_text("Agent agent_id", agent_id)
        if agent_id not in _FORMAL_AGENT_IDS:
            raise EvidenceAggregationError(f"未知正式 Agent：{agent_id}")
        if agent_id in agent_statuses:
            raise EvidenceAggregationError(f"F 层收到重复 Agent：{agent_id}")
        if envelope_status not in AGENT_STATUSES:
            raise EvidenceAggregationError(f"未知 Agent status：{envelope_status}")
        agent_statuses[agent_id] = envelope_status
        agent_outcomes[agent_id] = outcome
        if result_payload is None:
            continue
        payload_status = str(result_payload.get("status") or "")
        if payload_status and envelope_status != payload_status:
            raise EvidenceAggregationError(
                f"Agent {agent_id} 的执行 status 与 AgentResult status 不一致"
            )
        if str(result_payload.get("snapshot_id") or "") != str(package["data_snapshot_id"]):
            raise EvidenceAggregationError(f"Agent {agent_id} 的 snapshot_id 与证据包不一致")
        payload_run_id = str(result_payload.get("run_id") or "")
        if payload_run_id:
            if resolved_run_id and payload_run_id != resolved_run_id:
                raise EvidenceAggregationError(f"Agent {agent_id} 的 run_id 不一致")
            resolved_run_id = resolved_run_id or payload_run_id
        missing_by_agent[agent_id] = _text_list(result_payload.get("missing_evidence"))
        questions_by_agent[agent_id] = _text_list(result_payload.get("unresolved_questions"))
        if envelope_status not in {"ok", "degraded"} or str(result_payload.get("status") or "") not in {"ok", "degraded"}:
            continue
        validation_package = _market_validation_package(package, result_payload)
        for collection_name in ("claims", "counter_claims"):
            for index, claim in enumerate(result_payload.get(collection_name) or ()):
                claim_id = str(claim.get("claim_id") or f"{agent_id}-{collection_name}-{index + 1:03d}") if isinstance(claim, Mapping) else ""
                try:
                    normalized = validate_claim_evidence(
                        [dict(claim)], validation_package, name=f"{agent_id}.{collection_name}"
                    )[0]
                    for fact_id in normalized.get("evidence_refs") or ():
                        fact = fact_index.get(fact_id)
                        if fact is None:
                            raise EvidenceAggregationError(f"未知 fact_id：{fact_id}")
                        if _is_failover_fact(fact, package):
                            raise EvidenceAggregationError(f"failover 事实不能进入 F 层 verified claim：{fact_id}")
                    raw_claims.append((collection_name, claim_id, normalized))
                except Exception:
                    invalid_claim_count += 1

    claim_id_counts: dict[str, int] = {}
    for _collection, claim_id, _claim in raw_claims:
        claim_id_counts[claim_id] = claim_id_counts.get(claim_id, 0) + 1
    duplicate_ids = {claim_id for claim_id, count in claim_id_counts.items() if count > 1}
    if duplicate_ids:
        invalid_claim_count += sum(claim_id_counts[claim_id] for claim_id in duplicate_ids)
        raw_claims = [item for item in raw_claims if item[1] not in duplicate_ids]

    missing_agents = _FORMAL_AGENT_IDS - set(agent_statuses)
    if missing_agents:
        raise EvidenceAggregationError(
            "F 层要求完整的五 Agent 执行记录，缺少："
            + ", ".join(sorted(missing_agents))
        )
    resolved_run_id = _required_text("run_id", resolved_run_id)

    claims_by_code: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for collection_name, claim_id, claim in raw_claims:
        code = _claim_code(claim)
        if code and any(code == _candidate_code(record) for record in records):
            claims_by_code.setdefault(code, []).append((collection_name, claim_id, claim))

    statuses = list(agent_statuses.values())
    outcomes = list(agent_outcomes.values())
    if not statuses or all(statuses_item in {"failed", "skipped"} for statuses_item in statuses):
        aggregate_status = "failed"
    elif (
        any(statuses_item != "ok" for statuses_item in statuses)
        or any(outcome_item != "ok" for outcome_item in outcomes)
        or invalid_claim_count
    ):
        aggregate_status = "partial"
    else:
        aggregate_status = "ok"

    def questions_for(code: str) -> list[str]:
        result: list[str] = []
        for agent_id in agent_statuses:
            values = [*missing_by_agent.get(agent_id, []), *questions_by_agent.get(agent_id, [])]
            for value in values:
                if "." in value and value.split(".", 1)[0] not in {code, ""}:
                    continue
                if value not in result:
                    result.append(value)
        for _collection_name, _claim_id, claim in claims_by_code.get(code, []):
            if claim.get("type") == "insufficient" and str(claim.get("text") or "") not in result:
                result.append(str(claim["text"]))
        return result

    summaries: list[EvidenceSummary] = []
    for record in records:
        code = _candidate_code(record)
        candidate_id = _candidate_id(record)
        claims = claims_by_code.get(code, [])
        supporting = tuple(item[1] for item in claims if item[2].get("type") == "supports_lens")
        opposing = tuple(item[1] for item in claims if item[2].get("type") == "opposes_lens")
        conflicts: tuple[dict[str, Any], ...] = ()
        if supporting and opposing:
            conflicts = (
                EvidenceConflict(code, supporting, opposing).as_dict(),
            )
        labels = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in (record.get("lens_ids") or ())
                if str(value).strip()
            )
        )
        quality = dict(_jsonable(dict(package.get("data_quality_summary") or {})))
        quality.update(
            {
                "agent_statuses": dict(agent_statuses),
                "agent_outcomes": dict(agent_outcomes),
                "missing_evidence_count": sum(len(value) for value in missing_by_agent.values()),
                "invalid_claim_count": invalid_claim_count,
            }
        )
        summary_status = "failed" if aggregate_status == "failed" else ("partial" if aggregate_status != "ok" or conflicts or questions_for(code) else "ok")
        summaries.append(
            EvidenceSummary(
                candidate_id=candidate_id,
                code=code,
                status=summary_status,
                supporting_claim_ids=supporting,
                opposing_claim_ids=opposing,
                conflicts=conflicts,
                unresolved_questions=tuple(questions_for(code)),
                data_quality_summary=quality,
                candidate_type_labels=labels,
            )
        )

    result = EvidenceAggregationResult(
        run_id=resolved_run_id,
        snapshot_id=str(package["data_snapshot_id"]),
        candidate_set_digest=str(package["candidate_set_digest"]),
        status=aggregate_status,
        summaries=tuple(summaries),
        agent_statuses=agent_statuses,
        agent_outcomes=agent_outcomes,
        candidate_set_preserved=True,
        invalid_claim_count=invalid_claim_count,
        created_at=str(created_at or ""),
    )
    _assert_safe_output(result.as_dict())
    return result


__all__ = [
    "EvidenceAggregationError",
    "EvidenceAggregationResult",
    "EvidenceConflict",
    "EvidenceSummary",
    "F_SUMMARY_STATUSES",
    "aggregate_agent_evidence",
]
