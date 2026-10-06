"""ETL: trading calendar from ``query_trade_dates`` -> ``trade_calendar``.

Used downstream to align rebalance dates to real trading days and to tell a
genuine non-trading day apart from a stock-specific suspension.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.bs_session import BaostockSession, timed_query
from research.panel_db import PANEL_START_DATE, ResearchPanelDB, open_panel

TASK = "calendar"


def run(db: ResearchPanelDB | None = None,
        start_date: str = PANEL_START_DATE,
        end_date: str | None = None) -> dict:
    run_id = uuid.uuid4().hex[:12]
    end_date = end_date or date.today().isoformat()
    own = db is None
    db = db or open_panel()
    try:
        db.set_metadata(
            f"run_params:{TASK}:{run_id}",
            json.dumps({"start_date": start_date, "end_date": end_date}, ensure_ascii=False),
        )
        with BaostockSession(db, run_id, "query_trade_dates") as bs:
            df, dur = timed_query(
                lambda: bs.query_trade_dates(start_date=start_date, end_date=end_date)
            )
        rows = []
        for rec in df.to_dict("records"):
            day = str(rec.get("calendar_date", "")).strip()
            if not day:
                continue
            rows.append({
                "calendar_date": day,
                "is_trading_day": int(str(rec.get("is_trading_day", "0")).strip() or "0"),
            })
        n = db.upsert("trade_calendar", rows, ["calendar_date"])
        db.log_call(run_id, "query_trade_dates", "", "ok", n, dur)
        trading = sum(r["is_trading_day"] for r in rows)
        return {
            "task": TASK,
            "run_id": run_id,
            "start_date": start_date,
            "end_date": end_date,
            "calendar_days": len(rows),
            "trading_days": trading,
            "duration_sec": dur,
        }
    finally:
        if own:
            db.close()


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
