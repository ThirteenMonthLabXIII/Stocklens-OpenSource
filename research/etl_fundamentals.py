"""ETL: point-in-time fundamentals (long table) from baostock quarterly APIs.

Field mapping is **live-verified** (not guessed), see the Phase B notes:

    metric          source api            column            scale
    roe             query_profit_data     roeAvg            x100 (decimal->%)
    net_margin      query_profit_data     npMargin          x100
    gross_margin    query_profit_data     gpMargin          x100
    profit_growth   query_growth_data     YOYPNI (归母)     x100
    debt_ratio      query_balance_data    liabilityToAsset  as reported
    revenue_growth  query_profit_data     MBRevenue YoY     computed, x100

baostock's growth API exposes no revenue-YoY column, so ``revenue_growth`` is
computed from same-quarter MBRevenue year-over-year, with ``pub_date`` set to the
*later* period's pubDate (the moment both numbers were simultaneously visible) —
this is what keeps it look-ahead safe.

Each (code, year, quarter) call returns a single row with one pubDate; baostock
does not expose an intra-period restatement history, so the long table holds one
pub_date per (code, stat_date, metric). PIT granularity is therefore
"first publication of a period", which defends the dominant look-ahead risk
(using a period before it was reported).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.bs_session import BaostockSession, query_to_df
from research.metric_metadata import QUALITY_MISSING, QUALITY_PASS, QUALITY_SCALE_SUSPECT
from research.panel_db import ResearchPanelDB, now_iso, open_panel

TASK = "fundamentals"
FUND_TARGET_START_YEAR = 2023
FUND_TARGET_END_YEAR = 2024
FUND_FETCH_START_YEAR = FUND_TARGET_START_YEAR - 1
FUND_START_YEAR = FUND_FETCH_START_YEAR
FUND_END_YEAR = FUND_TARGET_END_YEAR

PROFIT_METRICS = {"roe": "roeAvg", "net_margin": "npMargin", "gross_margin": "gpMargin"}
GROWTH_METRICS = {"profit_growth": "YOYPNI"}
BALANCE_METRICS = {"debt_ratio": "liabilityToAsset"}


def _f(value) -> float | None:
    try:
        text = str(value).strip()
        if text == "":
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _year_range(
    ipo_date: str,
    out_date: str,
    start_year_override: int | None = None,
    end_year_override: int | None = None,
) -> tuple[int, int]:
    this_year = date.today().year
    start = start_year_override or FUND_START_YEAR
    if ipo_date and len(ipo_date) >= 4 and ipo_date[:4].isdigit():
        start = max(start, int(ipo_date[:4]) - 1)
    end = min(this_year, end_year_override) if end_year_override else this_year
    if out_date and len(out_date) >= 4 and out_date[:4].isdigit():
        end = min(end, int(out_date[:4]))
    return start, end


def collect_rows_for_code(
    bs,
    code: str,
    ipo_date: str = "",
    out_date: str = "",
    *,
    start_year: int | None = None,
    end_year: int | None = None,
) -> list[dict]:
    """Fetch all quarterly fundamentals for one code; return long-table rows.

    Pure data-collection (no DB writes) so a multiprocess worker can return rows
    to a single writer process in step 7.
    """
    start_year, end_year = _year_range(ipo_date, out_date, start_year, end_year)
    fetched = now_iso()
    rows: list[dict] = []
    # MBRevenue by quarter-key for YoY: {(year,q): (value, pubDate, statDate)}
    mbrev: dict[tuple[int, int], tuple[float | None, str, str]] = {}

    def emit(stat_date, pub_date, metric, value, api, quality=QUALITY_PASS, missing_reason=None):
        if not stat_date or not pub_date:
            return
        rows.append({
            "code": code, "stat_date": stat_date, "pub_date": pub_date,
            "metric": metric, "value": value, "source_api": api,
            "metric_quality": quality, "missing_reason": missing_reason,
            "fetched_at": fetched,
        })

    def emit_source_metric(stat_date, pub_date, metric, value, api):
        if value is None:
            emit(stat_date, pub_date, metric, None, api, QUALITY_MISSING, "source_value_missing")
        else:
            emit(stat_date, pub_date, metric, value, api)

    for year in range(start_year, end_year + 1):
        for q in (1, 2, 3, 4):
            profit = query_to_df(lambda y=year, qq=q: bs.query_profit_data(code=code, year=y, quarter=qq))
            if not profit.empty:
                rec = profit.iloc[0].to_dict()
                stat = str(rec.get("statDate", "")).strip()
                pub = str(rec.get("pubDate", "")).strip()
                for metric, col in PROFIT_METRICS.items():
                    v = _f(rec.get(col))
                    emit_source_metric(stat, pub, metric, None if v is None else v * 100.0, "query_profit_data")
                mb = _f(rec.get("MBRevenue"))
                if pub and stat:
                    mbrev[(year, q)] = (mb, pub, stat)

            growth = query_to_df(lambda y=year, qq=q: bs.query_growth_data(code=code, year=y, quarter=qq))
            if not growth.empty:
                rec = growth.iloc[0].to_dict()
                stat = str(rec.get("statDate", "")).strip()
                pub = str(rec.get("pubDate", "")).strip()
                for metric, col in GROWTH_METRICS.items():
                    v = _f(rec.get(col))
                    emit_source_metric(stat, pub, metric, None if v is None else v * 100.0, "query_growth_data")

            balance = query_to_df(lambda y=year, qq=q: bs.query_balance_data(code=code, year=y, quarter=qq))
            if not balance.empty:
                rec = balance.iloc[0].to_dict()
                stat = str(rec.get("statDate", "")).strip()
                pub = str(rec.get("pubDate", "")).strip()
                for metric, col in BALANCE_METRICS.items():
                    v = _f(rec.get(col))
                    emit_source_metric(stat, pub, metric, v, "query_balance_data")

    # revenue_growth: same-quarter MBRevenue YoY; visible at the later period's pubDate.
    for (year, q), (cur, pub, stat) in mbrev.items():
        prior = mbrev.get((year - 1, q))
        if cur is None:
            emit(stat, pub, "revenue_growth", None, "query_profit_data(MBRevenue)", QUALITY_MISSING, "current_mbrevenue_missing")
            continue
        if prior is None:
            emit(stat, pub, "revenue_growth", None, "query_profit_data(MBRevenue)", QUALITY_MISSING, "prior_year_mbrevenue_missing")
            continue
        base = prior[0]
        if base is None:
            emit(stat, pub, "revenue_growth", None, "query_profit_data(MBRevenue)", QUALITY_MISSING, "prior_year_mbrevenue_missing")
            continue
        if base in (None, 0):
            emit(stat, pub, "revenue_growth", None, "query_profit_data(MBRevenue)", QUALITY_MISSING, "prior_year_mbrevenue_zero")
            continue
        stat_date = _stat_date_for(year, q)
        emit(stat_date, pub, "revenue_growth", (cur / base - 1.0) * 100.0, "query_profit_data(MBRevenue)")
    _apply_debt_ratio_quality(rows)
    return rows


def _stat_date_for(year: int, q: int) -> str:
    return {1: f"{year}-03-31", 2: f"{year}-06-30", 3: f"{year}-09-30", 4: f"{year}-12-31"}[q]


def _apply_debt_ratio_quality(rows: list[dict]) -> None:
    by_code_year: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if row.get("metric") != "debt_ratio" or row.get("value") is None:
            continue
        stat_date = str(row.get("stat_date", ""))
        if len(stat_date) < 4:
            continue
        by_code_year.setdefault((str(row.get("code", "")), stat_date[:4]), []).append(row)

    for grouped in by_code_year.values():
        ordered = sorted(grouped, key=lambda r: str(r.get("stat_date", "")))
        for prev, cur in zip(ordered, ordered[1:]):
            a = abs(float(prev["value"]))
            b = abs(float(cur["value"]))
            high = max(a, b)
            low = min(a, b)
            jump = high > 0 if low == 0 else high / low > 20.0
            if jump:
                for row in (prev, cur):
                    row["metric_quality"] = QUALITY_SCALE_SUSPECT
                    row["missing_reason"] = "adjacent_quarter_scale_jump_gt_20x"


def analyze_debt_ratio_scale(
    rows: list[dict],
    *,
    target_start_year: int = FUND_TARGET_START_YEAR,
    target_end_year: int = FUND_TARGET_END_YEAR,
) -> dict:
    values = []
    examples = []
    for row in rows:
        if row.get("metric") != "debt_ratio" or row.get("value") is None:
            continue
        stat = str(row.get("stat_date", ""))
        if len(stat) < 4 or not stat[:4].isdigit():
            continue
        year = int(stat[:4])
        if year < target_start_year or year > target_end_year:
            continue
        value = float(row["value"])
        values.append(value)
        if (value < 0 or value > 100) and len(examples) < 8:
            examples.append({
                "code": row.get("code"),
                "stat_date": row.get("stat_date"),
                "pub_date": row.get("pub_date"),
                "raw_value": value,
            })
    total = len(values)
    le_1 = sum(1 for v in values if 0 <= v <= 1)
    between = sum(1 for v in values if 1 < v <= 100)
    gt_100 = sum(1 for v in values if v > 100)
    negative = sum(1 for v in values if v < 0)
    if total == 0:
        decision = "blocked_no_valid_values"
        raw_scale = "unknown"
        output_scale = "blocked"
        transform = "none"
    elif le_1 / total >= 0.8:
        decision = "decimal_ratio_to_percent"
        raw_scale = "decimal_ratio"
        output_scale = "percent"
        transform = "raw * 100"
    elif between / total >= 0.8:
        decision = "percent_as_reported"
        raw_scale = "percent_as_reported"
        output_scale = "percent"
        transform = "raw"
    else:
        decision = "blocked_mixed_scale"
        raw_scale = "mixed_or_unknown"
        output_scale = "blocked"
        transform = "none"
    return {
        "metric": "debt_ratio",
        "target_start_year": target_start_year,
        "target_end_year": target_end_year,
        "valid_values": total,
        "distribution": {"0_to_1": le_1, "gt_1_to_100": between, "gt_100": gt_100, "negative": negative},
        "decision": decision,
        "raw_scale": raw_scale,
        "output_scale": output_scale,
        "transform": transform,
        "majority_threshold": 0.8,
        "abnormal_examples": examples,
        "quality_rule": "adjacent same-year quarterly magnitude jumps over 20x are scale_suspect",
    }


def apply_debt_ratio_scale(rows: list[dict], decision: dict) -> None:
    if decision.get("decision") != "decimal_ratio_to_percent":
        return
    for row in rows:
        if row.get("metric") == "debt_ratio" and row.get("value") is not None:
            row["value"] = float(row["value"]) * 100.0


def run(
    codes: list[str],
    db: ResearchPanelDB | None = None,
    *,
    start_year: int | None = FUND_FETCH_START_YEAR,
    end_year: int | None = FUND_TARGET_END_YEAR,
    target_start_year: int = FUND_TARGET_START_YEAR,
    target_end_year: int = FUND_TARGET_END_YEAR,
    retry_errors: bool = False,
) -> dict:
    run_id = uuid.uuid4().hex[:12]
    own = db is None
    db = db or open_panel()
    skip_statuses = {"done"} if retry_errors else {"done", "error"}
    progress = db.query_df(
        "SELECT code, status FROM etl_progress WHERE task=?",
        (TASK,),
    ).to_dict("records")
    skip = {str(r["code"]) for r in progress if str(r.get("status")) in skip_statuses}
    todo = [c for c in codes if c not in skip]
    meta = {r["code"]: r for r in db.query_df(
        "SELECT code, ipo_date, out_date FROM universe"
    ).to_dict("records")}
    total_rows = 0
    processed = 0
    collected: dict[str, tuple[list[dict], float]] = {}
    try:
        db.set_metadata(
            f"run_params:{TASK}:{run_id}",
            json.dumps({
                "fetch_start_year": start_year,
                "fetch_end_year": end_year,
                "target_start_year": target_start_year,
                "target_end_year": target_end_year,
                "retry_errors": retry_errors,
            }, ensure_ascii=False),
        )
        db.set_metadata("fundamentals_target_start_year", str(target_start_year))
        db.set_metadata("fundamentals_target_end_year", str(target_end_year))
        with BaostockSession(db, run_id, "fundamentals") as bs:
            import time as _t
            for code in todo:
                started = _t.perf_counter()
                m = meta.get(code, {})
                try:
                    rows = collect_rows_for_code(
                        bs,
                        code,
                        str(m.get("ipo_date", "")),
                        str(m.get("out_date", "")),
                        start_year=start_year,
                        end_year=end_year,
                    )
                    dur = round(_t.perf_counter() - started, 3)
                    collected[code] = (rows, dur)
                except Exception as exc:  # noqa: BLE001
                    dur = round(_t.perf_counter() - started, 3)
                    db.mark_progress(TASK, code, "error", 0, dur)
                    db.log_call(run_id, "fundamentals", code, "error", 0, dur, str(exc)[:300])
                processed += 1
        all_rows = [row for rows, _ in collected.values() for row in rows]
        decision = analyze_debt_ratio_scale(
            all_rows,
            target_start_year=target_start_year,
            target_end_year=target_end_year,
        )
        apply_debt_ratio_scale(all_rows, decision)
        _apply_debt_ratio_quality(all_rows)
        db.set_metadata("debt_ratio_scale_decision", json.dumps(decision, ensure_ascii=False, sort_keys=True))
        by_code: dict[str, list[dict]] = {}
        for row in all_rows:
            by_code.setdefault(str(row["code"]), []).append(row)
        for code, (_, dur) in collected.items():
            rows = by_code.get(code, [])
            n = db.upsert("fundamentals_pit", rows, ["code", "stat_date", "metric", "pub_date"]) if rows else 0
            db.mark_progress(TASK, code, "done", n, dur)
            db.log_call(run_id, "fundamentals", code, "ok", n, dur)
            total_rows += n
        return {"task": TASK, "run_id": run_id, "codes": len(codes),
                "fetch_start_year": start_year or FUND_FETCH_START_YEAR,
                "fetch_end_year": end_year or date.today().year,
                "target_start_year": target_start_year,
                "target_end_year": target_end_year,
                "debt_ratio_scale_decision": decision,
                "processed": processed, "skipped_done": len(codes) - len(todo),
                "rows_written": total_rows}
    finally:
        if own:
            db.close()


def main(argv: list[str] | None = None) -> int:
    from research.sample_universe import all_sample

    parser = argparse.ArgumentParser(description="Phase B sample fundamentals ETL.")
    parser.add_argument("--sample-size", type=int, default=40)
    parser.add_argument("--start-year", type=int, default=FUND_FETCH_START_YEAR)
    parser.add_argument("--end-year", type=int, default=FUND_TARGET_END_YEAR)
    parser.add_argument("--target-start-year", type=int, default=FUND_TARGET_START_YEAR)
    parser.add_argument("--target-end-year", type=int, default=FUND_TARGET_END_YEAR)
    parser.add_argument("--retry-errors", action="store_true")
    args = parser.parse_args(argv)

    codes = [s.bs_code for s in all_sample()[:args.sample_size]]
    result = run(
        codes,
        start_year=args.start_year,
        end_year=args.end_year,
        target_start_year=args.target_start_year,
        target_end_year=args.target_end_year,
        retry_errors=args.retry_errors,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
