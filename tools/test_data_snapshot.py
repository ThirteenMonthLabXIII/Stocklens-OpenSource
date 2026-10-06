from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.data_snapshot import (
    DataSnapshotFact,
    DataSnapshotManifest,
    DataSnapshotStore,
    fact_set_digest,
    is_manifest_fresh,
)
from stock_tool.core.factor_store import FactorSnapshot
from stock_tool.core.query_schema import QuerySpec
from stock_tool.core.screening_contract import snapshot_id
from stock_tool.core.screening_workflow import ScreeningWorkflowService
from stock_tool.core.v1_database import V1Database


class _FakeStrategyStore:
    def __init__(self) -> None:
        self.runs: list[dict[str, object]] = []

    def record_run(self, **kwargs: object) -> int:
        self.runs.append(kwargs)
        return 17


def run_tests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        legacy_path = Path(tmp) / "legacy_v1.sqlite3"
        legacy_conn = sqlite3.connect(legacy_path)
        try:
            legacy_conn.execute(
                """
                CREATE TABLE data_snapshot_manifests (
                    snapshot_id TEXT PRIMARY KEY,
                    asof_at TEXT NOT NULL,
                    availability_cutoff TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    universe_id TEXT NOT NULL,
                    dataset_version TEXT NOT NULL,
                    source_versions_json TEXT NOT NULL DEFAULT '{}',
                    quality_status TEXT NOT NULL,
                    quality_summary_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            legacy_conn.commit()
        finally:
            legacy_conn.close()
        legacy_database = V1Database(legacy_path)
        try:
            with legacy_database.connect() as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(data_snapshot_manifests)")}
            assert "facts_digest" in columns, "旧快照表必须通过显式迁移补齐 facts_digest"
        finally:
            legacy_database.close()

        database = V1Database(Path(tmp) / "v1.sqlite3")
        store = DataSnapshotStore(database)
        fact_payloads = [
            {
                "code": "000001",
                "field_name": "pe_ttm",
                "value": 8.2,
                "effective_at": "2026-07-22",
                "available_at": "2026-07-23T09:00:00+08:00",
                "source": "fixture",
                "source_version": "fixture-1",
                "fetched_at": "2026-07-23T08:30:00+00:00",
                "revision_id": "pe-20260723-1",
            },
            {
                "code": "000001",
                "field_name": "roe",
                "value": 18.5,
                "effective_at": "2026-03-31",
                "available_at": "2026-05-01T10:00:00+08:00",
                "source": "fixture",
                "source_version": "fixture-2",
                "fetched_at": "2026-07-23T08:30:00+00:00",
                "revision_id": "roe-20260501-1",
            },
        ]
        manifest = DataSnapshotManifest.create(
            asof_at="2026-07-23T15:00:00+08:00",
            availability_cutoff="2026-07-23T15:00:00+08:00",
            data_source="fixture",
            universe_id="a_share.all.v1",
            dataset_version="fixture-20260723",
            facts_digest=fact_set_digest(fact_payloads),
            source_versions={"quote": "fixture-1", "financial": "fixture-2"},
            quality_summary={"coverage": 1.0},
            created_at="2026-07-23T08:00:00+00:00",
        )
        facts = [DataSnapshotFact(snapshot_id=manifest.snapshot_id, **payload) for payload in fact_payloads]
        store.create_snapshot(manifest, facts)
        assert is_manifest_fresh(
            manifest,
            timedelta(days=1),
            now=datetime.fromisoformat("2026-07-23T18:00:00+08:00"),
        )

        loaded_manifest = store.get_manifest(manifest.snapshot_id)
        assert loaded_manifest == manifest
        loaded_facts = store.list_facts(manifest.snapshot_id)
        assert [(fact.code, fact.field_name, fact.value) for fact in loaded_facts] == [
            ("000001", "pe_ttm", 8.2),
            ("000001", "roe", 18.5),
        ]
        assert all(fact.raw_hash for fact in loaded_facts)

        refresh_database = V1Database(Path(tmp) / "refresh.sqlite3")
        refresh_store = DataSnapshotStore(refresh_database)
        refresh_one = refresh_store.record_refresh_attempt(
            asof_at="2026-07-23T15:00:00+08:00",
            availability_cutoff="2026-07-23T15:00:00+08:00",
            data_source="fixture",
            universe_id="a_share.all.v1",
            universe_version="members-1",
            source_policy_version="policy-1",
            source_versions={"quote": "fixture-1", "financial": "fixture-2"},
            started_at="2026-07-23T07:59:00+00:00",
        )
        first_ref = refresh_store.finalize_refresh(
            refresh_one.refresh_event_id,
            fact_payloads,
            finished_at="2026-07-23T08:00:00+00:00",
            quality_summary={"coverage": 1.0},
            coverage={"facts": 2, "codes": 1},
        )
        assert first_ref is not None and not first_ref.reused
        refresh_two = refresh_store.record_refresh_attempt(
            asof_at="2026-07-23T15:00:00+08:00",
            availability_cutoff="2026-07-23T15:00:00+08:00",
            data_source="fixture",
            universe_id="a_share.all.v1",
            universe_version="members-1",
            source_policy_version="policy-1",
            source_versions={"quote": "fixture-1", "financial": "fixture-2"},
            started_at="2026-07-23T08:04:00+00:00",
        )
        second_ref = refresh_store.finalize_refresh(
            refresh_two.refresh_event_id,
            fact_payloads,
            finished_at="2026-07-23T08:05:00+00:00",
            quality_summary={"coverage": 1.0},
        )
        assert first_ref.snapshot_id == second_ref.snapshot_id
        assert second_ref.reused
        assert refresh_one.refresh_event_id != refresh_two.refresh_event_id
        assert refresh_store.get_refresh_event(refresh_two.refresh_event_id).snapshot_id == first_ref.snapshot_id  # type: ignore[union-attr]
        assert refresh_store.get_snapshot(first_ref.snapshot_id).manifest.universe_version == "members-1"  # type: ignore[union-attr]
        assert refresh_store.resolve_snapshot_for_cutoff(
            "2026-07-23T16:00:00+08:00", "a_share.all.v1"
        ).snapshot_id == first_ref.snapshot_id  # type: ignore[union-attr]
        quality = refresh_store.assess_snapshot_quality(
            first_ref.snapshot_id,
            {"required_fields": ("pe_ttm", "roe"), "codes": ("000001",)},
        )
        assert quality.status == "verified"

        failed_event = refresh_store.record_refresh_attempt(
            asof_at="2026-07-23T15:00:00+08:00",
            availability_cutoff="2026-07-23T15:00:00+08:00",
            data_source="fixture",
            universe_id="a_share.all.v1",
            source_policy_version="policy-1",
        )
        assert refresh_store.finalize_refresh(
            failed_event.refresh_event_id,
            status="failed",
            error_message="fixture timeout",
        ) is None
        failed_result = refresh_store.get_refresh_event(failed_event.refresh_event_id)
        assert failed_result is not None and failed_result.status == "failed"
        assert failed_result.snapshot_id is None

        failover_event = refresh_store.record_refresh_attempt(
            asof_at="2026-07-23T15:00:00+08:00",
            availability_cutoff="2026-07-23T15:00:00+08:00",
            data_source="fixture",
            universe_id="a_share.all.v1",
            source_policy_version="policy-2",
            source_route="failover",
            used_failover=True,
        )
        refresh_store.finalize_refresh(
            failover_event.refresh_event_id,
            fact_payloads,
            status="partial",
            quality_status="partial",
        )
        failover_result = refresh_store.get_refresh_event(failover_event.refresh_event_id)
        assert failover_result is not None
        assert failover_result.source_route == "failover"
        assert failover_result.used_failover
        assert refresh_store.resolve_snapshot_for_cutoff(
            "2026-07-23T16:00:00+08:00", "a_share.all.v1"
        ).snapshot_id == first_ref.snapshot_id  # type: ignore[union-attr]
        refresh_database.close()

        with database.connect() as conn:
            try:
                conn.execute(
                    "UPDATE data_snapshot_facts SET value_json = ? WHERE snapshot_id = ?",
                    ('"tampered"', manifest.snapshot_id),
                )
            except sqlite3.DatabaseError as exc:
                assert "append-only" in str(exc)
            else:
                raise AssertionError("事实表必须拒绝 UPDATE")

        late_payload = {
            "code": "000002",
            "field_name": "roe",
            "value": 20,
            "effective_at": "2026-03-31",
            "available_at": "2026-07-24",
            "source": "fixture",
            "source_version": "fixture-2",
            "fetched_at": "2026-07-24",
        }
        late_manifest = DataSnapshotManifest.create(
            asof_at="2026-07-23",
            availability_cutoff="2026-07-23",
            data_source="fixture",
            universe_id="a_share.all.v1",
            dataset_version="fixture-20260723",
            facts_digest=fact_set_digest([late_payload]),
        )
        late_fact = DataSnapshotFact(snapshot_id=late_manifest.snapshot_id, **late_payload)
        try:
            store.create_snapshot(late_manifest, [late_fact])
        except ValueError as exc:
            assert "available_at" in str(exc)
        else:
            raise AssertionError("未来可获得事实不能写入更早的快照")

        fake_strategy_store = _FakeStrategyStore()
        workflow = ScreeningWorkflowService(fake_strategy_store, None)
        blocked_manifest = DataSnapshotManifest.create(
            asof_at="2026-07-23T15:00:00+08:00",
            availability_cutoff="2026-07-23T15:00:00+08:00",
            data_source="fixture",
            universe_id="a_share.all.v1",
            dataset_version="fixture-20260723",
            facts_digest=fact_set_digest([]),
            quality_status="blocked",
        )
        screen_result = workflow.run_snapshot_screening(
            [
                FactorSnapshot(
                    code="000001",
                    name="样例",
                    price=10,
                    pe=8,
                    pb=1,
                    roe=20,
                    debt_ratio=30,
                    technical_status="missing",
                    updated_at="2026-07-23T15:00:00+08:00",
                )
            ],
            QuerySpec(with_technical=False),
            source_label="fixture",
            user_text="fixture",
            data_source="fixture",
            data_manifest=blocked_manifest,
        )
        assert screen_result.report.status == "blocked"
        assert screen_result.report.data_snapshot_id == blocked_manifest.snapshot_id
        assert fake_strategy_store.runs[0]["scoring_context"]["availability_cutoff"] == "2026-07-23T15:00:00+08:00"  # type: ignore[index]

        partial_manifest = DataSnapshotManifest.create(
            asof_at="2026-07-23T15:00:00+08:00",
            availability_cutoff="2026-07-23T15:00:00+08:00",
            data_source="fixture",
            universe_id="a_share.all.v1",
            dataset_version="fixture-20260723",
            facts_digest=fact_set_digest([]),
            quality_status="partial",
        )
        partial_result = workflow.run_snapshot_screening(
            [
                FactorSnapshot(
                    code="000001",
                    name="样例",
                    price=10,
                    pe=8,
                    pb=1,
                    roe=20,
                    debt_ratio=30,
                    technical_status="missing",
                    updated_at="2026-07-23T15:00:00+08:00",
                )
            ],
            QuerySpec(with_technical=False),
            source_label="fixture",
            user_text="fixture",
            data_source="fixture",
            data_manifest=partial_manifest,
        )
        assert partial_result.report.status == "blocked"
        assert "完整且已验证" in partial_result.report.errors[0]

        review_result = workflow.run_snapshot_screening(
            [
                FactorSnapshot(
                    code="000001",
                    name="样例",
                    price=10,
                    pe=8,
                    pb=1,
                    roe=20,
                    debt_ratio=30,
                    technical_status="missing",
                    updated_at="2026-07-23T15:00:00+08:00",
                )
            ],
            QuerySpec(with_technical=False, execution_mode="review_only"),
            source_label="fixture",
            user_text="fixture",
            data_source="fixture",
            data_manifest=partial_manifest,
        )
        assert review_result.report.status == "review_only"

        required_result = workflow.run_snapshot_screening(
            [
                FactorSnapshot(
                    code="000001",
                    name="样例",
                    price=10,
                    pe=8,
                    pb=1,
                    roe=20,
                    debt_ratio=30,
                    technical_status="missing",
                    updated_at="2026-07-23T15:00:00+08:00",
                )
            ],
            QuerySpec(with_technical=False),
            source_label="fixture",
            user_text="fixture",
            data_source="fixture",
            require_data_manifest=True,
        )
        assert required_result.report.status == "blocked"
        assert "data_manifest" in required_result.report.errors[0]

        base_snapshot = FactorSnapshot(
            code="000001",
            name="样例",
            roe=20,
            updated_at="2026-07-23T15:00:00+08:00",
        )
        changed_snapshot = FactorSnapshot(
            code="000001",
            name="样例",
            roe=21,
            updated_at="2026-07-23T15:00:00+08:00",
        )
        assert snapshot_id([base_snapshot], data_source="fixture") != snapshot_id(
            [changed_snapshot], data_source="fixture"
        )

        database.close()


if __name__ == "__main__":
    run_tests()
    print("[OK] data snapshot tests passed")
