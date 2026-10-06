from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.models import FinancialMetrics, StockQuote
from stock_tool.core.ai_analyzer import AIConfig
from stock_tool.core.data_snapshot import DataSnapshotFact, DataSnapshotManifest, fact_set_digest
from stock_tool.core.evidence_contracts import contract_hash
from stock_tool.core.evidence_run_store import EvidenceReplayError, SqliteEvidenceRunStore
from stock_tool.core.evidence_summary import aggregate_agent_evidence
from stock_tool.core.multi_agent_orchestrator import FiveAgentOrchestrator, SqliteAgentExecutionStore
from stock_tool.core.screening_evidence import build_screening_evidence_package
from stock_tool.core.screening_contract import stable_hash
from stock_tool.core.query_schema import QueryValidationError, query_spec_from_dict
from stock_tool.core.screener_engine import ScreenCandidate, ScreenReport
from stock_tool.core.strategy_store import (
    CSV_RESULT_COLUMNS,
    StrategyStore,
    query_spec_from_export_payload,
    run_results_csv_rows,
    strategy_export_payload,
)
from stock_tool.core.technical_profile import TechnicalProfile
from stock_tool.core.v1_database import V1Database, V1_SCHEMA_VERSION
from tools.test_phase4_multi_agent import _config, _package, _requesters


def _sample_spec():
    return query_spec_from_dict(
        {
            "intent": "screen",
            "conditions": [
                {"field": "roe", "op": ">=", "value": 15},
                {"field": "pe", "op": "<=", "value": 35},
                {"field": "debt_ratio", "op": "<=", "value": 60},
            ],
            "exclude": ["ST"],
            "sort": [{"field": "local_score", "direction": "desc"}],
            "scan_limit": 300,
            "result_limit": 80,
            "allow_missing_pe": True,
            "with_technical": True,
            "needs_confirmation": False,
            "clarifications": [],
            "raw_text": "找ROE高、PE温和、负债率稳的股票",
        }
    )


def _sample_report() -> ScreenReport:
    return ScreenReport(
        results=[
            ScreenCandidate(
                quote=StockQuote(code="000001", name="样例银行", price=10.5, pe=8.2, pb=0.9, market_cap=500_000_000_000),
                metrics=FinancialMetrics(
                    roe=18.5,
                    gross_margin=35.0,
                    net_margin=20.1,
                    debt_ratio=42.0,
                    revenue_growth=8.8,
                    profit_growth=12.3,
                ),
                technical=TechnicalProfile(
                    True,
                    status="candidate",
                    opportunity_label="A 回踩候选",
                    risk_flags=["财报需复核", "成交额观察"],
                    summary="趋势样例",
                ),
                local_score=91.2,
                reasons=["ROE较好", "PE较温和", "A 回踩候选"],
            )
        ],
        scanned=300,
        skipped=299,
        failed=1,
        errors=["000002 样例失败: 网络超时"],
    )


def _assert_rejects(payload: dict, expected: str) -> None:
    try:
        query_spec_from_export_payload(payload)
    except QueryValidationError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError("无效导入策略应被拒绝")


def test_f_layer_replays_across_process_boundaries() -> None:
    package = _package()
    orchestration = FiveAgentOrchestrator(
        _config(), requesters=_requesters(), retries=0
    ).run(package, run_id="phase6-replay")
    aggregation = aggregate_agent_evidence(package, orchestration)
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteEvidenceRunStore(str(Path(tmp) / "replay.sqlite3"))
        saved = store.save(package, aggregation, orchestration=orchestration)
        assert saved.evidence_package_hash == package["evidence_package_hash"]

        reopened = SqliteEvidenceRunStore(str(Path(tmp) / "replay.sqlite3"))
        replay = reopened.get("phase6-replay")
        assert replay is not None
        assert replay.package["evidence_package_hash"] == package["evidence_package_hash"]
        assert replay.aggregation.summary_hash == aggregation.summary_hash
        assert replay.orchestration is not None
        assert replay.orchestration.run_id == orchestration.run_id
        assert replay.orchestration.executions[0].result is not None

        conn = sqlite3.connect(Path(tmp) / "replay.sqlite3")
        try:
            conn.execute(
                "UPDATE formal_evidence_runs SET evidence_package_json = ? WHERE run_id = ?",
                ("{}", "phase6-replay"),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            reopened.get("phase6-replay")
        except EvidenceReplayError:
            pass
        else:
            raise AssertionError("篡改后的 F 层回放必须被拒绝")


def test_replay_rejects_mismatched_agent_execution_identity() -> None:
    package = _package()
    orchestration = FiveAgentOrchestrator(
        _config(), requesters=_requesters(), retries=0
    ).run(package, run_id="phase6-agent-identity")
    aggregation = aggregate_agent_evidence(package, orchestration)
    with tempfile.TemporaryDirectory() as tmp:
        database_path = Path(tmp) / "identity.sqlite3"
        store = SqliteEvidenceRunStore(str(database_path))
        store.save(package, aggregation, orchestration=orchestration)
        with closing(sqlite3.connect(database_path)) as conn:
            payload = json.loads(
                conn.execute(
                    "SELECT orchestration_json FROM formal_evidence_runs WHERE run_id = ?",
                    ("phase6-agent-identity",),
                ).fetchone()[0]
            )
            payload["executions"][0]["candidate_set_digest"] = "tampered"
            conn.execute(
                "UPDATE formal_evidence_runs SET orchestration_json = ?, orchestration_hash = ? WHERE run_id = ?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), contract_hash(payload), "phase6-agent-identity"),
            )
            conn.commit()
        try:
            store.get("phase6-agent-identity")
        except EvidenceReplayError as exc:
            assert "候选集哈希" in str(exc)
        else:
            raise AssertionError("Agent execution 身份被篡改时必须拒绝回放")


def test_missing_api_key_package_is_persisted_and_retryable() -> None:
    package = _package()
    with tempfile.TemporaryDirectory() as tmp:
        database_path = Path(tmp) / "agent-retry.sqlite3"
        store = SqliteEvidenceRunStore(str(database_path))
        queued = store.save_package(package, status="pending")
        skipped = FiveAgentOrchestrator(
            AIConfig(provider="fixture", api_key="", model="fixture-model"),
            retries=0,
        ).run(package, run_id="missing-key-run")
        assert skipped.candidate_set_preserved
        assert all(item.status == "skipped" for item in skipped.executions)
        assert all(item.error == "missing_api_key" for item in skipped.executions)
        skipped_aggregation = aggregate_agent_evidence(package, skipped)
        store.mark_package(package, status="skipped", run_id=skipped.run_id, error="missing_api_key")
        store.save(package, skipped_aggregation, orchestration=skipped)

        reopened = SqliteEvidenceRunStore(str(database_path))
        retryable = reopened.latest_package(
            snapshot_id=package["data_snapshot_id"],
            candidate_set_digest=package["candidate_set_digest"],
        )
        assert retryable is not None
        assert retryable.package_id == queued.package_id
        assert retryable.status == "skipped"
        assert retryable.package["evidence_package_hash"] == package["evidence_package_hash"]
        retryable_batches = reopened.list_retryable_batches()
        assert retryable_batches and retryable_batches[0]["snapshot_id"] == package["data_snapshot_id"]
        assert retryable_batches[0]["candidate_set_digest"] == package["candidate_set_digest"]
        assert retryable_batches[0]["evidence_package_hash"] == package["evidence_package_hash"]
        assert retryable_batches[0]["lens_version"] == package["formula_version"]
        assert retryable_batches[0]["batch_range"] == [1, len(package["candidate_records"])]
        assert isinstance(retryable_batches[0]["prompt_versions"], dict)
        assert isinstance(retryable_batches[0]["model_versions"], dict)

        agent_store = SqliteAgentExecutionStore(str(database_path))
        retried = FiveAgentOrchestrator(
            _config(),
            requesters=_requesters(),
            result_store=agent_store,
            cache=agent_store,
            retries=0,
        ).run(retryable.package, run_id="retry-after-key")
        assert retried.candidate_set_preserved
        assert all(item.status in {"ok", "degraded"} for item in retried.executions)
        assert retried.snapshot_id == skipped.snapshot_id
        assert retried.candidate_set_digest == skipped.candidate_set_digest
        retried_aggregation = aggregate_agent_evidence(retryable.package, retried)
        reopened.mark_package(retryable.package, status="completed", run_id=retried.run_id)
        reopened.save(retryable.package, retried_aggregation, orchestration=retried)
        assert reopened.latest_package(
            snapshot_id=package["data_snapshot_id"],
            candidate_set_digest=package["candidate_set_digest"],
        ).status == "completed"


def test_retry_metadata_preserves_persisted_batch_range() -> None:
    package = deepcopy(_package())
    package["batch_range"] = [31, 32]
    package.pop("evidence_package_hash", None)
    package["evidence_package_hash"] = stable_hash(package)
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteEvidenceRunStore(str(Path(tmp) / "batch-range.sqlite3"))
        queued = store.save_package(package, status="pending")
        assert queued.batch_range == (31, 32)
        metadata = store.list_retryable_batches()[0]
        assert metadata["batch_range"] == [31, 32]
        assert metadata["lens_version"] == package["formula_version"]


def test_screening_package_is_candidate_scoped_and_trade_field_free() -> None:
    raw_fact = DataSnapshotFact(
        snapshot_id="seed",
        code="000001",
        field_name="industry",
        value="样例行业",
        effective_at="2026-07-01T00:00:00+00:00",
        available_at="2026-07-01T00:00:00+00:00",
        source="fixture",
        source_version="v1",
        fetched_at="2026-07-01T00:00:00+00:00",
    )
    manifest = DataSnapshotManifest.create(
        asof_at="2026-07-01T00:00:00+00:00",
        availability_cutoff="2026-07-01T00:00:00+00:00",
        data_source="fixture",
        universe_id="fixture",
        dataset_version="v1",
        facts_digest=fact_set_digest([raw_fact]),
        created_at="2026-07-01T00:00:00+00:00",
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
    package = build_screening_evidence_package(
        manifest=manifest,
        facts=[fact],
        rows=[
            {
                "candidate_id": "000001",
                "code": "000001",
                "name": "样例股",
                "price": 10.5,
                "lens_ids": ["quality_research"],
                "eligibility_status": "eligible",
                "reason_codes": ["LENS_QUALITY_RESEARCH"],
            }
        ],
    )
    record = package["candidate_records"][0]
    assert "price" not in record
    assert package["snapshot_metadata"]["snapshot_id"] == manifest.snapshot_id
    assert package["evidence_package_hash"]


def test_multi_batch_agent_views_remain_candidate_scoped() -> None:
    from stock_tool.ui.qt_workbench import _agent_evidence_views

    first_package = _package()
    second_package = deepcopy(first_package)
    second_package["candidate_records"][0]["code"] = "000002"
    second_package["candidates"] = deepcopy(second_package["candidate_records"])
    second_package["candidate_set_digest"] = stable_hash(["000002"])
    second_package.pop("evidence_package_hash", None)
    second_package["evidence_package_hash"] = stable_hash(second_package)

    first_result = FiveAgentOrchestrator(
        _config(), requesters=_requesters(), retries=0
    ).run(first_package, run_id="phase6-batch-1")
    second_result = FiveAgentOrchestrator(
        _config(), requesters=_requesters(), retries=0
    ).run(second_package, run_id="phase6-batch-2")
    first_aggregation = aggregate_agent_evidence(first_package, first_result)
    second_aggregation = aggregate_agent_evidence(second_package, second_result)
    views = _agent_evidence_views(
        [first_result, second_result],
        [first_aggregation, second_aggregation],
        base_data_gaps_by_code={"000001": [], "000002": []},
        evidence_package_hashes=[
            first_package["evidence_package_hash"],
            second_package["evidence_package_hash"],
        ],
    )
    assert set(views) == {"000001", "000002"}
    assert views["000001"]["agent_run_id"] == "phase6-batch-1"
    assert views["000002"]["agent_run_id"] == "phase6-batch-2"


def run_tests() -> None:
    test_f_layer_replays_across_process_boundaries()
    test_replay_rejects_mismatched_agent_execution_identity()
    test_missing_api_key_package_is_persisted_and_retryable()
    test_retry_metadata_preserves_persisted_batch_range()
    test_screening_package_is_candidate_scoped_and_trade_field_free()
    test_multi_batch_agent_views_remain_candidate_scoped()
    spec = _sample_spec()
    payload = strategy_export_payload(
        name="商业工作台样例",
        user_text=spec.raw_text,
        spec=spec,
        summary="样例摘要",
        parse_source="phase6-test",
    )
    assert payload["format"] == "stock_tool_strategy"
    assert payload["version"] == 1
    assert payload["strategy_name"] == "商业工作台样例"
    assert payload["summary"] == "样例摘要"
    assert payload["query_spec"]["conditions"][0]["field"] == "roe"
    assert "exported_at" in payload
    assert "不构成投资建议" in payload["disclaimer"]

    name, user_text, imported_spec, summary, parse_source = query_spec_from_export_payload(json.loads(json.dumps(payload, ensure_ascii=False)))
    assert name == "商业工作台样例"
    assert user_text == spec.raw_text
    assert summary == "样例摘要"
    assert parse_source == "phase6-test"
    assert imported_spec.conditions[0].field == "roe"

    _assert_rejects({"query_spec": []}, "query_spec")
    _assert_rejects({"query_spec": {"conditions": [{"field": "dividend_yield", "op": ">=", "value": 3}]}}, "不支持的字段")
    _assert_rejects({"query_spec": {"stocks": [{"code": "000001"}]}}, "股票列表")
    outer_stock_list_payload = json.loads(json.dumps(payload, ensure_ascii=False))
    outer_stock_list_payload["stocks"] = [{"code": "000001"}]
    _assert_rejects(outer_stock_list_payload, "股票列表")

    with tempfile.TemporaryDirectory() as tmp:
        db = V1Database(Path(tmp) / "stock_tool_v1.sqlite3")
        store = StrategyStore(db)
        preset = store.save_preset("商业工作台样例", spec.raw_text, spec, parse_source="phase6-test")
        run_id = store.record_run(
            source_label="已保存策略",
            user_text=spec.raw_text,
            spec=spec,
            data_source="v1",
            report=_sample_report(),
            preset_id=preset.id,
            preset_name=preset.name,
            note="测试抓取失败摘要",
        )
        detail = store.get_run_detail(run_id)
        assert detail.run.id == run_id
        assert detail.run.preset_name == "商业工作台样例"
        assert detail.run.scanned == 300
        assert detail.run.matched == 1
        assert detail.run.failed == 1
        assert detail.run.note == "测试抓取失败摘要"
        assert len(detail.results) == 1

        result = detail.results[0]
        assert result.rank_order == 1
        assert result.code == "000001"
        assert result.risk_flags == ["财报需复核", "成交额观察"]
        assert result.metrics["price"] == 10.5
        assert result.metrics["market_cap_yi"] == 5000.0
        assert result.metrics["technical_label"] == "A 回踩候选"

        rows = run_results_csv_rows(detail.results)
        assert list(rows[0].keys()) == CSV_RESULT_COLUMNS
        assert rows[0]["排名"] == 1
        assert rows[0]["代码"] == "000001"
        assert rows[0]["风险标签"] == "财报需复核、成交额观察"
        assert rows[0]["价格"] == 10.5
        assert rows[0]["市值"] == 5000.0
        assert rows[0]["技术状态"] == "A 回踩候选"
        assert run_results_csv_rows([]) == []

        missing_parent = Path(tmp) / "not_a_directory"
        missing_parent.write_text("占位", encoding="utf-8")
        try:
            V1Database(missing_parent / "stock_tool_v1.sqlite3")
        except Exception:
            pass
        else:
            raise AssertionError("不可用 V1 数据库路径应显式失败，供 UI 回退提示")

        with db.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert version == V1_SCHEMA_VERSION == 16
        assert {"strategy_presets", "strategy_preset_versions", "query_runs", "query_run_results"}.issubset(tables)
        db.close()

    print("[OK] 第六阶段商业工作台离线测试通过")


if __name__ == "__main__":
    run_tests()
