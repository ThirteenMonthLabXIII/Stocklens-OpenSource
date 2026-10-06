from __future__ import annotations

import csv
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.factor_store import FactorSnapshot
from stock_tool.core.alert_service import AlertDraft, AlertService
from stock_tool.core.cache import CacheStore
from stock_tool.core.models import StockQuote
from stock_tool.core.performance_loop import PerformanceStore
from stock_tool.core.query_schema import QuerySpec, query_spec_from_dict
from stock_tool.core.screening_workflow import ScreeningWorkflowService
from stock_tool.core.strategy_store import StrategyStore, run_results_csv_rows
from stock_tool.core.v1_database import V1Database, v1_db_path_for
from stock_tool.core.review_workflow import ReviewWorkflowService


def _fixture_snapshots() -> list[FactorSnapshot]:
    return [
        FactorSnapshot(
            code="000001", name="样例银行", price=10.0, pe=8.0, pe_ttm=8.0, pb=0.9,
            market_cap_yi=500.0, roe=18.0, debt_ratio=55.0, gross_margin=35.0,
            net_margin=18.0, revenue_growth=10.0, profit_growth=12.0,
            box_score=82.0, industry="银行", industry_strength=78.0,
            technical_status="candidate", updated_at="2026-06-01T10:00:00",
        ),
        FactorSnapshot(
            code="000002", name="低ROE样例", price=9.0, pe=10.0, pe_ttm=10.0, pb=1.1,
            market_cap_yi=300.0, roe=8.0, debt_ratio=40.0, updated_at="2026-06-01T10:00:00",
        ),
    ]


def _spec() -> QuerySpec:
    return query_spec_from_dict(
        {
            "raw_text": "ROE大于15，PE小于40",
            "scan_limit": 300,
            "result_limit": 80,
            "conditions": [
                {"field": "roe", "op": ">=", "value": 15},
                {"field": "pe", "op": "<=", "value": 40},
            ],
        }
    )


def _run_core_workflow(data_dir: Path, source_label: str):
    database = V1Database(v1_db_path_for(data_dir))
    database.init_schema()
    strategy_store = StrategyStore(database)
    performance_store = PerformanceStore(database)
    workflow = ScreeningWorkflowService(strategy_store, performance_store)
    result = workflow.run_snapshot_screening(
        _fixture_snapshots(),
        _spec(),
        source_label=source_label,
        user_text="ROE大于15，PE小于40",
        data_source="v1_factor_snapshot_fresh",
        execution_mode="review_only",
        asof_date="2026-06-01",
    )
    detail = strategy_store.get_run_detail(result.run_id)
    run = strategy_store.get_run(result.run_id)
    return database, performance_store, result, detail, run


def _normalise(value):
    """Remove frontend-local database ids, clock values and filesystem paths."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, Path):
        return "<path>"
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items() if key not in {"id", "alert_id", "created_at", "updated_at", "activated_at", "last_triggered_at"}}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value


def _run_alert_and_review_workflow(data_dir: Path) -> dict:
    cache = CacheStore(data_dir / "cache.sqlite3")
    alerts = AlertService(cache, cooldown_minutes=5)
    created = alerts.create_alert(AlertDraft("000001", "样例银行", "sell", 10, "fixture", repeat=False), "fixture-user")
    assert created.ok and created.alert
    alert_id = created.alert.id
    assert alerts.update_alert(alert_id, AlertDraft("000001", "样例银行", "change_up", 2.5, "edited", repeat=False), "fixture-user").ok
    assert alerts.set_enabled(alert_id, False, "fixture-user").ok
    assert alerts.set_enabled(alert_id, True, "fixture-user").ok
    failed = alerts.check_alerts({"000001": None}, datetime(2026, 7, 10, 10, 1))
    checked = alerts.check_alerts({"000001": StockQuote("000001", "样例银行", price=10, change_pct=3)}, datetime(2026, 7, 10, 10, 0))
    assert checked.triggered_count == 1 and failed.failed_codes == ("000001",)
    assert alerts.update_monitor_policy(True, 10) == {"enabled": True, "interval_minutes": 10}

    review = ReviewWorkflowService(cache)
    task = review.create_tracking_task(
        code="000001", name="样例银行", entry_price=10, fail_price=9, target_price=11,
        horizon_days=3, due_date="2026-07-10", note="fixture", actor="fixture-user",
    )
    assert task.ok and task.payload
    task_id = int(task.payload["id"])
    assert review.review_tracking_task(task_id, None, "fixture-user").code == "missing_quote"
    assert review.append_tracking_note(task_id, "保持观察", "fixture-user").ok
    closed = review.review_tracking_task(task_id, StockQuote("000001", "样例银行", price=8.5), "fixture-user")
    assert closed.ok and closed.payload and closed.payload["result"] == "观察依据失效"

    alert_csv = data_dir / "alerts.csv"
    review_csv = data_dir / "review.csv"
    ReviewWorkflowService.write_csv(alert_csv, alerts.export_history_rows())
    ReviewWorkflowService.write_csv(review_csv, review.export_tracking_rows())
    with alert_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        alert_rows = list(csv.DictReader(handle))
    with review_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        review_rows = list(csv.DictReader(handle))
    snapshot = {
        "alerts": [asdict(item) for item in alerts.list_alerts()],
        "history": [asdict(item) for item in alerts.list_history()],
        "alert_audit": [{"entity_type": item.entity_type, "action": item.action, "actor": item.actor, "reason": item.reason} for item in cache.list_audit("alert")],
        "monitor": alerts.get_monitor_policy(),
        "tasks": [asdict(item) for item in review.list_tracking_tasks({"include_closed": True})],
        "task_audit": [{"entity_type": item.entity_type, "action": item.action, "actor": item.actor, "reason": item.reason} for item in cache.list_audit("tracking_task")],
        "alert_csv": alert_rows,
        "review_csv": review_rows,
    }
    # Reopen validates migration-created tables and persistent business state.
    reopened = CacheStore(data_dir / "cache.sqlite3")
    snapshot["reopened"] = {
        "alerts": [asdict(item) for item in reopened.list_price_alerts()],
        "history": [asdict(item) for item in reopened.list_alert_history()],
        "tasks": [asdict(item) for item in reopened.list_tracking_tasks(True)],
        "audit": [{"entity_type": item.entity_type, "action": item.action, "actor": item.actor, "reason": item.reason} for item in reopened.list_audit()],
    }
    return _normalise(snapshot)


def run_tests() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        first_db, first_performance, first_result, first_detail, first_run = _run_core_workflow(root / "first", "核心工作流样例 A")
        second_db, second_performance, second_result, second_detail, second_run = _run_core_workflow(root / "second", "核心工作流样例 B")

        assert first_result.observation_samples is None
        assert second_result.observation_samples is None
        assert first_result.warnings == second_result.warnings
        # review_only preserves the deterministic CandidateLens row even when
        # selection/PIT inputs are incomplete; the row remains insufficient
        # and must not create formal observation samples.
        assert [row.code for row in first_detail.results] == [row.code for row in second_detail.results] == ["000001"]
        assert [row.local_score for row in first_detail.results] == [row.local_score for row in second_detail.results] == [None]
        assert [row.reason for row in first_detail.results] == [row.reason for row in second_detail.results]
        assert first_run.scoring_context["ranking_formula_id"] == second_run.scoring_context["ranking_formula_id"] == "candidate_lens.v1"
        assert first_run.scoring_context["ranking_formula_version"] == second_run.scoring_context["ranking_formula_version"] == "candidate_lens.v1"
        assert first_run.scoring_context["selection_policy_version"] == second_run.scoring_context["selection_policy_version"] == "v1"
        assert first_run.scoring_context.get("active_formula") is None
        assert second_run.scoring_context.get("active_formula") is None

        for performance, result in ((first_performance, first_result), (second_performance, second_result)):
            assert performance.create_observation_samples_for_run(result.run_id) == 0

        csv_rows = run_results_csv_rows(first_detail.results)
        assert len(csv_rows) == 1 and csv_rows[0]["代码"] == "000001"

        # Independent runs must leave identical domain state, audit records and CSV rows.
        first_aux = _run_alert_and_review_workflow(root / "first_aux")
        second_aux = _run_alert_and_review_workflow(root / "second_aux")
        assert first_aux == second_aux

        first_db.close()
        second_db.close()
    print("[OK] 核心工作流筛选、预警与复盘离线一致性测试通过")


if __name__ == "__main__":
    run_tests()
