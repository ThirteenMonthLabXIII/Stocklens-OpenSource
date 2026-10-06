"""ETL: daily hfq prices + adjustment factors from baostock.

This module writes only to the independent research panel database. It is
resume-safe through ``etl_progress`` and records every per-code call in
``etl_log``. The sample path is intentionally sequential; a full-market run can
fan this same ``collect_rows_for_code`` worker out to separate processes because
baostock's global socket is not thread-safe.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.bs_session import BaostockSession, query_to_df
from research.panel_db import PANEL_START_DATE, ResearchPanelDB, now_iso, open_panel

TASK = "prices"

PRICE_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,turn,"
    "tradestatus,peTTM,pbMRQ,psTTM,pcfNcfTTM"
)


def _f(value: Any) -> float | None:
    try:
        text = str(value).strip().replace(",", "")
        if text in {"", "-", "--", "nan", "None"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _bounded_start(ipo_date: str, start_date: str) -> str:
    if ipo_date and len(ipo_date) >= 10 and ipo_date > start_date:
        return ipo_date
    return start_date


def _bounded_end(out_date: str, end_date: str) -> str:
    if out_date and len(out_date) >= 10 and out_date < end_date:
        return out_date
    return end_date


def collect_rows_for_code(
    bs,
    code: str,
    *,
    start_date: str = PANEL_START_DATE,
    end_date: str | None = None,
    ipo_date: str = "",
    out_date: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch hfq daily prices and adjustment factors for one baostock code."""
    end_date = end_date or date.today().isoformat()
    start = _bounded_start(ipo_date, start_date)
    end = _bounded_end(out_date, end_date)
    fetched = now_iso()
    prices: list[dict[str, Any]] = []
    factors: list[dict[str, Any]] = []
    if start > end:
        return prices, factors

    price_df = query_to_df(
        lambda: bs.query_history_k_data_plus(
            code,
            PRICE_FIELDS,
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="1",
        )
    )
    for rec in price_df.to_dict("records"):
        day = str(rec.get("date", "")).strip()
        if not day:
            continue
        prices.append(
            {
                "code": str(rec.get("code", code)).strip(),
                "date": day,
                "open": _f(rec.get("open")),
                "high": _f(rec.get("high")),
                "low": _f(rec.get("low")),
                "close": _f(rec.get("close")),
                "preclose": _f(rec.get("preclose")),
                "volume": _f(rec.get("volume")),
                "amount": _f(rec.get("amount")),
                "turn": _f(rec.get("turn")),
                "pe_ttm": _f(rec.get("peTTM")),
                "pb_mrq": _f(rec.get("pbMRQ")),
                "ps_ttm": _f(rec.get("psTTM")),
                "pcf_ncf_ttm": _f(rec.get("pcfNcfTTM")),
                "trade_status": str(rec.get("tradestatus", "")).strip(),
                "fetched_at": fetched,
            }
        )

    factor_df = query_to_df(
        lambda: bs.query_adjust_factor(code=code, start_date=start, end_date=end)
    )
    for rec in factor_df.to_dict("records"):
        day = str(rec.get("dividOperateDate", "")).strip()
        if not day:
            continue
        factors.append(
            {
                "code": str(rec.get("code", code)).strip(),
                "divid_operate_date": day,
                "fore_adjust_factor": _f(rec.get("foreAdjustFactor")),
                "back_adjust_factor": _f(rec.get("backAdjustFactor")),
                "fetched_at": fetched,
            }
        )
    return prices, factors


def run(
    codes: list[str],
    db: ResearchPanelDB | None = None,
    *,
    start_date: str = PANEL_START_DATE,
    end_date: str | None = None,
) -> dict:
    run_id = uuid.uuid4().hex[:12]
    end_date = end_date or date.today().isoformat()
    own = db is None
    db = db or open_panel()
    done = db.completed_codes(TASK)
    todo = [c for c in codes if c not in done]
    meta = {
        r["code"]: r
        for r in db.query_df("SELECT code, ipo_date, out_date FROM universe").to_dict("records")
    }
    total_price_rows = 0
    total_factor_rows = 0
    processed = 0
    try:
        db.set_metadata(
            f"run_params:{TASK}:{run_id}",
            json.dumps({"start_date": start_date, "end_date": end_date}, ensure_ascii=False),
        )
        with BaostockSession(db, run_id, "prices") as bs:
            for code in todo:
                started = time.perf_counter()
                m = meta.get(code, {})
                try:
                    prices, factors = collect_rows_for_code(
                        bs,
                        code,
                        start_date=start_date,
                        end_date=end_date,
                        ipo_date=str(m.get("ipo_date", "")),
                        out_date=str(m.get("out_date", "")),
                    )
                    price_n = db.upsert("prices_daily", prices, ["code", "date"]) if prices else 0
                    factor_n = db.upsert("adjust_factor", factors, ["code", "divid_operate_date"]) if factors else 0
                    dur = round(time.perf_counter() - started, 3)
                    db.mark_progress(TASK, code, "done", price_n, dur)
                    db.log_call(run_id, "prices", code, "ok", price_n + factor_n, dur)
                    total_price_rows += price_n
                    total_factor_rows += factor_n
                except Exception as exc:  # noqa: BLE001 - record and keep the batch resumable
                    dur = round(time.perf_counter() - started, 3)
                    db.mark_progress(TASK, code, "error", 0, dur)
                    db.log_call(run_id, "prices", code, "error", 0, dur, str(exc)[:300])
                processed += 1
        return {
            "task": TASK,
            "run_id": run_id,
            "codes": len(codes),
            "processed": processed,
            "skipped_done": len(codes) - len(todo),
            "price_rows_written": total_price_rows,
            "factor_rows_written": total_factor_rows,
        }
    finally:
        if own:
            db.close()


if __name__ == "__main__":
    from research.sample_universe import all_sample

    sample_codes = [s.bs_code for s in all_sample()]
    print(json.dumps(run(sample_codes), ensure_ascii=False, indent=2))
