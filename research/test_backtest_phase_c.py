from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research._testkit import raises, run_tests
from research.backtest_engine import BacktestConfig, assert_no_future_pubdates, coverage_status, run_backtest
from research.backtest_report import _holdout_run_count, _select_winner
from research.metrics_robustness import (
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
    deflated_sharpe_ratio,
    pbo_cscv,
    return_summary,
)
from research.panel_db import DATA_DIR, ResearchPanelDB, open_panel
from research.pit_join import universe_as_of

TEST_DB = DATA_DIR / "phase_c_selftest.sqlite3"


def _fresh_db() -> ResearchPanelDB:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(TEST_DB) + suffix)
        if p.exists():
            p.unlink()
    db = open_panel(TEST_DB)
    db.upsert(
        "universe",
        [
            {"code": "A", "code_name": "Alpha", "ipo_date": "2018-01-01", "out_date": "", "type": "1", "status": "1", "fetched_at": "t"},
            {"code": "B", "code_name": "Beta", "ipo_date": "2018-01-01", "out_date": "", "type": "1", "status": "1", "fetched_at": "t"},
            {"code": "D", "code_name": "Delist", "ipo_date": "2018-01-01", "out_date": "2019-03-01", "type": "1", "status": "0", "fetched_at": "t"},
        ],
        ["code"],
    )
    days = [f"2019-01-{d:02d}" for d in range(1, 29)] + [f"2019-02-{d:02d}" for d in range(1, 29)]
    db.upsert("trade_calendar", [{"calendar_date": d, "is_trading_day": 1} for d in days], ["calendar_date"])
    price_rows = []
    for i, day in enumerate(days):
        price_rows.extend(
            [
                _price("A", day, 100 + i * 2, 100 + max(i - 1, 0) * 2),
                _price("B", day, 100 - i, 100 - max(i - 1, 0)),
                _price("D", day, 50 + i, 50 + max(i - 1, 0)),
            ]
        )
    db.upsert("prices_daily", price_rows, ["code", "date"])
    facts = []
    for code, roe in [("A", 90), ("B", 10), ("D", 50)]:
        for metric, value in {
            "roe": roe,
            "net_margin": roe,
            "gross_margin": roe,
            "profit_growth": roe,
            "revenue_growth": roe,
            "debt_ratio": 100 - roe,
        }.items():
            facts.append({"code": code, "stat_date": "2018-12-31", "pub_date": "2019-01-01", "metric": metric, "value": value, "source_api": "fixture", "fetched_at": "t"})
    db.upsert("fundamentals_pit", facts, ["code", "stat_date", "metric", "pub_date"])
    db.upsert(
        "industry_map",
        [
            {"code": "A", "industry": "x", "industry_classification": "x", "update_date": "2019-01-01", "query_date": "2019-01-01", "fetched_at": "t"},
            {"code": "B", "industry": "x", "industry_classification": "x", "update_date": "2019-01-01", "query_date": "2019-01-01", "fetched_at": "t"},
            {"code": "D", "industry": "x", "industry_classification": "x", "update_date": "2019-01-01", "query_date": "2019-01-01", "fetched_at": "t"},
        ],
        ["code", "query_date"],
    )
    return db


def _price(code: str, day: str, close: float, preclose: float) -> dict:
    return {
        "code": code,
        "date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "preclose": preclose,
        "volume": 1,
        "amount": 1,
        "turn": 1,
        "pe_ttm": 10 if code == "A" else 50,
        "pb_mrq": 1 if code == "A" else 5,
        "ps_ttm": 1,
        "pcf_ncf_ttm": 1,
        "trade_status": "1",
        "fetched_at": "t",
    }


def test_perfect_signal_beats_equal_weight_benchmark():
    db = _fresh_db()
    try:
        weights = {"fundamental": 0.6, "growth": 0.2, "safety": 0.1, "valuation": 0.1, "trend": 0}
        rows = run_backtest(weights, db, config=BacktestConfig(start_date="2019-01-01", end_date="2019-02-28", holding_days=5, top_n=1, commission_bps=0, stamp_tax_sell_bps=0, slippage_bps=0))
        assert not rows.empty
        assert (rows["excess_return"] > 0).all()
    finally:
        db.close()


def test_higher_cost_monotonically_reduces_returns():
    db = _fresh_db()
    try:
        weights = {"fundamental": 1.0, "growth": 0, "safety": 0, "valuation": 0, "trend": 0}
        low = run_backtest(weights, db, config=BacktestConfig(start_date="2019-01-01", end_date="2019-02-28", holding_days=5, top_n=1, commission_bps=0, stamp_tax_sell_bps=0, slippage_bps=0))
        high = run_backtest(weights, db, config=BacktestConfig(start_date="2019-01-01", end_date="2019-02-28", holding_days=5, top_n=1, commission_bps=20, stamp_tax_sell_bps=10, slippage_bps=20))
        assert float(high["portfolio_return"].sum()) < float(low["portfolio_return"].sum())
    finally:
        db.close()


def test_future_fact_guard_and_survivorship_guard():
    db = _fresh_db()
    try:
        facts = db.query_df("SELECT * FROM fundamentals_pit")
        assert_no_future_pubdates("2019-01-01", facts)
        future = facts.copy()
        future.loc[0, "pub_date"] = "2030-01-01"
        with raises(AssertionError):
            assert_no_future_pubdates("2019-01-01", future)
        before = set(universe_as_of("2019-02-15", db)["code"])
        after = set(universe_as_of("2019-03-15", db)["code"])
        assert "D" in before
        assert "D" not in after
    finally:
        db.close()


def test_coverage_status_blocks_sample_sized_db():
    db = _fresh_db()
    try:
        status = coverage_status(db)
        assert status["full_panel_ready"] is False
        assert status["hs300_status"] == "MISSING_DATA"
    finally:
        db.close()


def test_robustness_metrics_are_bounded():
    import pandas as pd

    returns = pd.Series([0.02, 0.01, -0.005, 0.03, 0.0, 0.01])
    summary = return_summary(returns)
    assert summary["periods"] == 6
    assert summary["period_sharpe"] is not None
    dsr = deflated_sharpe_ratio(returns, trials=5)
    assert dsr["dsr"] is None or 0.0 <= dsr["dsr"] <= 1.0
    pbo = pbo_cscv(pd.DataFrame({"a": returns, "b": -returns}))
    assert pbo["pbo"] is None or 0.0 <= pbo["pbo"] <= 1.0


def test_psr_uses_period_scale_not_annualization():
    import pandas as pd

    returns = pd.Series([0.01, -0.01] * 20)
    psr_monthly = probabilistic_sharpe_ratio(returns, benchmark_sharpe=0.0, periods_per_year=12)
    psr_daily = probabilistic_sharpe_ratio(returns, benchmark_sharpe=0.0, periods_per_year=252)
    assert abs(psr_monthly - 0.5) < 0.02
    assert psr_monthly == psr_daily


def test_dsr_single_trial_equals_psr_and_hurdle_rises():
    import pandas as pd

    returns = pd.Series([0.02, 0.01, -0.005, 0.03, 0.0, 0.01, 0.02, -0.01])
    psr = probabilistic_sharpe_ratio(returns, benchmark_sharpe=0.0)
    dsr_single = deflated_sharpe_ratio(returns, sharpe_estimates=[0.2])
    assert dsr_single["expected_max_sharpe"] == 0.0
    assert dsr_single["dsr"] == psr

    low_hurdle = expected_max_sharpe([0.0, 0.1, 0.2])
    high_dispersion = expected_max_sharpe([-0.5, 0.1, 0.7])
    more_trials = expected_max_sharpe([0.0, 0.1, 0.2], trials=10)
    assert high_dispersion > low_hurdle
    assert more_trials > low_hurdle
    assert deflated_sharpe_ratio(returns, sharpe_estimates=[-0.5, 0.1, 0.7])["dsr"] <= dsr_single["dsr"]


def test_expected_max_sharpe_formula_anchor():
    value = expected_max_sharpe([0.0, 0.1, 0.2])
    assert abs(value - 0.185280) < 0.00001


def test_pbo_cscv_detects_stable_and_reversed_winners():
    import pandas as pd

    stable = pd.DataFrame(
        {
            "winner": [0.03, 0.02, 0.03, 0.02, 0.03, 0.02, 0.03, 0.02],
            "loser": [0.00, -0.01, 0.00, -0.01, 0.00, -0.01, 0.00, -0.01],
        }
    )
    reversed_oos = pd.DataFrame(
        {
            "flip": [0.04, 0.02, 0.04, 0.02, -0.02, -0.04, -0.02, -0.04],
            "steady": [0.01, 0.00, 0.01, 0.00, 0.02, 0.01, 0.02, 0.01],
        }
    )
    assert pbo_cscv(stable, n_splits=4)["pbo"] == 0.0
    assert pbo_cscv(reversed_oos, n_splits=2)["pbo"] == 1.0


def test_holdout_winner_and_ledger_helpers():
    summaries = [
        {"candidate_id": "b", "validation": {"dsr": 0.2, "mean": 0.5}},
        {"candidate_id": "a", "validation": {"dsr": 0.2, "mean": 0.5}},
        {"candidate_id": "c", "validation": {"dsr": 0.3, "mean": 0.1}},
    ]
    assert _select_winner(summaries) == "c"
    assert _holdout_run_count(None) == 0
    assert _holdout_run_count({"winner": "c"}) == 1


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
