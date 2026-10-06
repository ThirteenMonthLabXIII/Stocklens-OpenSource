from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.cache import CacheStore
from stock_tool.core.data_fetcher import DataFetcher
from stock_tool.core.data_location import LOCATION_FILENAME, db_path_for, read_data_dir
from stock_tool.core.notify import (
    LocalFileChannel,
    Notifier,
    WindowsToastChannel,
    build_candidate_message,
)
from stock_tool.core.pit_database import PITDatabase, pit_db_path_for
from stock_tool.core.pit_screener_adapter import screen_pit_asof
from stock_tool.core.query_schema import QueryValidationError, query_spec_summary, query_spec_to_screen_filter
from stock_tool.core.screener_engine import ScreenReport, screen_market
from stock_tool.core.strategy_store import StrategyStore
from stock_tool.core.v1_database import V1Database, v1_db_path_for


def main() -> int:
    parser = argparse.ArgumentParser(description="收盘后无界面运行策略筛选，并生成本地选股播报。")
    parser.add_argument("--engine", choices=("pit", "live"), default="pit", help="筛选引擎，默认 pit。")
    parser.add_argument("--preset-id", type=int, help="只运行指定策略；缺省时运行全部已保存策略。")
    parser.add_argument("--asof", help="PIT 点时日期 YYYY-MM-DD；缺省取 PIT 库最新可用日期。")
    parser.add_argument("--source", default="auto", help="PIT 源标签；auto 表示不过滤来源。")
    parser.add_argument("--limit", type=int, default=20, help="播报展示的候选股数量，默认 20。")
    parser.add_argument("--out", type=Path, help="通知 Markdown 输出目录；默认写入数据目录 notifications 子目录。")
    parser.add_argument("--no-record", action="store_true", help="不写入策略执行记录。")
    parser.add_argument("--dry-run", action="store_true", help="只计算并打印播报，不发通知、不落库。")
    args = parser.parse_args()

    data_dir = _read_cli_data_dir()
    if data_dir is None:
        print("未配置数据目录，请先在主程序设置数据位置。")
        return 1

    v1_db = V1Database(v1_db_path_for(data_dir))
    store = StrategyStore(v1_db)
    presets = _select_presets(store, args.preset_id)
    if not presets:
        print("没有可运行的已保存策略。请先在主程序保存至少一个策略。")
        return 1

    source = _source_arg(args.source)
    pit_db: PITDatabase | None = None
    fetcher: DataFetcher | None = None
    asof = args.asof or ""
    if args.engine == "pit":
        pit_db = PITDatabase(pit_db_path_for(data_dir))
        if not asof:
            asof = pit_db.latest_asof(source)
        if not asof:
            print("PIT 库没有可用 asof_date，请先完成 PIT 数据构建，或用 --asof 指定已有日期。")
            return 1
    else:
        fetcher = DataFetcher(CacheStore(db_path_for(data_dir)))
        asof = asof or "latest"

    out_dir = args.out or (data_dir / "notifications")
    notifier = Notifier([LocalFileChannel(out_dir), WindowsToastChannel()])
    ok_count = 0

    for preset in presets:
        try:
            loaded_preset, spec = store.load_query_spec(preset.id)
            filters = query_spec_to_screen_filter(spec)
            report = _run_screen(args.engine, filters, pit_db, fetcher, asof, source)
            if not report.results:
                print(f"{loaded_preset.name} 策略本次 0 只候选股，跳过通知。")
                ok_count += 1
                continue

            if not args.dry_run and not args.no_record:
                run_id = store.record_run(
                    source_label="auto_push",
                    user_text=loaded_preset.user_text,
                    spec=spec,
                    data_source=args.engine,
                    report=report,
                    preset_id=loaded_preset.id,
                    preset_name=loaded_preset.name,
                    universe_id=loaded_preset.universe_id,
                )
                print(f"{loaded_preset.name} 已写入执行记录 run_id={run_id}")

            message = build_candidate_message(
                loaded_preset.name,
                loaded_preset.user_text,
                query_spec_summary(spec),
                report,
                asof,
                args.engine,
                limit=args.limit,
            )
            if args.dry_run:
                print("\n" + message.plaintext + "\n")
            else:
                for result in notifier.send(message):
                    status = "OK" if result.ok else "WARN"
                    print(f"{loaded_preset.name} [{status}] {result.channel}: {result.detail}")
            ok_count += 1
        except QueryValidationError as exc:
            print(f"{preset.name} 策略已跳过：{exc}")
        except Exception as exc:
            print(f"{preset.name} 策略运行失败：{exc.__class__.__name__}: {exc}")

    return 0 if ok_count else 1


def _select_presets(store: StrategyStore, preset_id: int | None):
    if preset_id is not None:
        preset = store.get_preset(preset_id)
        return [preset] if preset is not None else []
    return store.list_presets()


def _read_cli_data_dir() -> Path | None:
    data_dir = read_data_dir()
    if data_dir is not None:
        return data_dir
    location = ROOT / LOCATION_FILENAME
    if not location.exists():
        return None
    text = location.read_text(encoding="utf-8").strip()
    if not text:
        return None
    candidate = Path(text).expanduser().resolve()
    return candidate if candidate.exists() else None


def _source_arg(value: str | None) -> str | None:
    text = str(value or "").strip()
    if text.lower() in {"", "auto", "all", "none"}:
        return None
    return text


def _run_screen(
    engine: str,
    filters,
    pit_db: PITDatabase | None,
    fetcher: DataFetcher | None,
    asof: str,
    source: str | None,
) -> ScreenReport:
    if engine == "pit":
        if pit_db is None:
            raise RuntimeError("PIT 数据库未初始化")
        return screen_pit_asof(pit_db, asof, filters, source=source, legacy=True)
    if fetcher is None:
        raise RuntimeError("实时行情抓取器未初始化")
    return screen_market(fetcher, filters)


if __name__ == "__main__":
    raise SystemExit(main())
