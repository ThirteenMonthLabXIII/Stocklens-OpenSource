"""Offline acceptance for formal certificates and observation gates."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.data_snapshot import DataSnapshotFact, DataSnapshotStore
from stock_tool.core.formal_certificate import (
    FormalObservationBlocked,
    FormalScreeningCertificate,
    FormalScreeningCertificateStore,
    UniverseDefinition,
)
from stock_tool.core.performance_loop import PerformanceStore
from stock_tool.core.v1_database import V1Database, v1_db_path_for


def _seed_run(database: V1Database, *, mode: str = "formal") -> int:
    with database.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO query_runs(
                source_label, user_text, query_spec_json, summary, data_source,
                scanned, matched, skipped, failed, scoring_context_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fixture",
                "fixture",
                "{}",
                "fixture",
                "fixture",
                1,
                1,
                0,
                0,
                json.dumps({"execution_mode": mode, "screening_status": "verified"}),
                "2026-07-24T09:00:00+00:00",
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO query_run_results(
                run_id, rank_order, code, name, local_score, metrics_json,
                score_breakdown_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, 1, "000001", "样例", 80.0, "{}", "{}"),
        )
    return run_id


def _seed_snapshot(database: V1Database) -> tuple[str, str]:
    store = DataSnapshotStore(database)
    event = store.record_refresh_attempt(
        asof_at="2026-07-24T09:00:00+00:00",
        availability_cutoff="2026-07-24T09:00:00+00:00",
        data_source="fixture",
        universe_id="a_share_listed_pit",
        universe_version="u-v1",
        dataset_version="fixture-v1",
        source_policy_version="fixture-policy-v1",
        source_versions={"fixture": "v1"},
        started_at="2026-07-24T09:00:00+00:00",
    )
    fact = DataSnapshotFact(
        snapshot_id="pending",
        code="000001",
        field_name="roe",
        value=20,
        effective_at="2026-07-23T00:00:00+00:00",
        available_at="2026-07-24T08:00:00+00:00",
        source="fixture",
        source_version="v1",
        fetched_at="2026-07-24T09:00:00+00:00",
    )
    ref = store.finalize_refresh(
        event.refresh_event_id,
        [fact],
        status="success",
        quality_status="verified",
        quality_summary={"fact_total": 1, "verified_fact_count": 1},
        finished_at="2026-07-24T09:01:00+00:00",
    )
    assert ref is not None
    return ref.snapshot_id, event.refresh_event_id


def test_formal_certificate_gate() -> None:
    with tempfile.TemporaryDirectory() as raw:
        database = V1Database(v1_db_path_for(Path(raw)))
        snapshot_id, refresh_event_id = _seed_snapshot(database)
        run_id = _seed_run(database)
        performance = PerformanceStore(database)

        try:
            performance.create_formal_observation_samples(run_id, "missing")
        except FormalObservationBlocked:
            pass
        else:
            raise AssertionError("缺少正式凭证时必须阻断")

        universe = UniverseDefinition(
            universe_definition_id="a_share_listed_pit",
            universe_version="u-v1",
            members_digest="members-v1",
            selection_scope="top_1_by_market_cap",
            selection_policy_version="v1",
            selected_limit=1,
            member_status_policy="listed_non_st",
        )
        certificate = FormalScreeningCertificate.issue(
            run_id=run_id,
            data_snapshot_id=snapshot_id,
            refresh_event_id=refresh_event_id,
            universe=universe,
            selection_scope="top_1_by_market_cap",
            formula_version="baseline-v1",
            execution_mode="formal",
            quality_status="verified",
            quality_summary={"fact_total": 1, "verified_fact_count": 1},
            condition_hash="condition-v1",
            result_digest="result-v1",
        )
        FormalScreeningCertificateStore(database).save(certificate)
        assert performance.create_formal_observation_samples(run_id, certificate.certificate_id) == 4

        with database.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO observation_samples(
                        result_id, run_id, horizon_days, sample_date, due_date,
                        code, name, rank_order, sample_class, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'formal', ?)
                    """,
                    (1, run_id, 5, "2026-07-24", "2026-07-31", "000001", "样例", 1, "2026-07-24"),
                )
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("raw SQL 无凭证插入 formal 样本必须失败")
        database.close()


def test_review_samples_are_not_formal_summary() -> None:
    with tempfile.TemporaryDirectory() as raw:
        database = V1Database(v1_db_path_for(Path(raw)))
        run_id = _seed_run(database, mode="review_only")
        performance = PerformanceStore(database)
        assert performance.create_review_observation_samples(run_id) == 4
        assert performance.summarize().sample_count == 0
        database.close()


if __name__ == "__main__":
    test_formal_certificate_gate()
    test_review_samples_are_not_formal_summary()
    print("[OK] formal screening certificate tests passed")
