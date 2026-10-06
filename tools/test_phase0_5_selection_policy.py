"""Offline acceptance checks for Phase 0.5 selection policy freeze."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.missing_data_policy import MissingDataPolicy
from stock_tool.core.data_snapshot import DataSnapshotFact, DataSnapshotManifest, DataSnapshotStore, fact_set_digest
from stock_tool.core.evidence_contracts import Fact
from stock_tool.core.exploratory_proposal_store import SqliteExploratoryProposalStore, proposals_from_agent_result
from stock_tool.core.formal_certificate import FormalScreeningCertificateStore
from stock_tool.core.priority_policy import FORBIDDEN_PRIORITY_FIELDS, LENS_SORT_FIELDS, PriorityPolicy
from stock_tool.core.factor_store import FactorSnapshot, V1FactorStore
from stock_tool.core.query_schema import query_spec_from_dict
from stock_tool.core.selection_policy import (
    DEFAULT_SELECTION_POLICY,
    ExploratoryProposal,
    SelectionPolicyInput,
    SelectionPolicy,
)
from stock_tool.core.screener_engine import ScreenFilter, screen_factor_snapshots
from stock_tool.core.screening_workflow import ScreeningWorkflowService
from stock_tool.core.strategy_store import StrategyStore
from stock_tool.core.universe_policy import DEFAULT_UNIVERSE_PROFILE, evaluate_universe_member
from stock_tool.core.v1_database import V1Database


def _member(**overrides):
    value = {
        "code": "000001",
        "name": "验收样例",
        "board_id": "main_board",
        "status": "listed",
        "snapshot_id": "snapshot-phase0-5",
        "available_at": "2026-07-23",
        "as_of": "2026-07-23",
        "listing_age_trading_days": 180,
        "valid_turnover_days_20d": 20,
        "valid_price_volume_days": 20,
        "median_turnover_value_20d": 30_000_000,
        "snapshot_quality_status": "verified",
        "latest_report_age_days": 30,
        "financial_quality_status": "verified",
        "report_date": "2026-06-30",
        "available_at": "2026-07-01",
        "verified_report_periods": 4,
        "roe_ttm": 12,
        "net_profit_positive_periods": 4,
        "operating_cashflow_positive_periods": 4,
        "industry_sector": "制造",
        "industry_classification_id": "csrc2012",
        "industry_classification_version": "2026.1",
        "industry_sample_count": 30,
        "peer_set_digest": "peer-digest-1",
        "debt_ratio": 50,
        "debt_sector_percentile": 50,
        "adjusted_price_series": True,
        "adjustment_factor_missing": False,
        "adjustment_factor": 1.0,
        "corporate_action_fact_id": "ca-1",
        "adjustment_method": "split_adjusted",
        "valid_price_days_20d": 20,
        "valid_volume_days_20d": 20,
        "trend_20d": 5,
        "trend_60d": 2,
        "relative_strength_20d": 3,
        "median_turnover_value_20d": 30_000_000,
        "technical_fields_quality": "verified",
        "events": [
            {
                "code": "000001",
                "event_title": "定期报告披露",
                "event_date": "2026-07-20",
                "available_at": "2026-07-20",
                "event_source": "exchange",
                "source_tier": 1,
                "source_route_verified": True,
                "deduped": True,
            }
        ],
    }
    value.update(overrides)
    return value


def test_four_board_profiles_are_independent() -> None:
    profile = DEFAULT_UNIVERSE_PROFILE
    assert profile.universe_id == "a_share_all"
    assert {item.board_id for item in profile.board_profiles} == {"main_board", "chinext", "star_market", "beijing"}
    assert len({item.price_limit_rule_id for item in profile.board_profiles}) == 4
    assert len({item.liquidity_policy_id for item in profile.board_profiles}) == 4
    for board_id in ("main_board", "chinext", "star_market", "beijing"):
        decision = evaluate_universe_member(_member(board_id=board_id), availability_cutoff="2026-07-23T12:00:00+08:00")
        assert decision.status == "eligible"
        assert decision.board_profile is not None
        assert decision.facts["price_limit_rule_id"] == decision.board_profile.price_limit_rule_id
        assert decision.facts["price_limit_rule_version"] == decision.board_profile.price_limit_rule_version


def test_universe_hard_gates_honor_explicit_status_flags_and_blocked_snapshot() -> None:
    assert "EXCLUDED_ST" in evaluate_universe_member(_member(is_st=True), availability_cutoff="2026-07-23").reason_codes
    assert "EXCLUDED_DELISTING" in evaluate_universe_member(_member(is_delisting=True), availability_cutoff="2026-07-23").reason_codes
    blocked = evaluate_universe_member(_member(snapshot_quality_status="blocked"), availability_cutoff="2026-07-23")
    assert blocked.status == "excluded"
    assert "SNAPSHOT_QUALITY_BLOCKED" in blocked.reason_codes
    assert "snapshot_quality_status" in blocked.blocking_fields


def test_universe_exclusions_and_identity_gate() -> None:
    assert "EXCLUDED_ST" in evaluate_universe_member(_member(name="ST样例"), availability_cutoff="2026-07-23").reason_codes
    assert "EXCLUDED_DELISTING" in evaluate_universe_member(_member(status="退市整理"), availability_cutoff="2026-07-23").reason_codes
    assert "SUSPENDED_NO_VALID_PRICE" in evaluate_universe_member(_member(suspended=True, price=None), availability_cutoff="2026-07-23").reason_codes
    assert "SNAPSHOT_IDENTITY_INCOMPLETE" in evaluate_universe_member(_member(snapshot_id=""), availability_cutoff="2026-07-23").reason_codes
    assert "FUTURE_AVAILABLE_AT" in evaluate_universe_member(_member(available_at="2026-07-24"), availability_cutoff="2026-07-23").reason_codes


def test_listing_age_uses_trading_days_and_modes_are_separate() -> None:
    formal = evaluate_universe_member(_member(listing_age_trading_days=119), availability_cutoff="2026-07-23")
    exploratory = evaluate_universe_member(_member(listing_age_trading_days=119), mode="exploratory", availability_cutoff="2026-07-23")
    assert formal.status == "excluded"
    assert "NEW_LISTING_FORMAL_BLOCK" in formal.reason_codes
    assert exploratory.status == "eligible"
    assert exploratory.exploratory_only is True
    very_new = evaluate_universe_member(_member(listing_age_trading_days=19), mode="exploratory", availability_cutoff="2026-07-23")
    assert very_new.status == "insufficient"


def test_liquidity_sample_gate_does_not_infer_from_one_or_two_days() -> None:
    decision = evaluate_universe_member(
        _member(valid_turnover_days_20d=2, valid_price_volume_days=2, median_turnover_value_20d=30_000_000),
        availability_cutoff="2026-07-23",
    )
    assert decision.status == "excluded"
    assert "LIQUIDITY_TURNOVER_DAYS_INSUFFICIENT" in decision.reason_codes
    missing = evaluate_universe_member(_member(valid_turnover_days_20d=None, valid_price_volume_days=None, median_turnover_value_20d=None), availability_cutoff="2026-07-23")
    assert missing.status == "insufficient"
    assert "LIQUIDITY_SAMPLE_INSUFFICIENT" in missing.reason_codes


def test_quality_formal_thresholds_and_special_industries() -> None:
    formal = DEFAULT_SELECTION_POLICY.evaluate(_member(), availability_cutoff="2026-07-23")
    assert formal.status == "candidate"
    assert "quality_research" in formal.matched_lenses
    low = DEFAULT_SELECTION_POLICY.evaluate(_member(roe_ttm=7), availability_cutoff="2026-07-23")
    quality = {item.lens_id: item for item in low.lens_results}["quality_research"]
    assert quality.status == "excluded"
    bank = DEFAULT_SELECTION_POLICY.evaluate(
        _member(industry_sector="银行", debt_ratio=30, financial_debt_metric=None, debt_sector_percentile=None),
        availability_cutoff="2026-07-23",
    )
    bank_quality = {item.lens_id: item for item in bank.lens_results}["quality_research"]
    assert bank_quality.status == "insufficient"
    assert "FINANCIAL_DEBT_FIELD_MISSING" in bank_quality.reason_codes
    property_result = DEFAULT_SELECTION_POLICY.evaluate(
        _member(industry_sector="公用事业", debt_ratio=95, debt_sector_percentile=50),
        availability_cutoff="2026-07-23",
    )
    assert {item.lens_id: item for item in property_result.lens_results}["quality_research"].status == "matched"


def test_pit_freshness_and_future_financial_facts() -> None:
    stale = DEFAULT_SELECTION_POLICY.evaluate(_member(latest_report_age_days=366), availability_cutoff="2026-07-23")
    quality = {item.lens_id: item for item in stale.lens_results}["quality_research"]
    assert quality.status == "stale"
    future = DEFAULT_SELECTION_POLICY.evaluate(
        _member(financial_reports=[{"report_date": "2026-06-30", "available_at": "2026-07-24", "quality_status": "verified"}]),
        availability_cutoff="2026-07-23",
    )
    assert "FUTURE_AVAILABLE_AT" in {item.lens_id: item for item in future.lens_results}["quality_research"].reason_codes


def test_financial_period_missing_is_not_counted_as_negative_or_positive() -> None:
    reports = [
        {"report_date": f"2026-0{month}-30", "available_at": f"2026-0{month + 1}-01", "net_profit": 10, "operating_cashflow": 5}
        for month in range(1, 4)
    ]
    reports.append({"report_date": "2025-12-31", "available_at": "2026-01-15", "net_profit": 10})
    result = DEFAULT_SELECTION_POLICY.evaluate(
        _member(
            financial_reports=reports,
            verified_report_periods=4,
            net_profit_positive_periods=None,
            operating_cashflow_positive_periods=None,
        ),
        availability_cutoff="2026-07-23",
    )
    quality = {item.lens_id: item for item in result.lens_results}["quality_research"]
    assert quality.status == "insufficient"
    assert "operating_cashflow_positive_periods" in quality.missing_fields


def test_trend_uses_adjusted_series_and_price_limit_is_insufficient() -> None:
    missing_adjustment = DEFAULT_SELECTION_POLICY.evaluate(_member(adjusted_price_series=False), availability_cutoff="2026-07-23")
    trend = {item.lens_id: item for item in missing_adjustment.lens_results}["trend_observation"]
    assert trend.status == "insufficient"
    assert "ADJUSTED_PRICE_REQUIRED" in trend.reason_codes
    locked = DEFAULT_SELECTION_POLICY.evaluate(_member(price_limit_locked=True), availability_cutoff="2026-07-23")
    trend = {item.lens_id: item for item in locked.lens_results}["trend_observation"]
    assert trend.status == "insufficient"
    assert "TREND_OBSERVATION_INSUFFICIENT_PRICE_LIMIT" in trend.reason_codes
    assert locked.facts["price_limit_locked"] is True
    exploratory = DEFAULT_SELECTION_POLICY.evaluate(_member(relative_strength_20d=None), mode="exploratory", availability_cutoff="2026-07-23")
    exploratory_trend = {item.lens_id: item for item in exploratory.lens_results}["trend_observation"]
    assert exploratory_trend.status == "insufficient"
    assert "RELATIVE_STRENGTH_INSUFFICIENT" in exploratory_trend.reason_codes


def test_event_source_cutoff_expiry_and_tier_rules() -> None:
    future = DEFAULT_SELECTION_POLICY.evaluate(
        _member(events=[{"code": "000001", "event_date": "2026-07-22", "available_at": "2026-07-24", "source_tier": 1, "source_route_verified": True}]),
        availability_cutoff="2026-07-23",
    )
    assert "FUTURE_AVAILABLE_AT" in {item.lens_id: item for item in future.lens_results}["event_verification"].reason_codes
    tier2 = DEFAULT_SELECTION_POLICY.evaluate(
        _member(events=[{"code": "000001", "event_date": "2026-07-22", "available_at": "2026-07-22", "source_tier": 2, "source_route_verified": True}]),
        availability_cutoff="2026-07-23",
    )
    assert {item.lens_id: item for item in tier2.lens_results}["event_verification"].status == "insufficient"
    old = DEFAULT_SELECTION_POLICY.evaluate(
        _member(events=[{"code": "000001", "event_date": "2026-06-01", "available_at": "2026-06-01", "source_tier": 1, "source_route_verified": True}]),
        availability_cutoff="2026-07-23",
    )
    assert {item.lens_id: item for item in old.lens_results}["event_verification"].status == "stale"
    exploratory_tier2 = DEFAULT_SELECTION_POLICY.evaluate(
        _member(events=[{"code": "000001", "event_date": "2026-07-22", "available_at": "2026-07-22", "source_tier": 2, "source_route_verified": True}]),
        mode="exploratory",
        availability_cutoff="2026-07-23",
    )
    partial = {item.lens_id: item for item in exploratory_tier2.lens_results}["event_verification"]
    assert partial.status == "partial"
    assert "SOURCE_TIER_PARTIAL" in partial.reason_codes


def test_event_code_must_match_the_candidate() -> None:
    result = DEFAULT_SELECTION_POLICY.evaluate(
        _member(events=[{"code": "000002", "event_date": "2026-07-22", "available_at": "2026-07-22", "source_tier": 1, "source_route_verified": True}]),
        availability_cutoff="2026-07-23",
    )
    event = {item.lens_id: item for item in result.lens_results}["event_verification"]
    assert event.status == "insufficient"
    assert "EVENT_CODE_MISMATCH" in event.reason_codes


def test_missing_values_are_never_zero_filled() -> None:
    policy = MissingDataPolicy(critical_fields=("roe",), supporting_fields=("cashflow",))
    result = policy.classify(mode="formal", missing_fields=("roe",), reason_codes=("MISSING_ROE",))
    assert result.status == "insufficient"
    assert result.missing_fields == ("roe",)
    assert "MISSING_ROE" in result.reason_codes
    assert 0 not in result.missing_fields


def test_optional_missing_data_does_not_change_status() -> None:
    policy = MissingDataPolicy(optional_fields=("volatility",))
    result = policy.classify(mode="formal", missing_fields=("volatility",))
    assert result.status == "matched"
    assert result.missing_fields == ("volatility",)
    assert result.priority_downgraded is False


def test_priority_is_stable_with_whitelisted_fields_and_no_scores() -> None:
    policy = PriorityPolicy()
    assert not set().union(*[set(fields) for fields in LENS_SORT_FIELDS.values()]) & FORBIDDEN_PRIORITY_FIELDS
    records = [
        {"code": "000002", "status": "candidate", "data_quality_status": "verified", "sort_values": {"roe_ttm": 10}},
        {"code": "000001", "status": "candidate", "data_quality_status": "verified", "sort_values": {"roe_ttm": 10}},
    ]
    assert [item["code"] for item in policy.sort_records(records, lens_id="quality_research", sort_fields=("roe_ttm", "code"))] == ["000001", "000002"]
    result = policy.classify(records[0], lens_id="quality_research")
    assert result.priority_tier == "一般候选"
    assert not (set(result.as_dict()) & FORBIDDEN_PRIORITY_FIELDS)


def test_exploratory_isolated_and_requires_human_recheck() -> None:
    proposal = ExploratoryProposal(
        proposal_id="proposal-1", code="000001", proposed_lens_id="trend_observation", source_run_id="run-1",
        evidence_refs=("fact-1",), proposal_reason="探索性发现待核验资料", missing_evidence=("adjustment_factor",),
    )
    exploratory = DEFAULT_SELECTION_POLICY.evaluate(_member(adjusted_price_series=False), mode="exploratory", availability_cutoff="2026-07-23")
    assert exploratory.exploratory_only is True
    assert exploratory.status != "candidate"
    formal = DEFAULT_SELECTION_POLICY.formalize_exploratory_proposal(proposal, _member(), actor="human:tester", availability_cutoff="2026-07-23")
    assert formal.status == "candidate"
    try:
        DEFAULT_SELECTION_POLICY.formalize_exploratory_proposal(proposal, _member(), actor="agent:one", availability_cutoff="2026-07-23")
    except ValueError:
        pass
    else:
        raise AssertionError("Agent 不得批准 ExploratoryProposal")


def test_exploratory_proposal_queue_is_persistent_and_human_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database = V1Database(Path(tmp) / "stock_tool_v1.sqlite3")
        database.init_schema()
        raw_fact = DataSnapshotFact(
            snapshot_id="pending",
            code="000001",
            field_name="event_title",
            value="定期报告披露",
            effective_at="2026-07-20T00:00:00+08:00",
            available_at="2026-07-20T00:00:00+08:00",
            source="fixture-exchange",
            source_version="v1",
            fetched_at="2026-07-21T00:00:00+08:00",
        )
        manifest = DataSnapshotManifest.create(
            asof_at="2026-07-23T00:00:00+08:00",
            availability_cutoff="2026-07-23T00:00:00+08:00",
            data_source="fixture",
            universe_id="fixture-universe",
            dataset_version="v1",
            facts_digest=fact_set_digest([raw_fact]),
            created_at="2026-07-23T00:00:00+08:00",
        )
        fact = DataSnapshotFact(
            snapshot_id=manifest.snapshot_id,
            code=raw_fact.code,
            field_name=raw_fact.field_name,
            value=raw_fact.value,
            effective_at=raw_fact.effective_at,
            available_at=raw_fact.available_at,
            source=raw_fact.source,
            source_version=raw_fact.source_version,
            fetched_at=raw_fact.fetched_at,
        )
        DataSnapshotStore(database).create_snapshot(manifest, [fact])
        fact_id = Fact.from_snapshot_fact(fact).fact_id
        store = SqliteExploratoryProposalStore(str(database.db_path))
        proposal = ExploratoryProposal(
            proposal_id="proposal-persisted",
            code="000001",
            proposed_lens_id="event_verification",
            source_run_id="agent-run-1",
            evidence_refs=(fact_id,),
            proposal_reason="探索 Agent 发现待人工核验的公告事实",
            snapshot_id=manifest.snapshot_id,
        )
        pending = store.save(proposal, snapshot_id=manifest.snapshot_id)
        assert pending.status == "pending_human_review"
        assert store.get("proposal-persisted").proposal.evidence_refs == (fact_id,)
        assert store.save(proposal, snapshot_id=manifest.snapshot_id).status == "pending_human_review"
        try:
            store.save(
                ExploratoryProposal(
                    proposal_id="proposal-persisted",
                    code="000001",
                    proposed_lens_id="event_verification",
                    source_run_id="agent-run-2",
                    evidence_refs=(fact_id,),
                    proposal_reason="不同内容不得覆盖原审计记录",
                    snapshot_id=manifest.snapshot_id,
                ),
                snapshot_id=manifest.snapshot_id,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("同 proposal_id 的不同内容不得覆盖审计记录")
        try:
            store.formalize(
                "proposal-persisted",
                actor="agent:one",
                snapshot_id=manifest.snapshot_id,
                item=_member(code="000001"),
                fact_ids=[fact_id],
                availability_cutoff=manifest.availability_cutoff,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("非人工操作者不能正式化探索建议")
        audited, selection = store.formalize(
            "proposal-persisted",
            actor="human:tester",
            snapshot_id=manifest.snapshot_id,
            item=_member(code="000001"),
            fact_ids=[fact_id],
            availability_cutoff=manifest.availability_cutoff,
        )
        assert audited.status == "formalized"
        assert selection.status == "candidate"
        store.attach_formal_run("proposal-persisted", "formal-run-1")
        reopened = SqliteExploratoryProposalStore(str(database.db_path))
        record = reopened.get("proposal-persisted")
        assert record is not None
        assert record.status == "formalized"
        assert record.formal_run_id == "formal-run-1"
        database.close()


def test_exploratory_agent_proposals_bind_to_the_batch_run() -> None:
    result = {
        "diagnostics": {
            "exploratory_proposals": [
                {
                    "code": "000001",
                    "proposed_lens_id": "event_verification",
                    "source_run_id": "spoofed-run",
                    "evidence_refs": ["fact-1"],
                    "proposal_reason": "待人工核验",
                },
                {
                    "proposed_lens_id": "event_verification",
                    "evidence_refs": ["fact-2"],
                    "proposal_reason": "缺少代码不得入队",
                },
            ]
        }
    }
    proposals = proposals_from_agent_result(result, snapshot_id="snapshot-1", source_run_id="batch-run-1")
    assert len(proposals) == 1
    assert proposals[0].source_run_id == "batch-run-1"
    assert proposals[0].code == "000001"


def test_snapshot_order_does_not_change_batch_digest() -> None:
    first = DEFAULT_SELECTION_POLICY.evaluate_many([_member(code="000002"), _member(code="000001")], availability_cutoff="2026-07-23")
    second = DEFAULT_SELECTION_POLICY.evaluate_many([_member(code="000001"), _member(code="000002")], availability_cutoff="2026-07-23")
    assert first.candidate_set_digest == second.candidate_set_digest
    assert [item.code for item in first.results] == ["000001", "000002"]


def test_screener_adapter_exposes_frozen_policy_fields() -> None:
    snapshot = FactorSnapshot(
        code="000001", name="验收样例", price=10, pe_ttm=12, roe=12, net_margin=10,
        debt_ratio=50, trend_20d=5, trend_60d=2, technical_status="candidate",
        updated_at="2026-07-23T10:00:00+08:00", data_date="2026-07-23",
    )
    selection_input = SelectionPolicyInput.from_mapping(_member())
    report = screen_factor_snapshots(
        [snapshot], ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None),
        candidate_mode=True, data_snapshot_id="snapshot-phase0-5",
        availability_cutoff="2026-07-23", selection_policy=DEFAULT_SELECTION_POLICY,
        selection_inputs_by_code={"000001": selection_input},
    )
    assert report.results
    candidate = report.results[0]
    assert candidate.selection_policy_version == "selection.v1"
    assert candidate.universe_id == "a_share_all"
    assert candidate.priority_tier in {"重点研究", "一般候选", "证据不足", "过期候选", "存在冲突"}
    assert "selection_policy" in candidate.score_breakdown


def test_legacy_factor_snapshot_cannot_pass_without_formal_input() -> None:
    snapshot = FactorSnapshot(
        code="000001", name="旧快照", price=10, roe=12, debt_ratio=50,
        trend_20d=5, trend_60d=2, technical_status="candidate",
        data_date="2026-07-23", updated_at="2026-07-23T10:00:00+08:00",
    )
    report = screen_factor_snapshots(
        [snapshot],
        ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None),
        candidate_mode=True,
        data_snapshot_id="legacy-only",
        availability_cutoff="2026-07-23",
        selection_policy=DEFAULT_SELECTION_POLICY,
    )
    assert report.results == []
    assert report.skipped == 1


def test_unknown_status_and_status_policy_are_explicit_and_digest_bound() -> None:
    unknown = DEFAULT_SELECTION_POLICY.evaluate(_member(status=""), availability_cutoff="2026-07-23")
    assert unknown.status == "excluded"
    assert "UNKNOWN_STOCK_STATUS" in unknown.reason_codes
    strict = evaluate_universe_member(_member(), member_status_policy="listed_verified", availability_cutoff="2026-07-23")
    assert strict.status == "excluded"
    assert strict.member_status_policy == "listed_verified"
    explicit = evaluate_universe_member(
        _member(security_status="listed"),
        member_status_policy="listed_verified",
        availability_cutoff="2026-07-23",
    )
    assert explicit.status == "eligible"
    assert DEFAULT_SELECTION_POLICY.evaluate(_member(), member_status_policy="listed_verified", availability_cutoff="2026-07-23").as_dict()["member_status_policy"] == "listed_verified"
    try:
        evaluate_universe_member(_member(), member_status_policy="unsafe", availability_cutoff="2026-07-23")
    except ValueError:
        pass
    else:
        raise AssertionError("未知 member_status_policy 必须直接报错")


def test_real_snapshot_fact_hydration_preserves_policy_fields_and_lineage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database = V1Database(Path(tmp) / "selection.sqlite3")
        code = "000001"
        values = _member(
            status=None,
            security_status="listed",
            available_at="2026-07-22",
            snapshot_available_at="2026-07-22",
            name="PIT样例",
            price=10,
            roe=12,
            net_margin=10,
            trend_20d=5,
            trend_60d=2,
            data_date="2026-07-23",
            updated_at="2026-07-23T10:00:00+08:00",
            technical_status="candidate",
            financial_reports=[
                {
                    "report_date": "2026-06-30",
                    "available_at": "2026-07-01",
                    "net_profit": 10,
                    "operating_cashflow": 5,
                    "quality_status": "verified",
                }
            ],
        )
        facts = [
            DataSnapshotFact(
                snapshot_id="pending",
                code=code,
                field_name=name,
                value=value,
                effective_at="2026-07-23T00:00:00+08:00",
                available_at="2026-07-23T00:00:00+08:00",
                source="fixture-source",
                source_version="fixture-v1",
                fetched_at="2026-07-23T01:00:00+08:00",
            )
            for name, value in values.items()
            if name not in {"code", "status"}
        ]
        facts = [
            DataSnapshotFact(
                snapshot_id="selection-facts-v1",
                code=fact.code,
                field_name=fact.field_name,
                value=fact.value,
                effective_at=fact.effective_at,
                available_at=fact.available_at,
                source=fact.source,
                source_version=fact.source_version,
                fetched_at=fact.fetched_at,
            )
            for fact in facts
        ]
        manifest = DataSnapshotManifest.create(
            asof_at="2026-07-23T00:00:00+08:00",
            availability_cutoff="2026-07-23T00:00:00+08:00",
            data_source="fixture-source",
            universe_id="a_share_all",
            universe_version="v1",
            dataset_version="selection-fixture.v1",
            facts_digest=fact_set_digest(facts),
            quality_status="verified",
            quality_summary={"fact_total": len(facts), "verified_fact_count": len(facts)},
            created_at="2026-07-23T02:00:00+08:00",
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
            )
            for fact in facts
        ]
        DataSnapshotStore(database).create_snapshot(manifest, facts)
        inputs = V1FactorStore(database).list_selection_inputs_for_manifest(manifest.snapshot_id)
        policy_input = inputs[code]
        assert isinstance(policy_input, SelectionPolicyInput)
        assert policy_input.board_id == "main_board"
        assert policy_input.snapshot_quality_status == "verified"
        assert policy_input.available_at == "2026-07-22"
        assert policy_input.fact_lineage["board_id"]["source"] == "fixture-source"
        assert policy_input.financial_reports[0]["report_date"] == "2026-06-30"
        snapshots = V1FactorStore(database).list_snapshots_for_manifest(manifest.snapshot_id)
        report = screen_factor_snapshots(
            snapshots,
            ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None),
            candidate_mode=True,
            data_snapshot_id=manifest.snapshot_id,
            availability_cutoff=manifest.availability_cutoff,
            data_quality_status=manifest.quality_status,
            data_source=manifest.data_source,
            selection_inputs_by_code=inputs,
            selection_policy=DEFAULT_SELECTION_POLICY,
        )
        assert report.results
        candidate = report.results[0]
        assert candidate.candidate_digest
        assert candidate.score_breakdown["selection_policy"]["selection_input"]["snapshot_id"] == manifest.snapshot_id
        assert candidate.score_breakdown["selection_policy"]["selection_input"]["fact_lineage"]["board_id"]["source_version"] == "fixture-v1"
        database.close()


def test_formal_workflow_uses_hydrated_inputs_and_issues_certificate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database = V1Database(Path(tmp) / "formal-selection.sqlite3")
        database.init_schema()
        values = _member(
            status=None,
            security_status="listed",
            available_at="2026-07-22",
            snapshot_available_at="2026-07-22",
            name="正式工作流样例",
            price=10,
            pe=8,
            pe_ttm=8,
            roe=12,
            net_margin=10,
            debt_ratio=50,
            trend_20d=5,
            trend_60d=2,
            data_date="2026-07-23",
            updated_at="2026-07-23T10:00:00+08:00",
            technical_status="candidate",
            financial_reports=[
                {
                    "report_date": "2026-06-30",
                    "available_at": "2026-07-01",
                    "net_profit": 10,
                    "operating_cashflow": 5,
                    "quality_status": "verified",
                }
            ],
        )
        facts = [
            DataSnapshotFact(
                snapshot_id="pending",
                code="000001",
                field_name=name,
                value=value,
                effective_at="2026-07-23T00:00:00+08:00",
                available_at="2026-07-23T00:00:00+08:00",
                source="fixture-source",
                source_version="fixture-v1",
                fetched_at="2026-07-23T01:00:00+08:00",
            )
            for name, value in values.items()
            if name not in {"code", "status"}
        ]
        snapshot_store = DataSnapshotStore(database)
        refresh_event = snapshot_store.record_refresh_attempt(
            asof_at="2026-07-23T00:00:00+08:00",
            availability_cutoff="2026-07-23T00:00:00+08:00",
            data_source="fixture-source",
            universe_id="a_share_all",
            source_policy_version="fixture-policy.v1",
            dataset_version="fixture-dataset.v1",
            universe_version="fixture-universe.v1",
            started_at="2026-07-23T01:00:00+08:00",
        )
        reference = snapshot_store.finalize_refresh(
            refresh_event.refresh_event_id,
            facts,
            status="success",
            quality_status="verified",
            quality_summary={
                "fact_total": len(facts),
                "verified_fact_count": len(facts),
                "partial_fact_count": 0,
                "unverified_fact_count": 0,
                "invalid_fact_count": 0,
                "blocked_fact_count": 0,
            },
            finished_at="2026-07-23T02:00:00+08:00",
        )
        assert reference is not None
        manifest = snapshot_store.get_manifest(reference.snapshot_id)
        assert manifest is not None
        snapshots = V1FactorStore(database).list_snapshots_for_manifest(manifest.snapshot_id)
        spec = query_spec_from_dict(
            {
                "raw_text": "ROE大于10，PE小于40",
                "execution_mode": "formal",
                "scan_limit": 300,
                "result_limit": 80,
                "conditions": [
                    {"field": "roe", "op": ">=", "value": 10},
                    {"field": "pe", "op": "<=", "value": 40},
                ],
            }
        )
        result = ScreeningWorkflowService(StrategyStore(database), None).run_snapshot_screening(
            snapshots,
            spec,
            source_label="formal-fixture",
            user_text="正式工作流样例",
            data_source="fixture-source",
            execution_mode="formal",
            data_manifest=manifest,
            require_data_manifest=True,
            universe_id="a_share_all",
            universe_version=manifest.universe_version,
            selection_scope="fixture-scope",
            members_digest="fixture-members",
            selection_policy=DEFAULT_SELECTION_POLICY,
            member_status_policy="listed_non_st",
            candidate_lens_mode=True,
        )
        assert result.report.status == "verified", (result.report.status, result.report.errors, result.report.missing_fields)
        assert result.report.results
        assert result.certificate_id
        certificate = FormalScreeningCertificateStore(database).get(result.certificate_id)
        assert certificate is not None
        assert certificate.refresh_event_id == refresh_event.refresh_event_id
        assert certificate.universe.member_status_policy == "listed_non_st"
        assert result.report.results[0].score_breakdown["selection_policy"]["selection_input"]["snapshot_id"] == manifest.snapshot_id
        database.close()


def run_tests() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"[OK] Phase 0.5 selection policy passed ({len(tests)} tests)")


if __name__ == "__main__":
    run_tests()
