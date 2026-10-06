"""Offline acceptance checks for the five-Agent phase-4 orchestrator."""

from __future__ import annotations

import sys
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.ai_analyzer import AIConfig
from stock_tool.core.data_snapshot import DataSnapshotFact, DataSnapshotManifest, fact_set_digest
from stock_tool.core.multi_agent_orchestrator import (
    FIVE_AGENT_IDS,
    FiveAgentOrchestrator,
    MemoryAgentExecutionCache,
    MemoryAgentExecutionStore,
    SqliteAgentExecutionStore,
    format_agent_statuses,
)
from stock_tool.core.research_discussion import build_evidence_package


ALL_FIELDS = {
    "report_date": "2026-03-31",
    "roe": 18.5,
    "debt_ratio": 45.0,
    "gross_margin": 32.0,
    "net_margin": 12.0,
    "revenue_growth": 15.0,
    "profit_growth": 20.0,
    "operating_cashflow_per_share": 0.8,
    "pe_ttm": 14.0,
    "industry": "示例行业",
    "industry_strength": 0.7,
    "market_regime": "range",
    "price": 10.0,
    "technical_status": "box",
    "updated_at": "2026-06-30T00:00:00+00:00",
    "trend_20d": 0.04,
    "fundflow_status": "observed",
    "money_flow_5d": 1200000.0,
    "event_title": "已核验公告资料",
    "event_date": "2026-06-20",
    "event_source": "fixture-announcement",
}


def _package(*, drop: set[str] | None = None, failover: bool = False) -> dict:
    values = {key: value for key, value in ALL_FIELDS.items() if key not in (drop or set())}
    facts = [
        DataSnapshotFact(
            snapshot_id="seed",
            code="000001",
            field_name=field_name,
            value=value,
            effective_at=str(value) if field_name == "report_date" else "2026-06-30T00:00:00+00:00",
            available_at="2026-06-30T00:00:00+00:00",
            source="fixture-fallback" if failover else "fixture-primary",
            source_version="fixture-v1",
            fetched_at="2026-07-01T00:00:00+00:00",
            source_route="fallback" if failover else "primary",
        )
        for field_name, value in values.items()
    ]
    manifest = DataSnapshotManifest.create(
        asof_at="2026-07-01T00:00:00+00:00",
        availability_cutoff="2026-06-30T00:00:00+00:00",
        data_source="fixture",
        universe_id="fixture-universe",
        dataset_version="fixture-v1",
        facts_digest=fact_set_digest(facts),
        quality_status="verified",
        created_at="2026-07-01T00:00:00+00:00",
    )
    facts = [
        DataSnapshotFact(
            snapshot_id=manifest.snapshot_id,
            code=fact.code,
            field_name=fact.field_name,
            value=fact.value,
            effective_at=fact.effective_at,
            available_at=fact.available_at,
            source=fact.source,
            source_version=fact.source_version,
            fetched_at=fact.fetched_at,
            source_route=fact.source_route,
        )
        for fact in facts
    ]
    return build_evidence_package(
        data_snapshot_id=manifest.snapshot_id,
        refresh_event_id="refresh-phase4",
        formula_version="lens.v1",
        snapshot_metadata=manifest,
        facts=facts,
        candidate_records=[
            {
                "code": "000001",
                "name": "五 Agent 样例",
                "lens_ids": ["quality_research", "trend_observation"],
                "eligibility_status": "candidate",
                "reason_codes": ["LENS_QUALITY_RESEARCH"],
                "data_snapshot_id": manifest.snapshot_id,
            }
        ],
        external_facts=[
            {
                "external_fact_id": "external-phase4-001",
                "code": "000001",
                "text": "外部公告资料（仅用于人工核查）",
                "source": "fixture-announcement",
                "effective_at": "2026-06-20",
            }
        ],
    )


def _payload() -> dict:
    return {
        "status": "ok",
        "coverage": {"reported_by": "fixture"},
        "claims": [],
        "counter_claims": [],
        "missing_evidence": [],
        "risk_flags": [],
        "unresolved_questions": [],
        "diagnostics": {"fixture": True},
        "cost_note": "fixture-cost",
    }


def _market_batch_package() -> dict:
    facts = [
        DataSnapshotFact(
            snapshot_id="market-seed",
            code="__market__",
            field_name="industry",
            value="示例行业",
            effective_at="2026-06-30T00:00:00+00:00",
            available_at="2026-06-30T00:00:00+00:00",
            source="fixture-primary",
            source_version="fixture-v1",
            fetched_at="2026-07-01T00:00:00+00:00",
        ),
        DataSnapshotFact(
            snapshot_id="market-seed",
            code="__market__",
            field_name="industry_strength",
            value=0.8,
            effective_at="2026-06-30T00:00:00+00:00",
            available_at="2026-06-30T00:00:00+00:00",
            source="fixture-primary",
            source_version="fixture-v1",
            fetched_at="2026-07-01T00:00:00+00:00",
        )
    ]
    manifest = DataSnapshotManifest.create(
        asof_at="2026-07-01T00:00:00+00:00",
        availability_cutoff="2026-06-30T00:00:00+00:00",
        data_source="fixture",
        universe_id="fixture-market",
        dataset_version="fixture-v1",
        facts_digest=fact_set_digest(facts),
        created_at="2026-07-01T00:00:00+00:00",
    )
    fact = DataSnapshotFact(
        snapshot_id=manifest.snapshot_id,
        code="__market__",
        field_name="industry",
        value="示例行业",
        effective_at="2026-06-30T00:00:00+00:00",
        available_at="2026-06-30T00:00:00+00:00",
        source="fixture-primary",
        source_version="fixture-v1",
        fetched_at="2026-07-01T00:00:00+00:00",
    )
    strength_fact = DataSnapshotFact(
        snapshot_id=manifest.snapshot_id,
        code="__market__",
        field_name="industry_strength",
        value=0.8,
        effective_at="2026-06-30T00:00:00+00:00",
        available_at="2026-06-30T00:00:00+00:00",
        source="fixture-primary",
        source_version="fixture-v1",
        fetched_at="2026-07-01T00:00:00+00:00",
    )
    return build_evidence_package(
        data_snapshot_id=manifest.snapshot_id,
        refresh_event_id="refresh-market",
        formula_version="lens.v1",
        snapshot_metadata=manifest,
        facts=[fact, strength_fact],
        candidate_records=[{"code": "000001", "name": "市场批量样例", "eligibility_status": "candidate"}],
    )


def _requesters(*, broken: str | None = None, invalid: str | None = None):
    def requester(agent_id: str):
        def call(_config, _prompt):
            if agent_id == broken:
                raise TimeoutError(f"{agent_id} fixture timeout")
            payload = _payload()
            if agent_id == invalid:
                payload["claims"] = [{
                    "claim_id": "invented-ref",
                    "type": "supports_lens",
                    "text": "引用不存在的事实",
                    "evidence_refs": ["fact-does-not-exist"],
                    "caveats": [],
                    "code": "000001",
                }]
            return payload, "fixture-cost", "{" + f'"agent":"{agent_id}"' + "}"
        return call
    return {agent_id: requester(agent_id) for agent_id in FIVE_AGENT_IDS}


def _config() -> AIConfig:
    return AIConfig(provider="fixture", api_key="fixture", model="fixture-model")


def test_five_agents_are_versioned_and_cache_key_is_complete() -> None:
    package = _package()
    result = FiveAgentOrchestrator(_config(), requesters=_requesters(), retries=0).run(package, run_id="run-key")
    assert tuple(item.agent_id for item in result.executions) == FIVE_AGENT_IDS
    assert all(item.agent_version and item.prompt_version and item.model_version for item in result.executions)
    assert all(len(item.cache_key) == 64 for item in result.executions)
    assert result.candidate_set_preserved
    assert result.candidate_records == tuple(package["candidate_records"])


def test_one_agent_failure_isolated_and_all_results_are_saved() -> None:
    store = MemoryAgentExecutionStore()
    result = FiveAgentOrchestrator(
        _config(), requesters=_requesters(broken="technical_state"), retries=1, result_store=store
    ).run(_package(), run_id="run-isolated")
    assert result.status == "partial"
    assert result.statuses["technical_state"] == "failed"
    assert result.statuses["fundamental"] == "ok"
    assert result.candidate_set_preserved
    assert {item.agent_id for item in store.list()} == set(FIVE_AGENT_IDS)


def test_invalid_claim_is_dropped_and_marked_without_poisoning_other_claims() -> None:
    result = FiveAgentOrchestrator(
        _config(), requesters=_requesters(invalid="event_risk"), retries=0
    ).run(_package(), run_id="run-invalid")
    event = next(item for item in result.executions if item.agent_id == "event_risk")
    assert event.status == "degraded"
    assert event.outcome == "evidence_invalid"
    assert event.result is not None and not event.result.claims
    assert event.result.diagnostics.get("invalid_claim_ids") == ["invented-ref"]
    assert result.candidate_records[0]["code"] == "000001"


def test_market_agent_accepts_batch_level_fact_without_changing_candidates() -> None:
    package = _market_batch_package()
    fact_id = package["facts"][0]["fact_id"]
    requesters = _requesters()

    def market_requester(_config, _prompt):
        payload = _payload()
        payload["claims"] = [{
            "claim_id": "market-001",
            "type": "context",
            "text": "市场批量事实可回溯",
            "evidence_refs": [fact_id],
            "caveats": [],
        }]
        return payload, "fixture-cost", "{\"market\":true}"

    requesters["market_industry"] = market_requester
    result = FiveAgentOrchestrator(_config(), requesters=requesters, retries=0).run(package, run_id="run-market")
    market = next(item for item in result.executions if item.agent_id == "market_industry")
    assert market.status == "ok"
    assert market.result is not None and market.result.claims[0]["evidence_refs"] == [fact_id]
    assert result.candidate_set_preserved


def test_missing_key_fact_is_insufficient_and_failover_is_not_verified() -> None:
    missing = FiveAgentOrchestrator(
        _config(), requesters=_requesters(), retries=0
    ).run(_package(drop={"updated_at"}), run_id="run-missing")
    fundflow = next(item for item in missing.executions if item.agent_id == "fundflow_participation")
    assert fundflow.status == "degraded"
    assert fundflow.outcome == "insufficient"
    assert fundflow.result is not None and "000001.updated_at" in fundflow.result.missing_evidence

    failover = FiveAgentOrchestrator(
        _config(), requesters=_requesters(), retries=0
    ).run(_package(failover=True), run_id="run-failover")
    assert any(item.outcome == "failover" for item in failover.executions)


def test_cache_replays_same_input_without_reinvoking_agents() -> None:
    cache = MemoryAgentExecutionCache()
    first = FiveAgentOrchestrator(_config(), requesters=_requesters(), cache=cache, retries=0).run(
        _package(), run_id="run-cache-1"
    )
    calls = 0

    def should_not_call(_config, _prompt):
        nonlocal calls
        calls += 1
        raise AssertionError("cache miss")

    second = FiveAgentOrchestrator(
        _config(),
        requesters={agent_id: should_not_call for agent_id in FIVE_AGENT_IDS},
        cache=cache,
        retries=0,
    ).run(_package(), run_id="run-cache-2")
    assert calls == 0
    assert all(item.cache_hit for item in second.executions)
    assert all(item.result is not None and item.result.run_id == "run-cache-2" for item in second.executions)
    assert first.candidate_records == second.candidate_records


def test_sqlite_store_keeps_history_separate_from_latest_cache() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = SqliteAgentExecutionStore(str(Path(directory) / "agent.sqlite3"))
        first = FiveAgentOrchestrator(_config(), requesters=_requesters(), result_store=store, cache=store, retries=0).run(
            _package(), run_id="run-sqlite-1"
        )
        second = FiveAgentOrchestrator(_config(), requesters=_requesters(), result_store=store, cache=store, retries=0).run(
            _package(), run_id="run-sqlite-2"
        )
        assert len(store.list()) == 10
        assert all(item.cache_hit for item in second.executions)
        assert first.snapshot_id == second.snapshot_id
        assert first.executions[0].evidence_package_hash


def test_ui_status_text_keeps_status_and_operational_outcome_distinct() -> None:
    result = FiveAgentOrchestrator(
        _config(), requesters=_requesters(invalid="event_risk"), retries=0
    ).run(_package(), run_id="run-ui")
    text = format_agent_statuses(result)
    assert "technical_state: 正常" in text
    assert "event_risk: 证据无效" in text


def run_tests() -> None:
    for test in (
        test_five_agents_are_versioned_and_cache_key_is_complete,
        test_one_agent_failure_isolated_and_all_results_are_saved,
        test_invalid_claim_is_dropped_and_marked_without_poisoning_other_claims,
        test_market_agent_accepts_batch_level_fact_without_changing_candidates,
        test_missing_key_fact_is_insufficient_and_failover_is_not_verified,
        test_cache_replays_same_input_without_reinvoking_agents,
        test_ui_status_text_keeps_status_and_operational_outcome_distinct,
    ):
        test()
    print("[OK] Phase 4 five-Agent orchestration tests passed")


if __name__ == "__main__":
    run_tests()
