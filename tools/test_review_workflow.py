from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.cache import CacheStore
from stock_tool.core.models import StockQuote
from stock_tool.core.performance_loop import PerformanceStore
from stock_tool.core.review_workflow import ReviewWorkflowService
from stock_tool.core.v1_database import V1Database, v1_db_path_for


def test_tracking_priority_audit_and_export() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "cache.sqlite3")
        workflow = ReviewWorkflowService(cache)
        invalid = workflow.create_tracking_task(
            code="000001", name="矛盾", entry_price=10, fail_price=11, target_price=9, actor="tester",
        )
        assert invalid.code == "validation_error"
        created = workflow.create_tracking_task(
            code="000001", name="样例", entry_price=10, fail_price=9, target_price=11,
            horizon_days=3, due_date="2026-07-10", actor="tester",
        )
        assert created.ok and created.payload
        task_id = int(created.payload["id"])
        missing = workflow.review_tracking_task(task_id, None, "tester", now=date(2026, 7, 12))
        assert missing.code == "missing_quote"
        # 同时满足到期时，失效优先于目标和到期。
        failed = workflow.review_tracking_task(task_id, StockQuote("000001", "样例", price=8.5), "tester", now=date(2026, 7, 12))
        assert failed.ok and failed.payload and failed.payload["result"] == "观察依据失效"
        assert workflow.export_tracking_rows()[0]["结论"] == "观察依据失效"
        assert workflow.delete_tracking_task(task_id, "tester", "test").ok
        assert {item.action for item in cache.list_audit("tracking_task")} >= {"create", "review", "delete"}


def test_formula_activation_confirmation_reason_and_rollback() -> None:
    with tempfile.TemporaryDirectory() as raw:
        database = V1Database(v1_db_path_for(Path(raw)))
        store = PerformanceStore(database)
        active = store.ensure_default_professional_formula()
        with database.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO professional_formula_candidates(
                    name, version, status, dimension_weights_json, field_weights_json,
                    evidence_json, validation_json, created_at, updated_at, activated_at
                ) VALUES('candidate', 'v2', 'suggest_enable', ?, ?, '{}', '{}', '2026-07-01', '2026-07-01', '')""",
                (json.dumps(active["dimension_weights"]), json.dumps(active["field_weights"])),
            )
            candidate_id = int(cursor.lastrowid)
        workflow = ReviewWorkflowService(CacheStore(Path(raw) / "cache.sqlite3"), store)
        assert workflow.activate_candidate(candidate_id, "tester", "", confirmed=True).code == "reason_required"
        assert workflow.activate_candidate(candidate_id, "tester", "test", confirmed=False).code == "confirmation_required"
        enabled = workflow.activate_candidate(candidate_id, "tester", "evidence verified", confirmed=True)
        assert enabled.ok and enabled.payload and enabled.payload["id"] == candidate_id
        rolled = workflow.rollback_previous_active("tester", "return to prior", confirmed=True)
        assert rolled.ok and rolled.payload and rolled.payload["id"] == active["id"]
        assert {row["action"] for row in workflow.list_governance_audit()} >= {"activate", "rollback"}
        database.close()


def main() -> int:
    test_tracking_priority_audit_and_export()
    test_formula_activation_confirmation_reason_and_rollback()
    print("[OK] ReviewWorkflowService 合同测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
