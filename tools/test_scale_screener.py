from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.factor_store import FactorSnapshot, V1FactorStore
from stock_tool.core.query_schema import query_spec_from_dict, query_spec_to_screen_filter
from stock_tool.core.screener_engine import screen_factor_snapshots
from stock_tool.core.v1_database import V1Database


def _snapshot(index: int) -> FactorSnapshot:
    code = f"{index % 1_000_000:06d}"
    return FactorSnapshot(
        code=code,
        name=f"规模样例{code}",
        price=8 + index % 80,
        pe=8 + index % 45,
        pb=0.8 + index % 8 * 0.2,
        market_cap_yi=50 + index % 5000,
        roe=5 + index % 35,
        gross_margin=20 + index % 55,
        net_margin=3 + index % 25,
        debt_ratio=20 + index % 75,
        revenue_growth=-10 + index % 70,
        profit_growth=-15 + index % 80,
        trend_20d=-8 + index % 25,
        trend_60d=-12 + index % 35,
        technical_label="A 回踩候选" if index % 3 == 0 else "B 趋势观察",
        technical_status="candidate",
        technical_summary="规模测试样本",
        updated_at="2026-06-01T10:00:00",
    )


def _spec_payload(scan_limit: int, result_limit: int) -> dict:
    return {
        "intent": "screen",
        "conditions": [
            {"field": "roe", "op": ">=", "value": 15},
            {"field": "debt_ratio", "op": "<=", "value": 60},
            {"field": "pe", "op": "<=", "value": 40},
        ],
        "exclude": ["ST"],
        "sort": [{"field": "roe", "direction": "desc"}],
        "scan_limit": scan_limit,
        "result_limit": result_limit,
        "allow_missing_pe": True,
        "with_technical": False,
        "needs_confirmation": False,
        "clarifications": [],
        "raw_text": "规模测试策略",
    }


def _assert_results(size: int, elapsed: float, max_seconds: float, matched: int, scanned: int) -> None:
    if scanned != size:
        raise AssertionError(f"扫描数量不符合预期：expected={size} actual={scanned}")
    if matched <= 0:
        raise AssertionError(f"规模 {size} 未命中任何样本，测试数据或筛选条件异常")
    if elapsed > max_seconds:
        raise AssertionError(f"规模 {size} 筛选耗时 {elapsed:.3f}s 超过阈值 {max_seconds:.3f}s")


def run_scale(size: int, max_seconds: float, result_limit: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = V1Database(Path(tmp) / "stock_tool_v1.sqlite3")
        store = V1FactorStore(db)
        store.upsert_snapshots([_snapshot(index) for index in range(1, size + 1)])
        snapshots = store.list_latest_snapshots()
        filters = query_spec_to_screen_filter(query_spec_from_dict(_spec_payload(size, result_limit)))
        start = time.perf_counter()
        report = screen_factor_snapshots(snapshots, filters)
        elapsed = time.perf_counter() - start
        _assert_results(size, elapsed, max_seconds, len(report.results), report.scanned)
        for item in report.results:
            if item.metrics.roe is None or item.metrics.roe < 15:
                raise AssertionError(f"{item.quote.code} ROE 未满足条件")
            if item.metrics.debt_ratio is None or item.metrics.debt_ratio > 60:
                raise AssertionError(f"{item.quote.code} 负债率未满足条件")
            if item.quote.pe is not None and item.quote.pe > 40:
                raise AssertionError(f"{item.quote.code} PE 未满足条件")
        db.close()
        print(f"[OK] 规模筛选 {size} 只：命中 {len(report.results)}，耗时 {elapsed:.3f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description="V1本地快照规模筛选验收。")
    parser.add_argument("--sizes", default="300,1000,5000", help="逗号分隔规模，例如 300,1000,5000")
    parser.add_argument("--max-seconds", type=float, default=5.0, help="每档规模筛选耗时上限")
    parser.add_argument("--result-limit", type=int, default=120)
    args = parser.parse_args()
    for raw in args.sizes.split(","):
        size = int(raw.strip())
        run_scale(size, args.max_seconds, args.result_limit)
    print("[OK] 规模筛选验收通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
