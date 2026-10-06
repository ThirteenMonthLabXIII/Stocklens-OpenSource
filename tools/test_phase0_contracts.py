"""Offline acceptance checks for the frozen Phase 0 contracts."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.data_snapshot import DataSnapshotFact
from stock_tool.core.evidence_contracts import (
    AGENT_RESULT_CONTRACT_VERSION,
    AGENT_STATUSES,
    CANDIDATE_STATUSES,
    DEFAULT_CANDIDATE_LENSES,
    FACT_CONTRACT_VERSION,
    Fact,
    CandidateLens,
    ContractValidationError,
    AgentResult,
    legacy_compatibility_identity,
    selection_policy_version_from_legacy,
    validate_agent_result_payload,
)


def run_tests() -> None:
    source_fact = DataSnapshotFact(
        snapshot_id="snapshot-1",
        code="000001",
        field_name="roe",
        value=18.5,
        effective_at="2026-03-31",
        available_at="2026-05-01T10:00:00+08:00",
        source="fixture",
        source_version="fixture-1",
        fetched_at="2026-07-23T08:30:00+00:00",
    )
    fact = Fact.from_snapshot_fact(source_fact, unit="%")
    assert fact.contract_version == FACT_CONTRACT_VERSION
    assert set(
        ("fact_id", "code", "field_name", "value", "unit", "source", "source_version", "effective_at", "available_at", "snapshot_id", "quality_status", "raw_hash")
    ) <= set(fact.as_dict())
    assert Fact.from_snapshot_fact(source_fact, unit="%").fact_id == fact.fact_id

    assert [lens.lens_id for lens in DEFAULT_CANDIDATE_LENSES] == [
        "quality_research",
        "trend_observation",
        "event_verification",
    ]
    lens = CandidateLens(
        lens_id="fixture",
        lens_version="v1",
        name="Fixture",
        purpose="Contract test",
        required_fields=("roe",),
        hard_filters={"data_quality": "verified"},
        exclusion_rules={"missing": "insufficient"},
        evidence_fields=("roe",),
        observation_conditions=("keep verified",),
    )
    assert lens.as_dict()["required_fields"] == ["roe"]

    base = {
        "contract_version": AGENT_RESULT_CONTRACT_VERSION,
        "agent_id": "fundamental",
        "agent_version": "v1",
        "run_id": "run-1",
        "snapshot_id": "snapshot-1",
        "prompt_version": "fundamental-v1",
        "model_version": "fixture-model",
        "status": "ok",
        "coverage": {"facts": 2, "required": 2},
        "claims": [
            {
                "claim_id": "claim-001",
                "type": "supports_lens",
                "text": "ROE 事实满足质量观察条件",
                "evidence_refs": [fact.fact_id],
                "caveats": [],
            }
        ],
        "counter_claims": [
            {
                "claim_id": "claim-002",
                "type": "insufficient",
                "text": "尚缺少连续报告期事实",
                "evidence_refs": [],
                "caveats": ["需要人工核查"],
            }
        ],
        "missing_evidence": ["连续报告期"],
        "risk_flags": ["报告期覆盖不足"],
        "unresolved_questions": ["是否存在最新修订"],
        "diagnostics": {"source": "fixture"},
        "cost_note": "fixture",
    }
    result = validate_agent_result_payload(base)
    assert isinstance(result, AgentResult)
    assert result.status in AGENT_STATUSES
    assert result.result_hash == validate_agent_result_payload(base).result_hash
    for status in AGENT_STATUSES:
        status_payload = dict(base)
        status_payload["status"] = status
        assert validate_agent_result_payload(status_payload).status == status
    missing_model = dict(base)
    missing_model.pop("model_version")
    try:
        validate_agent_result_payload(missing_model)
    except ContractValidationError:
        pass
    else:
        raise AssertionError("AgentResult 缺少 model_version 时必须被拒绝")
    assert CANDIDATE_STATUSES == {
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

    for forbidden in ("buy", "sell", "target_price", "rank", "score", "confidence", "final_rating"):
        invalid = dict(base)
        invalid[forbidden] = "forbidden"
        try:
            validate_agent_result_payload(invalid)
        except ContractValidationError:
            pass
        else:
            raise AssertionError(f"正式 Agent 结果必须拒绝字段：{forbidden}")

    invalid_claim = dict(base)
    invalid_claim["claims"] = [{"type": "supports_lens", "text": "建议买入"}]
    try:
        validate_agent_result_payload(invalid_claim)
    except ContractValidationError:
        pass
    else:
        raise AssertionError("正式 Agent 结果必须拒绝交易性文本")

    assert selection_policy_version_from_legacy("v1") == "legacy_formula.v1"
    compatibility = legacy_compatibility_identity(formula_version="v1", lens_version="v1")
    assert compatibility["formal_agent_allowed"] is False
    assert compatibility["selection_policy_version"] == "legacy_formula.v1"
    print("[OK] Phase 0 evidence contracts passed")


if __name__ == "__main__":
    run_tests()
