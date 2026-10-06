from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.pit_database import PITDatabase, PIT_SCHEMA_VERSION, now_iso, pit_db_path_for
from stock_tool.core.industry_strength import build_industry_strength_report
from stock_tool.core.pit_messages import format_pit_status_for_user
from stock_tool.core.pit_pipeline import PITDatasetBuilder, _deterministic_sample
from stock_tool.core.pit_providers import BaostockPITProvider, ProviderUnavailable, TusharePITProvider
from stock_tool.core.pit_protection import ProtectionProfile, RequestProtector, SourceCircuitOpen
from tools.build_pit_from_panel import derive_close_raw


class FakePITProvider:
    name = "fake"
    source_version = "test"

    def __init__(self) -> None:
        self.price_fail_once = {"000002"}
        self.price_calls: list[str] = []
        self.financial_calls: list[str] = []

    def available_status(self) -> tuple[str, str]:
        return "GO", "fake ready"

    def fetch_universe(self, asof_date: str) -> list[dict[str, Any]]:
        fetched_at = now_iso()
        return [
            {
                "code": "000001",
                "name": "样例一",
                "exchange": "sz",
                "ipo_date": "1991-01-01",
                "out_date": "",
                "status": "1",
                "is_index": 0,
                "asof_date": asof_date,
                "source": self.name,
                "source_version": self.source_version,
                "fetched_at": fetched_at,
                "raw": {"fixture": True},
            },
            {
                "code": "000002",
                "name": "样例二",
                "exchange": "sz",
                "ipo_date": "1991-01-01",
                "out_date": "",
                "status": "1",
                "is_index": 0,
                "asof_date": asof_date,
                "source": self.name,
                "source_version": self.source_version,
                "fetched_at": fetched_at,
                "raw": {"fixture": True},
            },
        ]

    def healthcheck(self, start_date: str, end_date: str) -> dict[str, Any]:
        return {
            "healthcheck_status": "GO",
            "latest_trade_day": end_date,
            "universe_method": "fake.healthcheck",
            "universe_rows": 2,
            "worker_restarts": 0,
        }

    def fetch_prices(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        code = code.zfill(6)
        self.price_calls.append(code)
        if code in self.price_fail_once:
            self.price_fail_once.remove(code)
            raise RuntimeError("fixture price source closed connection")
        return [
            {
                "code": code,
                "trade_date": start_date,
                "adj": "hfq",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "close_raw": 5.25,
                "volume": 1000,
                "amount": 10000,
                "turnover": 1.2,
                "pe_ttm": 12.0,
                "pb_mrq": 1.1,
                "asof_date": start_date,
                "source": self.name,
                "source_version": self.source_version,
                "fetched_at": now_iso(),
                "raw": {"fixture": True},
            }
        ]

    def fetch_financials(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        code = code.zfill(6)
        self.financial_calls.append(code)
        return [
            {
                "code": code,
                "report_date": "2020-03-31",
                "pub_date": "2020-04-30",
                "metric": "roe",
                "value": 15.0,
                "source": self.name,
                "source_version": self.source_version,
                "fetched_at": now_iso(),
                "raw": {"fixture": True},
            }
        ]

    def fetch_industry(self, asof_date: str) -> list[dict[str, Any]]:
        return [
            {
                "code": code,
                "industry": "银行",
                "industry_level": "fixture",
                "asof_date": asof_date,
                "source": self.name,
                "source_version": self.source_version,
                "fetched_at": now_iso(),
                "raw": {"fixture": True},
            }
            for code in ("000001", "000002")
        ]


class FlakyPITProvider(FakePITProvider):
    def __init__(self) -> None:
        super().__init__()
        self.price_fail_once = {"000002", "000003"}
        self.financial_fail_once = {"000001", "000003"}

    def fetch_universe(self, asof_date: str) -> list[dict[str, Any]]:
        rows = super().fetch_universe(asof_date)
        rows.append(
            {
                "code": "000003",
                "name": "样例三",
                "exchange": "sz",
                "ipo_date": "1991-01-01",
                "out_date": "",
                "status": "1",
                "is_index": 0,
                "asof_date": asof_date,
                "source": self.name,
                "source_version": self.source_version,
                "fetched_at": now_iso(),
                "raw": {"fixture": True},
            }
        )
        return rows

    def fetch_financials(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        code = code.zfill(6)
        self.financial_calls.append(code)
        if code in self.financial_fail_once:
            self.financial_fail_once.remove(code)
            raise RuntimeError("fixture financial source closed connection")
        return [
            {
                "code": code,
                "report_date": "2020-03-31",
                "pub_date": "2020-04-30",
                "metric": "roe",
                "value": 15.0,
                "source": self.name,
                "source_version": self.source_version,
                "fetched_at": now_iso(),
                "raw": {"fixture": True},
            }
        ]

    def fetch_industry(self, asof_date: str) -> list[dict[str, Any]]:
        return [
            {
                "code": code,
                "industry": "银行",
                "industry_level": "fixture",
                "asof_date": asof_date,
                "source": self.name,
                "source_version": self.source_version,
                "fetched_at": now_iso(),
                "raw": {"fixture": True},
            }
            for code in ("000001", "000002", "000003")
        ]


def _seed_industry_strength_fixture(db: PITDatabase) -> None:
    fetched_at = now_iso()
    asof = "2020-04-30"
    universe_rows = []
    industry_rows = []
    price_rows = []
    for idx in range(25):
        code = f"600{idx:03d}"
        industry = "行业A" if idx < 12 else ("行业B" if idx < 24 else "小样本行业")
        universe_rows.append(
            {
                "code": code,
                "name": f"样例{idx}",
                "exchange": "sh",
                "ipo_date": "2010-01-01",
                "out_date": "",
                "status": "1",
                "is_index": 0,
                "asof_date": asof,
                "source": "fixture",
                "source_version": "test",
                "fetched_at": fetched_at,
                "raw": {},
            }
        )
        industry_rows.append(
            {
                "code": code,
                "industry": industry,
                "industry_level": "fixture",
                "asof_date": asof,
                "source": "fixture",
                "source_version": "test",
                "fetched_at": fetched_at,
                "raw": {},
            }
        )
        for day in range(61):
            if idx < 12:
                close = 10 + day * 0.08 + (0.4 if idx % 4 else 0)
            elif idx < 24:
                close = 20 - day * 0.04 + (0.2 if idx % 5 else 0)
            else:
                close = 15 + day * 0.03
            trade_date = (datetime(2020, 3, 1) + timedelta(days=day)).date().isoformat()
            price_rows.append(
                {
                    "code": code,
                    "trade_date": trade_date,
                    "adj": "hfq",
                    "open": close * 0.99,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "close_raw": close,
                    "volume": 1000 + day,
                    "amount": close * 1000,
                    "turnover": 2.0,
                    "pe_ttm": 20,
                    "pb_mrq": 2,
                    "asof_date": trade_date,
                    "source": "fixture",
                    "source_version": "test",
                    "fetched_at": fetched_at,
                    "raw": {},
                }
            )
    db.upsert_universe(universe_rows)
    db.upsert_industries(industry_rows)
    db.upsert_prices(price_rows)


def test_industry_strength_report_scores_and_coverage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = PITDatabase(pit_db_path_for(Path(tmp)))
        _seed_industry_strength_fixture(db)
        report = build_industry_strength_report(db, "2020-04-30", source="fixture")
        assert report.available, report.status_message
        industry_a = report.by_industry["行业A"]
        industry_b = report.by_industry["行业B"]
        small = report.by_industry["小样本行业"]
        assert industry_a.score is not None and industry_b.score is not None
        assert industry_a.score > industry_b.score
        assert industry_a.rank < industry_b.rank
        assert industry_a.sample_count == 12
        assert small.low_coverage is True
        assert small.score is None
        assert "600000" in report.by_code
        assert report.by_code["600000"].industry == "行业A"
        assert report.by_code["600000"].strength == industry_a.score


class FakeBaostockWorkerClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.restart_count = 0

    def set_event_sink(self, _sink) -> None:
        pass

    def close(self) -> None:
        pass

    def request(self, command: str, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((command, dict(kwargs)))
        if command == "login":
            return [{"status": "ok"}]
        if command == "trade_dates":
            return [
                {"calendar_date": "2024-01-05", "is_trading_day": "1"},
                {"calendar_date": "2024-01-06", "is_trading_day": "0"},
                {"calendar_date": "2024-01-07", "is_trading_day": "0"},
            ]
        if command == "all_stock":
            return [
                {"code": "sh.600519", "code_name": "贵州茅台", "tradeStatus": "1"},
                {"code": "sz.000001", "code_name": "平安银行", "tradeStatus": "1"},
                {"code": "sh.000001", "code_name": "上证指数", "tradeStatus": "1"},
            ]
        if command == "stock_basic":
            if not kwargs.get("code"):
                raise RuntimeError("full stock_basic should not be called")
            return [{"code": kwargs["code"], "code_name": "贵州茅台"}]
        if command == "profit":
            return [{"code": kwargs["code"], "pubDate": "2024-04-30", "statDate": "2024-03-31"}]
        raise RuntimeError(f"unexpected command: {command}")


class EmptyLatestAllStockWorker(FakeBaostockWorkerClient):
    def request(self, command: str, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        if command == "trade_dates":
            self.calls.append((command, dict(kwargs)))
            return [
                {"calendar_date": "2024-01-04", "is_trading_day": "1"},
                {"calendar_date": "2024-01-05", "is_trading_day": "1"},
            ]
        if command == "all_stock" and kwargs.get("day") == "2024-01-05":
            self.calls.append((command, dict(kwargs)))
            return []
        return super().request(command, kwargs)


class FailingUniverseProvider(FakePITProvider):
    name = "baostock"

    def fetch_universe(self, asof_date: str) -> list[dict[str, Any]]:
        raise ProviderUnavailable("primary universe timeout", "SOURCE_UNREACHABLE")


class FallbackUniverseProvider(FakePITProvider):
    name = "tushare"


class BackoffUniverseProvider(FakePITProvider):
    """universe 落在比请求日更早的交易日（模拟 baostock all_stock 当天未发布而回退）。

    用于守护 1a：industry 的 asof 必须复用 universe 解析出的交易日，否则两表 asof 错位、
    _industry_coverage 会假阴性为 0。
    """

    backoff_asof = "2020-01-02"

    def fetch_universe(self, asof_date: str) -> list[dict[str, Any]]:
        rows = super().fetch_universe(asof_date)
        for row in rows:
            row["asof_date"] = self.backoff_asof
        return rows


class IndustryLabelWorker(FakeBaostockWorkerClient):
    """industry 返回一行真实行业、一行空行业（仅有分类体系名），守护 label-leak 修复（1c）。"""

    def request(self, command: str, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        if command == "industry":
            self.calls.append((command, dict(kwargs)))
            return [
                {"code": "sh.600000", "industry": "银行", "industryClassification": "证监会行业分类"},
                {"code": "sh.600001", "industry": "", "industryClassification": "证监会行业分类"},
            ]
        return super().request(command, kwargs)


class FinancialMetricsWorker(FakeBaostockWorkerClient):
    def request(self, command: str, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((command, dict(kwargs)))
        if command == "profit":
            if kwargs.get("year") == 2019 and kwargs.get("quarter") == 1:
                return [
                    {
                        "code": kwargs["code"],
                        "pubDate": "2019-04-30",
                        "statDate": "2019-03-31",
                        "MBRevenue": "100.0",
                    }
                ]
            if kwargs.get("year") == 2020 and kwargs.get("quarter") == 1:
                return [
                    {
                        "code": kwargs["code"],
                        "pubDate": "2020-04-30",
                        "statDate": "2020-03-31",
                        "roeAvg": "0.15",
                        "npMargin": "0.20",
                        "gpMargin": "0.30",
                        "netProfit": "10.0",
                        "epsTTM": "1.5",
                        "MBRevenue": "125.0",
                    }
                ]
            return []
        if command == "growth":
            if kwargs.get("year") == 2020 and kwargs.get("quarter") == 1:
                return [
                    {
                        "code": kwargs["code"],
                        "pubDate": "2020-04-30",
                        "statDate": "2020-03-31",
                        "YOYNI": "0.12",
                    }
                ]
            return []
        if command == "balance":
            if kwargs.get("year") == 2020 and kwargs.get("quarter") == 1:
                return [
                    {
                        "code": kwargs["code"],
                        "pubDate": "2020-04-30",
                        "statDate": "2020-03-31",
                        "liabilityToAsset": "0.45",
                    }
                ]
            return []
        return super().request(command, kwargs)


class PriceMergeWorker(FakeBaostockWorkerClient):
    def request(self, command: str, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((command, dict(kwargs)))
        if command == "prices" and kwargs.get("adjustflag") == "1":
            return [
                {"date": "2024-01-02", "code": kwargs["code"], "open": "100", "high": "110", "low": "90", "close": "105", "volume": "1000", "amount": "10000", "turn": "1.2", "peTTM": "12", "pbMRQ": "1.1"},
                {"date": "2024-01-03", "code": kwargs["code"], "open": "106", "high": "112", "low": "101", "close": "108", "volume": "1200", "amount": "13000", "turn": "1.4", "peTTM": "13", "pbMRQ": "1.2"},
            ]
        if command == "prices" and kwargs.get("adjustflag") == "3":
            return [
                {"date": "2024-01-02", "code": kwargs["code"], "close": "52.5"},
                {"date": "2024-01-04", "code": kwargs["code"], "close": "54.0"},
            ]
        return super().request(command, kwargs)


class FakeDataFrame:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict[str, Any]]:
        assert orient == "records"
        return self.rows


class FakeTushareModule:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def pro_bar(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(dict(kwargs))
        if kwargs.get("adj") == "hfq":
            return FakeDataFrame(
                [
                    {"trade_date": "20240102", "open": 100, "high": 110, "low": 90, "close": 105, "vol": 1000, "amount": 10000},
                    {"trade_date": "20240103", "open": 106, "high": 112, "low": 101, "close": 108, "vol": 1200, "amount": 13000},
                ]
            )
        return FakeDataFrame(
            [
                {"trade_date": "20240102", "close": 52.5},
                {"trade_date": "20240104", "close": 54.0},
            ]
        )


class MockTusharePITProvider(TusharePITProvider):
    def __init__(self, fake_ts: FakeTushareModule) -> None:
        super().__init__(token="fixture")
        self.fake_ts = fake_ts

    def _pro(self):
        return object(), self.fake_ts


def _count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _price_row(db_path: Path, code: str, trade_date: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM pit_prices WHERE code = ? AND trade_date = ?",
            (code, trade_date),
        ).fetchone()
        assert row is not None
        return row
    finally:
        conn.close()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 4))
        self.now += seconds


def test_request_protector_clock_retry_and_circuit() -> None:
    clock = FakeClock()
    events = []
    profile = ProtectionProfile("test", min_interval_sec=0.8, retry_delays_sec=(2.0, 5.0, 10.0), jitter_ratio=0.2, max_consecutive_retryable_failures=4, cooldown_sec=60.0)
    protector = RequestProtector(
        "fake",
        profile,
        sleep_func=clock.sleep,
        monotonic_func=clock.monotonic,
        random_func=lambda: 1.0,
        event_sink=events.append,
    )
    assert protector.call("one", lambda: "ok") == "ok"
    assert protector.call("two", lambda: "ok") == "ok"
    assert clock.sleeps == [0.8], clock.sleeps

    attempts = {"count": 0}

    def fail_once_then_ok():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary connection closed")
        return "ok"

    assert protector.call("three", fail_once_then_ok, error_classifier=lambda _exc: "SOURCE_UNREACHABLE") == "ok"
    assert 2.4 in clock.sleeps, clock.sleeps
    assert any(event.kind == "retry" for event in events)

    circuit_clock = FakeClock()
    circuit_events = []
    circuit = RequestProtector(
        "fake",
        profile,
        sleep_func=circuit_clock.sleep,
        monotonic_func=circuit_clock.monotonic,
        random_func=lambda: 0.5,
        event_sink=circuit_events.append,
    )
    try:
        circuit.call(
            "bad",
            lambda: (_ for _ in ()).throw(RuntimeError("remote disconnected")),
            error_classifier=lambda _exc: "SOURCE_UNREACHABLE",
        )
    except SourceCircuitOpen as exc:
        assert exc.consecutive_failures == 4
    else:
        raise AssertionError("expected circuit to open")
    assert [event.kind for event in circuit_events].count("request_failed") == 4
    assert any(event.kind == "cooldown" for event in circuit_events)
    assert any(event.kind == "circuit_open" for event in circuit_events)


def test_deterministic_sample_is_uniform() -> None:
    values = [f"{index:06d}" for index in range(10)]
    assert _deterministic_sample(values, 4) == ["000000", "000003", "000006", "000009"]


def test_schema_migration_adds_close_raw() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "pit.sqlite3"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE pit_prices (
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    adj TEXT NOT NULL DEFAULT 'hfq',
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    amount REAL,
                    turnover REAL,
                    pe_ttm REAL,
                    pb_mrq REAL,
                    asof_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(code, trade_date, adj, source)
                )
                """
            )
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        finally:
            conn.close()
        PITDatabase(db_path)
        conn = sqlite3.connect(db_path)
        try:
            assert "close_raw" in _columns(db_path, "pit_prices")
            assert conn.execute("PRAGMA user_version").fetchone()[0] == PIT_SCHEMA_VERSION
        finally:
            conn.close()


def test_price_upsert_writes_and_updates_close_raw() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = PITDatabase(Path(tmp) / "pit.sqlite3")
        base = {
            "code": "000001",
            "trade_date": "2020-01-02",
            "adj": "hfq",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 20.0,
            "close_raw": 10.0,
            "asof_date": "2020-01-02",
            "source": "fixture",
            "source_version": "test",
            "fetched_at": now_iso(),
            "raw": {},
        }
        assert db.upsert_prices([base]) == 1
        updated = dict(base)
        updated["close"] = 22.0
        updated["close_raw"] = 11.0
        assert db.upsert_prices([updated]) == 1
        assert _count(db.db_path, "pit_prices") == 1
        row = _price_row(db.db_path, "000001", "2020-01-02")
        assert row["close"] == 22.0
        assert row["close_raw"] == 11.0


def test_derive_close_raw_from_back_adjust_factor() -> None:
    factors = (["2020-01-10", "2020-02-10"], [2.0, 4.0])
    assert derive_close_raw(20.0, "2020-01-02", factors) == 10.0
    assert derive_close_raw(20.0, "2020-01-10", factors) == 10.0
    assert derive_close_raw(20.0, "2020-01-20", factors) == 10.0
    assert derive_close_raw(20.0, "2020-03-01", factors) == 5.0
    assert derive_close_raw(20.0, "2020-01-20", None) == 20.0
    assert derive_close_raw(20.0, "2020-01-20", (["2020-01-01"], [0.0])) is None
    assert derive_close_raw(None, "2020-01-20", factors) is None


def test_baostock_universe_uses_all_stock_not_full_stock_basic() -> None:
    worker = FakeBaostockWorkerClient()
    provider = BaostockPITProvider(timeout_sec=5, worker_client=worker)
    rows = provider.fetch_universe("2024-01-07")
    commands = [command for command, _kwargs in worker.calls]
    assert commands == ["trade_dates", "all_stock"], commands
    assert all(not (command == "stock_basic" and not kwargs.get("code")) for command, kwargs in worker.calls)
    assert provider.latest_trade_day_value == "2024-01-05"
    assert provider.universe_method == "baostock.query_all_stock"
    assert {row["code"] for row in rows} == {"600519", "000001"}
    assert all(row["ipo_date"] == "" and row["out_date"] == "" for row in rows)


def test_baostock_healthcheck_uses_single_stock_basic_and_profit() -> None:
    worker = FakeBaostockWorkerClient()
    provider = BaostockPITProvider(timeout_sec=5, worker_client=worker)
    payload = provider.healthcheck("2024-01-01", "2024-01-07")
    commands = [command for command, _kwargs in worker.calls]
    assert commands == ["login", "trade_dates", "all_stock", "stock_basic", "profit"], commands
    stock_basic_kwargs = [kwargs for command, kwargs in worker.calls if command == "stock_basic"][0]
    assert stock_basic_kwargs["code"] == "sh.600519"
    assert payload["healthcheck_status"] == "GO"
    assert payload["latest_trade_day"] == "2024-01-05"
    assert payload["universe_rows"] == 3


def test_baostock_all_stock_backs_off_when_latest_snapshot_empty() -> None:
    worker = EmptyLatestAllStockWorker()
    provider = BaostockPITProvider(timeout_sec=5, worker_client=worker)
    rows = provider.fetch_universe("2024-01-05")
    all_stock_days = [kwargs["day"] for command, kwargs in worker.calls if command == "all_stock"]
    assert all_stock_days == ["2024-01-05", "2024-01-04"], all_stock_days
    assert provider.latest_trade_day_value == "2024-01-04"
    assert rows


def test_industry_coverage_survives_universe_asof_backoff() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        db = PITDatabase(pit_db_path_for(data_dir))
        provider = BackoffUniverseProvider()
        builder = PITDatasetBuilder(db, provider)
        result = builder.run(mode="full", start_date="2020-01-02", end_date="2020-01-03")
        assert result.status in {"completed", "completed_with_errors"}, result
        conn = sqlite3.connect(db.db_path)
        try:
            uni_asof = conn.execute("SELECT DISTINCT asof_date FROM pit_universe").fetchall()
            ind_asof = conn.execute("SELECT DISTINCT asof_date FROM pit_industry").fetchall()
        finally:
            conn.close()
        # 1a：industry 复用 universe 解析出的 asof（2020-01-02），而非请求日 end_date（2020-01-03）
        assert uni_asof == [("2020-01-02",)], uni_asof
        assert ind_asof == [("2020-01-02",)], ind_asof
        # 1b：覆盖率不再因 asof 错位假阴性为 0
        coverage = db.stats().industry_coverage
        assert coverage == 1.0, coverage


def test_industry_drops_classification_label_leak() -> None:
    worker = IndustryLabelWorker()
    provider = BaostockPITProvider(timeout_sec=5, worker_client=worker)
    rows = provider.fetch_industry("2024-01-05")
    by_code = {row["code"]: row for row in rows}
    assert by_code["600000"]["industry"] == "银行", by_code["600000"]
    # 空行业不得被分类体系名（industry_level 标签）填充
    assert by_code["600001"]["industry"] == "", by_code["600001"]
    assert all(row["industry"] != "证监会行业分类" for row in rows), rows


def test_baostock_financials_emit_canonical_percent_metrics() -> None:
    worker = FinancialMetricsWorker()
    provider = BaostockPITProvider(timeout_sec=5, worker_client=worker)
    rows = provider.fetch_financials("600519", "2020-01-01", "2020-03-31")
    values = {row["metric"]: row["value"] for row in rows}
    assert values["roe"] == 15.0, values
    assert values["net_margin"] == 20.0, values
    assert values["gross_margin"] == 30.0, values
    assert values["debt_ratio"] == 45.0, values
    assert values["profit_growth"] == 12.0, values
    assert values["revenue_growth"] == 25.0, values
    assert values["net_profit"] == 10.0, values
    assert values["eps_ttm"] == 1.5, values
    assert values["revenue"] == 125.0, values


def test_baostock_prices_merge_hfq_and_raw_by_trade_date() -> None:
    worker = PriceMergeWorker()
    provider = BaostockPITProvider(timeout_sec=5, worker_client=worker)
    rows = provider.fetch_prices("600519", "2024-01-02", "2024-01-04")
    assert [kwargs["adjustflag"] for command, kwargs in worker.calls if command == "prices"] == ["1", "3"]
    assert [row["trade_date"] for row in rows] == ["2024-01-02", "2024-01-03"]
    assert rows[0]["close"] == 105.0
    assert rows[0]["close_raw"] == 52.5
    assert rows[1]["close"] == 108.0
    assert rows[1]["close_raw"] is None
    assert all(row["adj"] == "hfq" for row in rows)


def test_tushare_prices_merge_hfq_and_raw_by_trade_date() -> None:
    fake_ts = FakeTushareModule()
    provider = MockTusharePITProvider(fake_ts)
    rows = provider.fetch_prices("600519", "2024-01-02", "2024-01-04")
    assert [call["adj"] for call in fake_ts.calls] == ["hfq", None]
    assert [row["trade_date"] for row in rows] == ["2024-01-02", "2024-01-03"]
    assert rows[0]["close"] == 105.0
    assert rows[0]["close_raw"] == 52.5
    assert rows[1]["close"] == 108.0
    assert rows[1]["close_raw"] is None
    assert all(row["adj"] == "hfq" for row in rows)


def test_universe_asof_uses_lifecycle_dates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        db = PITDatabase(pit_db_path_for(data_dir))
        fetched_at = now_iso()
        db.upsert_universe(
            [
                {
                    "code": "000001",
                    "name": "长期在市",
                    "exchange": "sz",
                    "ipo_date": "1991-01-01",
                    "out_date": "",
                    "status": "1",
                    "is_index": 0,
                    "asof_date": "2026-06-16",
                    "source": "fixture",
                    "source_version": "test",
                    "fetched_at": fetched_at,
                    "raw": {"fixture": True},
                },
                {
                    "code": "000002",
                    "name": "后续退市",
                    "exchange": "sz",
                    "ipo_date": "1991-01-01",
                    "out_date": "2020-01-02",
                    "status": "0",
                    "is_index": 0,
                    "asof_date": "2026-06-16",
                    "source": "fixture",
                    "source_version": "test",
                    "fetched_at": fetched_at,
                    "raw": {"fixture": True},
                },
                {
                    "code": "000003",
                    "name": "尚未上市",
                    "exchange": "sz",
                    "ipo_date": "2020-01-05",
                    "out_date": "",
                    "status": "1",
                    "is_index": 0,
                    "asof_date": "2026-06-16",
                    "source": "fixture",
                    "source_version": "test",
                    "fetched_at": fetched_at,
                    "raw": {"fixture": True},
                },
                {
                    "code": "000004",
                    "name": "未知上市日",
                    "exchange": "sz",
                    "ipo_date": "",
                    "out_date": "",
                    "status": "1",
                    "is_index": 0,
                    "asof_date": "2026-06-16",
                    "source": "fixture",
                    "source_version": "test",
                    "fetched_at": fetched_at,
                    "raw": {"fixture": True},
                },
            ]
        )
        assert {row["code"] for row in db.universe_asof("2020-01-01", source="fixture")} == {"000001", "000002"}
        assert {row["code"] for row in db.universe_asof("2020-01-03", source="fixture")} == {"000001"}
        assert {row["code"] for row in db.universe_asof("2020-01-05", source="fixture")} == {"000001", "000003"}


def test_retry_failures_recovers_multi_stage_partial_failures() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        db = PITDatabase(pit_db_path_for(data_dir))
        provider = FlakyPITProvider()
        builder = PITDatasetBuilder(db, provider)

        first = builder.run(mode="full", start_date="2020-01-02", end_date="2020-01-03")
        assert first.status == "completed_with_errors", first
        assert _count(db.db_path, "pit_prices") == 1
        assert _count(db.db_path, "pit_financials") == 1
        assert db.stats().failed_logs == 4
        failed_states = db.list_failed_fetch_states(provider.name, "2020-01-02", "2020-01-03")
        assert {(state.stage, state.target) for state in failed_states} == {
            ("prices", "000002"),
            ("prices", "000003"),
            ("financials", "000001"),
            ("financials", "000003"),
        }

        provider.price_calls.clear()
        provider.financial_calls.clear()
        retry = builder.run(mode="retry-failures", start_date="2020-01-02", end_date="2020-01-03")
        assert retry.status == "completed", retry
        assert _count(db.db_path, "pit_prices") == 3
        assert _count(db.db_path, "pit_financials") == 3
        assert set(provider.price_calls) == {"000002", "000003"}, provider.price_calls
        assert set(provider.financial_calls) == {"000001", "000003"}, provider.financial_calls
        assert db.list_failed_fetch_states(provider.name, "2020-01-02", "2020-01-03") == []

        second_full = builder.run(mode="full", start_date="2020-01-02", end_date="2020-01-03")
        assert second_full.status == "completed", second_full


def test_mark_stale_running_jobs_interrupts_only_old_running() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = PITDatabase(Path(tmp) / "pit.sqlite3")
        old_running = db.create_job("full", "2020-01-02", "2020-01-03", params={"provider": "baostock"})
        recent_running = db.create_job("preflight", "2020-01-02", "2020-01-03", params={"provider": "baostock"})
        completed = db.create_job("healthcheck", "2020-01-02", "2020-01-03", params={"provider": "baostock"})
        db.update_job(completed, status="completed", message="ok", completed=True)
        old_updated = (datetime.now().astimezone() - timedelta(minutes=180)).isoformat(timespec="seconds")
        with db.connect() as conn:
            conn.execute("UPDATE pit_jobs SET updated_at = ? WHERE id = ?", (old_updated, old_running))

        assert db.mark_stale_running_jobs(stale_after_minutes=120) == 1
        jobs = {job.id: job for job in db.list_recent_jobs(limit=10)}
        assert jobs[old_running].status == "interrupted"
        assert "中断" in jobs[old_running].message
        assert jobs[recent_running].status == "running"
        assert jobs[completed].status == "completed"
        assert db.mark_stale_running_jobs(stale_after_minutes=120) == 0


def test_format_pit_status_for_user_translates_actionable_states() -> None:
    cooldown = format_pit_status_for_user("preflight", "SOURCE_COOLDOWN", "source_cooldown: baostock request cooled down")
    assert "免费源冷却" in cooldown
    assert "全量建库" in cooldown
    assert "Tushare Token" in cooldown
    assert "原始信息" in cooldown

    unreachable = format_pit_status_for_user("healthcheck", "SOURCE_UNREACHABLE", "baostock login failed")
    assert "免费源暂时连不上" in unreachable
    assert "Baostock 登录失败" in unreachable

    env = format_pit_status_for_user("full", "ENV_UNAVAILABLE", "baostock not installed")
    assert "当前环境还不能运行全量建库" in env
    assert "缺少 baostock 依赖" in env

    preflight_failed = format_pit_status_for_user("preflight", "preflight_failed", "PIT preflight 完成；失败率 8.0%。")
    assert "50 只预检未通过" in preflight_failed
    assert "已停止全量建库" in preflight_failed

    completed_with_errors = format_pit_status_for_user("full", "completed_with_errors", "失败 2，成功 98", token_available=True)
    assert "已完成，但有部分股票或字段失败" in completed_with_errors
    assert "重试失败项" in completed_with_errors


def run_tests() -> None:
    test_request_protector_clock_retry_and_circuit()
    test_deterministic_sample_is_uniform()
    test_schema_migration_adds_close_raw()
    test_price_upsert_writes_and_updates_close_raw()
    test_derive_close_raw_from_back_adjust_factor()
    test_baostock_universe_uses_all_stock_not_full_stock_basic()
    test_baostock_healthcheck_uses_single_stock_basic_and_profit()
    test_baostock_all_stock_backs_off_when_latest_snapshot_empty()
    test_industry_coverage_survives_universe_asof_backoff()
    test_industry_strength_report_scores_and_coverage()
    test_industry_drops_classification_label_leak()
    test_baostock_financials_emit_canonical_percent_metrics()
    test_baostock_prices_merge_hfq_and_raw_by_trade_date()
    test_tushare_prices_merge_hfq_and_raw_by_trade_date()
    test_universe_asof_uses_lifecycle_dates()
    test_retry_failures_recovers_multi_stage_partial_failures()
    test_mark_stale_running_jobs_interrupts_only_old_running()
    test_format_pit_status_for_user_translates_actionable_states()
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        db = PITDatabase(pit_db_path_for(data_dir))
        provider = FakePITProvider()
        builder = PITDatasetBuilder(db, provider)
        conn = sqlite3.connect(db.db_path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == PIT_SCHEMA_VERSION
        finally:
            conn.close()

        first = builder.run(mode="full", start_date="2020-01-02", end_date="2020-01-03")
        assert first.status == "completed_with_errors", first
        assert _count(db.db_path, "pit_prices") == 1
        assert _price_row(db.db_path, "000001", "2020-01-02")["close_raw"] == 5.25
        assert db.stats().failed_logs == 1
        assert provider.financial_calls == ["000001", "000002"]

        db.record_fetch_state(
            job_id=None,
            stage="prices",
            target="999999",
            source=provider.name,
            start_date="2020-02-01",
            end_date="2020-02-02",
            status="failed",
            error_class="SOURCE_UNREACHABLE",
            message="other interval",
        )
        db.record_fetch_state(
            job_id=None,
            stage="prices",
            target="888888",
            source="other",
            start_date="2020-01-02",
            end_date="2020-01-03",
            status="failed",
            error_class="SOURCE_UNREACHABLE",
            message="other source",
        )

        provider.price_calls.clear()
        retry = builder.run(mode="retry-failures", start_date="2020-01-02", end_date="2020-01-03")
        assert retry.status == "completed", retry
        assert _count(db.db_path, "pit_prices") == 2
        assert provider.price_calls == ["000002"], provider.price_calls

        financial_calls_before = list(provider.financial_calls)
        second_full = builder.run(mode="full", start_date="2020-01-02", end_date="2020-01-03")
        assert second_full.status == "completed", second_full
        assert _count(db.db_path, "pit_prices") == 2, "price upsert/resume should not duplicate rows"
        assert _count(db.db_path, "pit_financials") == 2, "financial upsert should be idempotent per code/pub_date/metric"
        assert _count(db.db_path, "pit_universe") == 2
        assert _count(db.db_path, "pit_industry") == 2
        assert provider.financial_calls == financial_calls_before, "successful financial interval should be skipped"

        refresh = builder.run(mode="full", start_date="2020-01-02", end_date="2020-01-03", refresh_existing=True)
        assert refresh.status == "completed", refresh
        assert provider.financial_calls[-2:] == ["000001", "000002"], "refresh-existing should refetch financials"

        before_pub = db.financials_visible_asof("000001", "2020-04-29")
        after_pub = db.financials_visible_asof("000001", "2020-04-30")
        assert before_pub == [], before_pub
        assert len(after_pub) == 1 and after_pub[0]["metric"] == "roe", after_pub

        jobs = db.list_recent_jobs()
        assert jobs[0].status == "completed"
        assert {job.mode for job in jobs[:3]} == {"full", "retry-failures"}
        assert Path(jobs[0].report_path).exists()
        assert db.list_failed_fetch_states(provider.name, "2020-01-02", "2020-01-03") == []

        healthcheck = builder.run(mode="healthcheck", start_date="2020-01-02", end_date="2020-01-03")
        assert healthcheck.status == "completed", healthcheck
        report_text = Path(healthcheck.report_path).read_text(encoding="utf-8")
        assert "Healthcheck：GO" in report_text
        assert "Universe 方法：fake.healthcheck" in report_text

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        db = PITDatabase(pit_db_path_for(data_dir))
        provider = FailingUniverseProvider()
        fallback = FallbackUniverseProvider()
        builder = PITDatasetBuilder(db, provider, fallback_providers={"tushare": fallback})
        result = builder.run(mode="preflight", start_date="2020-01-02", end_date="2020-01-03", stock_limit=1, universe_source="auto")
        assert result.status == "completed", result
        params = db.get_job_params(result.job_id)
        assert params["universe_source"] == "tushare", params
        logs = db.list_recent_logs(limit=20)
        assert any(log.status == "universe_fallback" and log.source == "tushare" for log in logs)

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        db = PITDatabase(pit_db_path_for(data_dir))
        provider = FakePITProvider()
        builder = PITDatasetBuilder(db, provider)
        result = builder.run(mode="preflight", start_date="2020-01-02", end_date="2020-01-03", universe_source="cache")
        assert result.status == "preflight_failed", result
        assert any("cache" in log.message for log in db.list_failed_logs(limit=10))

    print("[OK] PIT pipeline tests passed")


if __name__ == "__main__":
    run_tests()
