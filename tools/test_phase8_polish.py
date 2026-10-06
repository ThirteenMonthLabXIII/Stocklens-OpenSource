from __future__ import annotations

import csv
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.models import FinancialMetrics, StockQuote
from stock_tool.core.cache import CacheMetadata, FetchLogRecord
from stock_tool.core.query_schema import query_spec_from_dict
from stock_tool.core.screener_engine import ScreenCandidate, ScreenReport
from stock_tool.core.strategy_store import (
    RUN_COMPARISON_CSV_COLUMNS,
    StrategyStore,
    query_spec_diff_items,
    run_comparison_csv_rows,
    run_comparison_summary,
)
from stock_tool.core.technical_profile import TechnicalProfile
from stock_tool.core.v1_database import V1Database, V1_SCHEMA_VERSION
from stock_tool.ui import polish_helpers


def _spec(roe: float = 15, pe: float = 40, exclude: list[str] | None = None, sort_field: str = "local_score"):
    return query_spec_from_dict(
        {
            "intent": "screen",
            "conditions": [
                {"field": "roe", "op": ">=", "value": roe},
                {"field": "pe", "op": "<=", "value": pe},
            ],
            "exclude": exclude or ["ST"],
            "sort": [{"field": sort_field, "direction": "desc"}],
            "scan_limit": 300,
            "result_limit": 80,
            "allow_missing_pe": True,
            "with_technical": True,
            "needs_confirmation": False,
            "clarifications": [],
            "raw_text": f"找ROE高于{roe}，PE低于{pe}的股票",
        }
    )


def _candidate(code: str, name: str, seed: int, score: float) -> ScreenCandidate:
    return ScreenCandidate(
        quote=StockQuote(code=code, name=name, price=10 + seed, pe=8 + seed, pb=1.0, market_cap=100_000_000),
        metrics=FinancialMetrics(
            roe=15 + seed,
            gross_margin=30,
            net_margin=10,
            debt_ratio=40,
            revenue_growth=8,
            profit_growth=9,
        ),
        technical=TechnicalProfile(True, status="candidate", opportunity_label=f"候选{seed}", risk_flags=[], summary="phase8"),
        local_score=score,
        reasons=[f"{name}命中"],
    )


def _report(candidates: list[ScreenCandidate]) -> ScreenReport:
    return ScreenReport(results=candidates, scanned=300, skipped=300 - len(candidates), failed=0, errors=[])


def test_business_diff_and_version_compare() -> None:
    old = _spec(15, 40, ["ST"])
    new = _spec(18, 35, ["ST", "退市"], sort_field="roe")
    old_json = json.dumps(old, ensure_ascii=False, default=lambda value: value.__dict__)
    new_json = json.dumps(new, ensure_ascii=False, default=lambda value: value.__dict__)
    messages = [item.message for item in query_spec_diff_items(old_json, new_json)]
    assert "ROE >= 从 15% 改为 18%" in messages
    assert "PE <= 从 40倍 改为 35倍" in messages
    assert any(item.category == "排序变化" for item in query_spec_diff_items(old_json, new_json))
    assert any(item.category == "排除规则变化" for item in query_spec_diff_items(old_json, new_json))
    assert "策略 JSON 结构异常" in query_spec_diff_items("{bad", new_json)[0].message
    manual_old = {
        "conditions": [{"field": "roe", "op": ">=", "value": "15"}],
        "scan_limit": "300",
        "result_limit": 80,
        "allow_missing_pe": "false",
        "with_technical": "true",
    }
    manual_new = {
        "conditions": [{"field": "roe", "op": ">=", "value": 18}],
        "scan_limit": 300,
        "result_limit": "100",
        "allow_missing_pe": False,
        "with_technical": True,
    }
    manual_diff = query_spec_diff_items(
        json.dumps(manual_old, ensure_ascii=False),
        json.dumps(manual_new, ensure_ascii=False),
    )
    assert any(item.message == "ROE >= 从 15% 改为 18%" for item in manual_diff)
    assert any(item.message == "结果上限 从 80 改为 100" for item in manual_diff)
    assert not any("扫描上限" in item.message for item in manual_diff)
    assert not any("允许 PE 缺失" in item.message for item in manual_diff)
    assert not any("计算技术画像" in item.message for item in manual_diff)

    with tempfile.TemporaryDirectory() as tmp:
        assert V1_SCHEMA_VERSION == 16
        database = V1Database(Path(tmp) / "stock_tool_v1.sqlite3")
        store = StrategyStore(database)
        preset = store.save_preset("版本差异样例", old.raw_text, old)
        v1 = store.list_preset_versions(preset.id)[0]
        assert store.compare_preset_versions(v1.id, "previous")[0].message == "无上一版本可对比"
        store.save_preset("版本差异样例", new.raw_text, new)
        v2 = store.list_preset_versions(preset.id)[0]
        diff = store.compare_preset_versions(v2.id, "previous")
        assert any(item.message == "ROE >= 从 15% 改为 18%" for item in diff)
        database.close()


def test_run_comparison_filters_and_csv() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database = V1Database(Path(tmp) / "stock_tool_v1.sqlite3")
        store = StrategyStore(database)
        spec = _spec()
        preset = store.save_preset("复盘筛选样例", spec.raw_text, spec)
        run1 = store.record_run(
            "已保存策略",
            spec.raw_text,
            spec,
            "v1",
            _report([_candidate("000001", "样例A", 1, 90), _candidate("000002", "样例B", 2, 80)]),
            preset_id=preset.id,
            preset_name=preset.name,
        )
        run2 = store.record_run(
            "已保存策略",
            spec.raw_text,
            spec,
            "v1",
            _report([_candidate("000002", "样例B", 4, 86.5), _candidate("000003", "样例C", 3, 70)]),
            preset_id=preset.id,
            preset_name=preset.name,
        )
        comparison = store.compare_runs(run2, run1)
        summary = run_comparison_summary(comparison)
        assert (summary.added_count, summary.removed_count, summary.rank_changed_count, summary.metric_changed_count) == (1, 1, 1, 1)
        assert [item.code for item in polish_helpers.comparison_items_for_filter(comparison, "新增")] == ["000003"]
        assert [item.code for item in polish_helpers.comparison_items_for_filter(comparison, "消失")] == ["000001"]
        assert [item.code for item in polish_helpers.comparison_items_for_filter(comparison, "排名变化")] == ["000002"]
        rows = run_comparison_csv_rows(comparison)
        assert list(rows[0].keys()) == RUN_COMPARISON_CSV_COLUMNS
        kept = next(row for row in rows if row["代码"] == "000002")
        assert kept["变化类型"] == "排名变化、指标变化"
        assert kept["排名方向"] == "上升"
        csv_path = Path(tmp) / "对比.csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=RUN_COMPARISON_CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        text = csv_path.read_text(encoding="utf-8-sig")
        assert "变化类型" in text and "排名方向" in text
        database.close()


def test_acceptance_report_summaries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp)
        for index in range(12):
            payload = {
                "finished_at": f"2026-06-03T12:{index:02d}:00",
                "conclusion": "不可发版" if index == 11 else "可发版",
                "live_enabled": index % 2 == 0,
                "elapsed_seconds": index + 0.5,
                "txt_report": str(report_dir / f"report_{index}.txt"),
                "blocks": [{"name": "第八阶段专项", "status": "FAIL" if index == 11 else "PASS", "elapsed_seconds": 1.0}],
            }
            (report_dir / f"report_{index}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        summaries = polish_helpers.load_acceptance_report_summaries(report_dir, limit=10)
        assert len(summaries) == 10
        assert summaries[0].time_text == "2026-06-03T12:11:00"
        assert summaries[0].conclusion == "不可发版"
        assert summaries[0].failed_blocks == ["第八阶段专项"]
        assert summaries[1].live_enabled is True

        broken_dir = Path(tmp) / "broken"
        broken_dir.mkdir()
        (broken_dir / "bad.json").write_text("{bad", encoding="utf-8")
        broken = polish_helpers.load_acceptance_report_summaries(broken_dir, limit=10)
        assert broken[0].conclusion == "JSON损坏"
        assert broken[0].error


def test_data_source_health_helpers() -> None:
    now = datetime(2026, 6, 4, 12, 0, 0)
    logs = [
        FetchLogRecord(1, "全市场行情", "", "cache", "cache", "实时源失败，使用旧行情缓存", 10, now - timedelta(minutes=5)),
        FetchLogRecord(2, "全市场行情", "", "sina_http", "failed", "新浪接口超时", 1000, now - timedelta(minutes=6)),
        FetchLogRecord(3, "全市场行情", "", "eastmoney_http", "success", "获取 5000 条行情", 800, now - timedelta(hours=1)),
        FetchLogRecord(5, "全市场行情", "", "akshare", "failed", "较早失败", 1500, now - timedelta(hours=3)),
        FetchLogRecord(4, "财务指标", "000001", "akshare", "success", "报告期 2026Q1", 900, now - timedelta(hours=2)),
    ]
    cache_items = [
        CacheMetadata("spot_quotes", now - timedelta(hours=6)),
        CacheMetadata("financial:000001", now - timedelta(days=2)),
    ]
    rows = polish_helpers.build_data_source_health_rows(logs, cache_items, now=now)
    market = next(row for row in rows if row.action == "全市场行情")
    assert market.direct_success_rate == "33.3%"
    assert market.latest_status == "缓存"
    assert market.cache_fallback == "1次"
    assert market.cache_age == "6小时前（1条）"
    assert market.last_failure == "新浪接口超时"
    assert market.level == "cache"
    assert "缓存 2 条；最新 6小时前" in polish_helpers.cache_age_overview(cache_items, now=now)
    assert polish_helpers.format_relative_age(now - timedelta(seconds=20), now=now) == "刚刚"


def test_query_help_and_clarification_templates() -> None:
    rows = polish_helpers.supported_query_help_rows()
    by_field = {row.field: row for row in rows}
    assert "roe" in by_field
    assert ">=" in by_field["roe"].operators
    assert "PE <= 40" in by_field["pe"].example
    assert "sort" in by_field
    template = polish_helpers.clarification_template_for_text("找估值低、趋势好的股票")
    assert "PE <=" in template
    assert "近20日趋势" in template
    fallback = polish_helpers.clarification_template_for_text("")
    assert "PE" in fallback and "<=" in fallback


def test_export_filename_helper() -> None:
    assert polish_helpers.clean_export_filename('低估:高ROE*策略?.json') == "低估_高ROE_策略_.json"


def run_tests() -> None:
    test_business_diff_and_version_compare()
    test_run_comparison_filters_and_csv()
    test_acceptance_report_summaries()
    test_data_source_health_helpers()
    test_query_help_and_clarification_templates()
    test_export_filename_helper()
    print("[OK] 第八阶段 Polish 专项测试通过")


if __name__ == "__main__":
    run_tests()
