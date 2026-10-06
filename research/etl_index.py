"""ETL: HS300 daily index bars.

This side-car ETL intentionally creates ``hs300_daily`` outside
``panel_db.SCHEMA`` so Phase C methodology files remain unchanged before the
one-shot holdout gate.

baostock is tried first to match the rest of the panel ETL. If the account/IP
is rejected, the script falls back to AkShare's public HS300 daily endpoint and
records the actual source in run metadata/logs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.bs_session import BaostockSession, query_to_df
from research.panel_db import PANEL_START_DATE, ResearchPanelDB, now_iso, open_panel

HS300_CODE = "sh.000300"
HS300_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,pctChg"
HS300_SCHEMA = """
CREATE TABLE IF NOT EXISTS hs300_daily (
    code TEXT,
    date TEXT,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    preclose REAL,
    volume REAL,
    amount REAL,
    pct_chg REAL,
    fetched_at TEXT,
    PRIMARY KEY (code, date)
);
"""


def _f(value: Any) -> float | None:
    try:
        text = str(value).strip().replace(",", "")
        if text in {"", "-", "--", "nan", "None"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def ensure_schema(db: ResearchPanelDB) -> None:
    db.conn.executescript(HS300_SCHEMA)
    db.conn.commit()


def collect_hs300_rows(
    bs,
    *,
    start_date: str = PANEL_START_DATE,
    end_date: str = "2026-06-16",
) -> list[dict[str, Any]]:
    fetched = now_iso()
    df = query_to_df(
        lambda: bs.query_history_k_data_plus(
            HS300_CODE,
            HS300_FIELDS,
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="3",
        )
    )
    rows: list[dict[str, Any]] = []
    for rec in df.to_dict("records"):
        day = str(rec.get("date", "")).strip()
        if not day:
            continue
        rows.append(
            {
                "code": str(rec.get("code", HS300_CODE)).strip() or HS300_CODE,
                "date": day,
                "open": _f(rec.get("open")),
                "high": _f(rec.get("high")),
                "low": _f(rec.get("low")),
                "close": _f(rec.get("close")),
                "preclose": _f(rec.get("preclose")),
                "volume": _f(rec.get("volume")),
                "amount": _f(rec.get("amount")),
                "pct_chg": _f(rec.get("pctChg")),
                "fetched_at": fetched,
            }
        )
    return rows


def collect_hs300_rows_akshare(
    *,
    start_date: str = PANEL_START_DATE,
    end_date: str = "2026-06-16",
) -> list[dict[str, Any]]:
    import akshare as ak
    import pandas as pd

    fetched = now_iso()
    df = ak.stock_zh_index_daily(symbol="sh000300")
    if df.empty:
        return []
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out = out[(out["date"] >= start_date) & (out["date"] <= end_date)].sort_values("date")
    out["preclose"] = pd.to_numeric(out["close"], errors="coerce").shift(1)
    out["pct_chg"] = pd.to_numeric(out["close"], errors="coerce") / out["preclose"] * 100.0 - 100.0
    rows: list[dict[str, Any]] = []
    for rec in out.to_dict("records"):
        day = str(rec.get("date", "")).strip()
        if not day or day == "NaT":
            continue
        rows.append(
            {
                "code": HS300_CODE,
                "date": day,
                "open": _f(rec.get("open")),
                "high": _f(rec.get("high")),
                "low": _f(rec.get("low")),
                "close": _f(rec.get("close")),
                "preclose": _f(rec.get("preclose")),
                "volume": _f(rec.get("volume")),
                "amount": None,
                "pct_chg": _f(rec.get("pct_chg")),
                "fetched_at": fetched,
            }
        )
    return rows


def run(
    db: ResearchPanelDB | None = None,
    *,
    start_date: str = PANEL_START_DATE,
    end_date: str = "2026-06-16",
) -> dict:
    run_id = uuid.uuid4().hex[:12]
    own = db is None
    db = db or open_panel()
    started = time.perf_counter()
    try:
        ensure_schema(db)
        source = "baostock"
        try:
            with BaostockSession(db, run_id, "hs300") as bs:
                rows = collect_hs300_rows(bs, start_date=start_date, end_date=end_date)
        except Exception as exc:
            source = "akshare"
            db.log_call(run_id, "hs300_baostock", HS300_CODE, "error", 0, 0.0, str(exc)[:300])
            rows = collect_hs300_rows_akshare(start_date=start_date, end_date=end_date)
        db.set_metadata(
            f"run_params:hs300:{run_id}",
            json.dumps({"start_date": start_date, "end_date": end_date, "source": source}, ensure_ascii=False),
        )
        written = db.upsert("hs300_daily", rows, ["code", "date"]) if rows else 0
        duration = round(time.perf_counter() - started, 3)
        db.log_call(run_id, f"hs300_{source}", HS300_CODE, "ok", written, duration)
        return {
            "task": "hs300",
            "run_id": run_id,
            "code": HS300_CODE,
            "source": source,
            "start_date": start_date,
            "end_date": end_date,
            "rows_written": written,
            "total_rows": int(db.scalar("SELECT COUNT(*) FROM hs300_daily") or 0),
            "duration_sec": duration,
        }
    except Exception as exc:
        duration = round(time.perf_counter() - started, 3)
        db.log_call(run_id, "hs300", HS300_CODE, "error", 0, duration, str(exc)[:300])
        raise
    finally:
        if own:
            db.close()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default=PANEL_START_DATE)
    parser.add_argument("--end-date", default="2026-06-16")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    print(json.dumps(run(start_date=args.start_date, end_date=args.end_date), ensure_ascii=False, indent=2))
