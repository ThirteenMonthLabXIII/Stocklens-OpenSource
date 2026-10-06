from __future__ import annotations

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.factor_store import _pct_from_ma
from stock_tool.core.pit_database import PITDatabase
from stock_tool.core.pit_screener_adapter import build_pit_snapshots, screen_pit_asof
from stock_tool.core.screener_engine import ScreenFilter, screen_factor_snapshots


SOURCE = "fixture"
ASOF = "2024-03-01"
FETCHED_AT = "2024-03-01T18:00:00+08:00"


def _seed_db(db: PITDatabase) -> list[float]:
    db.upsert_universe(
        [
            {
                "code": "000001",
                "name": "样例银行",
                "exchange": "sz",
                "ipo_date": "1991-04-03",
                "out_date": "",
                "status": "1",
                "is_index": 0,
                "asof_date": ASOF,
                "source": SOURCE,
                "source_version": "test",
                "fetched_at": FETCHED_AT,
                "raw": {},
            },
            {
                "code": "000002",
                "name": "已退样例",
                "exchange": "sz",
                "ipo_date": "1991-01-01",
                "out_date": "2024-02-28",
                "status": "0",
                "is_index": 0,
                "asof_date": ASOF,
                "source": SOURCE,
                "source_version": "test",
                "fetched_at": FETCHED_AT,
                "raw": {},
            },
        ]
    )
    db.upsert_financials(
        [
            {
                "code": "000001",
                "report_date": "2023-09-30",
                "pub_date": "2023-10-31",
                "metric": "roe",
                "value": 10.0,
                "source": SOURCE,
                "source_version": "test",
                "fetched_at": FETCHED_AT,
                "raw": {},
            },
            {
                "code": "000001",
                "report_date": "2023-12-31",
                "pub_date": "2024-02-20",
                "metric": "roe",
                "value": 15.5,
                "source": SOURCE,
                "source_version": "test",
                "fetched_at": FETCHED_AT,
                "raw": {},
            },
            {
                "code": "000001",
                "report_date": "2024-03-31",
                "pub_date": "2024-04-20",
                "metric": "roe",
                "value": 99.0,
                "source": SOURCE,
                "source_version": "test",
                "fetched_at": FETCHED_AT,
                "raw": {},
            },
            {
                "code": "000001",
                "report_date": "2023-12-31",
                "pub_date": "2024-02-20",
                "metric": "gross_margin",
                "value": 38.0,
                "source": SOURCE,
                "source_version": "test",
                "fetched_at": FETCHED_AT,
                "raw": {},
            },
            {
                "code": "000001",
                "report_date": "2023-12-31",
                "pub_date": "2024-02-20",
                "metric": "net_margin",
                "value": 12.0,
                "source": SOURCE,
                "source_version": "test",
                "fetched_at": FETCHED_AT,
                "raw": {},
            },
            {
                "code": "000001",
                "report_date": "2023-12-31",
                "pub_date": "2024-02-20",
                "metric": "debt_ratio",
                "value": 45.0,
                "source": SOURCE,
                "source_version": "test",
                "fetched_at": FETCHED_AT,
                "raw": {},
            },
            {
                "code": "000001",
                "report_date": "2023-12-31",
                "pub_date": "2024-02-20",
                "metric": "revenue_growth",
                "value": 8.0,
                "source": SOURCE,
                "source_version": "test",
                "fetched_at": FETCHED_AT,
                "raw": {},
            },
            {
                "code": "000001",
                "report_date": "2023-12-31",
                "pub_date": "2024-02-20",
                "metric": "profit_growth",
                "value": 9.0,
                "source": SOURCE,
                "source_version": "test",
                "fetched_at": FETCHED_AT,
                "raw": {},
            },
        ]
    )

    closes: list[float] = []
    start = date(2024, 1, 1)
    price_rows = []
    for idx in range(61):
        trade_date = (start + timedelta(days=idx)).isoformat()
        close = 100.0 + idx
        closes.append(close)
        price_rows.append(
            {
                "code": "000001",
                "trade_date": trade_date,
                "adj": "hfq",
                "open": close - 0.5,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "close_raw": round(10 + idx * 0.1, 2),
                "volume": 1000 + idx,
                "amount": 10000 + idx,
                "turnover": 1.0,
                "pe_ttm": 20.0 + idx / 10,
                "pb_mrq": 1.5 + idx / 100,
                "asof_date": trade_date,
                "source": SOURCE,
                "source_version": "test",
                "fetched_at": FETCHED_AT,
                "raw": {},
            }
        )
    price_rows.append(
        {
            "code": "000001",
            "trade_date": "2024-03-02",
            "adj": "hfq",
            "close": 999.0,
            "close_raw": 99.9,
            "pe_ttm": 99.0,
            "pb_mrq": 9.9,
            "asof_date": "2024-03-02",
            "source": SOURCE,
            "source_version": "test",
            "fetched_at": FETCHED_AT,
            "raw": {},
        }
    )
    db.upsert_prices(price_rows)
    return closes


def run_tests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = PITDatabase(Path(tmp) / "pit.sqlite3")
        closes = _seed_db(db)

        price_window = db.prices_visible_asof("000001", ASOF, lookback_rows=60, source=SOURCE)
        assert len(price_window) == 60
        assert price_window[-1]["trade_date"] == ASOF
        assert price_window[-1]["close_raw"] == 16.0
        compact_price_window = db.prices_visible_asof("000001", "20240301", lookback_rows=60, source=SOURCE)
        assert [row["trade_date"] for row in compact_price_window] == [row["trade_date"] for row in price_window]

        snapshots = build_pit_snapshots(db, ASOF, source=SOURCE, scan_limit=300)
        compact_snapshots = build_pit_snapshots(db, "20240301", source=SOURCE, scan_limit=300)
        assert [(item.code, item.price, item.roe) for item in compact_snapshots] == [(item.code, item.price, item.roe) for item in snapshots]
        assert [snapshot.code for snapshot in snapshots] == ["000001"], "退市样例不应进入 asof 截面"
        snapshot = snapshots[0]
        assert snapshot.roe == 15.5, "future pub_date 的 roe=99 不应被使用"
        assert snapshot.financial_updated_at == "2024-02-20", "财务可获得时间必须来自公告日期"
        assert snapshot.price == 16.0
        assert snapshot.quote_updated_at == ASOF, "价格可获得时间必须来自 PIT asof_date"
        assert snapshot.pe is None
        assert snapshot.pe_ttm == 26.0
        assert round(snapshot.pb or 0, 2) == 2.10
        assert snapshot.trend_20d == _pct_from_ma(closes[-1], sum(closes[-20:]) / 20)
        assert snapshot.trend_60d == _pct_from_ma(closes[-1], sum(closes[-60:]) / 60)
        assert snapshot.market_cap_yi is None
        assert "market_cap_yi" in snapshot.missing_fields.split(",")
        assert snapshot.technical_status == "candidate"
        assert "technical_label" not in snapshot.missing_fields.split(",")

        filters = ScreenFilter(
            min_roe=10,
            max_debt_ratio=60,
            max_pe=40,
            allow_missing_pe=True,
            with_technical=False,
            scan_limit=300,
            result_limit=10,
        )
        direct_report = screen_factor_snapshots(snapshots, filters)
        assert direct_report.scanned == 1
        assert len(direct_report.results) == 1
        assert direct_report.results[0].quote.code == "000001"

        first = screen_pit_asof(db, ASOF, filters, source=SOURCE, legacy=True)
        second = screen_pit_asof(db, ASOF, filters, source=SOURCE, legacy=True)
        first_seq = [(item.quote.code, item.local_score) for item in first.results]
        second_seq = [(item.quote.code, item.local_score) for item in second.results]
        assert first_seq == second_seq == [("000001", direct_report.results[0].local_score)]
        assert first.data_source == "pit:fixture"
        assert first.snapshot_asof == ASOF
        assert first.data_snapshot_id and first.data_snapshot_id == second.data_snapshot_id

    print("[OK] PIT screener adapter tests passed")


if __name__ == "__main__":
    run_tests()
