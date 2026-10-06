from __future__ import annotations

"""P3 固定离线业务链：筛选样本→结算→统计→治理→导出→重开。"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.alert_service import AlertDraft, AlertService
from stock_tool.core.cache import CacheStore
from stock_tool.core.models import FinancialMetrics, StockQuote
from stock_tool.core.performance_loop import ObservationSettlementService, PerformanceStore
from stock_tool.core.review_workflow import ReviewWorkflowService
from stock_tool.core.screener_engine import ScreenCandidate, ScreenReport
from stock_tool.core.screening_workflow import ScreeningWorkflowService
from stock_tool.core.strategy_store import StrategyStore
from stock_tool.core.technical_profile import TechnicalProfile
from stock_tool.core.v1_database import V1Database, v1_db_path_for
from tools.test_frontend_consistency import _spec
from tools.test_performance_loop import FakePriceSource, _bars


def _verified_report() -> ScreenReport:
    """Downstream P3 chain fixture: the upstream screener has already verified this row."""
    return ScreenReport(
        results=[
            ScreenCandidate(
                quote=StockQuote(
                    code="000001", name="样例银行", price=10.0, pe=8.0, pb=0.9,
                    market_cap=50_000_000_000, industry="银行", industry_strength=78.0,
                ),
                metrics=FinancialMetrics(
                    roe=18.0, gross_margin=35.0, net_margin=18.0,
                    debt_ratio=55.0, revenue_growth=10.0, profit_growth=12.0,
                ),
                technical=TechnicalProfile(True, status="candidate", opportunity_label="A 箱体横盘"),
                local_score=82.0,
                reasons=["ROE较好", "PE较温和"],
                data_status="verified",
            )
        ],
        scanned=1,
        skipped=0,
        failed=0,
        status="verified",
    )


def test_fixed_p3_chain() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
        root = Path(raw)
        database = V1Database(v1_db_path_for(root)); database.init_schema()
        performance = PerformanceStore(database)
        workflow = ScreeningWorkflowService(StrategyStore(database), performance)
        screened = workflow.record_report(
            _verified_report(), _spec(), source_label="P3 e2e", user_text="ROE大于15，PE小于40",
            data_source="fixture", asof_date="2026-06-01", execution_mode="review_only",
        )
        assert screened.observation_samples == 4
        formal_attempt = workflow.record_report(
            _verified_report(), _spec(), source_label="formal bypass regression", user_text="ROE大于15",
            data_source="fixture", asof_date="2026-06-01",
        )
        assert formal_attempt.report.status == "blocked"
        assert formal_attempt.observation_samples is None
        with database.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM observation_samples WHERE run_id = ?",
                (formal_attempt.run_id,),
            ).fetchone()[0] == 0
        cache = CacheStore(root / "cache.sqlite3")
        alerts = AlertService(cache)
        assert alerts.create_alert(AlertDraft("000001", "样例银行", "sell", 9.0, "P3 e2e"), "tester").ok
        review = ReviewWorkflowService(cache, performance)
        assert review.create_tracking_task(code="000001", name="样例银行", entry_price=10, fail_price=9, target_price=11, due_date="2026-06-10", actor="tester").ok
        settled = review.settle_due_samples(
            "2099-01-01",
            FakePriceSource(
                {"000001": _bars("2026-06-01", [10 + index * 0.12 for index in range(25)])},
                _bars("2026-06-01", [100 + index * 0.4 for index in range(25)]),
                {"银行": 2.0},
            ),
        )
        assert settled > 0
        assert review.performance_details({"code": "000001", "industry": "银行", "horizon_days": 3})
        active = performance.ensure_default_professional_formula()
        with database.connect() as conn:
            candidate_id = int(conn.execute(
                """INSERT INTO professional_formula_candidates(name,version,status,dimension_weights_json,field_weights_json,evidence_json,validation_json,created_at,updated_at,activated_at)
                VALUES('p3-e2e','v1','suggest_enable',?,?, '{}','{}','2026-07-01','2026-07-01','')""",
                (json.dumps(active["dimension_weights"]), json.dumps(active["field_weights"])),
            ).lastrowid)
        assert review.activate_candidate(candidate_id, "tester", "e2e evidence", confirmed=True).ok
        assert review.rollback_previous_active("tester", "e2e rollback", confirmed=True).ok
        export_path = root / "performance.csv"
        review.write_csv(export_path, review.export_performance_rows({"code": "000001"}))
        assert export_path.read_bytes().startswith(b"\xef\xbb\xbf")
        database.close()
        reopened = V1Database(v1_db_path_for(root)); reopened.init_schema()
        reopened_review = ReviewWorkflowService(CacheStore(root / "cache.sqlite3"), PerformanceStore(reopened))
        assert reopened_review.performance_details({"code": "000001"})
        assert {item["action"] for item in reopened_review.list_governance_audit()} >= {"activate", "rollback"}
        reopened.close()


if __name__ == "__main__":
    test_fixed_p3_chain()
    print("[OK] P3 固定端到端业务链通过")
