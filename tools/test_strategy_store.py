from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.factor_store import FactorSnapshot, V1FactorStore
from stock_tool.core.query_schema import QueryValidationError, query_spec_from_dict, query_spec_to_screen_filter
from stock_tool.core.screener_engine import ScreenFilter, screen_factor_snapshots
from stock_tool.core.strategy_store import CSV_RESULT_COLUMNS, StrategyStore, run_results_csv_rows
from stock_tool.core.v1_database import V1Database, V1_SCHEMA_VERSION, v1_db_path_for


def _sample_spec():
    return query_spec_from_dict(
        {
            "intent": "screen",
            "conditions": [
                {"field": "roe", "op": ">=", "value": 15},
                {"field": "pe", "op": "<=", "value": 40},
            ],
            "exclude": ["ST"],
            "scan_limit": 300,
            "result_limit": 80,
            "allow_missing_pe": True,
            "with_technical": False,
            "needs_confirmation": False,
            "clarifications": [],
            "raw_text": "找ROE高于15，PE低于40的股票",
        }
    )


def run_tests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        db_path = v1_db_path_for(data_dir)
        db = V1Database(db_path)
        store = StrategyStore(db)

        with db.connect() as conn:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert str(journal_mode).lower() == "wal"
        assert int(synchronous) == 1  # NORMAL
        assert int(busy_timeout) == 5000

        with closing(sqlite3.connect(db_path)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert version == V1_SCHEMA_VERSION == 16
        assert {
            "factor_snapshots",
            "strategy_presets",
            "strategy_preset_versions",
            "query_runs",
            "query_run_results",
        }.issubset(tables)

        factor_store = V1FactorStore(db)
        factor_store.upsert_snapshot(
            FactorSnapshot(
                code="000001",
                name="样例银行",
                price=10,
                pe=8,
                pb=0.9,
                market_cap_yi=500,
                roe=18,
                debt_ratio=55,
                box_score=82,
                box_upper=11.2,
                box_lower=9.6,
                box_range_pct=16.67,
                box_position_label="箱体中部",
                box_summary="箱体评分82，箱体9.60-11.20，位置箱体中部",
                industry="银行",
                industry_strength=78,
                industry_rank=3,
                industry_sample_count=18,
                industry_updated_at="2026-06-01",
                technical_label="A 回踩候选",
                technical_status="candidate",
                updated_at="2026-06-01T10:00:00",
            )
        )
        assert len(factor_store.list_latest_snapshots()) == 1, "新增策略表不应破坏 factor_snapshots"

        spec = _sample_spec()
        preset = store.save_preset(
            "低估高ROE",
            spec.raw_text,
            spec,
            parse_source="本地解析",
            ai_repair_retry=False,
        )
        assert preset.id > 0
        assert preset.run_count == 0
        assert len(store.list_presets()) == 1

        overwritten = store.save_preset(
            "低估高ROE",
            "覆盖后的原文",
            spec,
            parse_source="AI解析",
            ai_repair_retry=True,
        )
        assert overwritten.id == preset.id, "同名保存应覆盖原策略"
        assert overwritten.parse_source == "AI解析"
        assert overwritten.ai_repair_retry is True
        assert len(store.list_presets()) == 1
        assert [item.version_no for item in store.list_preset_versions(preset.id)] == [2, 1]

        loaded_preset, loaded_spec = store.load_query_spec(preset.id)
        assert loaded_preset.name == "低估高ROE"
        filters = query_spec_to_screen_filter(loaded_spec)
        assert filters.min_roe == 15
        assert filters.max_pe == 40

        report = screen_factor_snapshots(
            factor_store.list_latest_snapshots(),
            ScreenFilter(min_roe=15, max_pe=40, with_technical=True),
        )
        run_id = store.record_run(
            source_label="已保存策略",
            user_text=loaded_spec.raw_text,
            spec=loaded_spec,
            data_source="v1",
            report=report,
            preset_id=preset.id,
            preset_name="低估高ROE",
        )
        assert run_id > 0
        recent = store.list_recent_runs(preset.id)
        assert len(recent) == 1
        assert recent[0].matched == 1
        assert recent[0].data_source == "v1"
        assert store.get_preset(preset.id).run_count == 1

        with closing(sqlite3.connect(db_path)) as conn:
            row_count = conn.execute("SELECT COUNT(*) FROM query_run_results WHERE run_id = ?", (run_id,)).fetchone()[0]
        assert row_count == 1
        detail = store.get_run_detail(run_id)
        result = detail.results[0]
        assert result.metrics["industry"] == "银行"
        assert result.metrics["box_score"] == 82
        assert result.metrics["industry_strength"] == 78
        csv_rows = run_results_csv_rows(detail.results)
        assert {"行业", "箱体评分", "箱体区间", "箱体位置", "行业强度"} <= set(CSV_RESULT_COLUMNS)
        assert csv_rows[0]["行业"] == "银行"
        assert csv_rows[0]["箱体评分"] == 82
        assert csv_rows[0]["箱体区间"].startswith("9.6-11.2")
        assert csv_rows[0]["箱体位置"] == "箱体中部"
        assert csv_rows[0]["行业强度"] == 78
        assert {"观察等级", "候选类型", "观察状态", "数据质量", "压档原因", "证据缺口"} <= set(CSV_RESULT_COLUMNS)
        assert csv_rows[0]["候选类型"]
        assert csv_rows[0]["观察等级"] in {"S", "A", "B", "C", "D"}
        assert csv_rows[0]["观察状态"] in {"优先观察", "普通观察", "数据待补", "风险回避"}
        assert csv_rows[0]["数据质量"] != ""

        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO strategy_presets(
                    name, user_text, query_spec_json, summary, parse_source,
                    ai_repair_retry, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, '', 0, '2026-06-01T00:00:00', '2026-06-01T00:00:00')
                """,
                (
                    "过期字段",
                    "旧策略",
                    json.dumps({"conditions": [{"field": "dividend_yield", "op": ">=", "value": 3}]}),
                    "旧策略",
                ),
            )
            invalid_id = conn.execute("SELECT id FROM strategy_presets WHERE name = '过期字段'").fetchone()["id"]
        try:
            store.load_query_spec(invalid_id)
        except QueryValidationError as exc:
            assert "策略已过期或当前版本不支持" in str(exc)
        else:
            raise AssertionError("无效旧策略应被拒绝")

        store.delete_preset(preset.id)
        assert store.get_preset(preset.id) is None
        assert len(store.list_recent_runs(preset.id)) == 1, "删除策略不应删除历史执行记录"
        db.close()

    print("[OK] V1 策略存储离线测试通过")


if __name__ == "__main__":
    run_tests()
