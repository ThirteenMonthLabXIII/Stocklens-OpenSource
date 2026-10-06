"""ETL: survivor-safe universe from ``query_stock_basic`` (all A incl. delisted).

This is the anti-survivor-bias root table: ``query_stock_basic`` returns every
listed *and delisted* security, each with ipoDate / outDate / status, so a
point-in-time universe at any T can be reconstructed (ipo<=T<(out or today)).
Indices are dropped; A-share stocks (type==1) are kept regardless of status.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.bs_session import BaostockSession, timed_query
from research.panel_db import ResearchPanelDB, now_iso, open_panel
from research.sources.common import filter_a_share_stock_rows

TASK = "universe"


def run(db: ResearchPanelDB | None = None, include_index: bool = False) -> dict:
    run_id = uuid.uuid4().hex[:12]
    own = db is None
    db = db or open_panel()
    try:
        db.set_metadata(f"run_params:{TASK}:{run_id}", json.dumps({"include_index": include_index}, ensure_ascii=False))
        with BaostockSession(db, run_id, "query_stock_basic") as bs:
            df, dur = timed_query(lambda: bs.query_stock_basic())
        total = int(len(df))
        keep = df if include_index else filter_a_share_stock_rows(
            df, "code", "type" if "type" in df.columns else None
        )
        rows = []
        for rec in keep.to_dict("records"):
            rows.append({
                "code": str(rec.get("code", "")).strip(),
                "code_name": str(rec.get("code_name", "")).strip(),
                "ipo_date": str(rec.get("ipoDate", "")).strip(),
                "out_date": str(rec.get("outDate", "")).strip(),
                "type": str(rec.get("type", "")).strip(),
                "status": str(rec.get("status", "")).strip(),
                "fetched_at": now_iso(),
            })
        n = db.upsert("universe", rows, ["code"])
        db.log_call(run_id, "query_stock_basic", "", "ok", n, dur)
        delisted = sum(1 for r in rows if r["out_date"])
        return {
            "task": TASK,
            "run_id": run_id,
            "raw_rows": total,
            "a_share_rows": len(rows),
            "delisted_rows": delisted,
            "active_rows": len(rows) - delisted,
            "duration_sec": dur,
        }
    finally:
        if own:
            db.close()


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
