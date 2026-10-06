"""Side-car HS300 relative performance disclosure for Phase C.

This report is additive only. It does not change the pre-registered equal
weight benchmark, excess returns, DSR, PBO, winner selection, or holdout gate.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.backtest_engine import (
    BacktestConfig,
    _attach_trend,
    _eligible_at_entry,
    _exit_date,
    _month_start_dates,
    _round_trip_cost,
    _trading_dates,
)
from research.backtest_report import LEDGER_PATH
from research.metrics_robustness import return_summary
from research.panel_db import OUT_DIR, ResearchPanelDB, open_panel
from research.pit_join import as_of
from research.scoring_offline import CandidateWeights, load_preregistered_weights
from research.scoring_offline import score_cross_section

WINDOWS = {
    "train": ("2019-01-01", "2023-12-31"),
    "validation": ("2024-01-01", "2024-12-31"),
}


def generate_report(
    out_md: Path | None = None,
    out_json: Path | None = None,
    *,
    ledger_path: Path = LEDGER_PATH,
) -> dict:
    out_md = out_md or (OUT_DIR / "hs300_relative.md")
    out_json = out_json or (OUT_DIR / "hs300_relative.json")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_json(out_json)
    with open_panel() as db:
        _assert_hs300_ready(db)
        candidates = load_preregistered_weights()
        payload: dict = {
            "generated_at": _now(),
            "scope": "side_car_disclosure_only",
            "benchmark": "hs300_daily close-to-close return",
            "methodology_note": (
                "Pre-registered equal-weight benchmark, excess_return, DSR, PBO, "
                "and winner selection are unchanged."
            ),
            "windows": {},
        }
        existing_windows = existing.get("windows", {}) if existing else {}
        for window_name, (start, end) in WINDOWS.items():
            if window_name in existing_windows:
                payload["windows"][window_name] = existing_windows[window_name]
            else:
                payload["windows"][window_name] = _candidate_window_results(db, candidates, start, end)

        ledger = _read_json(ledger_path)
        if ledger:
            payload["holdout"] = _holdout_result(db, ledger)
        else:
            payload["holdout"] = {
                "status": "NOT_RUN",
                "reason": "holdout_ledger.json not found",
            }

        out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        out_md.write_text(_render_markdown(payload), encoding="utf-8")
        return payload


def _candidate_window_results(
    db: ResearchPanelDB,
    candidates: list[CandidateWeights],
    start_date: str,
    end_date: str,
) -> list[dict]:
    by_candidate = _run_backtests_cached(db, candidates, BacktestConfig(start_date=start_date, end_date=end_date))
    out: list[dict] = []
    for candidate in candidates:
        rows = _rows_with_hs300(db, by_candidate[candidate.id])
        excess_vs_hs300 = pd.Series([row["excess_vs_hs300"] for row in rows], dtype="float64")
        out.append(
            {
                "candidate_id": candidate.id,
                "summary": return_summary(excess_vs_hs300),
                "period_rows": rows,
            }
        )
    return out


def _run_backtests_cached(
    db: ResearchPanelDB,
    candidates: list[CandidateWeights],
    config: BacktestConfig,
) -> dict[str, list[dict]]:
    dates = _trading_dates(db, config.start_date, config.end_date)
    rebalances = _month_start_dates(dates)
    rows_by_candidate: dict[str, list[dict]] = {candidate.id: [] for candidate in candidates}
    cost = _round_trip_cost(config)
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
        future = db.query_df(
            "SELECT code, close FROM prices_daily WHERE date=?",
            (exit_date,),
        ).set_index("code")
        entry = eligible.set_index("code")
        for candidate in candidates:
            scored = score_cross_section(eligible, candidate.weights)
            picks = scored.head(config.top_n)["code"].tolist()
            pick_returns = []
            for code in picks:
                if code not in future.index:
                    continue
                entry_close = _to_float(entry.loc[code, "close"])
                exit_close = _to_float(future.loc[code, "close"])
                if entry_close is not None and exit_close is not None and entry_close > 0 and exit_close > 0:
                    pick_returns.append(exit_close / entry_close - 1.0)
            if not pick_returns:
                continue
            rows_by_candidate[candidate.id].append(
                {
                    "rebalance_date": rebalance_date,
                    "exit_date": exit_date,
                    "n_eligible": int(len(eligible)),
                    "n_selected": int(len(pick_returns)),
                    "portfolio_return": float(pd.Series(pick_returns).mean()) - cost,
                    "selected_codes": ",".join(picks),
                    "round_trip_cost": cost,
                }
            )
    return rows_by_candidate


def _holdout_result(db: ResearchPanelDB, ledger: dict) -> dict:
    rows = _rows_with_hs300(db, ledger.get("period_rows") or [])
    excess_vs_hs300 = pd.Series([row["excess_vs_hs300"] for row in rows], dtype="float64")
    return {
        "status": "READY",
        "winner": ledger.get("winner"),
        "holdout_window": ledger.get("holdout_window"),
        "summary": return_summary(excess_vs_hs300),
        "period_rows": rows,
    }


def _rows_with_hs300(db: ResearchPanelDB, period_rows: Iterable[dict]) -> list[dict]:
    rows: list[dict] = []
    for row in period_rows:
        entry_date = str(row.get("rebalance_date", ""))
        exit_date = str(row.get("exit_date", ""))
        hs300_return = _hs300_return(db, entry_date, exit_date)
        portfolio_return = _to_float(row.get("portfolio_return"))
        enriched = dict(row)
        enriched["hs300_return"] = hs300_return
        enriched["excess_vs_hs300"] = (
            None if portfolio_return is None or hs300_return is None else portfolio_return - hs300_return
        )
        rows.append(enriched)
    return rows


def _hs300_return(db: ResearchPanelDB, entry_date: str, exit_date: str) -> float | None:
    prices = db.query_df(
        """
        SELECT date, close
        FROM hs300_daily
        WHERE code='sh.000300' AND date IN (?,?)
        """,
        (entry_date, exit_date),
    )
    if len(prices) < 2:
        return None
    by_date = prices.set_index("date")["close"]
    start = _to_float(by_date.get(entry_date))
    end = _to_float(by_date.get(exit_date))
    if start is None or end is None or start <= 0 or end <= 0:
        return None
    return end / start - 1.0


def _assert_hs300_ready(db: ResearchPanelDB) -> None:
    tables = db.query_df("SELECT name FROM sqlite_master WHERE type='table'")
    names = set(tables["name"].astype(str).tolist()) if not tables.empty else set()
    if "hs300_daily" not in names:
        raise RuntimeError("hs300_daily table is missing; run research/etl_index.py first")
    rows = int(db.scalar("SELECT COUNT(*) FROM hs300_daily") or 0)
    if rows <= 0:
        raise RuntimeError("hs300_daily table is empty; run research/etl_index.py first")


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        if pd.isna(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fmt(value: object) -> str:
    number = _to_float(value)
    if number is None:
        return "None"
    return f"{number:.6f}"


def _render_markdown(payload: dict) -> str:
    lines = [
        "# HS300 相对表现附加披露",
        "",
        f"- 生成时间：{payload['generated_at']}",
        "- 性质：side-car disclosure only；不改变预注册等权基准、excess_return、DSR、PBO、winner。",
        "- 指标：excess_vs_hs300 = portfolio_return - HS300 close-to-close return。",
        "",
    ]
    for window_name in ("train", "validation"):
        lines.extend([f"## {window_name}", ""])
        for row in payload["windows"].get(window_name, []):
            summary = row.get("summary", {})
            lines.append(
                f"- `{row['candidate_id']}`: periods={summary.get('periods')}, "
                f"mean_excess_vs_hs300={_fmt(summary.get('mean'))}, "
                f"period_sharpe={_fmt(summary.get('period_sharpe'))}, "
                f"max_drawdown={_fmt(summary.get('max_drawdown'))}, "
                f"win_rate={_fmt(summary.get('win_rate'))}"
            )
        lines.append("")
    holdout = payload.get("holdout") or {}
    lines.extend(["## holdout", ""])
    if holdout.get("status") == "READY":
        summary = holdout.get("summary", {})
        lines.append(
            f"- `{holdout.get('winner')}`: periods={summary.get('periods')}, "
            f"mean_excess_vs_hs300={_fmt(summary.get('mean'))}, "
            f"period_sharpe={_fmt(summary.get('period_sharpe'))}, "
            f"max_drawdown={_fmt(summary.get('max_drawdown'))}, "
            f"win_rate={_fmt(summary.get('win_rate'))}"
        )
    else:
        lines.append(f"- {holdout.get('status')}: {holdout.get('reason')}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(json.dumps(generate_report(), ensure_ascii=False, indent=2))
