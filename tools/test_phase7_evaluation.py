from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.evidence_contracts import AgentResult
from stock_tool.core.evidence_run_store import SqliteEvidenceRunStore
from stock_tool.core.data_snapshot import DataSnapshotFact, DataSnapshotStore
from stock_tool.core.evaluation_stores import (
    AgentEvaluationStore,
    DataQualityEvaluationStore,
    LensEvaluationStore,
    ObservationOutcomeStore,
)
from stock_tool.core.multi_agent_orchestrator import AgentExecution, SqliteAgentExecutionStore
from stock_tool.core.performance_loop import ObservationOutcome, PerformanceStore, StoreBackedPriceSource, add_trading_days
from stock_tool.core.pit_database import PITDatabase
from stock_tool.core.v1_database import V1Database, v1_db_path_for
from tools.test_performance_loop import _insert_run, _prepare_formal_certificate


def _add_lens(db: V1Database, result_id: int, *, lens_id: str = "quality_research", version: str = "v1") -> None:
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE query_run_results
            SET metrics_json = ?, score_breakdown_json = ?
            WHERE id = ?
            """,
            (
                json.dumps({"lens_ids": [lens_id], "industry": "银行"}, ensure_ascii=False),
                json.dumps(
                    {
                        "lens_ids": [lens_id],
                        "lens_evaluations": {lens_id: {"status": "matched", "lens_version": version}},
                    },
                    ensure_ascii=False,
                ),
                result_id,
            ),
        )


def _set_mode(db: V1Database, run_id: int, mode: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE query_runs SET scoring_context_json = ? WHERE id = ?",
            (json.dumps({"execution_mode": mode, "screening_status": "verified"}), run_id),
        )


def test_observation_modes_lens_metrics_and_exclusions() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = V1Database(v1_db_path_for(Path(tmp)))
        performance = PerformanceStore(db)

        formal_run, formal_result = _insert_run(db, created_at="2026-01-02T15:00:00")
        _add_lens(db, formal_result)
        certificate_id = _prepare_formal_certificate(db, formal_run)
        performance.create_formal_observation_samples(formal_run, certificate_id, horizons=(3,))

        review_run, review_result = _insert_run(db, code="000002", created_at="2026-01-03T15:00:00")
        _add_lens(db, review_result)
        _set_mode(db, review_run, "review_only")
        performance.create_review_observation_samples(review_run, horizons=(3,))

        exploratory_run, exploratory_result = _insert_run(db, code="000003", created_at="2026-01-04T15:00:00")
        _add_lens(db, exploratory_result)
        _set_mode(db, exploratory_run, "exploratory")
        performance.create_exploratory_observation_samples(exploratory_run, horizons=(3,))

        formal_sample = performance.list_recent_outcomes(limit=10, evaluation_mode="formal")
        review_sample = performance.list_recent_outcomes(limit=10, evaluation_mode="review_only")
        assert len(formal_sample) == 1
        assert len(review_sample) == 1
        assert formal_sample[0]["sample_class"] == "formal"
        assert review_sample[0]["sample_class"] == "review"

        performance.record_outcome(
            ObservationOutcome(
                sample_id=int(formal_sample[0]["sample_id"]),
                settled_at="2026-01-10T09:00:00+00:00",
                exit_date="2026-01-07",
                exit_price=10.8,
                return_pct=8.0,
                hs300_return_pct=2.0,
                excess_vs_hs300_pct=6.0,
                industry_return_pct=1.0,
                excess_vs_industry_pct=7.0,
                max_drawdown_pct=-2.0,
                rating="A",
            )
        )
        outcome_store = ObservationOutcomeStore(db)
        counts = outcome_store.mode_counts()
        assert counts["formal"]["valid_settled_count"] == 1
        assert counts["review_only"]["valid_settled_count"] == 0
        with db.connect() as conn:
            conn.execute("UPDATE observation_samples SET name = '更名后的样本' WHERE id = ?", (int(formal_sample[0]["sample_id"]),))
        lens_rows = LensEvaluationStore(outcome_store).evaluate(mode="formal")
        assert len(lens_rows) == 1
        assert lens_rows[0]["lens_id"] == "quality_research"
        assert lens_rows[0]["avg_excess_vs_hs300_pct"] == 6.0
        assert lens_rows[0]["avg_excess_vs_industry_pct"] == 7.0
        assert lens_rows[0]["out_of_sample"]["available"] is False

        sample_id = int(formal_sample[0]["sample_id"])
        try:
            outcome_store.record_exclusion(sample_id, "human_closed", mode="formal")
        except ValueError:
            pass
        else:
            raise AssertionError("human_closed 必须拒绝无人工 actor")
        outcome_store.record_exclusion(
            sample_id,
            "human_closed",
            mode="formal",
            actor="human:acceptance",
            reason_detail="人工关闭验收样本",
        )
        excluded = outcome_store.list_outcomes(mode="formal")[0]
        assert excluded.valid_outcome is False
        assert excluded.exclusion_reason == "human_closed"
        db.close()


def test_data_quality_and_agent_process_metrics() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = v1_db_path_for(Path(tmp))
        db = V1Database(db_path)
        _run_id, _result_id = _insert_run(db)
        snapshots = DataSnapshotStore(db)
        event = snapshots.record_refresh_attempt(
            asof_at="2026-01-02T09:00:00+00:00",
            availability_cutoff="2026-01-02T09:00:00+00:00",
            data_source="fixture",
            universe_id="a_share_listed_pit",
            universe_version="fixture-u-v1",
            dataset_version="fixture-d-v1",
            source_policy_version="fixture-policy-v1",
            source_versions={"fixture": "v1"},
            started_at="2026-01-02T09:00:00+00:00",
        )
        snapshots.finalize_refresh(
            event.refresh_event_id,
            [
                DataSnapshotFact(
                    snapshot_id="pending",
                    code="000001",
                    field_name="roe",
                    value=20,
                    effective_at="2026-01-01T00:00:00+00:00",
                    available_at="2026-01-02T08:00:00+00:00",
                    source="fixture",
                    source_version="v1",
                    fetched_at="2026-01-02T09:00:00+00:00",
                )
            ],
            status="success",
            quality_status="partial",
            quality_summary={"expected_fact_count": 2, "missing_fact_count": 1},
            finished_at="2026-01-02T09:01:00+00:00",
        )
        data_quality = DataQualityEvaluationStore(db).evaluate(start_date="2026-01-01", end_date="2026-01-03")
        assert data_quality.snapshot_count == 1
        assert data_quality.refresh_event_count == 1
        assert data_quality.metrics["field_missing_rate"] == 0.5
        assert data_quality.metrics["source_stability_rate"] == 1.0
        assert data_quality.metrics["snapshot_reproducibility_rate"] == 1.0
        assert data_quality.metrics["cache_usage_rate"] == 0.0

        result = AgentResult(
            agent_id="market_industry",
            agent_version="market_industry.v1",
            run_id="agent-run-1",
            snapshot_id="snapshot-1",
            prompt_version="market_industry_agent.v1",
            model_version="fixture:model",
            status="ok",
            coverage={"coverage_rate": 0.75},
            claims=(
                {
                    "claim_id": "claim-1",
                    "type": "supports_lens",
                    "text": "行业事实",
                    "evidence_refs": ["fact-1"],
                    "caveats": [],
                },
            ),
        )
        raw = json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True)
        execution_store = SqliteAgentExecutionStore(str(db_path))
        execution_store.save(
            AgentExecution(
                agent_id="market_industry",
                agent_version="market_industry.v1",
                prompt_version="market_industry_agent.v1",
                model_version="fixture:model",
                status="ok",
                outcome="ok",
                run_id="agent-run-1",
                snapshot_id="snapshot-1",
                candidate_set_digest="candidate-1",
                evidence_package_hash="package-1",
                lens_version="lens-v1",
                cache_key="cache-1",
                raw_response=raw,
                normalized_response=raw,
                cost_note="按返回用量估算费用：$0.001",
                latency_ms=12.0,
                result=result,
                created_at="2026-01-02T10:00:00+00:00",
            )
        )
        execution_store.save(
            AgentExecution(
                agent_id="event_risk",
                agent_version="event_risk.v1",
                prompt_version="event_risk_agent.v1",
                model_version="fixture:model",
                status="failed",
                outcome="failed",
                run_id="agent-run-2",
                snapshot_id="snapshot-1",
                candidate_set_digest="candidate-1",
                evidence_package_hash="package-1",
                lens_version="lens-v1",
                cache_key="cache-2",
                error="超时",
                latency_ms=60.0,
                created_at="2026-01-02T10:01:00+00:00",
            )
        )
        SqliteEvidenceRunStore(str(db_path))
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO formal_evidence_runs(
                    run_id, snapshot_id, candidate_set_digest, evidence_package_hash,
                    summary_hash, status, evidence_package_json, orchestration_json,
                    orchestration_hash, aggregation_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "agent-run-1",
                    "snapshot-1",
                    "candidate-1",
                    "package-1",
                    "summary-1",
                    "ok",
                    "{}",
                    "{}",
                    "",
                    json.dumps({"summaries": [{"conflicts": [{"claim_id": "claim-1"}]}]}),
                    "2026-01-02T10:02:00+00:00",
                ),
            )
        evaluation = AgentEvaluationStore(db).summarize()
        assert evaluation["execution_count"] == 2
        assert evaluation["success_rate"] == 0.5
        assert evaluation["timeout_rate"] == 0.5
        assert evaluation["json_compliance_rate"] == 1.0
        assert evaluation["evidence_reference_valid_rate"] == 1.0
        assert evaluation["coverage"]["average"] == 0.75
        assert evaluation["average_latency_ms"] == 36.0
        assert evaluation["conflict_count"] == 1
        assert evaluation["total_cost_usd"] == 0.001
        by_agent = AgentEvaluationStore(db).summarize_by_agent()
        assert {item["agent_id"] for item in by_agent} >= {
            "market_industry", "fundamental", "technical_state", "fundflow_participation", "event_risk"
        }
        db.close()


def test_pit_industry_return_is_reproducible() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        pit = PITDatabase(Path(tmp) / "pit.sqlite")
        with pit.connect() as conn:
            for code in ("000001", "000002", "000003"):
                conn.execute(
                    """
                    INSERT INTO pit_industry(code, industry, industry_level, asof_date, source, fetched_at)
                    VALUES(?, '银行', 'L1', '2026-01-01', 'fixture', '2026-01-01T09:00:00+00:00')
                    """,
                    (code,),
                )
                for offset, close in enumerate((10.0, 10.2, 10.4, 10.6)):
                    conn.execute(
                        """
                        INSERT INTO pit_prices(code, trade_date, adj, close, close_raw, asof_date, source, fetched_at)
                        VALUES(?, ?, 'hfq', ?, ?, ?, 'fixture', '2026-01-01T09:00:00+00:00')
                        """,
                        (
                            code,
                            "2026-01-02" if offset == 0 else add_trading_days("2026-01-02", offset),
                            close,
                            close,
                            "2026-01-02",
                        ),
                    )
        source = StoreBackedPriceSource(pit_database=pit)
        value = source.industry_return("银行", "2026-01-02", 3)
        assert value is not None
        assert round(value, 4) == round((6.0 + 6.0 + 6.0) / 3, 4)
        assert source.industry_return("银行", "2026-01-02", 3) == value


if __name__ == "__main__":
    test_observation_modes_lens_metrics_and_exclusions()
    test_data_quality_and_agent_process_metrics()
    test_pit_industry_return_is_reproducible()
    print("[OK] 阶段7评价离线验收通过")
