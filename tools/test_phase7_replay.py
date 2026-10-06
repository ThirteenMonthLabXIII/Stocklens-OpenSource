from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.factor_store import FactorSnapshot, FactorStoreStats, V1FactorStore, calculate_data_health_status
from stock_tool.core.models import FinancialMetrics, StockQuote
from stock_tool.core.query_schema import query_spec_from_dict
from stock_tool.core.screener_engine import ScreenCandidate, ScreenReport
from stock_tool.core.strategy_store import (
    RUN_COMPARISON_CSV_COLUMNS,
    StrategyStore,
    format_metric_changes,
    run_comparison_csv_rows,
)
from stock_tool.core.technical_profile import TechnicalProfile
from stock_tool.core.v1_database import V1Database, V1_SCHEMA_VERSION


def _spec(roe: float = 15, pe: float = 40):
    return query_spec_from_dict(
        {
            "intent": "screen",
            "conditions": [
                {"field": "roe", "op": ">=", "value": roe},
                {"field": "pe", "op": "<=", "value": pe},
            ],
            "exclude": ["ST"],
            "sort": [{"field": "local_score", "direction": "desc"}],
            "scan_limit": 300,
            "result_limit": 80,
            "allow_missing_pe": True,
            "with_technical": True,
            "needs_confirmation": False,
            "clarifications": [],
            "raw_text": f"找ROE高于{roe}，PE低于{pe}的股票",
        }
    )


def _candidate(code: str, name: str, rank_seed: int, score: float) -> ScreenCandidate:
    return ScreenCandidate(
        quote=StockQuote(
            code=code,
            name=name,
            price=10 + rank_seed,
            pe=8 + rank_seed,
            pb=1.0 + rank_seed * 0.1,
            market_cap=100_000_000_000 + rank_seed,
        ),
        metrics=FinancialMetrics(
            roe=15 + rank_seed,
            gross_margin=30 + rank_seed,
            net_margin=10 + rank_seed,
            debt_ratio=40 + rank_seed,
            revenue_growth=8 + rank_seed,
            profit_growth=9 + rank_seed,
        ),
        technical=TechnicalProfile(
            True,
            status="candidate",
            opportunity_label=f"A 回踩候选{rank_seed}",
            risk_flags=[],
            summary="phase7",
        ),
        local_score=score,
        reasons=[f"{name}命中"],
    )


def _report(candidates: list[ScreenCandidate]) -> ScreenReport:
    return ScreenReport(results=candidates, scanned=300, skipped=300 - len(candidates), failed=0, errors=[])


def _create_v2_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            CREATE TABLE strategy_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                user_text TEXT NOT NULL,
                query_spec_json TEXT NOT NULL,
                summary TEXT NOT NULL,
                parse_source TEXT NOT NULL DEFAULT '',
                ai_repair_retry INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_run_at TEXT NOT NULL DEFAULT '',
                run_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO strategy_presets(
                name, user_text, query_spec_json, summary, parse_source,
                ai_repair_retry, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                "迁移样例",
                "旧策略",
                json.dumps(_spec().__dict__, ensure_ascii=False, default=lambda value: value.__dict__),
                "旧策略摘要",
                "v2",
                "2026-06-01T00:00:00",
                "2026-06-01T00:00:00",
            ),
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()


def run_tests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "stock_tool_v1.sqlite3"
        _create_v2_database(db_path)
        db = V1Database(db_path)
        store = StrategyStore(db)
        with closing(sqlite3.connect(db_path)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == V1_SCHEMA_VERSION == 16
        migrated = store.list_presets()[0]
        migrated_versions = store.list_preset_versions(migrated.id)
        assert len(migrated_versions) == 1
        assert migrated_versions[0].version_no == 1
        db.close()

    with tempfile.TemporaryDirectory() as tmp:
        db = V1Database(Path(tmp) / "stock_tool_v1.sqlite3")
        store = StrategyStore(db)
        spec1 = _spec(15, 40)
        preset = store.save_preset("低估高ROE", spec1.raw_text, spec1, parse_source="本地解析")
        spec2 = _spec(18, 35)
        store.save_preset("低估高ROE", spec2.raw_text, spec2, parse_source="本地解析", change_note="提高ROE并收紧PE")
        spec3 = _spec(20, 35)
        store.save_preset("低估高ROE", spec3.raw_text, spec3, parse_source="AI解析")
        versions = store.list_preset_versions(preset.id)
        assert [item.version_no for item in versions] == [3, 2, 1]
        assert versions[1].change_note == "提高ROE并收紧PE"

        version1 = versions[-1]
        loaded_version, loaded_spec = store.load_preset_version_spec(version1.id)
        assert loaded_version.version_no == 1
        assert loaded_spec.conditions[0].value == 15
        assert store.get_preset(preset.id).summary != loaded_version.summary
        restored = store.restore_preset_version(version1.id, "验收恢复到初版")
        assert restored.id == preset.id
        restored_versions = store.list_preset_versions(preset.id)
        assert [item.version_no for item in restored_versions][:2] == [4, 3]
        assert restored_versions[0].change_note == "验收恢复到初版"

        run1 = store.record_run(
            "已保存策略",
            spec1.raw_text,
            spec1,
            "v1",
            _report([_candidate("000001", "样例A", 1, 90.0), _candidate("000002", "样例B", 2, 80.0)]),
            preset_id=preset.id,
            preset_name=preset.name,
            summary="第一次执行",
        )
        run2 = store.record_run(
            "已保存策略",
            spec1.raw_text,
            spec1,
            "v1",
            _report([_candidate("000002", "样例B", 4, 86.5), _candidate("000003", "样例C", 3, 70.0)]),
            preset_id=preset.id,
            preset_name=preset.name,
            summary="第二次执行",
        )
        with db.connect() as conn:
            conn.execute("UPDATE query_runs SET created_at = ? WHERE id = ?", ("2026-06-03T16:43:00", run2))
        comparison = store.compare_runs(run2)
        assert comparison.previous_run.id == run1
        assert [item.code for item in comparison.added] == ["000003"]
        assert [item.code for item in comparison.removed] == ["000001"]
        assert comparison.kept[0].code == "000002"
        assert comparison.kept[0].rank_delta == 1
        assert comparison.kept[0].score_delta == 6.5
        assert "roe" in comparison.kept[0].metric_changes
        rows = run_comparison_csv_rows(comparison)
        assert list(rows[0].keys()) == RUN_COMPARISON_CSV_COLUMNS
        assert rows[0]["状态"] == "新增"
        kept_metric_text = format_metric_changes(comparison.kept[0].metric_changes)
        assert "ROE" in kept_metric_text
        assert "roe:" not in kept_metric_text
        assert "技术标签" in kept_metric_text

        found_by_code = store.search_runs(keyword="000003", preset_id=preset.id)
        assert [item.id for item in found_by_code] == [run2]
        found_by_source = store.search_runs(data_source="v1", min_matched=2)
        assert {item.id for item in found_by_source} == {run1, run2}
        found_by_summary = store.search_runs(keyword="第一次")
        assert [item.id for item in found_by_summary] == [run1]
        found_by_end_date = store.search_runs(keyword="第二次", end_time="2026-06-03")
        assert [item.id for item in found_by_end_date] == [run2]

        store.delete_preset(preset.id)
        assert store.get_preset(preset.id) is None
        assert store.get_run_detail(run1).run.preset_name == "低估高ROE"
        archived_versions = store.list_archived_preset_versions()
        assert any(item.id == version1.id for item in archived_versions)
        recreated = store.save_preset("低估高ROE", "同名重建", _spec(30, 30), parse_source="recreate")
        restored_again = store.restore_preset_version(version1.id, "同名重建后恢复旧版本")
        assert restored_again.id == recreated.id
        _, restored_spec = store.load_query_spec(restored_again.id)
        assert restored_spec.conditions[0].value == 15

        csv_path = Path(tmp) / "compare.csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=RUN_COMPARISON_CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        assert "新增" in csv_path.read_text(encoding="utf-8-sig")
        db.close()

    now = datetime(2026, 6, 3, 12, 0, 0)
    good = calculate_data_health_status(
        FactorStoreStats(100, (now - timedelta(hours=2)).isoformat(timespec="seconds"), 5, 5),
        now=now,
    )
    stale = calculate_data_health_status(
        FactorStoreStats(100, (now - timedelta(hours=80)).isoformat(timespec="seconds"), 5, 5),
        now=now,
    )
    missing = calculate_data_health_status(
        FactorStoreStats(100, (now - timedelta(hours=2)).isoformat(timespec="seconds"), 80, 31),
        now=now,
    )
    unavailable = calculate_data_health_status(None, database_available=False, error_message="locked", now=now)
    assert good.title == "良好"
    assert stale.title == "可用但过旧"
    assert missing.title == "缺失较多"
    assert unavailable.title == "不可用"

    with tempfile.TemporaryDirectory() as tmp:
        db = V1Database(Path(tmp) / "stock_tool_v1.sqlite3")
        factor_store = V1FactorStore(db)
        factor_store.upsert_snapshot(
            FactorSnapshot(code="000001", name="缺字段样例", updated_at=now.isoformat(timespec="seconds"), missing_fields="")
        )
        actual_stats = factor_store.stats()
        actual_health = calculate_data_health_status(actual_stats, now=now)
        assert actual_stats.missing_snapshot_count == 1
        assert actual_health.title == "缺失较多"
        db.close()

    with tempfile.TemporaryDirectory() as tmp:
        json_report = Path(tmp) / "soak_fail.json"
        completed = subprocess.run(
            [
                sys.executable,
                "tools/test_soak_runner.py",
                "--cycles",
                "1",
                "--snapshot-count",
                "999",
                "--json-report",
                str(json_report),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        assert completed.returncode != 0
        payload = json.loads(json_report.read_text(encoding="utf-8"))
        assert payload["status"] == "FAIL"
        assert payload["failures"] >= 1
        assert payload["elapsed_seconds"] > 0
        assert payload["peak_memory_kb"] > 0

    print("[OK] 第七阶段复盘中心专项测试通过")


if __name__ == "__main__":
    run_tests()
