"""ETL + semantics check: ``query_stock_industry(date=T)`` -> ``industry_map``.

Stores one row per (code, query_date) so the same code can hold several as-of
snapshots. Whether baostock's ``date`` argument yields a genuine *historical*
classification (strict PIT) or merely the current classification filtered to the
listed set (quasi-PIT) is an open question (plan item #2); ``verify_asof_semantics``
answers it from the stored snapshots instead of assuming.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.bs_session import BaostockSession, timed_query
from research.panel_db import ResearchPanelDB, now_iso, open_panel
from research.sources.common import filter_a_share_stock_rows

TASK = "industry"

# Spread across years so any genuine historical reclassification shows up.
DEFAULT_DATES = ("2018-06-29", "2020-06-30", "2022-06-30", "2024-06-28", "2026-06-12")


def run(dates: tuple[str, ...] = DEFAULT_DATES, db: ResearchPanelDB | None = None) -> dict:
    run_id = uuid.uuid4().hex[:12]
    own = db is None
    db = db or open_panel()
    per_date: list[dict] = []
    try:
        db.set_metadata(f"run_params:{TASK}:{run_id}", json.dumps({"dates": dates}, ensure_ascii=False))
        with BaostockSession(db, run_id, "query_stock_industry") as bs:
            for day in dates:
                df, dur = timed_query(lambda day=day: bs.query_stock_industry(date=day))
                kept = filter_a_share_stock_rows(df, "code") if not df.empty else df
                rows = []
                for rec in kept.to_dict("records"):
                    rows.append({
                        "code": str(rec.get("code", "")).strip(),
                        "industry": str(rec.get("industry", "")).strip(),
                        "industry_classification": str(rec.get("industryClassification", "")).strip(),
                        "update_date": str(rec.get("updateDate", "")).strip(),
                        "query_date": day,
                        "fetched_at": now_iso(),
                    })
                n = db.upsert("industry_map", rows, ["code", "query_date"])
                db.log_call(run_id, "query_stock_industry", day, "ok", n, dur)
                distinct_update = sorted({r["update_date"] for r in rows if r["update_date"]})
                per_date.append({
                    "query_date": day,
                    "rows": n,
                    "non_empty_industry": sum(1 for r in rows if r["industry"]),
                    "distinct_update_dates": len(distinct_update),
                    "duration_sec": dur,
                })
        return {"task": TASK, "run_id": run_id, "per_date": per_date}
    finally:
        if own:
            db.close()


def verify_asof_semantics(db: ResearchPanelDB | None = None) -> dict:
    """Decide strict-PIT vs quasi-PIT from stored snapshots (no assumptions)."""
    own = db is None
    db = db or open_panel()
    try:
        df = db.query_df(
            """
            SELECT code, query_date, industry, industry_classification, update_date
            FROM industry_map
            ORDER BY code, query_date
            """
        )
        dates = sorted(df["query_date"].unique().tolist())
        by_code: dict[str, dict[str, str]] = defaultdict(dict)
        upd_by_code: dict[str, set[str]] = defaultdict(set)
        cls_by_code: dict[str, dict[str, str]] = defaultdict(dict)
        for rec in df.to_dict("records"):
            by_code[rec["code"]][rec["query_date"]] = rec["industry"]
            cls_by_code[rec["code"]][rec["query_date"]] = rec["industry_classification"]
            if str(rec["update_date"]).strip():
                upd_by_code[rec["code"]].add(str(rec["update_date"]).strip())

        changed_examples: list[dict] = []
        n_changed = 0
        for code, mapping in by_code.items():
            vals = {mapping[d] for d in dates if d in mapping and mapping[d]}
            cls_vals = {cls_by_code[code][d] for d in dates if d in cls_by_code[code] and cls_by_code[code][d]}
            if len(vals) > 1 or len(cls_vals) > 1:
                n_changed += 1
                if len(changed_examples) < 8:
                    changed_examples.append({
                        "code": code,
                        "industry_by_date": mapping,
                        "classification_by_date": cls_by_code[code],
                        "update_dates": sorted(upd_by_code.get(code, set())),
                    })

        # If update_date varies across query dates for a code, baostock is keying
        # the classification by date (a sign of genuine historical data).
        update_varies = sum(1 for s in upd_by_code.values() if len(s) > 1)

        if n_changed > 0 and update_varies > 0:
            verdict = "historical_variation_observed"
            note = (
                f"{n_changed} 只个股的 industry/industryClassification 随 query_date 变化，"
                f"{update_varies} 只个股的 updateDate 也变化；这支持历史口径，但仍不足以强称 strict_pit。"
            )
        else:
            verdict = "quasi_pit_or_scheme_changed"
            note = (
                "industry、industryClassification、updateDate 的联合证据不足以证明 strict PIT；"
                "行业字段按准点时/口径可能变化处理，局限已记录。"
            )
        return {
            "verdict": verdict,
            "note": note,
            "query_dates": dates,
            "codes_total": len(by_code),
            "codes_industry_changed_across_dates": n_changed,
            "codes_update_date_varies": update_varies,
            "changed_examples": changed_examples,
        }
    finally:
        if own:
            db.close()


if __name__ == "__main__":
    load = run()
    print(json.dumps(load, ensure_ascii=False, indent=2))
    print("=== as-of-T semantics ===")
    print(json.dumps(verify_asof_semantics(), ensure_ascii=False, indent=2))
