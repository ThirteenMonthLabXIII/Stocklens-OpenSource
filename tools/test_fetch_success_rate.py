from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from statistics import mean


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.cache import CacheStore
from stock_tool.core.data_fetcher import (
    _history_from_akshare,
    _history_from_eastmoney_http,
    _history_from_sina_http,
    _history_from_tencent_http,
    _short_error,
)


DEFAULT_CODES = [
    "600519", "000001", "300750", "600036", "601318",
    "000858", "002594", "600900", "601899", "600276",
    "000333", "601012", "300760", "600030", "600887",
    "000725", "002415", "603259", "600309", "601088",
    "000651", "600000", "300059", "002475", "601398",
    "600406", "600438", "300124", "002230", "601668",
]


SOURCES = [
    ("tencent_http", _history_from_tencent_http),
    ("sina_http", _history_from_sina_http),
    ("eastmoney_http", _history_from_eastmoney_http),
    ("akshare", _history_from_akshare),
]


def _parse_codes(raw: str | None, limit: int) -> list[str]:
    if raw:
        codes = [item.strip().zfill(6) for item in raw.split(",") if item.strip()]
    else:
        codes = DEFAULT_CODES[:]
    return codes[: max(1, limit)]


def main() -> int:
    parser = argparse.ArgumentParser(description="测试K线多源抓取成功率，并写入抓取日志。")
    parser.add_argument("--codes", help="逗号分隔股票代码，例如 600519,000001,300750")
    parser.add_argument("--limit", type=int, default=30, help="默认测试前30只内置股票")
    parser.add_argument("--days", type=int, default=30, help="抓取最近多少天，默认30")
    args = parser.parse_args()

    cache = CacheStore()
    codes = _parse_codes(args.codes, args.limit)
    source_success: dict[str, int] = {name: 0 for name, _ in SOURCES}
    source_times: dict[str, list[int]] = {name: [] for name, _ in SOURCES}
    failures: list[str] = []

    print(f"开始测试 {len(codes)} 只股票，顺序：腾讯 -> 新浪 -> 东方财富多节点 -> akshare")
    total_start = time.perf_counter()

    for index, code in enumerate(codes, start=1):
        code_start = time.perf_counter()
        attempts: list[str] = []
        ok_source = ""
        rows = 0
        for source_name, fetch_fn in SOURCES:
            start = time.perf_counter()
            try:
                df = fetch_fn(code, args.days)
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                rows = len(df)
                ok_source = source_name
                source_success[source_name] += 1
                source_times[source_name].append(elapsed_ms)
                attempts.append(f"{source_name}=成功/{elapsed_ms}ms/{rows}条")
                break
            except Exception as exc:
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                attempts.append(f"{source_name}=失败/{elapsed_ms}ms/{_short_error(exc)}")

        code_ms = int((time.perf_counter() - code_start) * 1000)
        if ok_source:
            print(f"[{index:02d}/{len(codes)}] {code} 成功 source={ok_source} rows={rows} time={code_ms}ms")
            cache.add_fetch_log(
                "成功率测试",
                target=code,
                source=ok_source,
                status="success",
                message="；".join(attempts)[-500:],
                duration_ms=code_ms,
            )
        else:
            print(f"[{index:02d}/{len(codes)}] {code} 失败 time={code_ms}ms")
            failures.append(code)
            cache.add_fetch_log(
                "成功率测试",
                target=code,
                source="tencent/sina/eastmoney/akshare",
                status="failed",
                message="；".join(attempts)[-500:],
                duration_ms=code_ms,
            )

    total_ms = int((time.perf_counter() - total_start) * 1000)
    success_count = len(codes) - len(failures)
    print("")
    print(f"总成功率：{success_count}/{len(codes)} = {success_count / len(codes) * 100:.1f}%")
    print(f"总耗时：{total_ms}ms，平均每只：{int(total_ms / len(codes))}ms")
    for source_name, _ in SOURCES:
        times = source_times[source_name]
        avg = int(mean(times)) if times else 0
        print(f"{source_name}: 成功 {source_success[source_name]} 次，平均耗时 {avg}ms")
    if failures:
        print("失败股票：" + ", ".join(failures))
    return 0 if success_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
