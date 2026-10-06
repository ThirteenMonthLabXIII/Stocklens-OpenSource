from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.factor_store import FactorSnapshot, V1FactorStore
from stock_tool.core.query_schema import query_spec_from_dict, query_spec_to_screen_filter
from stock_tool.core.screener_engine import screen_factor_snapshots
from stock_tool.core.strategy_store import StrategyStore
from stock_tool.core.v1_database import V1Database


def _snapshots(count: int) -> list[FactorSnapshot]:
    rows = []
    for index in range(1, count + 1):
        code = f"{index:06d}"
        rows.append(
            FactorSnapshot(
                code=code,
                name=f"长稳样例{code}",
                price=10 + index % 20,
                pe=8 + index % 30,
                pb=1 + index % 5 * 0.2,
                market_cap_yi=100 + index,
                roe=10 + index % 25,
                gross_margin=30 + index % 30,
                net_margin=8 + index % 18,
                debt_ratio=25 + index % 50,
                revenue_growth=5 + index % 40,
                profit_growth=5 + index % 45,
                trend_20d=-5 + index % 15,
                technical_label="B 趋势观察",
                technical_status="candidate",
                updated_at="2026-06-01T10:00:00",
            )
        )
    return rows


def _spec():
    return query_spec_from_dict(
        {
            "intent": "screen",
            "conditions": [
                {"field": "roe", "op": ">=", "value": 15},
                {"field": "pe", "op": "<=", "value": 35},
                {"field": "debt_ratio", "op": "<=", "value": 65},
            ],
            "exclude": ["ST"],
            "sort": [{"field": "local_score", "direction": "desc"}],
            "scan_limit": 300,
            "result_limit": 50,
            "allow_missing_pe": True,
            "with_technical": False,
            "needs_confirmation": False,
            "clarifications": [],
            "raw_text": "长期运行测试策略",
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="长期运行/循环执行稳定性验收。")
    parser.add_argument("--cycles", type=int, default=20, help="循环次数")
    parser.add_argument("--sleep", type=float, default=0.0, help="每轮间隔秒数")
    parser.add_argument("--snapshot-count", type=int, default=300)
    parser.add_argument("--max-current-kb", type=int, default=50_000, help="tracemalloc 当前内存上限")
    parser.add_argument("--max-seconds", type=float, default=30.0)
    parser.add_argument("--json-report", default="", help="输出结构化 JSON 报告路径")
    args = parser.parse_args()

    report_payload = {
        "cycles": args.cycles,
        "elapsed_seconds": 0.0,
        "current_memory_kb": 0,
        "peak_memory_kb": 0,
        "failures": 0,
        "total_matches": 0,
        "status": "FAIL",
    }
    with tempfile.TemporaryDirectory() as tmp:
        start: float | None = None
        tracemalloc_started = False
        failures = 0
        total_matches = 0
        database: V1Database | None = None
        try:
            database = V1Database(Path(tmp) / "stock_tool_v1.sqlite3")
            factor_store = V1FactorStore(database)
            strategy_store = StrategyStore(database)
            factor_store.upsert_snapshots(_snapshots(args.snapshot_count))
            spec = _spec()
            preset = strategy_store.save_preset("长期运行测试", "长期运行测试策略", spec, parse_source="soak")
            filters = query_spec_to_screen_filter(spec)
            snapshots = factor_store.list_latest_snapshots()

            tracemalloc.start()
            tracemalloc_started = True
            start = time.perf_counter()
            for cycle in range(1, args.cycles + 1):
                try:
                    report = screen_factor_snapshots(snapshots, filters)
                    if report.scanned != args.snapshot_count:
                        raise AssertionError(f"第 {cycle} 轮扫描数量异常：{report.scanned}")
                    if not report.results:
                        raise AssertionError(f"第 {cycle} 轮未命中任何结果")
                    total_matches += len(report.results)
                    strategy_store.record_run(
                        "长期运行",
                        "长期运行测试策略",
                        spec,
                        "v1",
                        report,
                        preset_id=preset.id,
                        preset_name=preset.name,
                        note=f"cycle={cycle}",
                    )
                    recent = strategy_store.list_recent_runs(preset.id, limit=5)
                    if not recent:
                        raise AssertionError(f"第 {cycle} 轮最近执行记录为空")
                    if args.sleep:
                        time.sleep(args.sleep)
                except Exception:
                    failures += 1
                    report_payload["failures"] = failures
                    report_payload["total_matches"] = total_matches
                    raise

            elapsed = time.perf_counter() - start
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            tracemalloc_started = False
            current_kb = current // 1024
            peak_kb = peak // 1024
            report_payload.update(
                {
                    "elapsed_seconds": round(elapsed, 6),
                    "current_memory_kb": current_kb,
                    "peak_memory_kb": peak_kb,
                    "failures": failures,
                    "total_matches": total_matches,
                }
            )
            if elapsed > args.max_seconds:
                report_payload["failures"] = failures + 1
                raise AssertionError(f"长期运行测试耗时 {elapsed:.3f}s 超过阈值 {args.max_seconds:.3f}s")
            if current_kb > args.max_current_kb:
                report_payload["failures"] = failures + 1
                raise AssertionError(f"当前内存 {current_kb}KB 超过阈值 {args.max_current_kb}KB")
            report_payload["status"] = "PASS"
        finally:
            if start is not None and report_payload["elapsed_seconds"] == 0.0:
                report_payload["elapsed_seconds"] = round(time.perf_counter() - start, 6)
            if tracemalloc_started:
                current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                report_payload["current_memory_kb"] = current // 1024
                report_payload["peak_memory_kb"] = peak // 1024
            report_payload["failures"] = max(int(report_payload["failures"]), failures)
            report_payload["total_matches"] = max(int(report_payload["total_matches"]), total_matches)
            if database is not None:
                database.close()
            if args.json_report:
                path = Path(args.json_report)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        print(
            f"[OK] 长期运行循环：cycles={args.cycles} total_matches={report_payload['total_matches']} "
            f"elapsed={report_payload['elapsed_seconds']:.3f}s current={report_payload['current_memory_kb']}KB "
            f"peak={report_payload['peak_memory_kb']}KB"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
