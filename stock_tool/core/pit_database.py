from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


PIT_DB_FILENAME = "stock_tool_pit.sqlite3"
PIT_SCHEMA_VERSION = 3


def pit_db_path_for(data_dir: Path) -> Path:
    return data_dir / PIT_DB_FILENAME


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class PITJobRecord:
    id: int
    mode: str
    status: str
    stage: str
    start_date: str
    end_date: str
    total: int
    done: int
    failed: int
    message: str
    report_path: str
    created_at: str
    updated_at: str
    completed_at: str


@dataclass(frozen=True)
class PITFetchLogRecord:
    id: int
    job_id: int | None
    stage: str
    target: str
    source: str
    status: str
    error_class: str
    message: str
    duration_ms: int
    created_at: str


@dataclass(frozen=True)
class PITFetchStateRecord:
    stage: str
    target: str
    source: str
    start_date: str
    end_date: str
    status: str
    row_count: int
    source_version: str
    job_id: int | None
    error_class: str
    message: str
    updated_at: str


@dataclass(frozen=True)
class PITStats:
    db_path: str
    universe_rows: int
    price_rows: int
    financial_rows: int
    industry_rows: int
    failed_logs: int
    latest_price_date: str
    latest_job_status: str
    latest_job_message: str
    latest_report_path: str
    industry_coverage: float | None
    latest_protection_profile: str
    request_logs: int
    request_success_logs: int
    request_failed_logs: int
    success_logs: int
    skipped_logs: int
    retry_logs: int
    cooldown_logs: int
    circuit_breaker_logs: int
    worker_restart_logs: int
    latest_failure_rate: float | None


class PITDatabase:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pit_universe (
                    code TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    exchange TEXT NOT NULL DEFAULT '',
                    ipo_date TEXT NOT NULL DEFAULT '',
                    out_date TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    is_index INTEGER NOT NULL DEFAULT 0,
                    asof_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(code, asof_date, source)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pit_universe_asof ON pit_universe(asof_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pit_universe_lifecycle ON pit_universe(source, is_index, ipo_date, out_date)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pit_prices (
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    adj TEXT NOT NULL DEFAULT 'hfq',
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    close_raw REAL,
                    volume REAL,
                    amount REAL,
                    turnover REAL,
                    pe_ttm REAL,
                    pb_mrq REAL,
                    asof_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(code, trade_date, adj, source)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pit_prices_date ON pit_prices(trade_date)")
            self._ensure_column(conn, "pit_prices", "close_raw", "REAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pit_financials (
                    code TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    pub_date TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    value REAL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(code, report_date, pub_date, metric, source)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pit_financials_visible ON pit_financials(code, pub_date)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pit_industry (
                    code TEXT NOT NULL,
                    industry TEXT NOT NULL DEFAULT '',
                    industry_level TEXT NOT NULL DEFAULT '',
                    asof_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(code, asof_date, industry_level, source)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pit_industry_asof ON pit_industry(asof_date)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pit_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    start_date TEXT NOT NULL DEFAULT '',
                    end_date TEXT NOT NULL DEFAULT '',
                    total INTEGER NOT NULL DEFAULT 0,
                    done INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    params_json TEXT NOT NULL DEFAULT '{}',
                    report_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pit_jobs_updated ON pit_jobs(updated_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pit_fetch_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER,
                    stage TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error_class TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pit_fetch_logs_job ON pit_fetch_logs(job_id, status)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pit_fetch_state (
                    stage TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    start_date TEXT NOT NULL DEFAULT '',
                    end_date TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    source_version TEXT NOT NULL DEFAULT '',
                    job_id INTEGER,
                    error_class TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(stage, target, source, start_date, end_date)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pit_fetch_state_status ON pit_fetch_state(source, start_date, end_date, status)")
            conn.execute("PRAGMA user_version = %d" % PIT_SCHEMA_VERSION)

    def create_job(self, mode: str, start_date: str = "", end_date: str = "", params: dict[str, Any] | None = None) -> int:
        ts = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO pit_jobs(mode, status, stage, start_date, end_date, params_json, created_at, updated_at)
                VALUES(?, 'running', '', ?, ?, ?, ?, ?)
                """,
                (mode, start_date, end_date, json.dumps(params or {}, ensure_ascii=False), ts, ts),
            )
            return int(cursor.lastrowid)

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        stage: str | None = None,
        total: int | None = None,
        done: int | None = None,
        done_delta: int = 0,
        failed: int | None = None,
        failed_delta: int = 0,
        message: str | None = None,
        report_path: str | None = None,
        completed: bool = False,
    ) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pit_jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return
            next_done = int(row["done"]) + int(done_delta) if done is None else int(done)
            next_failed = int(row["failed"]) + int(failed_delta) if failed is None else int(failed)
            conn.execute(
                """
                UPDATE pit_jobs
                SET status = ?, stage = ?, total = ?, done = ?, failed = ?, message = ?,
                    report_path = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status if status is not None else row["status"],
                    stage if stage is not None else row["stage"],
                    total if total is not None else row["total"],
                    next_done,
                    next_failed,
                    message if message is not None else row["message"],
                    report_path if report_path is not None else row["report_path"],
                    now_iso(),
                    now_iso() if completed else row["completed_at"],
                    job_id,
                ),
            )

    def update_job_params(self, job_id: int, updates: dict[str, Any]) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT params_json FROM pit_jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return
            try:
                params = json.loads(row["params_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                params = {}
            if not isinstance(params, dict):
                params = {}
            params.update(updates)
            conn.execute(
                "UPDATE pit_jobs SET params_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(params, ensure_ascii=False), now_iso(), job_id),
            )

    def get_job_params(self, job_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pit_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return {}
        return self._params_from_job_row(row)

    def mark_stale_running_jobs(self, stale_after_minutes: int = 120) -> int:
        minutes = max(1, int(stale_after_minutes or 1))
        cutoff = (datetime.now().astimezone() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        ts = now_iso()
        stale_note = "任务长时间没有进度，已标记为中断；再次启动会复用已完成数据。"
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pit_jobs
                SET status = 'interrupted',
                    message = CASE
                        WHEN TRIM(message) = '' THEN ?
                        WHEN INSTR(message, ?) > 0 THEN message
                        ELSE message || '；' || ?
                    END,
                    updated_at = ?,
                    completed_at = CASE WHEN completed_at = '' THEN ? ELSE completed_at END
                WHERE status = 'running' AND updated_at < ?
                """,
                (stale_note, stale_note, stale_note, ts, ts, cutoff),
            )
            return int(cursor.rowcount or 0)

    def log_fetch(
        self,
        *,
        job_id: int | None,
        stage: str,
        target: str = "",
        source: str = "",
        status: str = "info",
        error_class: str = "",
        message: str = "",
        duration_ms: int = 0,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pit_fetch_logs(job_id, stage, target, source, status, error_class, message, duration_ms, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, stage[:60], target[:120], source[:60], status[:30], error_class[:40], message[:800], int(duration_ms or 0), now_iso()),
            )

    def record_fetch_state(
        self,
        *,
        job_id: int | None,
        stage: str,
        target: str,
        source: str,
        start_date: str,
        end_date: str,
        status: str,
        row_count: int = 0,
        source_version: str = "",
        error_class: str = "",
        message: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pit_fetch_state(
                    stage, target, source, start_date, end_date, status, row_count,
                    source_version, job_id, error_class, message, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stage, target, source, start_date, end_date) DO UPDATE SET
                    status = excluded.status,
                    row_count = excluded.row_count,
                    source_version = excluded.source_version,
                    job_id = excluded.job_id,
                    error_class = excluded.error_class,
                    message = excluded.message,
                    updated_at = excluded.updated_at
                """,
                (
                    stage[:60],
                    target[:120],
                    source[:60],
                    start_date,
                    end_date,
                    status[:30],
                    int(row_count or 0),
                    source_version[:80],
                    job_id,
                    error_class[:40],
                    message[:800],
                    now_iso(),
                ),
            )

    def has_successful_fetch_state(self, stage: str, target: str, source: str, start_date: str, end_date: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM pit_fetch_state
                WHERE stage = ? AND target = ? AND source = ? AND start_date = ? AND end_date = ? AND status = 'success'
                LIMIT 1
                """,
                (stage, target, source, start_date, end_date),
            ).fetchone()
        return row is not None

    def list_failed_fetch_states(self, source: str, start_date: str, end_date: str, limit: int = 5000) -> list[PITFetchStateRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM pit_fetch_state
                WHERE source = ? AND start_date = ? AND end_date = ? AND status = 'failed'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (source, start_date, end_date, max(1, int(limit))),
            ).fetchall()
        return [self._state_from_row(row) for row in rows]

    def upsert_universe(self, rows: list[dict[str, Any]]) -> int:
        payload = []
        for row in rows:
            self._require_metadata(row, needs=("asof_date",))
            payload.append(
                (
                    row.get("code", ""),
                    row.get("name", ""),
                    row.get("exchange", ""),
                    row.get("ipo_date", ""),
                    row.get("out_date", ""),
                    row.get("status", ""),
                    1 if row.get("is_index") else 0,
                    row["asof_date"],
                    row["source"],
                    row.get("source_version", ""),
                    row["fetched_at"],
                    json.dumps(row.get("raw", {}), ensure_ascii=False),
                )
            )
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO pit_universe(
                    code, name, exchange, ipo_date, out_date, status, is_index, asof_date,
                    source, source_version, fetched_at, raw_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, asof_date, source) DO UPDATE SET
                    name = excluded.name,
                    exchange = excluded.exchange,
                    ipo_date = excluded.ipo_date,
                    out_date = excluded.out_date,
                    status = excluded.status,
                    is_index = excluded.is_index,
                    source_version = excluded.source_version,
                    fetched_at = excluded.fetched_at,
                    raw_json = excluded.raw_json
                """,
                payload,
            )
        return len(payload)

    def upsert_prices(self, rows: list[dict[str, Any]]) -> int:
        payload = []
        for row in rows:
            self._require_metadata(row, needs=("asof_date",))
            trade_date = row.get("trade_date") or row.get("asof_date")
            payload.append(
                (
                    row.get("code", ""),
                    trade_date,
                    row.get("adj", "hfq"),
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("close"),
                    row.get("close_raw"),
                    row.get("volume"),
                    row.get("amount"),
                    row.get("turnover"),
                    row.get("pe_ttm"),
                    row.get("pb_mrq"),
                    row["asof_date"],
                    row["source"],
                    row.get("source_version", ""),
                    row["fetched_at"],
                    json.dumps(row.get("raw", {}), ensure_ascii=False),
                )
            )
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO pit_prices(
                    code, trade_date, adj, open, high, low, close, close_raw, volume, amount,
                    turnover, pe_ttm, pb_mrq, asof_date, source, source_version, fetched_at, raw_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, trade_date, adj, source) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    close_raw = excluded.close_raw,
                    volume = excluded.volume,
                    amount = excluded.amount,
                    turnover = excluded.turnover,
                    pe_ttm = excluded.pe_ttm,
                    pb_mrq = excluded.pb_mrq,
                    asof_date = excluded.asof_date,
                    source_version = excluded.source_version,
                    fetched_at = excluded.fetched_at,
                    raw_json = excluded.raw_json
                """,
                payload,
            )
        return len(payload)

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    def upsert_financials(self, rows: list[dict[str, Any]]) -> int:
        payload = []
        for row in rows:
            self._require_metadata(row, needs=("pub_date",))
            payload.append(
                (
                    row.get("code", ""),
                    row.get("report_date", ""),
                    row["pub_date"],
                    row.get("metric", ""),
                    row.get("value"),
                    row["source"],
                    row.get("source_version", ""),
                    row["fetched_at"],
                    json.dumps(row.get("raw", {}), ensure_ascii=False),
                )
            )
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO pit_financials(
                    code, report_date, pub_date, metric, value, source, source_version, fetched_at, raw_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, report_date, pub_date, metric, source) DO UPDATE SET
                    value = excluded.value,
                    source_version = excluded.source_version,
                    fetched_at = excluded.fetched_at,
                    raw_json = excluded.raw_json
                """,
                payload,
            )
        return len(payload)

    def upsert_industries(self, rows: list[dict[str, Any]]) -> int:
        payload = []
        for row in rows:
            self._require_metadata(row, needs=("asof_date",))
            payload.append(
                (
                    row.get("code", ""),
                    row.get("industry", ""),
                    row.get("industry_level", ""),
                    row["asof_date"],
                    row["source"],
                    row.get("source_version", ""),
                    row["fetched_at"],
                    json.dumps(row.get("raw", {}), ensure_ascii=False),
                )
            )
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO pit_industry(
                    code, industry, industry_level, asof_date, source, source_version, fetched_at, raw_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, asof_date, industry_level, source) DO UPDATE SET
                    industry = excluded.industry,
                    source_version = excluded.source_version,
                    fetched_at = excluded.fetched_at,
                    raw_json = excluded.raw_json
                """,
                payload,
            )
        return len(payload)

    def _require_metadata(self, row: dict[str, Any], needs: tuple[str, ...]) -> None:
        missing = [key for key in ("source", "fetched_at", *needs) if not row.get(key)]
        if missing:
            raise ValueError(f"PIT row missing metadata: {','.join(missing)}")

    def has_price_rows(self, code: str, start_date: str, end_date: str, source: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM pit_prices
                WHERE code = ? AND source = ? AND trade_date BETWEEN ? AND ?
                """,
                (code, source, start_date, end_date),
            ).fetchone()
        return bool(row and int(row["count"]) > 0)

    def financials_visible_asof(self, code: str, asof_date: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT code, report_date, pub_date, metric, value, source
                FROM pit_financials
                WHERE code = ? AND pub_date <= ?
                ORDER BY pub_date, report_date, metric
                """,
                (code, asof_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def prices_visible_asof(
        self,
        code: str,
        asof_date: str,
        lookback_rows: int = 60,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return hfq price rows visible on or before a historical as-of date."""
        limit = max(1, int(lookback_rows or 1))
        asof = _compact_date(asof_date)
        if not asof:
            return []
        if source:
            sql = """
                SELECT code, trade_date, open, high, low, close, close_raw, volume, amount, turnover, pe_ttm, pb_mrq,
                       asof_date, source, source_version, fetched_at
                FROM pit_prices
                WHERE code = ? AND adj = 'hfq' AND REPLACE(trade_date, '-', '') <= ? AND source = ?
                ORDER BY REPLACE(trade_date, '-', '') DESC, trade_date DESC
                LIMIT ?
            """
            params: tuple[Any, ...] = (code, asof, source, limit)
        else:
            sql = """
                SELECT p.code, p.trade_date, p.open, p.high, p.low, p.close, p.close_raw, p.volume, p.amount, p.turnover,
                       p.pe_ttm, p.pb_mrq, p.asof_date, p.source, p.source_version, p.fetched_at
                FROM pit_prices p
                WHERE p.code = ? AND p.adj = 'hfq' AND REPLACE(p.trade_date, '-', '') <= ?
                  AND p.source = (
                      SELECT MIN(source)
                      FROM pit_prices
                      WHERE code = p.code AND trade_date = p.trade_date AND adj = p.adj
                  )
                ORDER BY REPLACE(p.trade_date, '-', '') DESC, p.trade_date DESC
                LIMIT ?
            """
            params = (code, asof, limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in reversed(rows)]

    def latest_price_factors(self) -> tuple[str, dict[str, dict[str, Any]]]:
        """Return the latest complete cross-section used to reconcile PE-TTM/PB into V1."""
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(trade_date) AS day FROM pit_prices").fetchone()
            day = str(row["day"] or "") if row else ""
            if not day:
                return "", {}
            rows = conn.execute(
                """
                SELECT code, trade_date, pe_ttm, pb_mrq, source, source_version, fetched_at
                FROM pit_prices
                WHERE trade_date = ?
                ORDER BY code, source
                """,
                (day,),
            ).fetchall()
        factors: dict[str, dict[str, Any]] = {}
        for price in rows:
            code = str(price["code"] or "").split(".")[-1].zfill(6)
            # Deterministic first source wins when a date contains multiple providers.
            factors.setdefault(code, dict(price))
        return day, factors

    def list_recent_jobs(self, limit: int = 20) -> list[PITJobRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pit_jobs ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def list_failed_logs(self, limit: int = 500) -> list[PITFetchLogRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM pit_fetch_logs
                WHERE status = 'failed'
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [self._log_from_row(row) for row in rows]

    def list_failed_logs_for_job(self, job_id: int, limit: int = 500) -> list[PITFetchLogRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM pit_fetch_logs
                WHERE job_id = ? AND status = 'failed'
                ORDER BY id DESC
                LIMIT ?
                """,
                (job_id, max(1, int(limit))),
            ).fetchall()
        return [self._log_from_row(row) for row in rows]

    def list_recent_logs(self, limit: int = 200) -> list[PITFetchLogRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pit_fetch_logs ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._log_from_row(row) for row in rows]

    def stats(self) -> PITStats:
        with self.connect() as conn:
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
                for table in ("pit_universe", "pit_prices", "pit_financials", "pit_industry")
            }
            failed = int(conn.execute("SELECT COUNT(*) AS count FROM pit_fetch_logs WHERE status = 'failed'").fetchone()["count"])
            latest_price = conn.execute("SELECT MAX(trade_date) AS value FROM pit_prices").fetchone()["value"] or ""
            latest_job = conn.execute("SELECT * FROM pit_jobs ORDER BY id DESC LIMIT 1").fetchone()
            coverage = self._industry_coverage(conn)
            latest_job_id = int(latest_job["id"]) if latest_job else None
            log_counts = self._log_counts(conn, latest_job_id)
            failure_rate = self._failure_rate(log_counts)
            profile = self._profile_from_job_row(latest_job) if latest_job else ""
        return PITStats(
            db_path=str(self.db_path),
            universe_rows=counts["pit_universe"],
            price_rows=counts["pit_prices"],
            financial_rows=counts["pit_financials"],
            industry_rows=counts["pit_industry"],
            failed_logs=failed,
            latest_price_date=latest_price,
            latest_job_status=latest_job["status"] if latest_job else "",
            latest_job_message=latest_job["message"] if latest_job else "",
            latest_report_path=latest_job["report_path"] if latest_job else "",
            industry_coverage=coverage,
            latest_protection_profile=profile,
            request_logs=log_counts.get("request", 0),
            request_success_logs=log_counts.get("request_success", 0),
            request_failed_logs=log_counts.get("request_failed", 0),
            success_logs=log_counts.get("success", 0),
            skipped_logs=log_counts.get("skipped", 0),
            retry_logs=log_counts.get("retry", 0),
            cooldown_logs=log_counts.get("cooldown", 0),
            circuit_breaker_logs=log_counts.get("circuit_open", 0),
            worker_restart_logs=log_counts.get("worker_restart", 0),
            latest_failure_rate=failure_rate,
        )

    def job_log_counts(self, job_id: int) -> dict[str, int]:
        with self.connect() as conn:
            return self._log_counts(conn, job_id)

    def has_recent_successful_preflight(self, source: str, start_date: str, end_date: str, max_age_hours: int = 24) -> bool:
        return self._has_recent_successful_job("preflight", source, start_date, end_date, max_age_hours)

    def has_recent_successful_healthcheck(self, source: str, start_date: str, end_date: str, max_age_hours: int = 24) -> bool:
        return self._has_recent_successful_job("healthcheck", source, start_date, end_date, max_age_hours)

    def _has_recent_successful_job(self, mode: str, source: str, start_date: str, end_date: str, max_age_hours: int) -> bool:
        cutoff = datetime.now().astimezone().timestamp() - (max(1, int(max_age_hours)) * 3600)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM pit_jobs
                WHERE mode = ? AND status = 'completed' AND start_date = ? AND end_date = ?
                ORDER BY completed_at DESC, updated_at DESC
                LIMIT 20
                """,
                (mode, start_date, end_date),
            ).fetchall()
        for row in rows:
            params = self._params_from_job_row(row)
            if params.get("provider") != source:
                continue
            completed = row["completed_at"] or row["updated_at"]
            try:
                completed_at = datetime.fromisoformat(completed).timestamp()
            except ValueError:
                continue
            if completed_at >= cutoff:
                return True
        return False

    def latest_asof(self, source: str | None = None) -> str | None:
        with self.connect() as conn:
            if source:
                row = conn.execute("SELECT MAX(asof_date) AS day FROM pit_universe WHERE source = ?", (source,)).fetchone()
            else:
                row = conn.execute("SELECT MAX(asof_date) AS day FROM pit_universe").fetchone()
        day = str(row["day"] or "") if row else ""
        return day or None

    def latest_universe_rows(self, source: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if source:
                day_row = conn.execute("SELECT MAX(asof_date) AS day FROM pit_universe WHERE source = ?", (source,)).fetchone()
            else:
                day_row = conn.execute("SELECT MAX(asof_date) AS day FROM pit_universe").fetchone()
            day = day_row["day"] if day_row else ""
            if not day:
                return []
            if source:
                rows = conn.execute(
                    """
                    SELECT code, name, exchange, ipo_date, out_date, status, is_index, asof_date, source, source_version, fetched_at, raw_json
                    FROM pit_universe
                    WHERE asof_date = ? AND source = ? AND is_index = 0
                    ORDER BY code
                    """,
                    (day, source),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT code, name, exchange, ipo_date, out_date, status, is_index, asof_date, source, source_version, fetched_at, raw_json
                    FROM pit_universe
                    WHERE asof_date = ? AND is_index = 0
                    ORDER BY code
                    """,
                    (day,),
                ).fetchall()
        out = []
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                raw = {}
            out.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "exchange": row["exchange"],
                    "ipo_date": row["ipo_date"],
                    "out_date": row["out_date"],
                    "status": row["status"],
                    "is_index": bool(row["is_index"]),
                    "asof_date": row["asof_date"],
                    "source": row["source"],
                    "source_version": row["source_version"],
                    "fetched_at": row["fetched_at"],
                    "raw": raw,
                }
            )
        return out

    def universe_asof(self, asof_date: str, source: str | None = None) -> list[dict[str, Any]]:
        """Return lifecycle-derived membership for a historical as-of date.

        This intentionally uses ipo_date/out_date from the universe snapshot rather than
        requiring a materialized daily universe row. Rows without a valid ipo_date are
        excluded because their historical membership cannot be proven.
        """
        asof = _compact_date(asof_date)
        if not asof:
            return []
        with self.connect() as conn:
            if source:
                rows = conn.execute(
                    """
                    SELECT code, name, exchange, ipo_date, out_date, status, is_index, asof_date, source, source_version, fetched_at, raw_json
                    FROM pit_universe
                    WHERE source = ? AND is_index = 0
                    ORDER BY code, asof_date DESC, source
                    """,
                    (source,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT code, name, exchange, ipo_date, out_date, status, is_index, asof_date, source, source_version, fetched_at, raw_json
                    FROM pit_universe
                    WHERE is_index = 0
                    ORDER BY code, asof_date DESC, source
                    """
                ).fetchall()
        out = []
        seen_codes: set[str] = set()
        for row in rows:
            code = str(row["code"] or "")
            if code in seen_codes:
                continue
            seen_codes.add(code)
            ipo = _compact_date(row["ipo_date"])
            out_date = _compact_date(row["out_date"])
            if not ipo or ipo > asof:
                continue
            if out_date and out_date < asof:
                continue
            out.append(self._universe_row_to_dict(row))
        return out

    def _industry_coverage(self, conn: sqlite3.Connection) -> float | None:
        day_row = conn.execute("SELECT MAX(asof_date) AS day FROM pit_universe").fetchone()
        day = day_row["day"] if day_row else ""
        if not day:
            return None
        universe = int(
            conn.execute(
                "SELECT COUNT(DISTINCT code) AS count FROM pit_universe WHERE asof_date = ? AND is_index = 0",
                (day,),
            ).fetchone()["count"]
        )
        if not universe:
            return None
        # 按 code 命中即算覆盖：industry 用其自身最新 asof，不强求与 universe 同 asof。
        # 两表 asof 可能差一天（universe 走交易日回退、industry 用请求日），同 asof 强匹配
        # 会假阴性为 0。写入侧已在 pit_pipeline 对齐 asof，这里再加固一层防回归。
        covered = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT u.code) AS count
                FROM pit_universe u
                WHERE u.asof_date = ? AND u.is_index = 0
                  AND EXISTS (
                      SELECT 1 FROM pit_industry i
                      WHERE i.code = u.code AND TRIM(i.industry) <> ''
                  )
                """,
                (day,),
            ).fetchone()["count"]
        )
        return round(covered / universe, 4)

    def _log_counts(self, conn: sqlite3.Connection, job_id: int | None) -> dict[str, int]:
        if job_id is None:
            return {}
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM pit_fetch_logs
            WHERE job_id = ?
            GROUP BY status
            """,
            (job_id,),
        ).fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def _failure_rate(self, log_counts: dict[str, int]) -> float | None:
        failed = int(log_counts.get("failed", 0))
        success = int(log_counts.get("success", 0))
        total = failed + success
        if total <= 0:
            return None
        return round(failed / total, 4)

    def _universe_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            raw = {}
        return {
            "code": row["code"],
            "name": row["name"],
            "exchange": row["exchange"],
            "ipo_date": row["ipo_date"],
            "out_date": row["out_date"],
            "status": row["status"],
            "is_index": bool(row["is_index"]),
            "asof_date": row["asof_date"],
            "source": row["source"],
            "source_version": row["source_version"],
            "fetched_at": row["fetched_at"],
            "raw": raw,
        }

    def _params_from_job_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            params = json.loads(row["params_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return params if isinstance(params, dict) else {}

    def _profile_from_job_row(self, row: sqlite3.Row) -> str:
        params = self._params_from_job_row(row)
        return str(params.get("protection_profile") or "")

    def export_failed_logs_csv(self, path: Path) -> int:
        rows = self.list_failed_logs(limit=5000)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(["id", "job_id", "stage", "target", "source", "error_class", "message", "duration_ms", "created_at"])
                for row in rows:
                    writer.writerow([row.id, row.job_id, row.stage, row.target, row.source, row.error_class, row.message, row.duration_ms, row.created_at])
            os.replace(temporary_path, path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return len(rows)

    def _job_from_row(self, row: sqlite3.Row) -> PITJobRecord:
        return PITJobRecord(
            id=int(row["id"]),
            mode=row["mode"],
            status=row["status"],
            stage=row["stage"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            total=int(row["total"]),
            done=int(row["done"]),
            failed=int(row["failed"]),
            message=row["message"],
            report_path=row["report_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    def _log_from_row(self, row: sqlite3.Row) -> PITFetchLogRecord:
        return PITFetchLogRecord(
            id=int(row["id"]),
            job_id=int(row["job_id"]) if row["job_id"] is not None else None,
            stage=row["stage"],
            target=row["target"],
            source=row["source"],
            status=row["status"],
            error_class=row["error_class"],
            message=row["message"],
            duration_ms=int(row["duration_ms"]),
            created_at=row["created_at"],
        )

    def _state_from_row(self, row: sqlite3.Row) -> PITFetchStateRecord:
        return PITFetchStateRecord(
            stage=row["stage"],
            target=row["target"],
            source=row["source"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            status=row["status"],
            row_count=int(row["row_count"]),
            source_version=row["source_version"],
            job_id=int(row["job_id"]) if row["job_id"] is not None else None,
            error_class=row["error_class"],
            message=row["message"],
            updated_at=row["updated_at"],
        )


def today_iso() -> str:
    return date.today().isoformat()


def _compact_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 8:
        return ""
    return digits
