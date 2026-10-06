from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.models import FinancialMetrics, StockQuote
from stock_tool.core.query_schema import query_spec_from_dict
from stock_tool.core.screener_engine import ScreenCandidate, ScreenReport
from stock_tool.core.strategy_store import StrategyStore
from stock_tool.core.technical_profile import TechnicalProfile
from stock_tool.core.v1_database import V1Database


def _spec(index: int = 0):
    return query_spec_from_dict(
        {
            "intent": "screen",
            "conditions": [
                {"field": "roe", "op": ">=", "value": 12 + index % 10},
                {"field": "pe", "op": "<=", "value": 50},
            ],
            "exclude": ["ST"],
            "sort": [{"field": "local_score", "direction": "desc"}],
            "scan_limit": 300,
            "result_limit": 80,
            "allow_missing_pe": True,
            "with_technical": False,
            "needs_confirmation": False,
            "clarifications": [],
            "raw_text": "策略压力测试",
        }
    )


def _report(result_count: int = 3) -> ScreenReport:
    results = []
    for index in range(result_count):
        code = f"{index + 1:06d}"
        results.append(
            ScreenCandidate(
                quote=StockQuote(code=code, name=f"压力样例{code}", price=10 + index, pe=12 + index, pb=1.2),
                metrics=FinancialMetrics(roe=20 + index, gross_margin=40, net_margin=15, debt_ratio=35),
                technical=TechnicalProfile(False, status="missing", opportunity_label="未计算技术画像"),
                local_score=88 - index,
                reasons=["压力测试命中"],
            )
        )
    return ScreenReport(results=results, scanned=300, skipped=297, failed=0, errors=[])


def main() -> int:
    parser = argparse.ArgumentParser(description="策略保存、覆盖、删除、执行记录压力测试。")
    parser.add_argument("--presets", type=int, default=300, help="保存/覆盖策略数量")
    parser.add_argument("--runs", type=int, default=1000, help="写入执行记录次数")
    parser.add_argument("--recent-limit", type=int, default=20)
    parser.add_argument("--max-seconds", type=float, default=15.0)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        database = V1Database(Path(tmp) / "stock_tool_v1.sqlite3")
        store = StrategyStore(database)
        report = _report()
        start = time.perf_counter()

        for index in range(args.presets):
            store.save_preset(
                f"压力策略{index:04d}",
                "策略压力测试",
                _spec(index),
                parse_source="stress",
            )
        for index in range(args.presets):
            store.save_preset(
                f"压力策略{index:04d}",
                "策略压力覆盖测试",
                _spec(index + 1),
                parse_source="stress-overwrite",
            )

        presets = store.list_presets()
        if len(presets) != args.presets:
            raise AssertionError(f"策略数量不符合预期：expected={args.presets} actual={len(presets)}")

        first = presets[0]
        for index in range(args.runs):
            preset = presets[index % len(presets)]
            _preset, spec = store.load_query_spec(preset.id)
            store.record_run(
                "压力执行",
                "策略压力测试",
                spec,
                "v1",
                report,
                preset_id=preset.id,
                preset_name=preset.name,
                note="stress",
            )

        recent = store.list_recent_runs(first.id, limit=args.recent_limit)
        if not recent:
            raise AssertionError("最近执行记录为空")
        store.delete_preset(first.id)
        recent_after_delete = store.list_recent_runs(first.id, limit=args.recent_limit)
        if not recent_after_delete:
            raise AssertionError("删除策略后历史执行记录不应丢失")

        elapsed = time.perf_counter() - start
        if elapsed > args.max_seconds:
            raise AssertionError(f"策略压力测试耗时 {elapsed:.3f}s 超过阈值 {args.max_seconds:.3f}s")
        print(
            f"[OK] 策略压力：presets={args.presets} runs={args.runs} "
            f"recent={len(recent)} elapsed={elapsed:.3f}s"
        )
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
