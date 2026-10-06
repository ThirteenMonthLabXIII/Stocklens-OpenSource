"""Independent point-in-time panel database for Phase B.

Hard isolation from the production stack:
  * the panel file lives under ``research/data/`` only;
  * this module never imports ``stock_tool.core.v1_database`` (or any production
    module) and never touches ``my_data/*.sqlite3``;
  * every write is an idempotent UPSERT (never append, never ``replace`` a whole
    table), so re-running an ETL converges to the same rows.

See the Phase B plan for the table semantics (long-table PIT fundamentals,
survivor-safe universe, point-in-time industry/valuation, adjust factors).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

RESEARCH_DIR = Path(__file__).resolve().parent
DATA_DIR = RESEARCH_DIR / "data"
OUT_DIR = RESEARCH_DIR / "out"
PANEL_DB_PATH = DATA_DIR / "panel.sqlite3"

# Panel history window: Phase C trains from 2019; keep 2018 for 20/60-day lookback.
PANEL_START_DATE = "2018-01-01"

# Production databases that the panel must never collide with. Resolved lazily so
# the assertion works regardless of the current working directory.
PROJECT_ROOT = RESEARCH_DIR.parent
PRODUCTION_DB_DIR = PROJECT_ROOT / "my_data"
FORBIDDEN_DB_NAMES = {
    "stock_tool.sqlite3",
    "stock_tool_v1.sqlite3",
    "stock_tool_before_migration_20260530_125858.sqlite3",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
    code TEXT PRIMARY KEY,
    code_name TEXT,
    ipo_date TEXT,
    out_date TEXT,
    type TEXT,
    status TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS prices_daily (
    code TEXT,
    date TEXT,
    open REAL, high REAL, low REAL, close REAL, preclose REAL,
    volume REAL, amount REAL, turn REAL,
    pe_ttm REAL, pb_mrq REAL, ps_ttm REAL, pcf_ncf_ttm REAL,
    trade_status TEXT,
    fetched_at TEXT,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS adjust_factor (
    code TEXT,
    divid_operate_date TEXT,
    fore_adjust_factor REAL,
    back_adjust_factor REAL,
    fetched_at TEXT,
    PRIMARY KEY (code, divid_operate_date)
);

CREATE TABLE IF NOT EXISTS fundamentals_pit (
    code TEXT,
    stat_date TEXT,
    pub_date TEXT,
    metric TEXT,
    value REAL,
    source_api TEXT,
    metric_quality TEXT DEFAULT 'pass',
    missing_reason TEXT,
    fetched_at TEXT,
    PRIMARY KEY (code, stat_date, metric, pub_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_asof ON fundamentals_pit (code, metric, pub_date);

CREATE TABLE IF NOT EXISTS industry_map (
    code TEXT,
    industry TEXT,
    industry_classification TEXT,
    update_date TEXT,
    query_date TEXT,
    fetched_at TEXT,
    PRIMARY KEY (code, query_date)
);

CREATE TABLE IF NOT EXISTS trade_calendar (
    calendar_date TEXT PRIMARY KEY,
    is_trading_day INTEGER
);

CREATE TABLE IF NOT EXISTS etl_progress (
    task TEXT,
    code TEXT,
    status TEXT,
    rows INTEGER,
    duration_sec REAL,
    updated_at TEXT,
    PRIMARY KEY (task, code)
);

CREATE TABLE IF NOT EXISTS etl_log (
    run_id TEXT,
    ts TEXT,
    api TEXT,
    code TEXT,
    status TEXT,
    rows INTEGER,
    duration_sec REAL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS run_metadata (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def assert_isolated(db_path: Path) -> Path:
    """Fail loudly if the panel path could overlap a production database."""
    resolved = Path(db_path).resolve()
    data_dir = DATA_DIR.resolve()
    if data_dir not in resolved.parents:
        raise AssertionError(f"panel db {resolved} must live under {data_dir}")
    if resolved.name in FORBIDDEN_DB_NAMES:
        raise AssertionError(f"panel db name {resolved.name} collides with a production db")
    prod_dir = PRODUCTION_DB_DIR.resolve()
    if prod_dir == resolved.parent or prod_dir in resolved.parents:
        raise AssertionError(f"panel db {resolved} must not live under production dir {prod_dir}")
    return resolved


def production_db_paths() -> list[Path]:
    if not PRODUCTION_DB_DIR.exists():
        return []
    return sorted(PRODUCTION_DB_DIR.glob("*.sqlite3"))


def snapshot_paths(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    """Record size+mtime of each existing path, for before/after isolation proof."""
    snap: dict[str, dict[str, Any]] = {}
    for path in paths:
        p = Path(path)
        if p.exists():
            st = p.stat()
            snap[str(p)] = {"size": st.st_size, "mtime": round(st.st_mtime, 3)}
        else:
            snap[str(p)] = {"size": None, "mtime": None}
    return snap


class ResearchPanelDB:
    """Thin sqlite wrapper with UPSERT helpers and isolation guarantees."""

    def __init__(self, db_path: Path | str = PANEL_DB_PATH) -> None:
        self.path = assert_isolated(Path(db_path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")

    def __enter__(self) -> "ResearchPanelDB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self._migrate_schema()
        self.conn.commit()

    def _migrate_schema(self) -> None:
        """Apply additive migrations for existing research panel files."""
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(fundamentals_pit)").fetchall()
        }
        if "metric_quality" not in cols:
            self.conn.execute("ALTER TABLE fundamentals_pit ADD COLUMN metric_quality TEXT DEFAULT 'pass'")
        if "missing_reason" not in cols:
            self.conn.execute("ALTER TABLE fundamentals_pit ADD COLUMN missing_reason TEXT")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS run_metadata ("
            "key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
        )

    # --- writes -----------------------------------------------------------
    def upsert(self, table: str, rows: Sequence[dict[str, Any]], pk_cols: Sequence[str]) -> int:
        """Idempotent bulk UPSERT. All rows must share the same column keys."""
        rows = list(rows)
        if not rows:
            return 0
        cols = list(rows[0].keys())
        col_sql = ",".join(cols)
        ph = ",".join("?" for _ in cols)
        non_pk = [c for c in cols if c not in pk_cols]
        update_cols = [c for c in non_pk if c != "fetched_at"]
        pk_sql = ",".join(pk_cols)
        if update_cols:
            set_sql = ",".join(f"{c}=excluded.{c}" for c in update_cols)
            sql = f"INSERT INTO {table} ({col_sql}) VALUES ({ph}) ON CONFLICT({pk_sql}) DO UPDATE SET {set_sql}"
        else:
            sql = f"INSERT INTO {table} ({col_sql}) VALUES ({ph}) ON CONFLICT({pk_sql}) DO NOTHING"
        data = [tuple(r[c] for c in cols) for r in rows]
        self.conn.executemany(sql, data)
        self.conn.commit()
        return len(data)

    def log_call(self, run_id: str, api: str, code: str, status: str,
                 rows: int, duration_sec: float, error: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO etl_log (run_id, ts, api, code, status, rows, duration_sec, error) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, now_iso(), api, code, status, rows, duration_sec, error),
        )
        self.conn.commit()

    def mark_progress(self, task: str, code: str, status: str, rows: int, duration_sec: float) -> None:
        self.upsert(
            "etl_progress",
            [{"task": task, "code": code, "status": status, "rows": rows,
              "duration_sec": duration_sec, "updated_at": now_iso()}],
            ["task", "code"],
        )

    def set_metadata(self, key: str, value: str) -> None:
        self.upsert(
            "run_metadata",
            [{"key": key, "value": value, "updated_at": now_iso()}],
            ["key"],
        )

    def get_metadata(self, key: str, default: Any = None) -> Any:
        value = self.scalar("SELECT value FROM run_metadata WHERE key=?", (key,))
        return default if value is None else value

    # --- reads ------------------------------------------------------------
    def query_df(self, sql: str, params: Sequence[Any] = ()) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.conn, params=list(params))

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        cur = self.conn.execute(sql, tuple(params))
        row = cur.fetchone()
        return row[0] if row else None

    def completed_codes(self, task: str) -> set[str]:
        cur = self.conn.execute(
            "SELECT code FROM etl_progress WHERE task=? AND status='done'", (task,)
        )
        return {r[0] for r in cur.fetchall()}

    def table_fingerprint(self, table: str) -> dict[str, Any]:
        """(row count, content hash) for idempotency checks — order-independent."""
        rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
        import hashlib

        h = hashlib.sha256()
        for r in sorted(repr(tuple(x for x in row)) for row in rows):
            h.update(r.encode("utf-8"))
        return {"rows": len(rows), "sha256": h.hexdigest()}


def open_panel(db_path: Path | str = PANEL_DB_PATH) -> ResearchPanelDB:
    db = ResearchPanelDB(db_path)
    db.init_schema()
    return db
