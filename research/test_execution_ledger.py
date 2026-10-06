"""Offline acceptance for the trade-level execution ledger."""

from __future__ import annotations

from research.event_backtest import run_ledger_backtest
from research.execution_ledger import ExecutionPolicy, OrderIntent


def test_t_plus_one_and_asset_identity() -> None:
    policy = ExecutionPolicy(initial_cash=100_000, max_position_weight=1.0)
    buy = OrderIntent("2026-07-23", "2026-07-23", "2026-07-24", "000001", "buy", 100)
    sell_same_day = OrderIntent("2026-07-24", "2026-07-24", "2026-07-24", "000001", "sell", 100)
    sell_next_day = OrderIntent("2026-07-25", "2026-07-25", "2026-07-25", "000001", "sell", 100)
    ledger = run_ledger_backtest(
        {
            "2026-07-24": [buy, sell_same_day],
            "2026-07-25": [sell_next_day],
        },
        {
            "2026-07-24": {"000001": {"open": 10, "close": 10.5, "trade_status": "normal"}},
            "2026-07-25": {"000001": {"open": 11, "close": 11, "trade_status": "normal"}},
        },
        policy=policy,
    )
    assert [fill.trade_status for fill in ledger.fills] == ["filled", "failed", "filled"]
    assert ledger.fills[1].untradeable_reason == "t_plus_one"
    ledger.summary()
    assert ledger.daily_equity[0].total_equity == ledger.daily_equity[0].cash + ledger.daily_equity[0].holdings_value


def test_limit_and_missing_market_are_audited() -> None:
    policy = ExecutionPolicy(initial_cash=100_000, max_position_weight=1.0)
    buy = OrderIntent("2026-07-23", "2026-07-23", "2026-07-24", "000001", "buy", 100)
    missing = OrderIntent("2026-07-23", "2026-07-23", "2026-07-24", "000002", "buy", 100)
    ledger = run_ledger_backtest(
        {"2026-07-24": [buy, missing]},
        {"2026-07-24": {"000001": {"open": 10, "close": 10, "trade_status": "normal", "limit_up": True}}},
        policy=policy,
    )
    assert {fill.untradeable_reason for fill in ledger.fills} == {"limit_up_buy_locked", "suspended_or_untradeable"}


if __name__ == "__main__":
    test_t_plus_one_and_asset_identity()
    test_limit_and_missing_market_are_audited()
    print("[OK] execution ledger tests passed")
