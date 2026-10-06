"""Event-semantics research backtest for the point-in-time panel.

Signals are formed after the signal date close and execute on the next
trading day's open.  The implementation is deliberately small and explicit;
it is a validation path, not a replacement for a full order-management engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from research.backtest_engine import _eligible_at_entry, _month_start_dates, _trading_dates
from research.factor_ic import _attach_derived_price_features
from research.panel_db import ResearchPanelDB, open_panel
from research.pit_join import as_of
from research.execution_ledger import (
    BacktestLedger,
    ExecutionLedgerEngine,
    ExecutionPolicy,
    OrderIntent,
)


@dataclass(frozen=True)
class EventBacktestConfig:
    start_date: str = "2019-01-01"
    end_date: str = "2026-06-16"
    holding_days: int = 20
    top_n: int = 50
    commission_bps: float = 3.0
    stamp_tax_sell_bps: float = 5.0
    slippage_bps: float = 5.0
    limit_lock_threshold: float = 0.095
    semantics_version: str = "next_open_to_holding_close_v1"
    cost_parameter_version: str = "cost-v1"
    data_snapshot_id: str = ""
    refresh_event_id: str = ""
    universe_version: str = ""
    formula_version: str = ""
    formal_mode: bool = False


ScoreFunction = Callable[[pd.DataFrame], pd.DataFrame]


def run_event_backtest(
    score_fn: ScoreFunction,
    db: ResearchPanelDB | None = None,
    *,
    config: EventBacktestConfig = EventBacktestConfig(),
) -> pd.DataFrame:
    """Run monthly signals with explicit next-open/exit-close semantics."""
    if config.holding_days < 1 or config.top_n < 1:
        raise ValueError("holding_days and top_n must be positive")
    if config.formal_mode:
        missing_identity = [
            name
            for name in ("data_snapshot_id", "refresh_event_id", "universe_version", "formula_version")
            if not str(getattr(config, name) or "").strip()
        ]
        if missing_identity:
            raise ValueError(f"正式事件回测缺少身份绑定：{', '.join(missing_identity)}")
    own = db is None
    db = db or open_panel()
    try:
        dates = _trading_dates(db, config.start_date, config.end_date)
        rows: list[dict] = []
        for signal_date in _month_start_dates(dates):
            execution_date = _offset_date(dates, signal_date, 1)
            if execution_date is None:
                continue
            exit_date = _offset_date(dates, execution_date, config.holding_days - 1)
            if exit_date is None:
                continue
            panel = as_of(signal_date, db)
            if panel.empty:
                continue
            panel = _attach_derived_price_features(panel, db, signal_date)
            eligible = _eligible_at_entry(panel, config.limit_lock_threshold)
            if eligible.empty:
                continue
            scored = score_fn(eligible.copy())
            if "code" not in scored.columns or "score" not in scored.columns:
                raise ValueError("score_fn must return code and score columns")
            picks = scored.sort_values(["score", "code"], ascending=[False, True]).head(config.top_n)["code"].astype(str).tolist()
            if not picks:
                continue

            ledger_summary: dict[str, Any] = {}
            if config.formal_mode:
                portfolio_return, selected_reasons, ledger_summary, n_traded = _formal_ledger_period(
                    db, picks, signal_date, execution_date, exit_date, config
                )
            else:
                selected_returns, selected_reasons = _returns_for_codes(db, picks, execution_date, exit_date, config)
                portfolio_return = float(pd.Series(selected_returns).mean()) if selected_returns else None
                n_traded = len(selected_returns)
            benchmark_returns, _ = _returns_for_codes(db, eligible["code"].astype(str).tolist(), execution_date, exit_date, config)
            benchmark_return = float(pd.Series(benchmark_returns).mean()) if benchmark_returns else None
            rows.append(
                {
                    "semantics_version": config.semantics_version,
                    "cost_parameter_version": config.cost_parameter_version,
                    "data_snapshot_id": config.data_snapshot_id,
                    "refresh_event_id": config.refresh_event_id,
                    "universe_version": config.universe_version,
                    "formula_version": config.formula_version,
                    "formal_mode": config.formal_mode,
                    "signal_date": signal_date,
                    "execution_date": execution_date,
                    "exit_date": exit_date,
                    "n_eligible": int(len(eligible)),
                    "n_selected": int(len(picks)),
                    "n_traded": int(n_traded),
                    "n_skipped": int(len(picks) - n_traded),
                    "skip_reasons": selected_reasons,
                    "portfolio_return": portfolio_return,
                    "benchmark_return": benchmark_return,
                    "excess_return": (
                        portfolio_return - benchmark_return
                        if portfolio_return is not None and benchmark_return is not None
                        else None
                    ),
                    "turnover": 2.0 * n_traded / max(len(picks), 1),
                    "selected_codes": ",".join(picks),
                    "execution_model": "trade_ledger_v1" if config.formal_mode else "period_return_smoke_v1",
                    "execution_ledger_summary": ledger_summary,
                }
            )
        return pd.DataFrame(rows)
    finally:
        if own:
            db.close()


def run_ledger_backtest(
    intents_by_date: dict[str, list[OrderIntent]],
    market_by_date: dict[str, dict[str, dict[str, object]]],
    *,
    policy: ExecutionPolicy = ExecutionPolicy(),
) -> BacktestLedger:
    """Execute explicit intents through the trade-level ledger.

    ``market_by_date[date][code]`` must contain a raw ``open`` and may include
    ``close``, ``trade_status``, ``limit_up``, ``limit_down`` and ``one_word``.
    A missing market row becomes a recorded failed order rather than silently
    disappearing from the result.
    """
    engine = ExecutionLedgerEngine(policy)
    dates = sorted(set(market_by_date) | set(intents_by_date))
    for index, trade_date in enumerate(dates):
        next_date = dates[index + 1] if index + 1 < len(dates) else None
        day_market = market_by_date.get(trade_date, {})
        for intent in intents_by_date.get(trade_date, []):
            market = day_market.get(str(intent.code).zfill(6))
            if market is None:
                market = {"trade_status": "missing"}
            engine.execute_next_open(
                intent,
                market,
                fill_time=trade_date,
                next_trading_date=next_date,
            )
        close_prices = {
            str(code).zfill(6): row.get("close")
            for code, row in day_market.items()
            if isinstance(row, dict) and row.get("close") is not None
        }
        engine.mark_to_market(trade_date, close_prices)
    engine.validate_invariants()
    return engine.ledger


def _returns_for_codes(
    db: ResearchPanelDB,
    codes: list[str],
    execution_date: str,
    exit_date: str,
    config: EventBacktestConfig,
) -> tuple[list[float], dict[str, int]]:
    if not codes:
        return [], {}
    placeholders = ",".join("?" for _ in codes)
    entry = db.query_df(
        f"SELECT code, open, preclose, trade_status FROM prices_daily WHERE date=? AND code IN ({placeholders})",
        (execution_date, *codes),
    ).set_index("code")
    exitp = db.query_df(
        f"SELECT code, close, trade_status FROM prices_daily WHERE date=? AND code IN ({placeholders})",
        (exit_date, *codes),
    ).set_index("code")
    returns: list[float] = []
    reasons: dict[str, int] = {}
    for code in codes:
        if code not in entry.index:
            _count_reason(reasons, "missing_entry")
            continue
        if code not in exitp.index:
            _count_reason(reasons, "missing_exit")
            continue
        entry_row = entry.loc[code]
        exit_row = exitp.loc[code]
        if not _is_tradeable(entry_row.get("trade_status")):
            _count_reason(reasons, "entry_not_tradeable")
            continue
        if not _is_tradeable(exit_row.get("trade_status")):
            _count_reason(reasons, "exit_not_tradeable")
            continue
        entry_open = _number(entry_row.get("open"))
        entry_preclose = _number(entry_row.get("preclose"))
        exit_close = _number(exit_row.get("close"))
        if entry_open is None or entry_preclose is None or exit_close is None or entry_open <= 0 or entry_preclose <= 0 or exit_close <= 0:
            _count_reason(reasons, "non_positive_price")
            continue
        if abs(entry_open / entry_preclose - 1.0) >= config.limit_lock_threshold:
            _count_reason(reasons, "entry_limit_lock")
            continue
        buy = entry_open * (1.0 + config.slippage_bps / 10000.0)
        sell = exit_close * (1.0 - config.slippage_bps / 10000.0)
        net = sell / buy - 1.0
        net -= (config.commission_bps * 2.0 + config.stamp_tax_sell_bps) / 10000.0
        returns.append(net)
    return returns, reasons


def _formal_ledger_period(
    db: ResearchPanelDB,
    picks: list[str],
    signal_date: str,
    execution_date: str,
    exit_date: str,
    config: EventBacktestConfig,
) -> tuple[float | None, dict[str, int], dict[str, Any], int]:
    """Execute one formal event period through the trade-level ledger."""

    dates = (execution_date, exit_date)
    market_by_date: dict[str, dict[str, dict[str, object]]] = {}
    placeholders = ",".join("?" for _ in picks)
    for trade_date in dates:
        frame = db.query_df(
            f"SELECT code, open, close, trade_status "
            f"FROM prices_daily WHERE date=? AND code IN ({placeholders})",
            (trade_date, *picks),
        )
        market_by_date[trade_date] = {
            str(row["code"]).zfill(6): {
                key: row[key]
                for key in ("open", "close", "trade_status")
                if key in row and pd.notna(row[key])
            }
            for row in frame.to_dict("records")
        }
    intents_by_date = {
        execution_date: [
            OrderIntent(signal_date, signal_date, execution_date, code, "buy", 100, "event_entry")
            for code in picks
        ],
        exit_date: [
            OrderIntent(signal_date, execution_date, exit_date, code, "sell", 100, "event_exit")
            for code in picks
        ],
    }
    ledger = run_ledger_backtest(
        intents_by_date,
        market_by_date,
        policy=ExecutionPolicy(
            initial_cash=1_000_000.0,
            max_position_weight=1.0,
            lot_size=100,
            commission_bps=config.commission_bps,
            commission_min=0.0,
            stamp_tax_sell_bps=config.stamp_tax_sell_bps,
            slippage_bps=config.slippage_bps,
            t_plus_one=True,
        ),
    )
    summary = ledger.summary()
    filled_entries = sum(
        1
        for fill in ledger.fills
        if fill.order.side == "buy" and fill.trade_status == "filled"
    )
    reasons = dict(summary.get("failure_reasons") or {})
    return (
        float(summary["net_return"]) if filled_entries else None,
        reasons,
        summary,
        filled_entries,
    )


def _offset_date(dates: list[str], date: str, offset: int) -> str | None:
    try:
        index = dates.index(date) + offset
    except ValueError:
        return None
    return dates[index] if 0 <= index < len(dates) else None


def _is_tradeable(value: object) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "normal", "trade", "trading", "正常", "交易"}


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def _count_reason(reasons: dict[str, int], reason: str) -> None:
    reasons[reason] = reasons.get(reason, 0) + 1
