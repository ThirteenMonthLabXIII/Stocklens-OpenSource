"""Step-1 self-tests: panel DB schema, isolation, and UPSERT idempotency.

Run with either::

    python -m pytest research/test_panel_db.py
    python research/test_panel_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research._testkit import raises, run_tests
from research.panel_db import (
    DATA_DIR,
    FORBIDDEN_DB_NAMES,
    PRODUCTION_DB_DIR,
    ResearchPanelDB,
    assert_isolated,
    open_panel,
    production_db_paths,
    snapshot_paths,
)

SELFTEST_DB = DATA_DIR / "panel_selftest.sqlite3"

EXPECTED_TABLES = {
    "universe", "prices_daily", "adjust_factor", "fundamentals_pit",
    "industry_map", "trade_calendar", "etl_progress", "etl_log", "run_metadata",
}


def _fresh_db() -> ResearchPanelDB:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(SELFTEST_DB) + suffix)
        if p.exists():
            p.unlink()
    return open_panel(SELFTEST_DB)


def test_isolation_rejects_production_paths():
    for name in FORBIDDEN_DB_NAMES:
        with raises(AssertionError):
            assert_isolated(PRODUCTION_DB_DIR / name)
    # any file inside my_data/ is rejected, even a novel name
    with raises(AssertionError):
        assert_isolated(PRODUCTION_DB_DIR / "anything.sqlite3")
    # a path outside research/data/ is rejected
    with raises(AssertionError):
        assert_isolated(ROOT / "panel.sqlite3")


def test_isolation_accepts_research_data_path():
    ok = assert_isolated(DATA_DIR / "panel.sqlite3")
    assert ok.parent == DATA_DIR.resolve()


def test_schema_creates_all_tables():
    db = _fresh_db()
    try:
        names = {
            r[0]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert EXPECTED_TABLES.issubset(names), EXPECTED_TABLES - names
    finally:
        db.close()


def test_upsert_is_idempotent():
    db = _fresh_db()
    try:
        rows = [
            {"code": "sh.600519", "code_name": "贵州茅台", "ipo_date": "2001-08-27",
             "out_date": "", "type": "1", "status": "1", "fetched_at": "t0"},
            {"code": "sz.000001", "code_name": "平安银行", "ipo_date": "1991-04-03",
             "out_date": "", "type": "1", "status": "1", "fetched_at": "t0"},
        ]
        db.upsert("universe", rows, ["code"])
        fp1 = db.table_fingerprint("universe")
        # re-run identical rows -> no new rows, same fingerprint
        db.upsert("universe", rows, ["code"])
        fp2 = db.table_fingerprint("universe")
        assert fp1 == fp2 == {"rows": 2, "sha256": fp1["sha256"]}
        assert fp1["rows"] == 2
        # update one column -> row count stable, fingerprint changes (UPSERT replaced)
        rows[0]["status"] = "0"
        db.upsert("universe", rows, ["code"])
        fp3 = db.table_fingerprint("universe")
        assert fp3["rows"] == 2
        assert fp3["sha256"] != fp1["sha256"]
    finally:
        db.close()


def test_production_snapshot_is_stable_across_panel_writes():
    prod = production_db_paths()
    before = snapshot_paths(prod)
    db = _fresh_db()
    try:
        db.upsert(
            "trade_calendar",
            [{"calendar_date": "2024-01-02", "is_trading_day": 1}],
            ["calendar_date"],
        )
    finally:
        db.close()
    after = snapshot_paths(prod)
    assert before == after, "panel writes must not touch any production db"


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
