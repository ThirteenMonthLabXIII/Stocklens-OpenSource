"""One-off alphalens-reloaded crosscheck for native rank IC.

This is intentionally not imported by production or regular research modules.
It verifies that ``research.factor_ic.rank_ic`` agrees with alphalens on a small
sample after orienting each factor so higher score means better.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from alphalens import performance, utils

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.backtest_engine import _eligible_at_entry, _exit_date, _trading_dates
from research.factor_ic import (
    FACTOR_DIRECTIONS,
    _attach_derived_price_features,
    _attach_dimension_scores,
    _attach_forward_returns,
    rank_ic,
)
from research.panel_db import OUT_DIR, open_panel
from research.pit_join import as_of
from research.scoring_offline import _percentile

SAMPLE_DATES = ("2025-01-02", "2025-01-03", "2025-01-06")
SAMPLE_FACTORS = ("pb_mrq", "volatility_20d", "liquidity_amount_20d")
HORIZON = 20
TOLERANCE = 1e-6


def run_crosscheck(
    *,
    sample_dates: tuple[str, ...] = SAMPLE_DATES,
    sample_factors: tuple[str, ...] = SAMPLE_FACTORS,
    horizon: int = HORIZON,
) -> dict:
    with open_panel() as db:
        trading_dates = _trading_dates(db, min(sample_dates), "2026-06-16")
        rows: list[dict] = []
        factor_series: list[pd.Series] = []
        native_by_key: dict[tuple[str, str], float] = {}
        codes: set[str] = set()
        max_exit = max(sample_dates)

        for sample_date in sample_dates:
            exit_date = _exit_date(trading_dates, sample_date, horizon)
            if exit_date is None:
                continue
            max_exit = max(max_exit, exit_date)
            panel = as_of(sample_date, db)
            panel = _attach_derived_price_features(panel, db, sample_date)
            panel = _eligible_at_entry(panel, 0.095)
            panel = _attach_dimension_scores(panel)
            frame = _attach_forward_returns(panel, db, exit_date, horizon)
            if frame.empty:
                continue
            codes.update(frame["code"].astype(str).tolist())
            for factor in sample_factors:
                if factor not in frame.columns:
                    continue
                direction = FACTOR_DIRECTIONS[factor]
                oriented = _percentile(frame[factor], direction)
                native = rank_ic(oriented, frame[f"forward_return_{horizon}d"], "high", min_obs=30)
                if native is None:
                    continue
                native_by_key[(sample_date, factor)] = native
                index = pd.MultiIndex.from_arrays(
                    [
                        pd.to_datetime([sample_date] * len(frame)),
                        frame["code"].astype(str),
                    ],
                    names=["date", "asset"],
                )
                factor_series.append(pd.Series(oriented.to_numpy(), index=index, name=factor).dropna())

        prices = _prices_frame(min(sample_dates), max_exit, sorted(codes))
        for factor in sample_factors:
            factor_input = pd.concat([s for s in factor_series if s.name == factor]).sort_index()
            clean = utils.get_clean_factor_and_forward_returns(
                factor_input,
                prices,
                quantiles=5,
                periods=(horizon,),
                filter_zscore=None,
                max_loss=1.0,
                cumulative_returns=True,
            )
            alphalens_ic = performance.factor_information_coefficient(clean)
            period_col = f"{horizon}D"
            if period_col not in alphalens_ic.columns:
                period_col = str(alphalens_ic.columns[0])
            for sample_date in sample_dates:
                key = (sample_date, factor)
                if key not in native_by_key:
                    continue
                got = alphalens_ic.loc[pd.Timestamp(sample_date), period_col]
                diff = abs(float(got) - float(native_by_key[key]))
                rows.append(
                    {
                        "date": sample_date,
                        "factor": factor,
                        "horizon_days": horizon,
                        "native_ic": float(native_by_key[key]),
                        "alphalens_ic": float(got),
                        "abs_diff": diff,
                        "pass": diff <= TOLERANCE,
                    }
                )

        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "library": "alphalens-reloaded",
            "sample_dates": list(sample_dates),
            "sample_factors": list(sample_factors),
            "horizon_days": horizon,
            "tolerance": TOLERANCE,
            "rows": rows,
            "passed": bool(rows) and all(row["pass"] for row in rows),
        }


def write_report(payload: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = OUT_DIR / "alphalens_crosscheck.json"
    out_md = OUT_DIR / "alphalens_crosscheck.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Alphalens IC 交叉对标",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 库：{payload['library']}",
        f"- horizon：{payload['horizon_days']} trading days",
        f"- 容差：{payload['tolerance']}",
        f"- 结论：{'PASS' if payload['passed'] else 'FAIL'}",
        "",
        "| date | factor | native IC | alphalens IC | abs diff | pass |",
        "|---|---|---:|---:|---:|---|",
    ]
    for row in payload["rows"]:
        lines.append(
            f"| {row['date']} | {row['factor']} | {row['native_ic']:.10f} | {row['alphalens_ic']:.10f} | {row['abs_diff']:.2e} | {row['pass']} |"
        )
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _prices_frame(start_date: str, end_date: str, codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    with open_panel() as db:
        prices = db.query_df(
            f"""
            SELECT date, code, close
            FROM prices_daily
            WHERE date BETWEEN ? AND ? AND code IN ({','.join('?' for _ in codes)})
            ORDER BY date, code
            """,
            (start_date, end_date, *codes),
        )
    prices["date"] = pd.to_datetime(prices["date"])
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    return prices.pivot(index="date", columns="code", values="close").sort_index()


if __name__ == "__main__":
    result = run_crosscheck()
    write_report(result)
    print(json.dumps({"passed": result["passed"], "rows": len(result["rows"])}, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)
