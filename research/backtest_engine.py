"""Vectorized monthly cross-sectional backtest for Phase C research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pandas as pd

from research.panel_db import ResearchPanelDB, open_panel
from research.pit_join import as_of
from research.scoring_offline import score_cross_section


@dataclass(frozen=True)
class BacktestConfig:
    start_date: str = "2019-01-01"
    end_date: str = "2026-06-16"
    holding_days: int = 20
    top_n: int = 50
    commission_bps: float = 3.0
    stamp_tax_sell_bps: float = 5.0
    slippage_bps: float = 5.0
    limit_lock_threshold: float = 0.095


def run_backtest(
    weights: Mapping[str, float],
    db: ResearchPanelDB | None = None,
    *,
    config: BacktestConfig = BacktestConfig(),
) -> pd.DataFrame:
    own = db is None
    db = db or open_panel()
    try:
        dates = _trading_dates(db, config.start_date, config.end_date)
        rebalances = _month_start_dates(dates)
        rows: list[dict] = []
        for rebalance_date in rebalances:
            exit_date = _exit_date(dates, rebalance_date, config.holding_days)
            if exit_date is None:
                continue
            panel = as_of(rebalance_date, db)
            if panel.empty:
                continue
            panel = _attach_trend(panel, db, rebalance_date)
            eligible = _eligible_at_entry(panel, config.limit_lock_threshold)
            if eligible.empty:
                continue
            scored = score_cross_section(eligible, weights)
            picks = scored.head(config.top_n)["code"].tolist()
            if not picks:
                continue
            entry = eligible.set_index("code")
            future = db.query_df(
                f"SELECT code, close FROM prices_daily WHERE date=? AND code IN ({','.join('?' for _ in picks)})",
                (exit_date, *picks),
            ).set_index("code")
            pick_returns = []
            for code in picks:
                if code not in future.index:
                    continue
                entry_close = float(entry.loc[code, "close"])
                exit_close = float(future.loc[code, "close"])
                if entry_close > 0 and exit_close > 0:
                    pick_returns.append(exit_close / entry_close - 1.0)
            if not pick_returns:
                continue
            cost = _round_trip_cost(config)
            portfolio_return = float(pd.Series(pick_returns).mean()) - cost
            benchmark_return = _benchmark_equal_weight_return(db, eligible["code"].tolist(), rebalance_date, exit_date)
            rows.append(
                {
                    "rebalance_date": rebalance_date,
                    "exit_date": exit_date,
                    "n_eligible": int(len(eligible)),
                    "n_selected": int(len(pick_returns)),
                    "portfolio_return": portfolio_return,
                    "benchmark_return": benchmark_return,
                    "excess_return": portfolio_return - benchmark_return,
                    "selected_codes": ",".join(picks),
                    "round_trip_cost": cost,
                }
            )
        return pd.DataFrame(rows)
    finally:
        if own:
            db.close()


def coverage_status(db: ResearchPanelDB | None = None) -> dict:
    own = db is None
    db = db or open_panel()
    try:
        prices = db.query_df(
            "SELECT MIN(date) AS min_date, MAX(date) AS max_date, COUNT(DISTINCT code) AS codes, COUNT(*) AS rows FROM prices_daily"
        ).iloc[0].to_dict()
        fundamentals = db.query_df(
            "SELECT MIN(pub_date) AS min_pub_date, MAX(pub_date) AS max_pub_date, COUNT(DISTINCT code) AS codes, COUNT(*) AS rows FROM fundamentals_pit"
        ).iloc[0].to_dict()
        ok = (
            str(prices.get("min_date") or "") <= "2018-01-02"
            and str(prices.get("max_date") or "") >= "2026-06-01"
            and int(prices.get("codes") or 0) >= 3000
            and int(fundamentals.get("codes") or 0) >= 3000
        )
        return {
            "full_panel_ready": bool(ok),
            "prices": prices,
            "fundamentals": fundamentals,
            "hs300_status": _hs300_status(db),
        }
    finally:
        if own:
            db.close()


def _hs300_status(db: ResearchPanelDB) -> str:
    tables = db.query_df("SELECT name FROM sqlite_master WHERE type='table'")
    names = set(tables["name"].astype(str).tolist()) if not tables.empty else set()
    if "hs300_daily" not in names:
        return "MISSING_DATA"
    rows = int(db.scalar("SELECT COUNT(*) FROM hs300_daily") or 0)
    return "READY" if rows else "MISSING_DATA"


def assert_no_future_pubdates(asof_date: str, facts: pd.DataFrame) -> None:
    if "pub_date" not in facts.columns:
        return
    future = facts[pd.to_datetime(facts["pub_date"]) > pd.Timestamp(asof_date)]
    if not future.empty:
        raise AssertionError(f"{len(future)} future facts visible at {asof_date}")


def _trading_dates(db: ResearchPanelDB, start_date: str, end_date: str) -> list[str]:
    rows = db.query_df(
        """
        SELECT calendar_date
        FROM trade_calendar
        WHERE is_trading_day=1 AND calendar_date BETWEEN ? AND ?
        ORDER BY calendar_date
        """,
        (start_date, end_date),
    )
    if rows.empty:
        rows = db.query_df(
            "SELECT DISTINCT date AS calendar_date FROM prices_daily WHERE date BETWEEN ? AND ? ORDER BY date",
            (start_date, end_date),
        )
    return [str(x) for x in rows["calendar_date"].tolist()]


def _month_start_dates(dates: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for day in dates:
        month = day[:7]
        if month not in seen:
            seen.add(month)
            out.append(day)
    return out


def _exit_date(dates: list[str], rebalance_date: str, holding_days: int) -> str | None:
    try:
        idx = dates.index(rebalance_date)
    except ValueError:
        return None
    exit_idx = idx + holding_days
    if exit_idx >= len(dates):
        return None
    return dates[exit_idx]


def _eligible_at_entry(panel: pd.DataFrame, threshold: float) -> pd.DataFrame:
    out = panel.copy()
    out["close"] = pd.to_numeric(out.get("close"), errors="coerce")
    out["preclose"] = pd.to_numeric(out.get("preclose"), errors="coerce")
    out = out[(out["close"] > 0) & (out["preclose"] > 0)]
    if "trade_status" in out.columns:
        out = out[out["trade_status"].astype(str).eq("1")]
    locked = (out["close"] / out["preclose"] - 1.0).abs() >= threshold
    return out.loc[~locked].copy()


def _attach_trend(panel: pd.DataFrame, db: ResearchPanelDB, asof_date: str) -> pd.DataFrame:
    codes = panel["code"].astype(str).tolist()
    if not codes:
        return panel
    prices = db.query_df(
        f"""
        SELECT code, date, close
        FROM prices_daily
        WHERE date<=? AND code IN ({','.join('?' for _ in codes)})
        ORDER BY code, date
        """,
        (asof_date, *codes),
    )
    if prices.empty:
        panel = panel.copy()
        panel["trend_20d"] = pd.NA
        panel["trend_60d"] = pd.NA
        return panel
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    prices["trend_20d"] = prices.groupby("code")["close"].pct_change(20)
    prices["trend_60d"] = prices.groupby("code")["close"].pct_change(60)
    latest = prices.groupby("code").tail(1)[["code", "trend_20d", "trend_60d"]]
    return panel.merge(latest, on="code", how="left")


def _benchmark_equal_weight_return(
    db: ResearchPanelDB,
    codes: list[str],
    entry_date: str,
    exit_date: str,
) -> float:
    if not codes:
        return 0.0
    placeholders = ",".join("?" for _ in codes)
    entry = db.query_df(
        f"SELECT code, close FROM prices_daily WHERE date=? AND code IN ({placeholders})",
        (entry_date, *codes),
    ).set_index("code")
    exitp = db.query_df(
        f"SELECT code, close FROM prices_daily WHERE date=? AND code IN ({placeholders})",
        (exit_date, *codes),
    ).set_index("code")
    returns = []
    for code in set(entry.index).intersection(exitp.index):
        start = float(entry.loc[code, "close"])
        end = float(exitp.loc[code, "close"])
        if start > 0 and end > 0:
            returns.append(end / start - 1.0)
    return float(pd.Series(returns).mean()) if returns else 0.0


def _round_trip_cost(config: BacktestConfig) -> float:
    bps = config.commission_bps * 2.0 + config.stamp_tax_sell_bps + config.slippage_bps * 2.0
    return bps / 10000.0
