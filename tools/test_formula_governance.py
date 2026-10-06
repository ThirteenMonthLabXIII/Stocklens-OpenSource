from __future__ import annotations

"""P3 公式治理：确认、审计、回滚与重开后的状态保持。"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.cache import CacheStore
from stock_tool.core.performance_loop import PerformanceStore
from stock_tool.core.review_workflow import ReviewWorkflowService
from stock_tool.core.v1_database import V1Database, v1_db_path_for


def test_confirmation_audit_rollback_and_reopen() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
        root = Path(raw)
        database = V1Database(v1_db_path_for(root)); database.init_schema()
        database.init_schema()  # 幂等迁移不得覆盖旧治理记录。
        store = PerformanceStore(database)
        active = store.ensure_default_professional_formula()
        with database.connect() as conn:
            candidate_id = int(conn.execute(
                """INSERT INTO professional_formula_candidates(
                name, version, status, dimension_weights_json, field_weights_json,
                evidence_json, validation_json, created_at, updated_at, activated_at
                ) VALUES('p3-candidate', 'p3-v1', 'suggest_enable', ?, ?, '{}', '{}', '2026-07-01', '2026-07-01', '')""",
                (json.dumps(active["dimension_weights"]), json.dumps(active["field_weights"])),
            ).lastrowid)
        workflow = ReviewWorkflowService(CacheStore(root / "cache.sqlite3"), store)
        assert workflow.activate_candidate(candidate_id, "tester", "", confirmed=True).code == "reason_required"
        assert workflow.activate_candidate(candidate_id, "tester", "evidence reviewed", confirmed=False).code == "confirmation_required"
        assert workflow.activate_candidate(candidate_id, "tester", "evidence reviewed", confirmed=True).ok
        assert workflow.rollback_previous_active("tester", "revert after review", confirmed=True).payload["id"] == active["id"]
        database.close()
        reopened = V1Database(v1_db_path_for(root)); reopened.init_schema()
        reopened_store = PerformanceStore(reopened)
        assert reopened_store.get_active_professional_formula()["id"] == active["id"]
        actions = {item["action"] for item in ReviewWorkflowService(CacheStore(root / "cache.sqlite3"), reopened_store).list_governance_audit()}
        assert {"activate", "rollback"}.issubset(actions)
        with reopened.connect() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"professional_formula_candidates", "formula_governance_audit"}.issubset(tables)
        reopened.close()


if __name__ == "__main__":
    test_confirmation_audit_rollback_and_reopen()
    print("[OK] P3 公式治理核心测试通过")
