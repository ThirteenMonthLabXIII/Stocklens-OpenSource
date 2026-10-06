from __future__ import annotations

import tempfile
import sys
import sqlite3
import gc
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.factor_store import FACTOR_SNAPSHOT_MAX_AGE, FactorSnapshot, V1FactorStore
from stock_tool.core.factor_store import update_factor_snapshots
from stock_tool.core.models import FinancialMetrics, StockQuote
from stock_tool.core.screener_engine import ScreenFilter, screen_factor_snapshots
from stock_tool.core.v1_database import V1Database, v1_db_path_for


class FakeFetcher:
    def spot_quotes(self):
        return [
            StockQuote(code="000010", name="更新成功", price=12, market_cap=90_000_000_000, pe=18, pb=2.1),
            StockQuote(code="000011", name="更新失败", price=8, market_cap=80_000_000_000, pe=16, pb=1.8),
            StockQuote(code="000012", name="ST跳过", price=5, market_cap=70_000_000_000, pe=20, pb=1.2),
        ], None

    def financial_metrics(self, code: str):
        if code == "000011":
            raise RuntimeError("财务接口失败")
        return FinancialMetrics(roe=16, net_margin=11, debt_ratio=50, revenue_growth=18, profit_growth=20), None

    def history(self, code: str, days: int = 180):
        import pandas as pd

        rows = []
        for idx in range(80):
            close = 10 + idx * 0.05
            rows.append({"收盘": close, "最高": close + 0.2, "最低": close - 0.2, "开盘": close - 0.05, "成交量": 1000 + idx})
        return pd.DataFrame(rows), None


class FailingSpotFetcher:
    def spot_quotes(self):
        raise RuntimeError("行情源不可用")


def run_tests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        legacy_path = data_dir / "legacy_v1.sqlite3"
        conn = sqlite3.connect(legacy_path)
        try:
            conn.execute("CREATE TABLE factor_snapshots (code TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO factor_snapshots(code, name, updated_at) VALUES('000099', '旧库样例', '2026-06-01T10:00:00')"
            )
            conn.commit()
        finally:
            conn.close()
        legacy_db = V1Database(legacy_path)
        legacy_store = V1FactorStore(legacy_db)
        legacy_store.upsert_snapshot(FactorSnapshot(code="000099", name="旧库样例", price=9.9, updated_at="2026-06-04T10:00:00"))
        legacy_row = legacy_store.list_latest_snapshots()[0]
        assert legacy_row.price == 9.9
        assert legacy_row.missing_fields == ""
        assert legacy_row.to_technical().available is False, "异常/旧版技术状态不应被当作可用画像"
        assert legacy_row.box_score is None
        assert legacy_row.industry == ""
        legacy_db.close()
        del legacy_row, legacy_store, legacy_db
        gc.collect()

        legacy_strategy_path = data_dir / "legacy_strategy.sqlite3"
        conn = sqlite3.connect(legacy_strategy_path)
        try:
            conn.execute(
                """
                CREATE TABLE strategy_presets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    user_text TEXT NOT NULL,
                    query_spec_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO strategy_presets(name, user_text, query_spec_json, summary, created_at, updated_at) VALUES('旧策略', 'roe>10', '{}', '旧', '2026-01-01', '2026-01-01')"
            )
            conn.execute(
                "CREATE TABLE query_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, source_label TEXT NOT NULL, user_text TEXT NOT NULL, query_spec_json TEXT NOT NULL, summary TEXT NOT NULL, data_source TEXT NOT NULL, scanned INTEGER NOT NULL, matched INTEGER NOT NULL, skipped INTEGER NOT NULL, failed INTEGER NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()
        migrated_strategy_db = V1Database(legacy_strategy_path)
        with migrated_strategy_db.connect() as conn:
            preset_cols = {row["name"] for row in conn.execute("PRAGMA table_info(strategy_presets)").fetchall()}
            run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(query_runs)").fetchall()}
        assert {"parse_source", "ai_repair_retry", "last_run_at", "run_count"} <= preset_cols
        assert {"preset_id", "preset_name", "note"} <= run_cols
        migrated_strategy_db.close()

        db = V1Database(v1_db_path_for(data_dir))
        store = V1FactorStore(db)
        assert v1_db_path_for(data_dir).exists(), "V1 SQLite 应创建成功"

        snapshot = FactorSnapshot(
            code="000001",
            name="样例银行",
            price=10.5,
            pe=8,
            pb=0.9,
            market_cap_yi=500,
            roe=18,
            gross_margin=35,
            net_margin=12,
            debt_ratio=55,
            revenue_growth=22,
            profit_growth=25,
            trend_20d=4.2,
            trend_60d=8.3,
            box_score=82,
            box_upper=11.2,
            box_lower=9.6,
            box_range_pct=16.67,
            box_position_pct=56.25,
            box_position_label="箱体中部",
            box_days=60,
            box_ma20_slope_pct=1.2,
            box_boundary_touch_count=4,
            box_is_sideways=True,
            box_summary="箱体评分82，箱体9.60-11.20，位置箱体中部",
            industry="银行",
            industry_strength=78,
            industry_rank=3,
            industry_sample_count=18,
            industry_updated_at="2026-06-01",
            technical_label="A 回踩候选",
            technical_status="candidate",
            technical_summary="样例技术画像",
            data_date="2026-06-01",
            quote_updated_at="2026-06-01T09:30:00",
            financial_updated_at="2026-06-01T09:31:00",
            history_updated_at="2026-06-01T09:32:00",
            updated_at="2026-06-01T09:33:00",
            missing_fields="",
        )
        store.upsert_snapshot(snapshot)
        rows = store.list_latest_snapshots()
        assert len(rows) == 1, f"upsert 后应该只有1条，实际 {len(rows)}"
        assert rows[0].code == "000001"
        assert rows[0].roe == 18
        assert rows[0].to_quote().industry == "银行"
        assert rows[0].to_technical().box_score == 82
        assert len(store.list_latest_snapshots(max_age=FACTOR_SNAPSHOT_MAX_AGE)) == 0

        snapshot.price = 10.8
        store.upsert_snapshot(snapshot)
        rows = store.list_latest_snapshots()
        assert len(rows) == 1, "二次 upsert 不应重复插入"
        assert rows[0].price == 10.8, "二次 upsert 应更新原快照"
        assert store.latest_updated_at() == "2026-06-01T09:33:00"

        missing = FactorSnapshot(
            code="000002",
            name="样例科技",
            price=20,
            pe=None,
            market_cap_yi=800,
            roe=10,
            debt_ratio=40,
            revenue_growth=30,
            profit_growth=35,
            industry="地产",
            industry_strength=45,
            industry_rank=20,
            industry_sample_count=30,
            industry_updated_at="2026-06-01",
            technical_label="技术画像缺失",
            technical_status="missing",
            updated_at="2026-06-01T10:00:00",
            missing_fields="pe,net_margin,trend_60d",
        )
        store.upsert_snapshot(missing)
        stats = store.stats()
        assert stats.snapshot_count == 2
        assert stats.latest_updated_at == "2026-06-01T10:00:00"
        assert stats.missing_field_count == 12, "新增 PE-TTM/箱体字段后缺失计数应覆盖兼容样本和技术画像缺失"
        assert stats.missing_snapshot_count == 2, "未提供 PE-TTM 的兼容样本都应标记为存在缺失"

        filters = ScreenFilter(
            min_roe=15,
            max_debt_ratio=60,
            max_pe=40,
            min_market_cap_yi=100,
            max_market_cap_yi=1000,
            with_technical=False,
        )
        report = screen_factor_snapshots(store.list_latest_snapshots(), filters)
        assert report.scanned == 2
        assert len(report.results) == 1
        assert report.results[0].quote.code == "000001"

        expanded_filters = ScreenFilter(
            min_roe=8,
            max_debt_ratio=60,
            max_pe=40,
            max_pb=2,
            min_gross_margin=30,
            min_trend_20d=0,
            min_market_cap_yi=100,
            max_market_cap_yi=1000,
            with_technical=False,
        )
        report = screen_factor_snapshots(store.list_latest_snapshots(), expanded_filters)
        assert len(report.results) == 1, "V1 快照筛选应支持 pb、gross_margin、trend_20d"
        assert report.results[0].quote.code == "000001"

        missing_trend_filters = ScreenFilter(
            min_roe=8,
            max_debt_ratio=60,
            min_trend_20d=0,
            allow_missing_pe=True,
            with_technical=False,
        )
        report = screen_factor_snapshots(store.list_latest_snapshots(), missing_trend_filters)
        assert len(report.results) == 1, "缺失 trend_20d 时不应误当 0 通过"
        assert report.results[0].quote.code == "000001"

        missing_pe_filters = ScreenFilter(
            min_roe=8,
            max_debt_ratio=60,
            max_pe=40,
            min_market_cap_yi=100,
            max_market_cap_yi=1000,
            allow_missing_pe=True,
            with_technical=False,
        )
        report = screen_factor_snapshots(store.list_latest_snapshots(), missing_pe_filters)
        assert len(report.results) == 2, "允许 PE 缺失时应保留缺 PE 快照"

        box_industry_filters = ScreenFilter(
            min_roe=8,
            max_debt_ratio=60,
            max_pe=40,
            require_box_sideways=True,
            min_box_score=60,
            max_box_range_pct=25,
            min_industry_strength=60,
            include_industries=["银行"],
            exclude_industries=["地产"],
            with_technical=True,
        )
        report = screen_factor_snapshots(store.list_latest_snapshots(), box_industry_filters)
        assert len(report.results) == 1, "V1 快照应支持箱体 + 行业强弱过滤"
        assert report.results[0].quote.code == "000001"
        assert report.results[0].sort_values["box_score"] == 82
        assert report.results[0].sort_values["industry_strength"] == 78

        unavailable_industry_report = screen_factor_snapshots(
            [FactorSnapshot(code="000003", name="无行业强弱", roe=20, debt_ratio=20, pe=10, updated_at="2026-06-01")],
            ScreenFilter(min_roe=8, max_debt_ratio=60, max_pe=40, min_industry_strength=60, with_technical=False),
        )
        assert unavailable_industry_report.errors
        assert "行业强弱数据不可用" in unavailable_industry_report.errors[0]

        strict_pe_filters = ScreenFilter(
            min_roe=8,
            max_debt_ratio=60,
            max_pe=40,
            min_market_cap_yi=100,
            max_market_cap_yi=1000,
            allow_missing_pe=False,
            with_technical=False,
        )
        report = screen_factor_snapshots(store.list_latest_snapshots(), strict_pe_filters)
        assert len(report.results) == 1, "不允许 PE 缺失时应过滤缺 PE 快照"

        old_failed_snapshot = FactorSnapshot(
            code="000011",
            name="更新失败",
            price=7.5,
            pe=15,
            market_cap_yi=700,
            roe=19,
            debt_ratio=45,
            technical_label="A 回踩候选",
            technical_status="candidate",
            updated_at="2026-06-01T08:00:00",
        )
        store.upsert_snapshot(old_failed_snapshot)
        result = update_factor_snapshots(FakeFetcher(), store, limit=3)
        assert result.total == 2, "批量更新应跳过 ST 股票"
        assert result.success == 1
        assert result.failed == 1
        assert result.data_snapshot_id, "成功刷新必须生成不可变数据快照"
        assert result.refresh_event_id, "每次刷新必须有独立 refresh_event_id"
        with db.connect() as conn:
            refresh_row = conn.execute(
                "SELECT status, snapshot_id FROM snapshot_ingestion_events WHERE refresh_event_id = ?",
                (result.refresh_event_id,),
            ).fetchone()
        assert refresh_row["status"] == "partial"
        assert refresh_row["snapshot_id"] == result.data_snapshot_id
        manifest = store.latest_data_manifest()
        assert manifest is not None
        assert manifest.snapshot_id == result.data_snapshot_id
        manifest_snapshots = store.list_snapshots_for_manifest(manifest.snapshot_id)
        assert [item.code for item in manifest_snapshots] == ["000010"]
        assert manifest.quality_status == "partial"
        failed_row = [item for item in store.list_latest_snapshots() if item.code == "000011"][0]
        assert failed_row.updated_at == "2026-06-01T08:00:00", "单股失败不应覆盖旧快照"
        success_row = [item for item in store.list_latest_snapshots() if item.code == "000010"][0]
        assert success_row.roe == 16
        assert success_row.technical_label != "技术画像缺失"

        before_count = store.stats().snapshot_count
        batch_failure = update_factor_snapshots(FailingSpotFetcher(), store, limit=3)
        assert batch_failure.total == 0
        assert batch_failure.success == 0
        assert batch_failure.failed == 1
        assert "全市场行情获取失败" in batch_failure.errors[0]
        assert store.stats().snapshot_count == before_count, "批次失败不应清空旧快照"
        assert batch_failure.refresh_event_id
        with db.connect() as conn:
            failed_refresh = conn.execute(
                "SELECT status, snapshot_id FROM snapshot_ingestion_events WHERE refresh_event_id = ?",
                (batch_failure.refresh_event_id,),
            ).fetchone()
        assert failed_refresh["status"] == "failed"
        assert failed_refresh["snapshot_id"] is None

        fresh_db = V1Database(data_dir / "fresh_filter.sqlite3")
        fresh_store = V1FactorStore(fresh_db)
        fresh_store.upsert_snapshots(
            [
                FactorSnapshot(code="000101", name="旧快照", market_cap_yi=900, updated_at="2026-01-01T09:00:00"),
                FactorSnapshot(
                    code="000102",
                    name="新快照",
                    market_cap_yi=800,
                    technical_label="A 回踩候选",
                    technical_status="candidate",
                    updated_at=datetime.now().isoformat(timespec="seconds"),
                ),
            ]
        )
        fresh_rows = fresh_store.list_latest_snapshots(max_age=FACTOR_SNAPSHOT_MAX_AGE)
        assert [row.code for row in fresh_rows] == ["000102"], "新鲜度过滤应只返回72小时内快照"
        fresh_db.close()
        db.close()

    print("[OK] V1 factor store 离线测试通过")


if __name__ == "__main__":
    run_tests()
