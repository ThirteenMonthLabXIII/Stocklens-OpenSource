"""Offline acceptance checks for the deterministic Phase 1 candidate layer."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.data_snapshot import DataSnapshotFact, DataSnapshotManifest, DataSnapshotStore, fact_set_digest
from stock_tool.core.factor_store import FactorSnapshot, V1FactorStore
from stock_tool.core.query_schema import QuerySpec
from stock_tool.core.screener_engine import ScreenFilter, screen_factor_snapshots
from stock_tool.core.screening_workflow import ScreeningWorkflowService
from stock_tool.core.selection_policy import DEFAULT_SELECTION_POLICY
from stock_tool.core.strategy_store import StrategyStore
from stock_tool.core.performance_loop import PerformanceStore
from stock_tool.core.v1_database import V1Database, v1_db_path_for


def _snapshots() -> list[FactorSnapshot]:
    return [
        FactorSnapshot(
            code="000002",
            name="趋势样例B",
            price=12,
            pe=18,
            pe_ttm=19,
            roe=16,
            net_margin=12,
            debt_ratio=45,
            trend_20d=8,
            trend_60d=10,
            technical_status="candidate",
            report_date="2026-03-31",
            updated_at="2026-07-23T10:00:00+08:00",
            data_date="2026-07-23",
            market_cap_yi=200,
        ),
        FactorSnapshot(
            code="000001",
            name="趋势样例A",
            price=10,
            pe=12,
            pe_ttm=13,
            roe=20,
            net_margin=15,
            debt_ratio=35,
            trend_20d=3,
            trend_60d=5,
            technical_status="candidate",
            report_date="2026-03-31",
            updated_at="2026-07-23T10:00:00+08:00",
            data_date="2026-07-23",
            market_cap_yi=300,
        ),
    ]


def _run_candidate(snapshots: list[FactorSnapshot]):
    return screen_factor_snapshots(
        snapshots,
        ScreenFilter(
            min_roe=None,
            max_debt_ratio=None,
            max_pe=None,
            with_technical=True,
            result_limit=20,
        ),
        candidate_mode=True,
        data_snapshot_id="snapshot-phase1",
        data_quality_status="verified",
        data_source="fixture-source",
        fact_lineage_by_code={
            "000001": {
                "roe": {"source": "financial", "source_version": "v1"},
                "trend_20d": {"source": "history", "source_version": "v1"},
            },
            "000002": {
                "roe": {"source": "financial", "source_version": "v1"},
                "trend_20d": {"source": "history", "source_version": "v1"},
            },
        },
    )


def test_deterministic_lenses_and_audit_fields() -> None:
    first = _run_candidate(_snapshots())
    second = _run_candidate(list(reversed(_snapshots())))
    assert first.status == second.status == "verified"
    assert [item.quote.code for item in first.results] == [item.quote.code for item in second.results]
    assert [item.candidate_digest for item in first.results] == [item.candidate_digest for item in second.results]
    assert all(item.local_score is None for item in first.results)
    assert all("professional_formula" not in item.score_breakdown for item in first.results)
    assert all(item.data_snapshot_id == "snapshot-phase1" for item in first.results)
    assert all(item.reason_codes and item.fact_sources for item in first.results)
    assert set(first.lens_results) == {"quality_research", "trend_observation", "event_verification"}
    quality_codes = [item.quote.code for item in first.lens_results["quality_research"]]
    assert quality_codes == ["000001", "000002"], "质量 Lens 应按 ROE 再代码稳定排序"
    assert first.candidate_set_digest == second.candidate_set_digest


def test_missing_values_are_not_zero_filled() -> None:
    missing = FactorSnapshot(
        code="000003",
        name="缺失样例",
        debt_ratio=40,
        pe=10,
        trend_20d=2,
        technical_status="candidate",
        updated_at="2026-07-23T10:00:00+08:00",
    )
    report = screen_factor_snapshots(
        [missing],
        ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None, with_technical=True),
        candidate_mode=True,
        data_snapshot_id="snapshot-missing",
    )
    assert len(report.results) == 1
    candidate = report.results[0]
    assert candidate.local_score is None
    assert candidate.eligibility_status == "insufficient"
    assert "MISSING_ROE" in candidate.reason_codes
    assert "MISSING_PE_TTM" in candidate.reason_codes


def test_review_only_without_selection_input_keeps_deterministic_candidates() -> None:
    report = screen_factor_snapshots(
        _snapshots(),
        ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None, with_technical=True),
        candidate_mode=True,
        policy_mode="review_only",
        selection_policy=DEFAULT_SELECTION_POLICY,
        selection_inputs_by_code={},
        data_snapshot_id="review-only-no-input",
        data_source="fixture-source",
    )
    assert [item.quote.code for item in report.results] == ["000001", "000002"]
    assert all(item.lens_ids for item in report.results)
    assert all(item.eligibility_status == "insufficient" for item in report.results)
    assert all(item.data_status == "partial" for item in report.results)
    assert all(item.missing_fields for item in report.results)
    assert all("SELECTION_POLICY_INPUT_INCOMPLETE" in item.reason_codes for item in report.results)
    assert all(item.score_breakdown["candidate_digest"] == item.candidate_digest for item in report.results)
    assert all(
        tuple(item.score_breakdown["candidate_contract"]["lens_ids"]) == tuple(item.lens_ids)
        for item in report.results
    )


def test_review_only_partial_selection_input_keeps_both_candidate_rows() -> None:
    from tools.test_phase0_5_selection_policy import _member

    report = screen_factor_snapshots(
        _snapshots(),
        ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None, with_technical=True),
        candidate_mode=True,
        policy_mode="review_only",
        selection_policy=DEFAULT_SELECTION_POLICY,
        selection_inputs_by_code={"000002": _member(code="000002")},
        data_snapshot_id="review-only-partial-input",
        data_source="fixture-source",
    )
    assert {item.quote.code for item in report.results} == {"000001", "000002"}
    assert all(item.lens_ids for item in report.results)
    assert all(item.data_status in {"review_only", "partial"} for item in report.results)


def test_review_only_partial_lens_evidence_keeps_filter_passed_rows() -> None:
    report = screen_factor_snapshots(
        _snapshots(),
        ScreenFilter(min_roe=15, max_debt_ratio=None, max_pe=40, with_technical=True),
        candidate_mode=True,
        policy_mode="review_only",
        selection_policy=DEFAULT_SELECTION_POLICY,
        selection_inputs_by_code={},
        data_snapshot_id="review-only-partial-lens",
        data_source="fixture-source",
        data_quality_status="partial",
    )
    assert [item.quote.code for item in report.results] == ["000001", "000002"]
    assert all(item.eligibility_status == "insufficient" for item in report.results)
    assert all(item.lens_ids for item in report.results)
    assert all("CANDIDATE_LENS_INPUT_INCOMPLETE" in item.reason_codes for item in report.results)


def test_formal_without_selection_input_is_blocked() -> None:
    report = screen_factor_snapshots(
        _snapshots(),
        ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None, with_technical=True),
        candidate_mode=True,
        policy_mode="formal",
        selection_policy=DEFAULT_SELECTION_POLICY,
        selection_inputs_by_code={},
        data_snapshot_id="formal-no-input",
        data_source="fixture-source",
    )
    assert report.results == []
    assert report.status == "blocked"


def test_priority_tier_precedes_declared_lens_order_and_stock_code() -> None:
    from tools.test_phase0_5_selection_policy import _member

    report = screen_factor_snapshots(
        _snapshots(),
        ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None, with_technical=True),
        candidate_mode=True,
        policy_mode="review_only",
        selection_policy=DEFAULT_SELECTION_POLICY,
        selection_inputs_by_code={"000002": _member(code="000002")},
        data_snapshot_id="priority-order",
        data_source="fixture-source",
    )
    assert [item.quote.code for item in report.results] == ["000002", "000001"]

    tied = [
        FactorSnapshot(
            code=code,
            name="同值排序样例",
            price=10,
            pe_ttm=12,
            roe=16,
            net_margin=10,
            debt_ratio=40,
            trend_20d=5,
            trend_60d=2,
            technical_status="candidate",
            report_date="2026-03-31",
            updated_at="2026-07-23T10:00:00+08:00",
            data_date="2026-07-23",
        )
        for code in ("000002", "000001")
    ]
    tied_report = screen_factor_snapshots(
        tied,
        ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None, with_technical=True),
        candidate_mode=True,
        data_snapshot_id="priority-code-tie",
        data_source="fixture-source",
    )
    assert [item.quote.code for item in tied_report.results] == ["000001", "000002"]


def test_event_facts_match_and_future_facts_are_excluded() -> None:
    report = screen_factor_snapshots(
        _snapshots(),
        ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None, with_technical=True),
        candidate_mode=True,
        data_snapshot_id="snapshot-events",
        availability_cutoff="2026-07-23T10:00:00+08:00",
        lens_values_by_code={
            "000001": {
                "event_verification": {
                    "event_title": "定期报告披露",
                    "event_date": "2026-07-22",
                    "event_source": "exchange",
                    "available_at": "2026-07-22T09:00:00+08:00",
                }
            },
            "000002": {
                "event_verification": {
                    "event_title": "未来事件",
                    "event_date": "2026-07-24",
                    "event_source": "exchange",
                    "available_at": "2026-07-24T09:00:00+08:00",
                }
            },
        },
    )
    by_code = {item.quote.code: item for item in report.results}
    assert "event_verification" in by_code["000001"].lens_ids
    assert by_code["000002"].lens_evaluations["event_verification"]["status"] == "excluded"
    assert "FUTURE_AVAILABLE_AT" in by_code["000002"].reason_codes

    unverified = screen_factor_snapshots(
        [_snapshots()[0]],
        ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None, with_technical=True),
        candidate_mode=True,
        lens_values_by_code={
            "000002": {
                "event_verification": {
                    "event_title": "待核验事件",
                    "event_date": "2026-07-22",
                    "event_source": "exchange",
                    "available_at": "2026-07-22T09:00:00+08:00",
                    "__quality_status_by_field": {"event_title": "partial"},
                }
            }
        },
    )
    event_evaluation = unverified.results[0].lens_evaluations["event_verification"]
    assert event_evaluation["status"] == "insufficient"
    assert "FIELD_QUALITY_PARTIAL_EVENT_TITLE" in event_evaluation["reason_codes"]
    assert unverified.results[0].data_status == "partial"


def test_blocked_quality_stops_candidate_layer() -> None:
    report = screen_factor_snapshots(
        _snapshots(),
        ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None),
        candidate_mode=True,
        data_snapshot_id="blocked-snapshot",
        data_quality_status="blocked",
    )
    assert report.status == "blocked"
    assert report.results == []


def test_formal_workflow_blocks_partial_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database = V1Database(v1_db_path_for(Path(tmp)))
        strategy = StrategyStore(database)
        performance = PerformanceStore(database)
        manifest = DataSnapshotManifest.create(
            asof_at="2026-07-23T10:00:00+08:00",
            availability_cutoff="2026-07-23T10:00:00+08:00",
            data_source="fixture-source",
            universe_id="fixture-universe",
            dataset_version="fixture-v1",
            facts_digest=fact_set_digest([]),
            quality_status="partial",
            quality_summary={"reason": "fixture partial"},
            created_at="2026-07-23T10:00:00+08:00",
        )
        DataSnapshotStore(database).create_snapshot(manifest, [])
        result = ScreeningWorkflowService(strategy, performance).run_snapshot_screening(
            _snapshots(),
            QuerySpec(conditions=[]),
            source_label="phase1-fixture",
            user_text="fixture",
            data_source="fixture-source",
            data_manifest=manifest,
            require_data_manifest=True,
        )
        assert result.report.status == "blocked"
        assert result.report.results == []
        assert result.observation_samples is None
        database.close()


def test_formal_workflow_uses_candidate_layer_for_verified_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database = V1Database(v1_db_path_for(Path(tmp)))
        snapshot_store = DataSnapshotStore(database)
        refresh_event = snapshot_store.record_refresh_attempt(
            asof_at="2026-07-23T10:00:00+08:00",
            availability_cutoff="2026-07-23T10:00:00+08:00",
            data_source="fixture-source",
            universe_id="fixture-universe",
            universe_version="fixture-universe-v1",
            dataset_version="fixture-v1",
            source_policy_version="fixture-policy-v1",
            started_at="2026-07-23T10:00:00+08:00",
        )
        snapshot_ref = snapshot_store.finalize_refresh(
            refresh_event.refresh_event_id,
            [
                DataSnapshotFact(
                    snapshot_id="pending",
                    code=snapshot.code,
                    field_name=field_name,
                    value=value,
                    effective_at="2026-07-23T10:00:00+08:00",
                    available_at="2026-07-23T09:00:00+08:00",
                    source="fixture-source",
                    source_version="fixture-v1",
                    fetched_at="2026-07-23T10:00:00+08:00",
                )
                for snapshot in _snapshots()
                for field_name, value in {
                    "name": snapshot.name,
                    "price": snapshot.price,
                    "pe": snapshot.pe,
                    "pe_ttm": snapshot.pe_ttm,
                    "roe": snapshot.roe,
                    "net_margin": snapshot.net_margin,
                    "debt_ratio": snapshot.debt_ratio,
                    "trend_20d": snapshot.trend_20d,
                    "trend_60d": snapshot.trend_60d,
                    "technical_status": snapshot.technical_status,
                    "report_date": snapshot.report_date,
                    "updated_at": snapshot.updated_at,
                    "data_date": snapshot.data_date,
                    "market_cap_yi": snapshot.market_cap_yi,
                    "board_id": "main_board",
                    "security_status": "listed",
                    "snapshot_available_at": "2026-07-23T09:00:00+08:00",
                    "listing_age_trading_days": 180,
                    "valid_turnover_days_20d": 20,
                    "valid_price_volume_days": 20,
                    "median_turnover_value_20d": 30_000_000,
                    "snapshot_quality_status": "verified",
                    "financial_quality_status": "verified",
                    "financial_reports": [
                        {
                            "report_date": "2026-03-31",
                            "available_at": "2026-04-30",
                            "net_profit": 10,
                            "operating_cashflow": 5,
                            "quality_status": "verified",
                        }
                    ],
                    "verified_report_periods": 4,
                    "net_profit_positive_periods": 4,
                    "operating_cashflow_positive_periods": 4,
                    "industry_sector": "制造",
                    "industry_classification_id": "csrc2012",
                    "industry_classification_version": "2026.1",
                    "industry_sample_count": 30,
                    "peer_set_digest": "phase1-peer-digest",
                    "debt_sector_percentile": 50,
                    "adjusted_price_series": True,
                    "adjustment_factor_missing": False,
                    "adjustment_factor": 1.0,
                    "corporate_action_fact_id": "phase1-ca",
                    "adjustment_method": "split_adjusted",
                    "valid_price_days_20d": 20,
                    "valid_volume_days_20d": 20,
                    "relative_strength_20d": 3,
                    "technical_fields_quality": "verified",
                    "events": [
                        {
                            "code": snapshot.code,
                            "event_title": "定期报告披露",
                            "event_date": "2026-07-22",
                            "available_at": "2026-07-22T09:00:00+08:00",
                            "event_source": "exchange",
                            "source_tier": 1,
                            "source_route_verified": True,
                            "deduped": True,
                        }
                    ],
                }.items()
                if value is not None
            ],
            status="success",
            quality_status="verified",
            quality_summary={"fact_total": 0, "verified_fact_count": 0},
            finished_at="2026-07-23T10:01:00+08:00",
        )
        assert snapshot_ref is not None
        manifest = snapshot_store.get_manifest(snapshot_ref.snapshot_id)
        assert manifest is not None
        strategy = StrategyStore(database)
        performance = PerformanceStore(database)
        result = ScreeningWorkflowService(strategy, performance).run_snapshot_screening(
            V1FactorStore(database).list_snapshots_for_manifest(manifest.snapshot_id),
            QuerySpec(conditions=[]),
            source_label="phase1-formal-fixture",
            user_text="fixture",
            data_source="fixture-source",
            data_manifest=manifest,
            require_data_manifest=True,
            refresh_event_id=refresh_event.refresh_event_id,
            universe_id="fixture-universe",
            universe_version="fixture-universe-v1",
            members_digest="fixture-members",
            selection_scope="top_2_by_market_cap",
            selected_limit=2,
        )
        assert result.report.status == "verified"
        assert result.certificate_id
        assert result.observation_samples is not None
        assert result.report.results
        assert all(item.local_score is None for item in result.report.results)
        with database.connect() as conn:
            context = conn.execute(
                "SELECT scoring_context_json FROM query_runs WHERE id = ?",
                (result.run_id,),
            ).fetchone()
        assert context is not None
        assert '"candidate_lens_mode": true' in context[0]
        database.close()


def test_all_new_modes_run_without_active_professional_formula() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database = V1Database(v1_db_path_for(Path(tmp)))
        strategy = StrategyStore(database)
        performance = PerformanceStore(database)
        workflow = ScreeningWorkflowService(strategy, performance)
        try:
            with patch.object(
                PerformanceStore,
                "get_active_professional_formula",
                side_effect=AssertionError("新 CandidateLens 流程不得读取 active professional formula"),
            ):
                for mode in ("formal", "review_only", "exploratory"):
                    result = workflow.run_snapshot_screening(
                        _snapshots(),
                        QuerySpec(conditions=[], execution_mode=mode),
                        source_label=f"phase1-{mode}-fixture",
                        user_text="fixture",
                        data_source="fixture-source",
                        execution_mode=mode,
                        require_data_manifest=False,
                    )
                    assert result.report.status in {"verified", "review_only", "exploratory", "partial", "blocked"}
                    assert result.run_id
                    with database.connect() as conn:
                        row = conn.execute(
                            "SELECT scoring_context_json FROM query_runs WHERE id = ?",
                            (result.run_id,),
                        ).fetchone()
                    assert row is not None
                    assert '"ranking_formula_id": "candidate_lens.v1"' in row[0]
                    assert '"ranking_formula_version": "candidate_lens.v1"' in row[0]
                    assert '"selection_policy_version": "v1"' in row[0]
        finally:
            database.close()


def test_new_screening_path_rejects_professional_formula_parameter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database = V1Database(v1_db_path_for(Path(tmp)))
        strategy = StrategyStore(database)
        workflow = ScreeningWorkflowService(strategy, None)
        try:
            try:
                workflow.run_snapshot_screening(
                    _snapshots(),
                    QuerySpec(conditions=[], execution_mode="review_only"),
                    source_label="phase1-formula-rejection",
                    user_text="fixture",
                    data_source="fixture-source",
                    execution_mode="review_only",
                    professional_formula={"id": "legacy"},
                )
            except ValueError as exc:
                assert "professional_formula" in str(exc)
            else:
                raise AssertionError("新 CandidateLens 路径不得静默接受 professional_formula")
        finally:
            database.close()


def run_tests() -> None:
    test_deterministic_lenses_and_audit_fields()
    test_missing_values_are_not_zero_filled()
    test_review_only_without_selection_input_keeps_deterministic_candidates()
    test_review_only_partial_selection_input_keeps_both_candidate_rows()
    test_review_only_partial_lens_evidence_keeps_filter_passed_rows()
    test_formal_without_selection_input_is_blocked()
    test_priority_tier_precedes_declared_lens_order_and_stock_code()
    test_event_facts_match_and_future_facts_are_excluded()
    test_blocked_quality_stops_candidate_layer()
    test_formal_workflow_blocks_partial_manifest()
    test_formal_workflow_uses_candidate_layer_for_verified_manifest()
    test_all_new_modes_run_without_active_professional_formula()
    test_new_screening_path_rejects_professional_formula_parameter()
    print("[OK] Phase 1 deterministic candidate layer passed")


if __name__ == "__main__":
    run_tests()
