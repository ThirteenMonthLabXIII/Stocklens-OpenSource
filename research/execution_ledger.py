"""Small, deterministic A-share execution ledger for offline research.

This is intentionally independent from the UI and from third-party backtest
engines.  It models orders, fills, cash, position lots and daily equity so an
event backtest can be audited at trade level instead of reducing a period to
an average of stock returns.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping


def _iso(value: str | date | datetime) -> str:
    return value.isoformat()[:10] if isinstance(value, (date, datetime)) else str(value)[:10]


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class ExecutionPolicy:
    version: str = "execution_policy_v1"
    initial_cash: float = 1_000_000.0
    lot_size: int = 100
    max_position_weight: float = 0.20
    commission_bps: float = 3.0
    commission_min: float = 5.0
    stamp_tax_sell_bps: float = 5.0
    slippage_bps: float = 5.0
    t_plus_one: bool = True

    def __post_init__(self) -> None:
        if self.initial_cash <= 0 or self.lot_size < 1:
            raise ValueError("initial_cash 和 lot_size 必须为正数")
        if not 0 < self.max_position_weight <= 1:
            raise ValueError("max_position_weight 必须在 (0, 1] 内")
        if min(self.commission_bps, self.commission_min, self.stamp_tax_sell_bps, self.slippage_bps) < 0:
            raise ValueError("费用和滑点不能为负数")


@dataclass(frozen=True)
class OrderIntent:
    signal_time: str
    decision_time: str
    order_time: str
    code: str
    side: str
    quantity: int
    reason: str = ""

    def __post_init__(self) -> None:
        if str(self.side).lower() not in {"buy", "sell"}:
            raise ValueError("side 必须是 buy 或 sell")
        if int(self.quantity) <= 0:
            raise ValueError("订单数量必须为正数")


@dataclass(frozen=True)
class OrderFill:
    order: OrderIntent
    fill_time: str
    quantity: int
    raw_price: float | None
    cash_before: float
    cash_after: float
    holding_before: int
    holding_after: int
    fee: float = 0.0
    tax: float = 0.0
    slippage: float = 0.0
    trade_status: str = "filled"
    untradeable_reason: str = ""


@dataclass(frozen=True)
class PositionLot:
    code: str
    quantity: int
    raw_price: float
    opened_at: str
    available_to_sell_at: str


@dataclass(frozen=True)
class CashLedgerEntry:
    time: str
    reason: str
    amount: float
    balance_after: float
    code: str = ""


@dataclass(frozen=True)
class DailyEquity:
    trade_date: str
    cash: float
    holdings_value: float
    total_equity: float
    positions: dict[str, int]


@dataclass
class BacktestLedger:
    policy_version: str
    initial_cash: float
    fills: list[OrderFill] = field(default_factory=list)
    positions: list[PositionLot] = field(default_factory=list)
    cash_entries: list[CashLedgerEntry] = field(default_factory=list)
    daily_equity: list[DailyEquity] = field(default_factory=list)

    @property
    def orders(self) -> list[OrderIntent]:
        return [fill.order for fill in self.fills]

    def summary(self) -> dict[str, Any]:
        return summarize_ledger(self)


class ExecutionLedgerEngine:
    """Execute next-open intents against explicit daily market fixtures."""

    def __init__(self, policy: ExecutionPolicy = ExecutionPolicy()) -> None:
        self.policy = policy
        self.ledger = BacktestLedger(policy.version, policy.initial_cash)
        self._cash = float(policy.initial_cash)
        self._lots: dict[str, list[PositionLot]] = {}

    def execute_next_open(
        self,
        intent: OrderIntent,
        market: Mapping[str, Any],
        *,
        fill_time: str | None = None,
        next_trading_date: str | None = None,
    ) -> OrderFill:
        """Fill an order at the next trading day's raw open or record failure."""
        fill_date = _iso(fill_time or intent.order_time)
        code = str(intent.code).zfill(6)
        side = str(intent.side).lower()
        current_lots = self._lots.get(code, [])
        holding_before = sum(lot.quantity for lot in current_lots)
        cash_before = self._cash
        status, reason = self._gate_order(side, code, intent, market, fill_date, current_lots)
        raw_price = _number(market.get("open")) if status == "filled" else None
        quantity = self._normalized_quantity(intent.quantity)
        if status == "filled" and raw_price is not None:
            if side == "buy":
                quantity, reason = self._buy_quantity(quantity, raw_price, market)
                if quantity <= 0:
                    status, reason = "failed", reason or "cash_or_position_limit"
            else:
                available = sum(
                    lot.quantity for lot in current_lots if lot.available_to_sell_at <= fill_date
                )
                quantity = min(quantity, available)
                if quantity <= 0:
                    status, reason = "failed", "t_plus_one_or_no_position"
        if status != "filled" or raw_price is None or quantity <= 0:
            fill = OrderFill(
                order=OrderIntent(intent.signal_time, intent.decision_time, intent.order_time, code, side, int(intent.quantity), intent.reason),
                fill_time=fill_date,
                quantity=0,
                raw_price=None,
                cash_before=cash_before,
                cash_after=cash_before,
                holding_before=holding_before,
                holding_after=holding_before,
                trade_status="failed",
                untradeable_reason=reason or "not_tradeable",
            )
            self.ledger.fills.append(fill)
            return fill

        notional = raw_price * quantity
        slippage = notional * self.policy.slippage_bps / 10000.0
        fee = max(notional * self.policy.commission_bps / 10000.0, self.policy.commission_min)
        tax = notional * self.policy.stamp_tax_sell_bps / 10000.0 if side == "sell" else 0.0
        if side == "buy":
            cash_after = cash_before - notional - slippage - fee
            available_at = _iso(next_trading_date or ("9999-12-31" if self.policy.t_plus_one else fill_date))
            lot = PositionLot(code, quantity, raw_price, fill_date, available_at)
            self._lots.setdefault(code, []).append(lot)
        else:
            cash_after = cash_before + notional - slippage - fee - tax
            self._consume_lots(code, quantity, fill_date)
        self._cash = cash_after
        holding_after = sum(lot.quantity for lot in self._lots.get(code, []))
        self.ledger.cash_entries.append(CashLedgerEntry(fill_date, f"{side}:{code}", cash_after - cash_before, cash_after, code))
        fill = OrderFill(
            order=OrderIntent(intent.signal_time, intent.decision_time, intent.order_time, code, side, int(intent.quantity), intent.reason),
            fill_time=fill_date,
            quantity=quantity,
            raw_price=raw_price,
            cash_before=cash_before,
            cash_after=cash_after,
            holding_before=holding_before,
            holding_after=holding_after,
            fee=fee,
            tax=tax,
            slippage=slippage,
        )
        self.ledger.fills.append(fill)
        return fill

    def mark_to_market(self, trade_date: str, prices: Mapping[str, Any]) -> DailyEquity:
        positions = self.positions_at(trade_date)
        holdings_value = 0.0
        for code, quantity in positions.items():
            price = _number(prices.get(code))
            if price is not None:
                holdings_value += quantity * price
        equity = DailyEquity(_iso(trade_date), self._cash, holdings_value, self._cash + holdings_value, positions)
        self.ledger.daily_equity.append(equity)
        return equity

    def positions_at(self, trade_date: str) -> dict[str, int]:
        cutoff = _iso(trade_date)
        return {
            code: sum(lot.quantity for lot in lots)
            for code, lots in self._lots.items()
            if sum(lot.quantity for lot in lots) > 0
        }

    def validate_invariants(self) -> None:
        for fill in self.ledger.fills:
            if fill.trade_status == "filled" and fill.cash_after < -1e-7 and fill.order.side == "buy":
                raise AssertionError("买入成交后现金不能为负")
        for equity in self.ledger.daily_equity:
            if abs(equity.total_equity - equity.cash - equity.holdings_value) > 1e-6:
                raise AssertionError(f"每日资产恒等式失败：{equity.trade_date}")

    def _gate_order(
        self,
        side: str,
        code: str,
        intent: OrderIntent,
        market: Mapping[str, Any],
        fill_date: str,
        lots: list[PositionLot],
    ) -> tuple[str, str]:
        status = str(market.get("trade_status") or "normal").lower()
        if status not in {"normal", "trade", "trading", "1", "交易", "正常"}:
            return "failed", "suspended_or_untradeable"
        if bool(market.get("one_word")):
            return "failed", "one_word_board"
        if side == "buy" and (bool(market.get("limit_up")) or bool(market.get("buy_limit_locked"))):
            return "failed", "limit_up_buy_locked"
        if side == "sell" and (bool(market.get("limit_down")) or bool(market.get("sell_limit_locked"))):
            return "failed", "limit_down_sell_locked"
        if side == "sell" and self.policy.t_plus_one:
            available = sum(lot.quantity for lot in lots if lot.available_to_sell_at <= fill_date)
            if available <= 0:
                return "failed", "t_plus_one"
        return "filled", ""

    def _buy_quantity(self, requested: int, price: float, market: Mapping[str, Any]) -> tuple[int, str]:
        if price <= 0:
            return 0, "non_positive_price"
        equity = self._cash + sum(
            lot.quantity * price for lots in self._lots.values() for lot in lots
        )
        cap = max(0.0, equity * self.policy.max_position_weight)
        max_by_weight = int(cap // price // self.policy.lot_size) * self.policy.lot_size
        max_by_cash = int(self._cash // price // self.policy.lot_size) * self.policy.lot_size
        quantity = min(requested, max_by_weight, max_by_cash)
        while quantity > 0:
            notional = quantity * price
            fee = max(notional * self.policy.commission_bps / 10000.0, self.policy.commission_min)
            total = notional * (1.0 + self.policy.slippage_bps / 10000.0) + fee
            if total <= self._cash + 1e-9:
                break
            quantity -= self.policy.lot_size
        return quantity, "" if quantity > 0 else "cash_or_position_limit"

    def _consume_lots(self, code: str, quantity: int, fill_date: str) -> None:
        remaining = quantity
        kept: list[PositionLot] = []
        for lot in self._lots.get(code, []):
            sellable = lot.available_to_sell_at <= fill_date
            take = min(remaining, lot.quantity) if sellable else 0
            remaining -= take
            left = lot.quantity - take
            if left:
                kept.append(PositionLot(lot.code, left, lot.raw_price, lot.opened_at, lot.available_to_sell_at))
        if remaining:
            raise AssertionError("成交卖出数量超过可卖持仓")
        self._lots[code] = kept

    def _normalized_quantity(self, quantity: int) -> int:
        return max(0, int(quantity) // self.policy.lot_size * self.policy.lot_size)


def summarize_ledger(ledger: BacktestLedger) -> dict[str, Any]:
    equities = [item.total_equity for item in ledger.daily_equity]
    returns: list[float] = []
    drawdowns: list[float] = []
    peak = None
    for current in equities:
        if peak is not None and peak > 0:
            returns.append(current / peak - 1.0 if not returns else current / equities[len(returns) - 1] - 1.0)
        peak = max(peak or current, current)
        drawdowns.append(current / peak - 1.0 if peak else 0.0)
    filled = [fill for fill in ledger.fills if fill.trade_status == "filled"]
    failed = [fill for fill in ledger.fills if fill.trade_status != "filled"]
    fees = sum(fill.fee for fill in filled)
    taxes = sum(fill.tax for fill in filled)
    slippage = sum(fill.slippage for fill in filled)
    final_equity = equities[-1] if equities else ledger.initial_cash
    return {
        "net_return": final_equity / ledger.initial_cash - 1.0,
        "max_drawdown": min(drawdowns, default=0.0),
        "volatility": _std(returns),
        "sharpe": _ratio(_mean(returns), _std(returns)),
        "sortino": _ratio(_mean(returns), _std([item for item in returns if item < 0])),
        "filled_orders": len(filled),
        "failed_orders": len(failed),
        "fill_rate": len(filled) / max(len(ledger.fills), 1),
        "fees": fees,
        "taxes": taxes,
        "slippage": slippage,
        "cost_ratio": (fees + taxes + slippage) / ledger.initial_cash,
        "failure_reasons": _reason_counts(failed),
        "trade_status": {"filled": len(filled), "failed": len(failed)},
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _reason_counts(fills: Iterable[OrderFill]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for fill in fills:
        reason = fill.untradeable_reason or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    return counts
