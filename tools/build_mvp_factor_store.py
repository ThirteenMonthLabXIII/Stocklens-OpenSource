from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.cache import CacheStore
from stock_tool.core.data_fetcher import DataFetcher
from stock_tool.core.data_location import db_path_for, read_data_dir
from stock_tool.core.factor_store import V1FactorStore
from stock_tool.core.mvp_registry import load_product_registry
from stock_tool.core.mvp_universe import (
    UniverseSnapshotStore,
    activation_status,
    fetch_and_save_hs300,
    save_quote_universes,
    update_universe_factor_snapshots,
)
from stock_tool.core.pit_database import PITDatabase, pit_db_path_for
from stock_tool.core.v1_database import V1Database, v1_db_path_for


def main() -> int:
    parser = argparse.ArgumentParser(description="构建MVP全池因子快照；最多2个worker，支持失败/过期项续跑")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--universe", choices=("all_a", "hs300", "chinext"), default="all_a")
    parser.add_argument("--template", choices=("quality", "growth", "value", "quality_value", "trend"), default="quality")
    parser.add_argument("--refresh-existing", action="store_true")
    args = parser.parse_args()
    data_dir = (args.data_dir or read_data_dir())
    if data_dir is None:
        parser.error("未配置数据目录，请提供 --data-dir")
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    database = V1Database(v1_db_path_for(data_dir))
    factor_store = V1FactorStore(database)
    universe_store = UniverseSnapshotStore(database)
    fetcher = DataFetcher(CacheStore(db_path_for(data_dir)))
    quotes, updated_at = fetcher.spot_quotes()
    asof = (updated_at.date() if updated_at else date.today()).isoformat()
    saved = save_quote_universes(quotes, universe_store, asof_date=asof, source="production_spot_quotes")
    if args.universe == "hs300":
        universe = fetch_and_save_hs300(universe_store)
    else:
        universe = saved[args.universe]

    pit_path = pit_db_path_for(data_dir)
    pit_database = PITDatabase(pit_path) if pit_path.exists() else None

    def progress(current: int, total: int, message: str) -> None:
        print(f"[{current}/{total}] {message}", flush=True)

    result = update_universe_factor_snapshots(
        fetcher,
        factor_store,
        database,
        universe,
        pit_database=pit_database,
        max_workers=2,
        refresh_existing=args.refresh_existing,
        progress=progress,
    )
    gate = activation_status(
        universe,
        factor_store.list_latest_snapshots(),
        args.template,
        registry=load_product_registry(),
        database=database,
    )
    print(f"批处理：成功 {result.success}，失败 {result.failed}；门控：{'通过' if gate.active else '关闭'}")
    if gate.reasons:
        print("；".join(gate.reasons))
    return 0 if gate.active else 3


if __name__ == "__main__":
    raise SystemExit(main())
