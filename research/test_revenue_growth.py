from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import research.etl_fundamentals as etl_fundamentals
from research._testkit import run_tests
from research.metric_metadata import QUALITY_MISSING, QUALITY_PASS
from research.panel_db import DATA_DIR, ResearchPanelDB, open_panel
from research.pit_join import fundamentals_as_of

TEST_DB = DATA_DIR / "panel_revenue_growth_selftest.sqlite3"


class FakeBaostock:
    def query_profit_data(self, code: str, year: int, quarter: int) -> pd.DataFrame:
        if (year, quarter) == (2023, 1):
            return pd.DataFrame(
                [
                    {
                        "code": code,
                        "statDate": "2023-03-31",
                        "pubDate": "2023-04-30",
                        "roeAvg": "0.10",
                        "npMargin": "0.20",
                        "gpMargin": "0.30",
                        "MBRevenue": "100",
                    }
                ]
            )
        if (year, quarter) == (2024, 1):
            return pd.DataFrame(
                [
                    {
                        "code": code,
                        "statDate": "2024-03-31",
                        "pubDate": "2024-04-30",
                        "roeAvg": "0.11",
                        "npMargin": "0.21",
                        "gpMargin": "0.31",
                        "MBRevenue": "150",
                    }
                ]
            )
        if (year, quarter) == (2024, 2):
            return pd.DataFrame(
                [
                    {
                        "code": code,
                        "statDate": "2024-06-30",
                        "pubDate": "2024-08-30",
                        "roeAvg": "0.12",
                        "npMargin": "0.22",
                        "gpMargin": "0.32",
                        "MBRevenue": "",
                    }
                ]
            )
        return pd.DataFrame()

    def query_growth_data(self, code: str, year: int, quarter: int) -> pd.DataFrame:
        return pd.DataFrame()

    def query_balance_data(self, code: str, year: int, quarter: int) -> pd.DataFrame:
        return pd.DataFrame()


def _fresh_db() -> ResearchPanelDB:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(TEST_DB) + suffix)
        if p.exists():
            p.unlink()
    return open_panel(TEST_DB)


def test_revenue_growth_uses_later_pub_date_and_asof_visibility():
    original = etl_fundamentals.query_to_df
    etl_fundamentals.query_to_df = lambda factory: factory()
    try:
        rows = etl_fundamentals.collect_rows_for_code(
            FakeBaostock(),
            "sh.600001",
            start_year=2023,
            end_year=2024,
        )
    finally:
        etl_fundamentals.query_to_df = original

    growth = [row for row in rows if row["metric"] == "revenue_growth" and row["stat_date"] == "2024-03-31"][0]
    assert growth["pub_date"] == "2024-04-30"
    assert round(float(growth["value"]), 6) == 50.0
    assert growth["metric_quality"] == QUALITY_PASS

    db = _fresh_db()
    try:
        db.upsert("fundamentals_pit", rows, ["code", "stat_date", "metric", "pub_date"])
        before = fundamentals_as_of("2024-04-29", db, ["sh.600001"])
        after = fundamentals_as_of("2024-04-30", db, ["sh.600001"])
        before_target = before[
            (before["metric"] == "revenue_growth") & (before["stat_date"] == "2024-03-31")
        ] if not before.empty else before
        after_rows = after[after["metric"] == "revenue_growth"]
        assert before_target.empty
        assert len(after_rows) == 1
        assert after_rows.iloc[0]["stat_date"] == "2024-03-31"
        assert round(float(after_rows.iloc[0]["value"]), 6) == 50.0
    finally:
        db.close()


def test_revenue_growth_missing_mbrevenue_gets_reason_not_zero():
    original = etl_fundamentals.query_to_df
    etl_fundamentals.query_to_df = lambda factory: factory()
    try:
        rows = etl_fundamentals.collect_rows_for_code(
            FakeBaostock(),
            "sh.600001",
            start_year=2023,
            end_year=2024,
        )
    finally:
        etl_fundamentals.query_to_df = original

    missing = [row for row in rows if row["metric"] == "revenue_growth" and row["stat_date"] == "2024-06-30"][0]
    assert missing["value"] is None
    assert missing["metric_quality"] == QUALITY_MISSING
    assert missing["missing_reason"] == "current_mbrevenue_missing"


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
