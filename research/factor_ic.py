"""Exploratory factor IC diagnostics for the research panel.

This module is research-only. It reads ``research/data/panel.sqlite3`` and
does not write to the panel or rerun the consumed Phase C holdout.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.backtest_engine import BacktestConfig, _eligible_at_entry, _exit_date, _month_start_dates, _trading_dates
from research.panel_db import OUT_DIR, ResearchPanelDB, open_panel
from research.pit_join import as_of
from research.scoring_offline import DIMENSION_METRICS, METRIC_DIRECTIONS, _percentile, score_cross_section

DERIVED_DIRECTIONS: dict[str, str] = {
    "momentum_20d": "high",
    "momentum_60d": "high",
    "momentum_120d": "high",
    "volatility_20d": "low",
    "max_drawdown_60d": "low",
    "turnover_20d": "high",
    "liquidity_amount_20d": "high",
}

PRICE_RATIO_DIRECTIONS: dict[str, str] = {
    "ps_ttm": "low",
    "pcf_ncf_ttm": "low",
}

FACTOR_DIRECTIONS: dict[str, str] = {
    **METRIC_DIRECTIONS,
    **PRICE_RATIO_DIRECTIONS,
    **DERIVED_DIRECTIONS,
    "dimension_fundamental": "high",
    "dimension_growth": "high",
    "dimension_safety": "high",
    "dimension_valuation": "high",
    "dimension_trend": "high",
}


@dataclass(frozen=True)
class ICConfig:
    start_date: str = "2019-01-01"
    end_date: str = "2026-06-16"
    holding_days: tuple[int, ...] = (5, 20, 60)
    limit_lock_threshold: float = 0.095
    min_obs: int = 30


def rank_ic(factor: pd.Series, forward_return: pd.Series, direction: str = "high", *, min_obs: int = 3) -> float | None:
    """Spearman-style rank IC, oriented so higher factor score means better."""
    both_raw = pd.concat(
        [pd.to_numeric(factor, errors="coerce"), pd.to_numeric(forward_return, errors="coerce")],
        axis=1,
    ).dropna()
    if len(both_raw) < min_obs:
        return None
    f_rank = _percentile(both_raw.iloc[:, 0], direction)
    r_rank = _percentile(both_raw.iloc[:, 1], "high")
    both = pd.concat([f_rank, r_rank], axis=1).dropna()
    if both.iloc[:, 0].nunique() < 2 or both.iloc[:, 1].nunique() < 2:
        return None
    value = both.iloc[:, 0].corr(both.iloc[:, 1])
    if pd.isna(value):
        return None
    return float(value)


def grouped_rank_ic(
    frame: pd.DataFrame,
    factor_col: str,
    return_col: str,
    group_col: str,
    direction: str,
    *,
    min_group_obs: int = 3,
) -> float | None:
    """Weighted average of within-group rank IC values."""
    if frame.empty or group_col not in frame.columns:
        return None
    rows = frame[[factor_col, return_col, group_col]].dropna()
    if rows.empty:
        return None
    weighted: list[tuple[float, int]] = []
    for _, group in rows.groupby(group_col):
        ic = rank_ic(group[factor_col], group[return_col], direction, min_obs=min_group_obs)
        if ic is not None:
            weighted.append((ic, len(group)))
    total = sum(n for _, n in weighted)
    if total == 0:
        return None
    return float(sum(ic * n for ic, n in weighted) / total)


def assign_size_bucket(size_proxy: pd.Series, *, buckets: int = 5) -> pd.Series:
    """Return stable size buckets from a positive size/liquidity proxy."""
    clean = pd.to_numeric(size_proxy, errors="coerce").where(lambda s: s > 0)
    out = pd.Series(pd.NA, index=clean.index, dtype="object")
    valid = clean.dropna()
    if len(valid) < buckets:
        return out
    ranks = valid.rank(method="first")
    labels = pd.qcut(ranks, q=buckets, labels=False, duplicates="drop")
    out.loc[valid.index] = labels.astype("Int64").astype(str)
    return out


def summarize_ic(rows: pd.DataFrame) -> list[dict]:
    """Aggregate per-date IC rows by factor, horizon, and neutralization type."""
    if rows.empty:
        return []
    out: list[dict] = []
    keys = ["factor", "horizon_days", "ic_type"]
    for keys_value, group in rows.groupby(keys, dropna=False):
        values = pd.to_numeric(group["ic"], errors="coerce").dropna()
        factor, horizon, ic_type = keys_value
        std = float(values.std(ddof=1)) if len(values) > 1 else None
        mean = float(values.mean()) if len(values) else None
        ir = None if not std or mean is None else mean / std
        t_stat = None if not std or mean is None else mean / (std / math.sqrt(len(values)))
        out.append(
            {
                "factor": factor,
                "horizon_days": int(horizon),
                "ic_type": ic_type,
                "periods": int(len(values)),
                "mean_ic": mean,
                "ic_std": std,
                "ir": ir,
                "t_stat": t_stat,
                "positive_rate": float((values > 0).mean()) if len(values) else None,
            }
        )
    return sorted(out, key=lambda r: (r["horizon_days"], str(r["ic_type"]), str(r["factor"])))


def ic_decay_from_summary(
    summary: list[dict],
    *,
    factors: Sequence[str] | None = None,
    ic_type: str = "raw",
) -> list[dict]:
    """Return per-factor IC decay rows sorted by factor and horizon."""
    selected = set(factors) if factors is not None else None
    rows = [
        {
            "factor": str(row["factor"]),
            "horizon_days": int(row["horizon_days"]),
            "ic_type": str(row["ic_type"]),
            "mean_ic": row.get("mean_ic"),
            "ir": row.get("ir"),
            "periods": row.get("periods"),
        }
        for row in summary
        if str(row.get("ic_type")) == ic_type
        and (selected is None or str(row.get("factor")) in selected)
    ]
    return sorted(rows, key=lambda row: (row["factor"], row["horizon_days"]))


def run_factor_ic(
    db: ResearchPanelDB | None = None,
    *,
    config: ICConfig = ICConfig(),
    factors: Sequence[str] | None = None,
) -> dict:
    own = db is None
    db = db or open_panel()
    try:
        dates = _trading_dates(db, config.start_date, config.end_date)
        rebalances = _month_start_dates(dates)
        selected_factors = list(factors or FACTOR_DIRECTIONS.keys())
        ic_rows: list[dict] = []
        diagnostics: list[dict] = []
        for rebalance_date in rebalances:
            panel = as_of(rebalance_date, db)
            if panel.empty:
                continue
            panel = _attach_derived_price_features(panel, db, rebalance_date)
            eligible = _eligible_at_entry(panel, config.limit_lock_threshold)
            if eligible.empty:
                continue
            eligible = _attach_dimension_scores(eligible)
            eligible["size_bucket"] = assign_size_bucket(_size_proxy(eligible))
            exits = {
                horizon: _exit_date(dates, rebalance_date, horizon)
                for horizon in config.holding_days
            }
            diagnostics.append(
                {
                    "rebalance_date": rebalance_date,
                    "n_panel": int(len(panel)),
                    "n_eligible": int(len(eligible)),
                    "exit_dates": exits,
                }
            )
            for horizon, exit_date in exits.items():
                if exit_date is None:
                    continue
                frame = _attach_forward_returns(eligible, db, exit_date, horizon)
                if frame.empty:
                    continue
                for factor in selected_factors:
                    if factor not in frame.columns:
                        continue
                    direction = FACTOR_DIRECTIONS[factor]
                    raw_ic = rank_ic(frame[factor], frame[f"forward_return_{horizon}d"], direction, min_obs=config.min_obs)
                    if raw_ic is not None:
                        ic_rows.append(_ic_row(rebalance_date, horizon, factor, "raw", raw_ic, frame))
                    industry_ic = grouped_rank_ic(
                        frame,
                        factor,
                        f"forward_return_{horizon}d",
                        "industry",
                        direction,
                        min_group_obs=max(3, min(config.min_obs, 10)),
                    )
                    if industry_ic is not None:
                        ic_rows.append(_ic_row(rebalance_date, horizon, factor, "industry_neutral", industry_ic, frame))
                    size_ic = grouped_rank_ic(
                        frame,
                        factor,
                        f"forward_return_{horizon}d",
                        "size_bucket",
                        direction,
                        min_group_obs=max(3, min(config.min_obs, 10)),
                    )
                    if size_ic is not None:
                        ic_rows.append(_ic_row(rebalance_date, horizon, factor, "size_bucket_neutral", size_ic, frame))
        rows_df = pd.DataFrame(ic_rows)
        return {
            "generated_at": _now(),
            "scope": "exploratory_ic_only_not_confirmation",
            "config": {
                "start_date": config.start_date,
                "end_date": config.end_date,
                "holding_days": list(config.holding_days),
                "min_obs": config.min_obs,
            },
            "period_diagnostics": diagnostics,
            "ic_rows": rows_df.to_dict("records"),
            "summary": summarize_ic(rows_df),
            "decision_rule": "|mean IC| < ~0.02 and IR < ~0.3 is treated as noise, not confirmation.",
        }
    finally:
        if own:
            db.close()


def generate_factor_ic_report(
    out_md: Path | None = None,
    out_json: Path | None = None,
    *,
    config: ICConfig = ICConfig(),
) -> dict:
    out_md = out_md or (OUT_DIR / "factor_ic.md")
    out_json = out_json or (OUT_DIR / "factor_ic.json")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    payload = run_factor_ic(config=config)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_render_markdown(payload), encoding="utf-8")
    return payload


def _attach_derived_price_features(panel: pd.DataFrame, db: ResearchPanelDB, asof_date: str) -> pd.DataFrame:
    codes = panel["code"].astype(str).tolist()
    out = panel.copy()
    if not codes:
        for col in DERIVED_DIRECTIONS:
            out[col] = pd.NA
        return out
    lookback_start = (pd.Timestamp(asof_date) - pd.Timedelta(days=260)).strftime("%Y-%m-%d")
    prices = db.query_df(
        f"""
        SELECT code, date, close, turn, amount
        FROM prices_daily
        WHERE date BETWEEN ? AND ? AND code IN ({','.join('?' for _ in codes)})
        ORDER BY code, date
        """,
        (lookback_start, asof_date, *codes),
    )
    if prices.empty:
        for col in DERIVED_DIRECTIONS:
            out[col] = pd.NA
        return out
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    prices["turn"] = pd.to_numeric(prices.get("turn"), errors="coerce")
    prices["amount"] = pd.to_numeric(prices.get("amount"), errors="coerce")
    prices = prices.sort_values(["code", "date"])
    grouped = prices.groupby("code", group_keys=False)
    returns = grouped["close"].pct_change()
    for days in (20, 60, 120):
        prices[f"momentum_{days}d"] = grouped["close"].pct_change(days)
    prices["volatility_20d"] = returns.groupby(prices["code"]).rolling(20).std().reset_index(level=0, drop=True)
    rolling_max = prices.groupby("code")["close"].rolling(60).max().reset_index(level=0, drop=True)
    prices["max_drawdown_60d"] = prices["close"] / rolling_max - 1.0
    prices["turnover_20d"] = grouped["turn"].rolling(20).mean().reset_index(level=0, drop=True)
    prices["liquidity_amount_20d"] = grouped["amount"].rolling(20).mean().reset_index(level=0, drop=True)
    latest = prices.groupby("code").tail(1)[["code", *DERIVED_DIRECTIONS.keys()]]
    return out.merge(latest, on="code", how="left", suffixes=("", "_derived"))


def _attach_dimension_scores(panel: pd.DataFrame) -> pd.DataFrame:
    out = panel.copy()
    weights = {name: 0.0 for name in DIMENSION_METRICS}
    scored = score_cross_section(out, weights)
    score_cols = ["code", *[f"{dimension}_score" for dimension in DIMENSION_METRICS]]
    scored = scored[score_cols].rename(
        columns={f"{dimension}_score": f"dimension_{dimension}" for dimension in DIMENSION_METRICS}
    )
    out = out.merge(scored, on="code", how="left")
    return out


def _attach_forward_returns(panel: pd.DataFrame, db: ResearchPanelDB, exit_date: str, horizon: int) -> pd.DataFrame:
    codes = panel["code"].astype(str).tolist()
    if not codes:
        return panel.iloc[0:0].copy()
    future = db.query_df(
        f"SELECT code, close AS exit_close FROM prices_daily WHERE date=? AND code IN ({','.join('?' for _ in codes)})",
        (exit_date, *codes),
    )
    frame = panel.merge(future, on="code", how="inner")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["exit_close"] = pd.to_numeric(frame["exit_close"], errors="coerce")
    frame[f"forward_return_{horizon}d"] = frame["exit_close"] / frame["close"] - 1.0
    return frame[(frame["close"] > 0) & (frame["exit_close"] > 0)].copy()


def _size_proxy(frame: pd.DataFrame) -> pd.Series:
    amount = pd.to_numeric(frame.get("amount"), errors="coerce")
    turn = pd.to_numeric(frame.get("turn"), errors="coerce")
    proxy = amount / (turn / 100.0)
    return proxy.where((amount > 0) & (turn > 0), amount)


def _ic_row(rebalance_date: str, horizon: int, factor: str, ic_type: str, value: float, frame: pd.DataFrame) -> dict:
    return {
        "rebalance_date": rebalance_date,
        "horizon_days": int(horizon),
        "factor": factor,
        "ic_type": ic_type,
        "ic": float(value),
        "n": int(len(frame)),
    }


def _render_markdown(payload: dict) -> str:
    lines = [
        "# Factor IC 体检",
        "",
        f"- 生成时间：{payload['generated_at']}",
        "- 性质：探索性诊断，不是确认性 holdout；不支持直接声称策略有效。",
        f"- 窗口：{payload['config']['start_date']} 至 {payload['config']['end_date']}",
        f"- horizons：{payload['config']['holding_days']}",
        f"- 判读规则：{payload['decision_rule']}",
        "",
        "## 20 日 raw IC 摘要",
        "",
    ]
    summary = [
        row for row in payload.get("summary", [])
        if row["horizon_days"] == 20 and row["ic_type"] == "raw"
    ]
    summary = sorted(summary, key=lambda r: abs(r["mean_ic"] or 0), reverse=True)
    lines.append("| factor | periods | mean IC | IR | t-stat | positive rate |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for row in summary:
        lines.append(
            "| {factor} | {periods} | {mean_ic} | {ir} | {t_stat} | {positive_rate} |".format(
                factor=row["factor"],
                periods=row["periods"],
                mean_ic=_fmt(row["mean_ic"]),
                ir=_fmt(row["ir"]),
                t_stat=_fmt(row["t_stat"]),
                positive_rate=_fmt(row["positive_rate"]),
            )
        )
    lines.extend(["", "## 中性化对照", ""])
    lines.append("| factor | horizon | type | periods | mean IC | IR |")
    lines.append("|---|---:|---|---:|---:|---:|")
    neutral = [
        row for row in payload.get("summary", [])
        if row["horizon_days"] == 20 and row["ic_type"] != "raw"
    ]
    neutral = sorted(neutral, key=lambda r: (r["factor"], r["ic_type"]))
    for row in neutral:
        lines.append(
            f"| {row['factor']} | {row['horizon_days']} | {row['ic_type']} | {row['periods']} | {_fmt(row['mean_ic'])} | {_fmt(row['ir'])} |"
        )
    return "\n".join(lines) + "\n"


def _fmt(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default=ICConfig.start_date)
    parser.add_argument("--end-date", default=ICConfig.end_date)
    parser.add_argument("--horizons", default="5,20,60", help="Comma-separated trading-day horizons.")
    return parser.parse_args(list(argv))


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    horizons = tuple(int(x.strip()) for x in args.horizons.split(",") if x.strip())
    payload = generate_factor_ic_report(config=ICConfig(start_date=args.start_date, end_date=args.end_date, holding_days=horizons))
    print(json.dumps({"out": "research/out/factor_ic.md", "summary_rows": len(payload["summary"])}, ensure_ascii=False))
