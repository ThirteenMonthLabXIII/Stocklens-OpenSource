"""Offline acceptance checks for Phase 5 F/G evidence and state layers."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.candidate_state import (  # noqa: E402
    CandidateStateError,
    CandidateStateService,
    MemoryCandidateStateStore,
    SqliteCandidateStateStore,
    transition_candidate_state,
)
from stock_tool.core.evidence_summary import (  # noqa: E402
    EvidenceAggregationError,
    aggregate_agent_evidence,
)
from stock_tool.core.multi_agent_orchestrator import (  # noqa: E402
    FIVE_AGENT_IDS,
    FiveAgentOrchestrator,
)
from tools.test_phase4_multi_agent import _config, _package, _payload, _requesters  # noqa: E402


def _requesters_with_conflict(package: dict) -> dict:
    fact_by_field = {item["field_name"]: item["fact_id"] for item in package["facts"]}
    requesters = _requesters()

    def technical(_config_value, _prompt):
        payload = _payload()
        payload["claims"] = [{
            "claim_id": "technical-support-001",
            "type": "supports_lens",
            "text": "技术窗口事实可回溯。",
            "evidence_refs": [fact_by_field["trend_20d"]],
            "caveats": [],
            "code": "000001",
        }]
        return payload, "fixture-cost", "{\"technical\":true}"

    def fundflow(_config_value, _prompt):
        payload = _payload()
        payload["claims"] = [{
            "claim_id": "fundflow-opposing-001",
            "type": "opposes_lens",
            "text": "资金流事实需要人工反向核查。",
            "evidence_refs": [fact_by_field["money_flow_5d"]],
            "caveats": [],
            "code": "000001",
        }]
        return payload, "fixture-cost", "{\"fundflow\":true}"

    requesters["technical_state"] = technical
    requesters["fundflow_participation"] = fundflow
    return requesters


def test_f_layer_binds_claim_ids_and_preserves_conflict() -> None:
    package = _package()
    orchestration = FiveAgentOrchestrator(
        _config(), requesters=_requesters_with_conflict(package), retries=0
    ).run(package, run_id="phase5-conflict")
    summary = aggregate_agent_evidence(package, orchestration)

    assert summary.status == "ok"
    assert summary.candidate_set_preserved
    item = summary.summaries[0]
    assert item.supporting_claim_ids == ("technical-support-001",)
    assert item.opposing_claim_ids == ("fundflow-opposing-001",)
    assert item.conflicts
    assert item.conflicts[0]["supporting_claim_ids"] == ["technical-support-001"]
    assert item.candidate_type_labels == ("quality_research", "trend_observation")
    payload = summary.as_dict()
    assert "candidate_status" not in str(payload)
    assert "score" not in str(payload)


def test_f_layer_rejects_changed_candidate_set() -> None:
    package = _package()
    orchestration = FiveAgentOrchestrator(
        _config(), requesters=_requesters(), retries=0
    ).run(package, run_id="phase5-preservation")
    changed = orchestration.as_dict()
    changed["candidate_records"] = [{"code": "999999", "name": "changed"}]
    try:
        aggregate_agent_evidence(package, changed)
    except EvidenceAggregationError:
        pass
    else:
        raise AssertionError("F 层必须拒绝被修改的候选集合")


def test_f_layer_rejects_mismatched_execution_identity() -> None:
    package = _package()
    orchestration = FiveAgentOrchestrator(
        _config(), requesters=_requesters(), retries=0
    ).run(package, run_id="phase5-identity")
    changed = orchestration.as_dict()
    changed["executions"][0]["candidate_set_digest"] = "wrong-candidate-set"
    try:
        aggregate_agent_evidence(package, changed)
    except EvidenceAggregationError:
        pass
    else:
        raise AssertionError("F 层必须拒绝证据包身份不一致的 Agent 执行记录")


def test_f_layer_rejects_incomplete_or_unknown_agent_set() -> None:
    package = _package()
    orchestration = FiveAgentOrchestrator(
        _config(), requesters=_requesters(), retries=0
    ).run(package, run_id="phase5-agent-set")
    incomplete = orchestration.as_dict()
    incomplete["executions"] = incomplete["executions"][:-1]
    try:
        aggregate_agent_evidence(package, incomplete)
    except EvidenceAggregationError:
        pass
    else:
        raise AssertionError("F 层不得把不完整的五 Agent 记录误报为 ok")

    unknown = orchestration.as_dict()
    item = unknown["executions"][0]
    item["agent_id"] = "unknown-agent"
    item["result"]["agent_id"] = "unknown-agent"
    try:
        aggregate_agent_evidence(package, unknown)
    except EvidenceAggregationError:
        pass
    else:
        raise AssertionError("F 层必须拒绝未知 Agent")


def test_f_layer_rejects_run_id_mismatch() -> None:
    package = _package()
    orchestration = FiveAgentOrchestrator(
        _config(), requesters=_requesters(), retries=0
    ).run(package, run_id="phase5-run-id")
    changed = orchestration.as_dict()
    changed["executions"][0]["run_id"] = "wrong-run-id"
    try:
        aggregate_agent_evidence(package, changed)
    except EvidenceAggregationError:
        pass
    else:
        raise AssertionError("F 层必须拒绝执行信封 run_id 不一致")


def test_f_layer_one_agent_failure_is_partial_but_records_remain() -> None:
    package = _package()
    orchestration = FiveAgentOrchestrator(
        _config(), requesters=_requesters(broken="technical_state"), retries=0
    ).run(package, run_id="phase5-failure")
    # One failure is isolated by Phase 4; the F layer still reports a usable
    # aggregate and does not turn operational failure into a candidate edit.
    summary = aggregate_agent_evidence(package, orchestration)
    assert summary.status == "partial"
    assert summary.candidate_set_preserved


def test_f_layer_all_agent_failures_are_failed_but_records_remain() -> None:
    package = _package()

    def failure(_config_value, _prompt):
        raise TimeoutError("fixture timeout")

    orchestration = FiveAgentOrchestrator(
        _config(), requesters={agent_id: failure for agent_id in FIVE_AGENT_IDS}, retries=0
    ).run(package, run_id="phase5-all-failed")
    summary = aggregate_agent_evidence(package, orchestration)
    assert summary.status == "failed"
    assert summary.candidate_set_preserved
    assert summary.summaries[0].status == "failed"


def test_g_layer_requires_human_watch_action_and_records_transitions() -> None:
    store = MemoryCandidateStateStore()
    service = CandidateStateService(store)
    record = {
        "candidate_id": "000001",
        "code": "000001",
        "eligibility_status": "eligible",
        "lens_ids": ["quality_research"],
        "candidate_digest": "digest-1",
        "reason_codes": ["CANDIDATE_ELIGIBLE"],
    }
    batch = service.resolve_batch(
        [record],
        [],
        snapshot_id="snapshot-1",
        created_at="2026-07-28T01:00:00+00:00",
    )
    assert batch.candidate_set_preserved
    assert batch.states[0].status == "candidate"
    assert [(item.from_status, item.to_status) for item in batch.transitions] == [("eligible", "candidate")]

    try:
        service.add_to_watch(
            "000001",
            actor="agent:fundamental",
            reason_codes=["AI_REQUEST"],
            snapshot_id="snapshot-1",
        )
    except CandidateStateError:
        pass
    else:
        raise AssertionError("Agent 不能自动加入 watch")

    try:
        service.add_to_watch(
            "000001",
            actor="automated",
            reason_codes=["SPOOFED_HUMAN_ACTION"],
            snapshot_id="snapshot-1",
        )
    except CandidateStateError:
        pass
    else:
        raise AssertionError("自动化 actor 不能伪造人工加入 watch")

    state, transition = service.add_to_watch(
        "000001",
        actor="human:test",
        reason_codes=["MANUAL_REVIEW_CONFIRMED"],
        snapshot_id="snapshot-1",
        created_at="2026-07-28T01:01:00+00:00",
    )
    assert state.status == "watch"
    assert (transition.from_status, transition.to_status) == ("candidate", "watch")
    assert transition.actor == "human:test"
    assert len(store.list_transitions("000001")) == 2


def test_g_layer_cannot_bootstrap_watch_without_manual_action() -> None:
    service = CandidateStateService(MemoryCandidateStateStore())
    try:
        service.resolve_one(
            {"code": "000004", "eligibility_status": "watch"},
            None,
            snapshot_id="snapshot-watch-bootstrap",
        )
    except CandidateStateError:
        pass
    else:
        raise AssertionError("没有人工动作时不得初次建立 watch 状态")


def test_g_layer_does_not_promote_insufficient_or_conflicted_automatically() -> None:
    store = MemoryCandidateStateStore()
    service = CandidateStateService(store)
    insufficient = {
        "candidate_id": "000002",
        "code": "000002",
        "eligibility_status": "eligible",
        "missing_fields": ["roe"],
        "reason_codes": ["MISSING_ROE"],
    }
    first, first_transition = service.resolve_one(
        insufficient,
        None,
        snapshot_id="snapshot-1",
        created_at="2026-07-28T01:00:00+00:00",
    )
    assert first.status == "insufficient"
    assert first_transition is not None
    assert (first_transition.from_status, first_transition.to_status) == ("eligible", "insufficient")
    recovered = dict(insufficient)
    recovered["missing_fields"] = []
    recovered["reason_codes"] = ["CANDIDATE_ELIGIBLE"]
    second, second_transition = service.resolve_one(
        recovered,
        {"candidate_id": "000002", "code": "000002", "status": "ok"},
        snapshot_id="snapshot-2",
        created_at="2026-07-28T02:00:00+00:00",
    )
    assert second.status == "insufficient"
    assert second_transition is None

    try:
        transition_candidate_state(
            second,
            "candidate",
            reason_codes=["AUTO_PROMOTION"],
            actor="local_rule",
            snapshot_id="snapshot-2",
        )
    except CandidateStateError:
        pass
    else:
        raise AssertionError("insufficient 不得自动升级为 candidate")


def test_g_layer_requires_summary_candidate_and_code_identity() -> None:
    service = CandidateStateService(MemoryCandidateStateStore())
    state, _ = service.resolve_one(
        {"candidate_id": "candidate-1", "code": "000005", "eligibility_status": "eligible"},
        {"candidate_id": "candidate-1", "code": "999999", "status": "ok", "conflicts": [{"id": "wrong"}]},
        snapshot_id="snapshot-identity",
        created_at="2026-07-28T04:00:00+00:00",
    )
    assert state.status == "candidate"


def test_g_layer_allows_local_candidate_exclusion_with_audit() -> None:
    store = MemoryCandidateStateStore()
    service = CandidateStateService(store)
    state, _ = service.resolve_one(
        {"code": "000006", "eligibility_status": "eligible"},
        None,
        snapshot_id="snapshot-excluded-1",
        created_at="2026-07-28T05:00:00+00:00",
    )
    transition = transition_candidate_state(
        state,
        "excluded",
        reason_codes=["LOCAL_ELIGIBILITY_REVOKED"],
        actor="local_rule",
        snapshot_id="snapshot-excluded-2",
        created_at="2026-07-28T06:00:00+00:00",
    )
    assert (transition.from_status, transition.to_status) == ("candidate", "excluded")


def test_sqlite_state_and_transition_replay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = SqliteCandidateStateStore(str(Path(directory) / "candidate-state.sqlite3"))
        service = CandidateStateService(store)
        service.resolve_one(
            {
                "candidate_id": "000003",
                "code": "000003",
                "eligibility_status": "eligible",
                "reason_codes": ["CANDIDATE_ELIGIBLE"],
            },
            None,
            snapshot_id="snapshot-sqlite",
            created_at="2026-07-28T03:00:00+00:00",
        )
        assert store.get("000003") is not None
        assert store.get("000003").status == "candidate"
        assert len(store.list_transitions("000003")) == 1


def run_tests() -> None:
    for test in (
        test_f_layer_binds_claim_ids_and_preserves_conflict,
        test_f_layer_rejects_changed_candidate_set,
        test_f_layer_rejects_mismatched_execution_identity,
        test_f_layer_rejects_incomplete_or_unknown_agent_set,
        test_f_layer_rejects_run_id_mismatch,
        test_f_layer_one_agent_failure_is_partial_but_records_remain,
        test_f_layer_all_agent_failures_are_failed_but_records_remain,
        test_g_layer_requires_human_watch_action_and_records_transitions,
        test_g_layer_cannot_bootstrap_watch_without_manual_action,
        test_g_layer_does_not_promote_insufficient_or_conflicted_automatically,
        test_g_layer_requires_summary_candidate_and_code_identity,
        test_g_layer_allows_local_candidate_exclusion_with_audit,
        test_sqlite_state_and_transition_replay,
    ):
        test()
    print("[OK] Phase 5 evidence summary and candidate state tests passed")


if __name__ == "__main__":
    run_tests()
