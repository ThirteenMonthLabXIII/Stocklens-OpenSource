from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.data_location import read_data_dir
from stock_tool.core.pit_database import PITDatabase, pit_db_path_for
from stock_tool.core.pit_pipeline import PITDatasetBuilder
from stock_tool.core.pit_providers import BaostockPITProvider, TusharePITProvider


def _default_data_dir() -> Path:
    configured = read_data_dir()
    if configured is not None:
        return configured
    return ROOT / "my_data"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build point-in-time A-share dataset.")
    parser.add_argument("--start", default="2019-01-01", help="开始日期，默认 2019-01-01")
    parser.add_argument("--end", default="today", help="结束日期，默认 today")
    parser.add_argument("--mode", choices=["healthcheck", "preflight", "full", "incremental", "retry-failures"], default="full")
    parser.add_argument("--data-dir", type=Path, default=None, help="数据目录，默认使用软件配置目录或 ./my_data")
    parser.add_argument("--source", choices=["baostock", "tushare"], default="baostock", help="默认 baostock；Tushare 仅增强")
    parser.add_argument("--timeout-sec", type=float, default=60.0)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--stock-limit", type=int, default=None, help="限制股票数；preflight 默认 50，抽样为确定性均匀抽样")
    parser.add_argument("--refresh-existing", action="store_true", help="重拉已成功抓取过的财务区间")
    parser.add_argument("--protection-profile", choices=["balanced", "conservative"], default="balanced", help="外部请求保护档位，默认 balanced")
    parser.add_argument("--universe-source", choices=["auto", "baostock", "tushare", "akshare", "cache"], default="auto", help="Universe 来源，preflight 不允许 cache 伪通过")
    args = parser.parse_args()

    data_dir = (args.data_dir or _default_data_dir()).resolve()
    db = PITDatabase(pit_db_path_for(data_dir))
    provider = (
        TusharePITProvider(timeout_sec=args.timeout_sec, protection_profile=args.protection_profile)
        if args.source == "tushare"
        else BaostockPITProvider(timeout_sec=args.timeout_sec, protection_profile=args.protection_profile)
    )
    builder = PITDatasetBuilder(db, provider, batch_size=args.batch_size)

    def progress(done: int, total: int, message: str) -> None:
        print(f"[{done}/{total}] {message}", flush=True)

    result = builder.run(
        mode=args.mode,
        start_date=args.start,
        end_date=args.end,
        stock_limit=args.stock_limit,
        refresh_existing=args.refresh_existing,
        universe_source=args.universe_source,
        progress=progress,
    )
    print(f"job_id={result.job_id}")
    print(f"status={result.status}")
    print(f"message={result.message}")
    print(f"report={result.report_path or '-'}")
    if result.status in {"ENV_UNAVAILABLE", "SOURCE_UNREACHABLE", "SOURCE_COOLDOWN", "preflight_failed", "healthcheck_failed"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
