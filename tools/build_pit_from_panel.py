"""从研究面板库 panel.sqlite3 本地构建点时 PIT 库（零联网、零封 IP）。

背景：research/data/panel.sqlite3（约 2GB）已含全市场 baostock 数据——910 万行 hfq 日线、
101 万行带 pub_date 的点时财务、universe、industry。PIT `--mode full` 联网抓的就是同一批，
故改为本地 ETL，避免重复抓取与 baostock 封 IP 风险。

已核实的口径（详见会话诊断，均有数据佐证）：
  * 价格：panel.prices_daily 为后复权（research/etl_prices.py adjustflag="1"=hfq）→ adj='hfq'。
  * 原始价：同一 pit_prices 行的 close_raw 由 hfq close / back_adjust_factor 反推。
  * 财务指标名：panel 6 个指标按生产 field_set canonical 名称写入（roe 保持 roe）。
  * 单位：panel 比率类指标为百分数，PIT 与 field_set 对齐后同样存百分数，不再 ÷100。
  * 质量：仅取 panel metric_quality='pass'，过滤极端脏值。
  * 指标范围：带入 panel 全 6 个（roe、gross_margin、net_margin、debt_ratio、
    profit_growth、revenue_growth）。

universe 灌入全量生命周期快照（含退市股，依赖 ipo_date/out_date 做 universe_asof 查询）；
prices/financials 含历史全部代码（含退市），保留历史完整性。industry 取每个 code 最新一期
（query_date 最大）。

注意：本脚本会**写入** my_data/stock_tool_pit.sqlite3（默认数据目录的 PIT 库），source 端只读。
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.data_location import read_data_dir
from stock_tool.core.pit_database import PITDatabase, now_iso, pit_db_path_for

SOURCE = "baostock"
# panel 6 个指标 → PIT 指标名映射；未列出的保持原名
METRIC_RENAME: dict[str, str] = {}
PRICE_CHUNK = 50_000
CORE_TABLES = ("pit_universe", "pit_industry", "pit_financials", "pit_prices")
FactorSeries = tuple[list[str], list[float | None]]


def _plain_code(code: str) -> str:
    return code.split(".")[-1].strip()


def _exchange(code: str) -> str:
    return code.split(".")[0].strip() if "." in code else ""


def _default_data_dir() -> Path:
    configured = read_data_dir()
    return configured if configured is not None else (ROOT / "my_data")


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _latest_trade_day(src: sqlite3.Connection) -> str:
    row = src.execute("SELECT MAX(date) FROM prices_daily").fetchone()
    return row[0] or now_iso()[:10]


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_back_adjust_factors(src: sqlite3.Connection) -> dict[str, FactorSeries]:
    by_code: dict[str, list[tuple[str, float | None]]] = {}
    for r in src.execute(
        """
        SELECT code, divid_operate_date, back_adjust_factor
        FROM adjust_factor
        WHERE TRIM(code) <> '' AND TRIM(divid_operate_date) <> ''
        ORDER BY code, divid_operate_date
        """
    ):
        code = _plain_code(r["code"])
        by_code.setdefault(code, []).append((str(r["divid_operate_date"]), _to_float(r["back_adjust_factor"])))
    return {code: ([date for date, _factor in rows], [factor for _date, factor in rows]) for code, rows in by_code.items()}


def derive_close_raw(close: Any, trade_date: str, factors: FactorSeries | None) -> float | None:
    close_value = _to_float(close)
    if close_value is None:
        return None
    if not factors:
        return close_value
    dates, values = factors
    if not dates:
        return close_value
    idx = bisect_right(dates, trade_date) - 1
    if idx < 0:
        idx = 0
    factor = values[idx]
    if factor is None or factor <= 0:
        return None
    return round(close_value / factor, 4)


def etl_universe(src: sqlite3.Connection, db: PITDatabase, asof: str, fetched_at: str) -> int:
    rows = []
    for r in src.execute(
        "SELECT code, code_name, ipo_date, out_date, status FROM universe"
    ):
        rows.append(
            {
                "code": _plain_code(r["code"]),
                "name": r["code_name"] or "",
                "exchange": _exchange(r["code"]),
                "ipo_date": r["ipo_date"] or "",
                "out_date": r["out_date"] or "",
                "status": r["status"] or "",
                "is_index": 0,
                "asof_date": asof,
                "source": SOURCE,
                "source_version": "panel-etl",
                "fetched_at": fetched_at,
                "raw": {},
            }
        )
    return db.upsert_universe(rows)


def reset_core_tables(db: PITDatabase) -> None:
    with db.connect() as conn:
        for table in CORE_TABLES:
            conn.execute(f"DELETE FROM {table}")


def etl_industry(src: sqlite3.Connection, db: PITDatabase, asof: str, fetched_at: str) -> int:
    # 每个 code 取 query_date 最新一期
    latest: dict[str, sqlite3.Row] = {}
    for r in src.execute(
        "SELECT code, industry, industry_classification, query_date FROM industry_map ORDER BY query_date"
    ):
        latest[r["code"]] = r  # 升序遍历，后写覆盖前 → 留下最新
    rows = []
    for code, r in latest.items():
        rows.append(
            {
                "code": _plain_code(code),
                "industry": r["industry"] or "",  # 空保持空，不灌分类名（与 provider 修复一致）
                "industry_level": r["industry_classification"] or "baostock",
                "asof_date": asof,
                "source": SOURCE,
                "source_version": "panel-etl",
                "fetched_at": fetched_at,
                "raw": {},
            }
        )
    return db.upsert_industries(rows)


def etl_financials(src: sqlite3.Connection, db: PITDatabase, fetched_at: str) -> int:
    cur = src.execute(
        """
        SELECT code, stat_date, pub_date, metric, value
        FROM fundamentals_pit
        WHERE metric_quality='pass' AND value IS NOT NULL AND TRIM(pub_date) <> ''
        """
    )
    total = 0
    batch = []
    while True:
        chunk = cur.fetchmany(PRICE_CHUNK)
        if not chunk:
            break
        for r in chunk:
            metric = METRIC_RENAME.get(r["metric"], r["metric"])
            value = r["value"]
            batch.append(
                {
                    "code": _plain_code(r["code"]),
                    "report_date": r["stat_date"] or "",
                    "pub_date": r["pub_date"],
                    "metric": metric,
                    "value": value,
                    "source": SOURCE,
                    "source_version": "panel-etl",
                    "fetched_at": fetched_at,
                    "raw": {},
                }
            )
        total += db.upsert_financials(batch)
        print(f"  financials upserted: {total}", flush=True)
        batch = []
    return total


def etl_prices(src: sqlite3.Connection, db: PITDatabase, fetched_at: str) -> int:
    factors_by_code = _load_back_adjust_factors(src)
    cur = src.execute(
        """
        SELECT code, date, open, high, low, close, volume, amount, turn, pe_ttm, pb_mrq
        FROM prices_daily
        """
    )
    total = 0
    while True:
        chunk = cur.fetchmany(PRICE_CHUNK)
        if not chunk:
            break
        batch = []
        for r in chunk:
            code = _plain_code(r["code"])
            batch.append(
                {
                    "code": code,
                    "trade_date": r["date"],
                    "adj": "hfq",
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "close_raw": derive_close_raw(r["close"], r["date"], factors_by_code.get(code)),
                    "volume": r["volume"],
                    "amount": r["amount"],
                    "turnover": r["turn"],
                    "pe_ttm": r["pe_ttm"],
                    "pb_mrq": r["pb_mrq"],
                    "asof_date": r["date"],  # 价格快照日即交易日，满足 trade_date <= asof
                    "source": SOURCE,
                    "source_version": "panel-etl",
                    "fetched_at": fetched_at,
                    "raw": {},
                }
            )
        total += db.upsert_prices(batch)
        print(f"  prices upserted: {total}", flush=True)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地 ETL：panel.sqlite3 → PIT 库")
    parser.add_argument("--panel", default="research/data/panel.sqlite3", help="源面板库路径")
    parser.add_argument("--data-dir", type=Path, default=None, help="目标数据目录，默认配置目录或 ./my_data")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做什么与计数，不写库")
    args = parser.parse_args(argv)

    panel_path = Path(args.panel)
    if not panel_path.exists():
        print(f"找不到面板库：{panel_path}", file=sys.stderr)
        return 2
    data_dir = (args.data_dir or _default_data_dir()).resolve()
    target = pit_db_path_for(data_dir)
    print(f"源：{panel_path}\n目标：{target}\n")

    src = _ro(panel_path)
    fetched_at = now_iso()
    asof = _latest_trade_day(src)
    print(f"asof（universe/industry 快照日，取最新交易日）= {asof}")

    if args.dry_run:
        for name, sql in [
            ("universe(all lifecycle rows)", "SELECT COUNT(*) FROM universe"),
            ("industry_map distinct code", "SELECT COUNT(DISTINCT code) FROM industry_map"),
            ("fundamentals_pit(pass)", "SELECT COUNT(*) FROM fundamentals_pit WHERE metric_quality='pass'"),
            ("prices_daily", "SELECT COUNT(*) FROM prices_daily"),
            ("adjust_factor", "SELECT COUNT(*) FROM adjust_factor"),
            ("adjust_factor distinct code", "SELECT COUNT(DISTINCT code) FROM adjust_factor"),
        ]:
            print(f"  {name}: {src.execute(sql).fetchone()[0]}")
        src.close()
        return 0

    db = PITDatabase(target)
    reset_core_tables(db)
    print("[reset] cleared pit_universe/pit_industry/pit_financials/pit_prices")
    n_uni = etl_universe(src, db, asof, fetched_at)
    print(f"[universe] {n_uni}")
    n_ind = etl_industry(src, db, asof, fetched_at)
    print(f"[industry] {n_ind}")
    n_fin = etl_financials(src, db, fetched_at)
    print(f"[financials] {n_fin}")
    n_prc = etl_prices(src, db, fetched_at)
    print(f"[prices] {n_prc}")
    src.close()

    stats = db.stats()
    print("\n=== 建库后核验 ===")
    print(f"industry_coverage = {stats.industry_coverage}")
    print("提示：再跑 python tools/verify_pit_pointintime.py --db", target, "做点时校验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
