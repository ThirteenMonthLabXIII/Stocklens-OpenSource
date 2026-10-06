from __future__ import annotations

from pathlib import Path

import pandas as pd

from research._testkit import run_tests
from research.event_backtest import EventBacktestConfig, run_event_backtest
from research.panel_db import DATA_DIR, open_panel

TEST_DB = DATA_DIR / "event_backtest_selftest.sqlite3"


def _fresh_db():
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{TEST_DB}{suffix}")
        if path.exists():
            path.unlink()
    db = open_panel(TEST_DB)
    db.upsert(
        "universe",
        [
            {"code": "A", "code_name": "Alpha", "ipo_date": "2018-01-01", "out_date": "", "type": "1", "status": "1", "fetched_at": "t"},
            {"code": "B", "code_name": "Beta", "ipo_date": "2018-01-01", "out_date": "", "type": "1", "status": "1", "fetched_at": "t"},
        ],
        ["code"],
    )
    days = [f"2019-01-{day:02d}" for day in range(1, 9)]
    db.upsert("trade_calendar", [{"calendar_date": day, "is_trading_day": 1} for day in days], ["calendar_date"])
    prices = []
    for index, day in enumerate(days):
        prices.extend(
            [
                _price("A", day, open_price=100 + index, close=100 + index),
                _price("B", day, open_price=100 + index, close=100 + index),
            ]
        )
    for row in prices:
        if row["code"] == "A" and row["date"] == "2019-01-02":
            row["open"] = 108
            row["preclose"] = 100
        if row["code"] == "A" and row["date"] == "2019-01-04":
            row["close"] = 150
    db.upsert("prices_daily", prices, ["code", "date"])
    facts = []
    for code, roe in (("A", 0.20), ("B", 0.10)):
        for metric, value in (("roe", roe), ("net_margin", roe), ("gross_margin", roe), ("revenue_growth", roe), ("profit_growth", roe), ("debt_ratio", 0.4)):
            facts.append({"code": code, "stat_date": "2018-12-31", "pub_date": "2019-01-01", "metric": metric, "value": value, "source_api": "fixture", "fetched_at": "t"})
    db.upsert("fundamentals_pit", facts, ["code", "stat_date", "metric", "pub_date"])
    db.upsert(
        "industry_map",
        [{"code": code, "industry": "fixture", "industry_classification": "fixture", "update_date": "2019-01-01", "query_date": "2019-01-01", "fetched_at": "t"} for code in ("A", "B")],
        ["code", "query_date"],
    )
    return db


def _price(code: str, day: str, open_price: float, close: float) -> dict:
    return {"code": code, "date": day, "open": open_price, "high": close, "low": close, "close": close, "preclose": close - 1, "volume": 1, "amount": 1, "turn": 1, "pe_ttm": 10, "pb_mrq": 1, "ps_ttm": 1, "pcf_ncf_ttm": 1, "trade_status": "1", "fetched_at": "t"}


def test_signal_uses_next_open_and_holding_close():
    db = _fresh_db()
    try:
        def score(frame: pd.DataFrame) -> pd.DataFrame:
            return pd.DataFrame({"code": frame["code"], "score": frame["code"].eq("A").astype(float)})

        rows = run_event_backtest(score, db, config=EventBacktestConfig(start_date="2019-01-01", end_date="2019-01-08", holding_days=3, top_n=1, commission_bps=0, stamp_tax_sell_bps=0, slippage_bps=0))
        assert len(rows) == 1
        assert rows.iloc[0]["execution_date"] == "2019-01-02"
        assert rows.iloc[0]["exit_date"] == "2019-01-04"
        assert abs(float(rows.iloc[0]["portfolio_return"]) - (150 / 108 - 1.0)) < 1e-9
    finally:
        db.close()
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{TEST_DB}{suffix}")
            if path.exists():
                path.unlink()


def test_event_engine_records_cost_and_untradeable_reasons():
    db = _fresh_db()
    try:
        db.conn.execute("UPDATE prices_daily SET trade_status='LIMIT_UP' WHERE code='A' AND date='2019-01-02'")
        db.conn.commit()
        score = lambda frame: pd.DataFrame({"code": frame["code"], "score": frame["code"].eq("A").astype(float)})
        low = run_event_backtest(score, db, config=EventBacktestConfig(start_date="2019-01-01", end_date="2019-01-08", holding_days=3, top_n=1, commission_bps=0, stamp_tax_sell_bps=0, slippage_bps=0))
        high = run_event_backtest(score, db, config=EventBacktestConfig(start_date="2019-01-01", end_date="2019-01-08", holding_days=3, top_n=1, commission_bps=20, stamp_tax_sell_bps=10, slippage_bps=20))
        assert low.iloc[0]["n_traded"] == 0
        assert low.iloc[0]["skip_reasons"]["entry_not_tradeable"] == 1
        assert high.iloc[0]["portfolio_return"] is None
    finally:
        db.close()
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{TEST_DB}{suffix}")
            if path.exists():
                path.unlink()


def test_formal_event_backtest_requires_identity_binding():
    db = _fresh_db()
    try:
        score = lambda frame: pd.DataFrame({"code": frame["code"], "score": frame["code"].eq("A").astype(float)})
        try:
            run_event_backtest(
                score,
                db,
                config=EventBacktestConfig(
                    start_date="2019-01-01",
                    end_date="2019-01-08",
                    holding_days=3,
                    top_n=1,
                    commission_bps=0,
                    stamp_tax_sell_bps=0,
                    slippage_bps=0,
                    formal_mode=True,
                ),
            )
        except ValueError as exc:
            assert "身份绑定" in str(exc)
        else:
            raise AssertionError("正式回测缺少身份绑定时必须阻断")
        rows = run_event_backtest(
            score,
            db,
            config=EventBacktestConfig(
                start_date="2019-01-01",
                end_date="2019-01-08",
                holding_days=3,
                top_n=1,
                commission_bps=0,
                stamp_tax_sell_bps=0,
                slippage_bps=0,
                data_snapshot_id="snapshot-1",
                refresh_event_id="refresh-1",
                universe_version="universe-1",
                formula_version="baseline-v1",
                formal_mode=True,
            ),
        )
        assert rows.iloc[0]["data_snapshot_id"] == "snapshot-1"
        assert bool(rows.iloc[0]["formal_mode"])
        assert rows.iloc[0]["execution_model"] == "trade_ledger_v1"
        assert rows.iloc[0]["execution_ledger_summary"]["filled_orders"] >= 1
    finally:
        db.close()
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{TEST_DB}{suffix}")
            if path.exists():
                path.unlink()


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
