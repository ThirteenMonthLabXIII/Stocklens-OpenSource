from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research._testkit import run_tests
from research.data_quality_report import generate
from research.metric_metadata import QUALITY_SCALE_SUSPECT
from research.panel_db import DATA_DIR, ResearchPanelDB, open_panel
from research.pit_join import as_of, fundamentals_as_of, universe_as_of

TEST_DB = DATA_DIR / "panel_join_selftest.sqlite3"


def _fresh_db() -> ResearchPanelDB:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(TEST_DB) + suffix)
        if p.exists():
            p.unlink()
    db = open_panel(TEST_DB)
    db.upsert(
        "universe",
        [
            {"code": "sh.600001", "code_name": "退市样例", "ipo_date": "2018-01-01", "out_date": "2020-06-01", "type": "1", "status": "0", "fetched_at": "t"},
            {"code": "sh.600519", "code_name": "贵州茅台", "ipo_date": "2001-08-27", "out_date": "", "type": "1", "status": "1", "fetched_at": "t"},
        ],
        ["code"],
    )
    db.upsert(
        "fundamentals_pit",
        [
            {"code": "sh.600519", "stat_date": "2020-03-31", "pub_date": "2020-04-30", "metric": "roe", "value": 10.0, "source_api": "fixture", "fetched_at": "t"},
            {"code": "sh.600519", "stat_date": "2020-06-30", "pub_date": "2020-08-30", "metric": "roe", "value": 12.0, "source_api": "fixture", "fetched_at": "t"},
        ],
        ["code", "stat_date", "metric", "pub_date"],
    )
    db.upsert(
        "fundamentals_pit",
        [
            {
                "code": "sh.600519",
                "stat_date": "2020-06-30",
                "pub_date": "2020-08-30",
                "metric": "debt_ratio",
                "value": 60.0,
                "source_api": "fixture",
                "metric_quality": QUALITY_SCALE_SUSPECT,
                "missing_reason": "adjacent_quarter_scale_jump_gt_20x",
                "fetched_at": "t",
            }
        ],
        ["code", "stat_date", "metric", "pub_date"],
    )
    db.upsert(
        "industry_map",
        [{"code": "sh.600519", "industry": "酒、饮料和精制茶制造业", "industry_classification": "C15", "update_date": "2020-01-01", "query_date": "2020-06-30", "fetched_at": "t"}],
        ["code", "query_date"],
    )
    db.upsert(
        "prices_daily",
        [{"code": "sh.600519", "date": "2020-08-31", "open": 1, "high": 1, "low": 1, "close": 1, "preclose": 1, "volume": 1, "amount": 1, "turn": 1, "pe_ttm": 1, "pb_mrq": 1, "ps_ttm": 1, "pcf_ncf_ttm": 1, "trade_status": "1", "fetched_at": "t"}],
        ["code", "date"],
    )
    db.upsert(
        "adjust_factor",
        [{"code": "sh.600519", "divid_operate_date": "2020-07-01", "fore_adjust_factor": 1.0, "back_adjust_factor": 2.0, "fetched_at": "t"}],
        ["code", "divid_operate_date"],
    )
    return db


def test_asof_hides_financial_before_pub_date():
    db = _fresh_db()
    try:
        before = fundamentals_as_of("2020-04-29", db, ["sh.600519"])
        on_pub = fundamentals_as_of("2020-04-30", db, ["sh.600519"])
        later = fundamentals_as_of("2020-09-01", db, ["sh.600519"])
        assert before.empty
        assert float(on_pub.loc[on_pub["metric"] == "roe"].iloc[0]["value"]) == 10.0
        assert float(later.loc[later["metric"] == "roe"].iloc[0]["value"]) == 12.0
    finally:
        db.close()


def test_universe_drops_delisted_after_out_date():
    db = _fresh_db()
    try:
        before = set(universe_as_of("2020-05-31", db)["code"])
        after = set(universe_as_of("2020-06-15", db)["code"])
        assert "sh.600001" in before
        assert "sh.600001" not in after
    finally:
        db.close()


def test_asof_assembles_panel_and_report():
    db = _fresh_db()
    try:
        panel = as_of("2020-08-31", db)
        row = panel.loc[panel["code"] == "sh.600519"].iloc[0]
        assert row["industry"] == "酒、饮料和精制茶制造业"
        assert float(row["roe"]) == 12.0
        assert row["debt_ratio_quality"] == QUALITY_SCALE_SUSPECT
        assert row["debt_ratio_missing_reason"] == "adjacent_quarter_scale_jump_gt_20x"
        out = DATA_DIR / "selftest_quality.md"
        payload = generate(db, out)
        assert out.exists()
        assert payload["assertions"]["lookahead"]["status"] == "PASS"
        assert payload["assertions"]["survivorship"]["status"] == "PASS"
    finally:
        db.close()


def test_survivorship_assertion_uses_runtime_date():
    db = _fresh_db()
    try:
        out = DATA_DIR / "selftest_quality.md"
        payload = generate(db, out)
        evidence = payload["assertions"]["survivorship"]["evidence"]
        assert evidence["checked_current_date"] == date.today().isoformat()
        assert len(evidence["checked_current_date"]) == 10
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
