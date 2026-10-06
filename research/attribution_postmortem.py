"""Postmortem attribution for the consumed Phase C holdout ledger.

The script only reads the existing ledger and research panel. It does not rerun
the holdout or change any production scoring path.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.backtest_report import LEDGER_PATH
from research.panel_db import OUT_DIR, ResearchPanelDB, open_panel
from research.pit_join import industry_as_of


def load_ledger_rows(path: Path = LEDGER_PATH) -> tuple[dict, pd.DataFrame]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = pd.DataFrame(payload.get("period_rows", []))
    if rows.empty:
        return payload, rows
    rows["excess_return"] = pd.to_numeric(rows["excess_return"], errors="coerce")
    rows["portfolio_return"] = pd.to_numeric(rows["portfolio_return"], errors="coerce")
    rows["benchmark_return"] = pd.to_numeric(rows["benchmark_return"], errors="coerce")
    rows["selected_list"] = rows["selected_codes"].fillna("").map(_split_codes)
    return payload, rows


def generate_attribution(
    ledger_path: Path = LEDGER_PATH,
    db: ResearchPanelDB | None = None,
) -> dict:
    ledger, rows = load_ledger_rows(ledger_path)
    own = db is None
    db = db or open_panel()
    try:
        period_details: list[dict] = []
        industry_rows: list[dict] = []
        selected_return_rows: list[dict] = []
        board_counter: Counter[str] = Counter()
        code_counter: Counter[str] = Counter()
        overlap_rows: list[dict] = []
        previous: set[str] | None = None

        for _, row in rows.iterrows():
            rebalance_date = str(row["rebalance_date"])
            exit_date = str(row["exit_date"])
            selected = list(row["selected_list"])
            code_counter.update(selected)
            board_counter.update(_board(code) for code in selected)
            if previous is not None:
                current = set(selected)
                union = previous | current
                overlap_rows.append(
                    {
                        "rebalance_date": rebalance_date,
                        "overlap_count": len(previous & current),
                        "jaccard": len(previous & current) / len(union) if union else None,
                    }
                )
            previous = set(selected)

            entry_prices = _prices_on(db, rebalance_date)
            selected_prices = entry_prices[entry_prices["code"].isin(selected)].copy()
            selected_prices["size_proxy"] = _size_proxy(selected_prices)
            entry_prices["size_proxy"] = _size_proxy(entry_prices)
            entry_prices["size_pct"] = entry_prices["size_proxy"].rank(pct=True)
            selected_prices = selected_prices.merge(entry_prices[["code", "size_pct"]], on="code", how="left")
            selected_prices["size_bucket"] = pd.cut(
                selected_prices["size_pct"],
                bins=[0, 0.2, 0.8, 1.0],
                labels=["smallest_20pct", "middle_60pct", "largest_20pct"],
                include_lowest=True,
            )

            holding_returns = _holding_returns(db, selected, rebalance_date, exit_date)
            selected_return_rows.extend(
                {
                    "rebalance_date": rebalance_date,
                    "exit_date": exit_date,
                    "code": r["code"],
                    "holding_return": r["holding_return"],
                }
                for r in holding_returns.to_dict("records")
            )

            industries = industry_as_of(rebalance_date, db, selected)
            by_code = holding_returns.merge(industries[["code", "industry"]], on="code", how="left")
            if not by_code.empty:
                for industry, group in by_code.groupby(by_code["industry"].fillna("__missing__")):
                    industry_rows.append(
                        {
                            "rebalance_date": rebalance_date,
                            "industry": str(industry),
                            "count": int(len(group)),
                            "mean_holding_return": _safe_mean(group["holding_return"]),
                        }
                    )

            period_details.append(
                {
                    "rebalance_date": rebalance_date,
                    "exit_date": exit_date,
                    "excess_return": float(row["excess_return"]),
                    "portfolio_return": float(row["portfolio_return"]),
                    "benchmark_return": float(row["benchmark_return"]),
                    "n_selected": int(len(selected)),
                    "selected_size_proxy_median_pct": _safe_median(selected_prices["size_pct"]),
                    "smallest_20pct_share": _share(selected_prices["size_bucket"], "smallest_20pct"),
                    "largest_20pct_share": _share(selected_prices["size_bucket"], "largest_20pct"),
                    "board_share": _board_share(selected),
                    "selected_mean_holding_return": _safe_mean(holding_returns["holding_return"]),
                }
            )

        period_df = pd.DataFrame(period_details)
        industry_df = pd.DataFrame(industry_rows)
        selected_return_df = pd.DataFrame(selected_return_rows)
        worst_periods = period_df.sort_values("excess_return").head(6).to_dict("records") if not period_df.empty else []
        worst_industries = _worst_industries(industry_df)
        repeated = [
            {"code": code, "selected_periods": count}
            for code, count in code_counter.most_common(20)
        ]
        payload = {
            "generated_at": _now(),
            "scope": "holdout_ledger_postmortem_only_no_rerun",
            "ledger_winner": ledger.get("winner"),
            "holdout_window": ledger.get("holdout_window"),
            "result_summary": ledger.get("result_summary"),
            "period_count": int(len(rows)),
            "excess_sum_check": {
                "sum_period_excess": float(rows["excess_return"].sum()) if not rows.empty else None,
                "mean_times_periods": _mean_times_periods(ledger),
            },
            "worst_periods": worst_periods,
            "period_details": period_details,
            "industry_attribution": industry_df.to_dict("records") if not industry_df.empty else [],
            "worst_industries": worst_industries,
            "holding_return_by_code": selected_return_df.to_dict("records") if not selected_return_df.empty else [],
            "board_counts": dict(board_counter),
            "top_repeated_codes": repeated,
            "overlap": overlap_rows,
            "notes": [
                "size_proxy uses amount/(turn/100) when both fields are positive; otherwise amount. It is a liquidity/free-float proxy, not audited market capitalization.",
                "industry attribution is selected-stock holding return by industry, not a full Brinson decomposition versus benchmark.",
            ],
        }
        return payload
    finally:
        if own:
            db.close()


def generate_attribution_report(
    out_md: Path | None = None,
    out_json: Path | None = None,
    *,
    ledger_path: Path = LEDGER_PATH,
) -> dict:
    out_md = out_md or (OUT_DIR / "attribution_postmortem.md")
    out_json = out_json or (OUT_DIR / "attribution_postmortem.json")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    payload = generate_attribution(ledger_path)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_render_markdown(payload), encoding="utf-8")
    return payload


def _prices_on(db: ResearchPanelDB, date: str) -> pd.DataFrame:
    frame = db.query_df(
        """
        SELECT code, close, preclose, amount, turn, trade_status
        FROM prices_daily
        WHERE date=?
        """,
        (date,),
    )
    for col in ("close", "preclose", "amount", "turn"):
        frame[col] = pd.to_numeric(frame.get(col), errors="coerce")
    frame = frame[(frame["close"] > 0) & (frame["preclose"] > 0)]
    if "trade_status" in frame.columns:
        frame = frame[frame["trade_status"].astype(str).eq("1")]
    return frame.copy()


def _holding_returns(db: ResearchPanelDB, codes: list[str], entry_date: str, exit_date: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame(columns=["code", "holding_return"])
    placeholders = ",".join("?" for _ in codes)
    entry = db.query_df(
        f"SELECT code, close AS entry_close FROM prices_daily WHERE date=? AND code IN ({placeholders})",
        (entry_date, *codes),
    )
    exitp = db.query_df(
        f"SELECT code, close AS exit_close FROM prices_daily WHERE date=? AND code IN ({placeholders})",
        (exit_date, *codes),
    )
    out = entry.merge(exitp, on="code", how="inner")
    out["entry_close"] = pd.to_numeric(out["entry_close"], errors="coerce")
    out["exit_close"] = pd.to_numeric(out["exit_close"], errors="coerce")
    out = out[(out["entry_close"] > 0) & (out["exit_close"] > 0)].copy()
    out["holding_return"] = out["exit_close"] / out["entry_close"] - 1.0
    return out[["code", "holding_return"]]


def _size_proxy(frame: pd.DataFrame) -> pd.Series:
    amount = pd.to_numeric(frame.get("amount"), errors="coerce")
    turn = pd.to_numeric(frame.get("turn"), errors="coerce")
    proxy = amount / (turn / 100.0)
    return proxy.where((amount > 0) & (turn > 0), amount)


def _worst_industries(frame: pd.DataFrame) -> list[dict]:
    if frame.empty:
        return []
    grouped = frame.groupby("industry").agg(
        periods=("rebalance_date", "nunique"),
        observations=("count", "sum"),
        mean_holding_return=("mean_holding_return", "mean"),
    )
    grouped = grouped[grouped["observations"] >= 5]
    return grouped.sort_values("mean_holding_return").head(12).reset_index().to_dict("records")


def _render_markdown(payload: dict) -> str:
    lines = [
        "# Holdout 归因复盘",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 范围：{payload['scope']}",
        f"- winner：`{payload.get('ledger_winner')}`",
        f"- holdout：{payload.get('holdout_window')}",
        "- 注意：市值为 `amount/(turn/100)` 代理，不等于审计后的真实流通市值。",
        "",
        "## 亏损集中期",
        "",
        "| rebalance | exit | excess | portfolio | benchmark | size pct median | small share | boards |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in payload.get("worst_periods", []):
        lines.append(
            f"| {row['rebalance_date']} | {row['exit_date']} | {_fmt(row['excess_return'])} | {_fmt(row['portfolio_return'])} | {_fmt(row['benchmark_return'])} | {_fmt(row['selected_size_proxy_median_pct'])} | {_fmt(row['smallest_20pct_share'])} | {json.dumps(row['board_share'], ensure_ascii=False)} |"
        )
    lines.extend(["", "## 重复持仓", ""])
    lines.append("| code | selected periods |")
    lines.append("|---|---:|")
    for row in payload.get("top_repeated_codes", [])[:15]:
        lines.append(f"| {row['code']} | {row['selected_periods']} |")
    lines.extend(["", "## 最弱行业（按入选票持有期收益，非完整 Brinson）", ""])
    lines.append("| industry | periods | observations | mean holding return |")
    lines.append("|---|---:|---:|---:|")
    for row in payload.get("worst_industries", []):
        lines.append(
            f"| {row['industry']} | {int(row['periods'])} | {int(row['observations'])} | {_fmt(row['mean_holding_return'])} |"
        )
    lines.extend(["", "## 粘性", ""])
    lines.append("| rebalance | overlap count | jaccard |")
    lines.append("|---|---:|---:|")
    for row in payload.get("overlap", []):
        lines.append(f"| {row['rebalance_date']} | {row['overlap_count']} | {_fmt(row['jaccard'])} |")
    return "\n".join(lines) + "\n"


def _split_codes(value: str) -> list[str]:
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _board(code: str) -> str:
    if code.startswith("sh.688"):
        return "STAR_688"
    if code.startswith("sz.300"):
        return "ChiNext_300"
    if code.startswith("sz.301"):
        return "ChiNext_301"
    if code.startswith("sz.002"):
        return "SME_002"
    if code.startswith("sh.60"):
        return "SH_60"
    if code.startswith("sz.00"):
        return "SZ_00"
    return "other"


def _board_share(codes: list[str]) -> dict[str, float]:
    if not codes:
        return {}
    counts = Counter(_board(code) for code in codes)
    return {key: value / len(codes) for key, value in sorted(counts.items())}


def _share(values: pd.Series, label: str) -> float | None:
    clean = values.dropna()
    if clean.empty:
        return None
    return float((clean.astype(str) == label).mean())


def _safe_mean(values: pd.Series) -> float | None:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    return float(clean.mean()) if len(clean) else None


def _safe_median(values: pd.Series) -> float | None:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    return float(clean.median()) if len(clean) else None


def _mean_times_periods(ledger: dict) -> float | None:
    summary = ledger.get("result_summary") or {}
    mean = summary.get("mean")
    periods = summary.get("periods")
    if mean is None or periods is None:
        return None
    return float(mean) * int(periods)


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
    parser.add_argument("--ledger", default=str(LEDGER_PATH))
    return parser.parse_args(list(argv))


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    payload = generate_attribution_report(ledger_path=Path(args.ledger))
    print(json.dumps({"out": "research/out/attribution_postmortem.md", "periods": payload["period_count"]}, ensure_ascii=False))
