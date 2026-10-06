from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research._testkit import run_tests
from research.attribution_postmortem import generate_attribution
from research.panel_db import DATA_DIR, ResearchPanelDB, open_panel

TEST_DB = DATA_DIR / "attribution_selftest.sqlite3"
TEST_LEDGER = DATA_DIR / "attribution_selftest_ledger.json"


def _fresh_db() -> ResearchPanelDB:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(TEST_DB) + suffix)
        if path.exists():
            path.unlink()
    db = open_panel(TEST_DB)
    rows = []
    for date, a_close, b_close, c_close in [
        ("2025-01-02", 10, 10, 10),
        ("2025-01-30", 8, 9, 12),
        ("2025-02-03", 10, 10, 10),
        ("2025-02-28", 11, 8, 10),
    ]:
        rows.extend(
            [
                _price("A", date, a_close, amount=10, turn=10),
                _price("B", date, b_close, amount=1000, turn=1),
                _price("C", date, c_close, amount=500, turn=1),
            ]
        )
    db.upsert("prices_daily", rows, ["code", "date"])
    db.upsert(
        "industry_map",
        [
            {"code": "A", "industry": "weak", "industry_classification": "x", "update_date": "2025-01-01", "query_date": "2025-01-01", "fetched_at": "t"},
            {"code": "B", "industry": "weak", "industry_classification": "x", "update_date": "2025-01-01", "query_date": "2025-01-01", "fetched_at": "t"},
            {"code": "C", "industry": "strong", "industry_classification": "x", "update_date": "2025-01-01", "query_date": "2025-01-01", "fetched_at": "t"},
        ],
        ["code", "query_date"],
    )
    return db


def _price(code: str, date: str, close: float, *, amount: float, turn: float) -> dict:
    return {
        "code": code,
        "date": date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "preclose": close,
        "volume": 1,
        "amount": amount,
        "turn": turn,
        "pe_ttm": 1,
        "pb_mrq": 1,
        "ps_ttm": 1,
        "pcf_ncf_ttm": 1,
        "trade_status": "1",
        "fetched_at": "t",
    }


def _write_ledger() -> None:
    payload = {
        "winner": "fixture",
        "holdout_window": ["2025-01-01", "2025-02-28"],
        "result_summary": {"periods": 2, "mean": -0.1},
        "period_rows": [
            {
                "rebalance_date": "2025-01-02",
                "exit_date": "2025-01-30",
                "portfolio_return": -0.15,
                "benchmark_return": 0.00,
                "excess_return": -0.15,
                "selected_codes": "A,B",
            },
            {
                "rebalance_date": "2025-02-03",
                "exit_date": "2025-02-28",
                "portfolio_return": -0.05,
                "benchmark_return": 0.00,
                "excess_return": -0.05,
                "selected_codes": "A,C",
            },
        ],
    }
    TEST_LEDGER.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_attribution_marks_worst_period_and_repeated_code():
    db = _fresh_db()
    try:
        _write_ledger()
        payload = generate_attribution(TEST_LEDGER, db)
        assert payload["worst_periods"][0]["rebalance_date"] == "2025-01-02"
        assert payload["top_repeated_codes"][0] == {"code": "A", "selected_periods": 2}
        assert payload["period_details"][0]["largest_20pct_share"] == 0.5
        assert abs(payload["excess_sum_check"]["sum_period_excess"] + 0.2) < 1e-12
    finally:
        db.close()
        if TEST_LEDGER.exists():
            TEST_LEDGER.unlink()


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
