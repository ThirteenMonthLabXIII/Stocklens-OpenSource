from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from stock_tool.core.data_location import DB_FILENAME

APP_DIR = Path.home() / ".stock_selector_mvp"
DB_PATH = APP_DIR / DB_FILENAME
NOTE_LIMIT = 1000
NOTE_TRUNCATED_SUFFIX = "\n...[已截断]"


@dataclass(frozen=True)
class CacheRecord:
    key: str
    payload: Any
    updated_at: datetime


@dataclass(frozen=True)
class CacheMetadata:
    key: str
    updated_at: datetime


@dataclass(frozen=True)
class FetchLogRecord:
    id: int
    action: str
    target: str
    source: str
    status: str
    message: str
    duration_ms: int
    created_at: datetime


@dataclass(frozen=True)
class TrackingTaskRecord:
    id: int
    code: str
    name: str
    task_type: str
    status: str
    entry_price: float | None
    fail_price: float | None
    target_price: float | None
    horizon_days: int
    created_at: datetime
    due_date: str
    note: str
    result: str
    last_review: str


@dataclass(frozen=True)
class PriceAlertRecord:
    id: int
    code: str
    name: str
    kind: str
    threshold: float
    note: str
    enabled: bool
    repeat: bool
    created_at: datetime
    last_triggered_at: str
    trigger_count: int
    last_state: int


@dataclass(frozen=True)
class AlertHistoryRecord:
    id: int
    alert_id: int
    code: str
    name: str
    kind: str
    threshold: float
    price: float | None
    change_pct: float | None
    message: str
    created_at: datetime
    quote_source: str = ""
    quote_at: str = ""


@dataclass(frozen=True)
class AuditRecord:
    """不可变的用户业务操作审计记录。"""

    id: int
    entity_type: str
    entity_id: str
    action: str
    actor: str
    before_json: str
    after_json: str
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class ReviewAutomationRunRecord:
    """自动复盘运行账本的一条记录。"""

    id: int
    run_key: str
    trigger: str
    owner_token: str
    business_date: str
    started_at: str
    heartbeat_at: str
    finished_at: str
    status: str
    attempt: int
    tracking_status: str
    tracking_finished_at: str
    tracking_scanned: int
    tracking_reviewed: int
    tracking_closed: int
    settlement_status: str
    settlement_finished_at: str
    samples_scanned: int
    samples_settled: int
    valid_settled: int
    invalid_settled: int
    missing_data_count: int
    retryable_count: int
    error_summary: str
    next_retry_at: str
    app_version: str


class CacheStore:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    added_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fetch_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tracking_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    entry_price REAL,
                    fail_price REAL,
                    target_price REAL,
                    horizon_days INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    note TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '',
                    last_review TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_table_columns(
                conn,
                "tracking_tasks",
                {
                    "result": "TEXT NOT NULL DEFAULT ''",
                    "last_review": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    threshold REAL NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    repeat INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_triggered_at TEXT NOT NULL DEFAULT '',
                    trigger_count INTEGER NOT NULL DEFAULT 0,
                    last_state INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._ensure_table_columns(
                conn,
                "price_alerts",
                {"last_state": "INTEGER NOT NULL DEFAULT 0"},
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    threshold REAL NOT NULL,
                    price REAL,
                    change_pct REAL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    quote_source TEXT NOT NULL DEFAULT '',
                    quote_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_table_columns(
                conn,
                "alert_history",
                {
                    "quote_source": "TEXT NOT NULL DEFAULT ''",
                    "quote_at": "TEXT NOT NULL DEFAULT ''",
                },
            )
            # 预警与观察任务共用。CREATE IF NOT EXISTS 使既有缓存库升级可重复执行。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operation_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_operation_audit_entity "
                "ON operation_audit(entity_type, entity_id, created_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_automation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_key TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    business_date TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    tracking_status TEXT NOT NULL DEFAULT 'pending',
                    tracking_finished_at TEXT NOT NULL DEFAULT '',
                    tracking_scanned INTEGER NOT NULL DEFAULT 0,
                    tracking_reviewed INTEGER NOT NULL DEFAULT 0,
                    tracking_closed INTEGER NOT NULL DEFAULT 0,
                    settlement_status TEXT NOT NULL DEFAULT 'pending',
                    settlement_finished_at TEXT NOT NULL DEFAULT '',
                    samples_scanned INTEGER NOT NULL DEFAULT 0,
                    samples_settled INTEGER NOT NULL DEFAULT 0,
                    valid_settled INTEGER NOT NULL DEFAULT 0,
                    invalid_settled INTEGER NOT NULL DEFAULT 0,
                    missing_data_count INTEGER NOT NULL DEFAULT 0,
                    retryable_count INTEGER NOT NULL DEFAULT 0,
                    error_summary TEXT NOT NULL DEFAULT '',
                    next_retry_at TEXT NOT NULL DEFAULT '',
                    app_version TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_automation_running "
                "ON review_automation_runs(run_key) WHERE status = 'running'"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_automation_retries (
                    sample_id INTEGER PRIMARY KEY,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('review_automation_enabled', '1')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('review_tracking_interval_minutes', '15')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('review_settlement_interval_minutes', '60')"
            )
            # Feature-level readiness settings are separate from the short
            # ``auto_data_refresh_enabled`` quote polling switch.  Keep the
            # defaults in the existing settings table so old data directories
            # receive them without a schema migration or a new table.
            feature_defaults = {
                "feature_auto_refresh_enabled": "1",
                "feature_refresh_quote_ttl_seconds": "30",
                "feature_refresh_market_context_ttl_seconds": "300",
                "feature_refresh_history_ttl_seconds": "900",
                "feature_refresh_money_flow_ttl_seconds": "300",
                "feature_refresh_event_ttl_seconds": "3600",
                "feature_refresh_financial_ttl_seconds": "86400",
                "feature_refresh_factor_snapshot_trading_days": "1",
                "feature_refresh_pit_ttl_seconds": "86400",
            }
            conn.executemany(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                tuple(feature_defaults.items()),
            )

    def _ensure_table_columns(self, conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] for row in rows}
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def set_json(self, key: str, payload: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO cache(key, payload, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(payload, ensure_ascii=False), datetime.now().isoformat()),
            )

    def get_json(self, key: str, max_age: timedelta | None = None) -> CacheRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        updated_at = datetime.fromisoformat(row["updated_at"])
        if max_age and datetime.now() - updated_at > max_age:
            return None
        return CacheRecord(key=key, payload=json.loads(row["payload"]), updated_at=updated_at)

    def clear_cache(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM cache")

    def list_cache_metadata(self, limit: int = 1000) -> list[CacheMetadata]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT key, updated_at
                FROM cache
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        records: list[CacheMetadata] = []
        for row in rows:
            try:
                updated_at = datetime.fromisoformat(row["updated_at"])
            except ValueError:
                continue
            records.append(CacheMetadata(key=row["key"], updated_at=updated_at))
        return records

    def cleanup_expired_cache(
        self,
        quote_days: int = 1,
        history_days: int = 15,
        financial_days: int = 7,
        other_days: int = 30,
    ) -> dict[str, int]:
        """Delete stale cache rows only; settings, watchlist and logs are kept."""
        now = datetime.now()
        removed = {"quote": 0, "history": 0, "financial": 0, "other": 0, "total": 0}
        with self.connect() as conn:
            rows = conn.execute("SELECT key, updated_at FROM cache").fetchall()
            stale_keys: list[tuple[str, str]] = []
            for row in rows:
                key = row["key"]
                try:
                    updated_at = datetime.fromisoformat(row["updated_at"])
                except ValueError:
                    stale_keys.append((key, "other"))
                    continue
                if key == "spot_quotes":
                    bucket, max_days = "quote", quote_days
                elif key.startswith("history:"):
                    bucket, max_days = "history", history_days
                elif key.startswith("financial:"):
                    bucket, max_days = "financial", financial_days
                else:
                    bucket, max_days = "other", other_days
                if (now - updated_at).days >= max(1, max_days):
                    stale_keys.append((key, bucket))

            for key, bucket in stale_keys:
                conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                removed[bucket] += 1
                removed["total"] += 1
        return removed

    def add_watch(self, code: str, name: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO watchlist(code, name, added_at)
                VALUES(?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET name = excluded.name
                """,
                (code, name, datetime.now().isoformat()),
            )

    def remove_watch(self, code: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM watchlist WHERE code = ?", (code,))

    def list_watch(self) -> list[dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT code, name, added_at FROM watchlist ORDER BY added_at DESC").fetchall()
        return [dict(row) for row in rows]

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def delete_setting(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    # ---- review automation ledger ----------------------------------

    def claim_review_automation_run(
        self,
        *,
        run_key: str,
        trigger: str,
        owner_token: str,
        business_date: str,
        started_at: str,
        app_version: str,
        stale_after_seconds: int = 1800,
    ) -> int | None:
        """以短 ``BEGIN IMMEDIATE`` 事务抢占单次自动复盘运行权。"""
        cutoff = (datetime.fromisoformat(started_at) - timedelta(seconds=max(1, int(stale_after_seconds)))).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE review_automation_runs
                SET status = 'interrupted', finished_at = ?, error_summary = ?
                WHERE run_key = ? AND status = 'running' AND heartbeat_at < ?
                """,
                (started_at, "超过心跳期限，启动补偿接管。", run_key, cutoff),
            )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO review_automation_runs(
                        run_key, trigger, owner_token, business_date, started_at, heartbeat_at,
                        status, attempt, app_version
                    ) VALUES(?, ?, ?, ?, ?, ?, 'running', 1, ?)
                    """,
                    (run_key, trigger, owner_token, business_date, started_at, started_at, app_version),
                )
            except sqlite3.IntegrityError:
                return None
            return int(cursor.lastrowid)

    def heartbeat_review_automation_run(self, run_id: int, owner_token: str, heartbeat_at: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE review_automation_runs
                SET heartbeat_at = ?
                WHERE id = ? AND owner_token = ? AND status = 'running'
                """,
                (heartbeat_at, int(run_id), owner_token),
            )
            return cursor.rowcount == 1

    def finish_review_automation_run(
        self,
        run_id: int,
        owner_token: str,
        *,
        status: str,
        finished_at: str,
        values: dict[str, Any] | None = None,
    ) -> bool:
        allowed = {
            "tracking_status", "tracking_finished_at", "tracking_scanned", "tracking_reviewed", "tracking_closed",
            "settlement_status", "settlement_finished_at", "samples_scanned", "samples_settled", "valid_settled",
            "invalid_settled", "missing_data_count", "retryable_count", "error_summary", "next_retry_at",
        }
        updates = {"status": status, "finished_at": finished_at, "heartbeat_at": finished_at}
        updates.update({key: value for key, value in (values or {}).items() if key in allowed})
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.connect() as conn:
            cursor = conn.execute(
                f"UPDATE review_automation_runs SET {assignments} WHERE id = ? AND owner_token = ? AND status = 'running'",
                (*updates.values(), int(run_id), owner_token),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _review_automation_run_from_row(row: sqlite3.Row) -> ReviewAutomationRunRecord:
        return ReviewAutomationRunRecord(
            id=int(row["id"]), run_key=row["run_key"], trigger=row["trigger"], owner_token=row["owner_token"],
            business_date=row["business_date"], started_at=row["started_at"], heartbeat_at=row["heartbeat_at"],
            finished_at=row["finished_at"] or "", status=row["status"], attempt=int(row["attempt"] or 1),
            tracking_status=row["tracking_status"], tracking_finished_at=row["tracking_finished_at"] or "",
            tracking_scanned=int(row["tracking_scanned"] or 0), tracking_reviewed=int(row["tracking_reviewed"] or 0),
            tracking_closed=int(row["tracking_closed"] or 0), settlement_status=row["settlement_status"],
            settlement_finished_at=row["settlement_finished_at"] or "", samples_scanned=int(row["samples_scanned"] or 0),
            samples_settled=int(row["samples_settled"] or 0), valid_settled=int(row["valid_settled"] or 0),
            invalid_settled=int(row["invalid_settled"] or 0), missing_data_count=int(row["missing_data_count"] or 0),
            retryable_count=int(row["retryable_count"] or 0), error_summary=row["error_summary"] or "",
            next_retry_at=row["next_retry_at"] or "", app_version=row["app_version"] or "",
        )

    def latest_review_automation_run(self) -> ReviewAutomationRunRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM review_automation_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._review_automation_run_from_row(row) if row else None

    def list_review_automation_runs(self, limit: int = 50) -> list[ReviewAutomationRunRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM review_automation_runs ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [self._review_automation_run_from_row(row) for row in rows]

    def get_review_automation_retry(self, sample_id: int) -> tuple[int, str, str] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT attempt, next_retry_at, last_error FROM review_automation_retries WHERE sample_id = ?",
                (int(sample_id),),
            ).fetchone()
        return (int(row["attempt"]), row["next_retry_at"], row["last_error"]) if row else None

    def save_review_automation_retry(self, sample_id: int, attempt: int, next_retry_at: str, last_error: str, updated_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO review_automation_retries(sample_id, attempt, next_retry_at, last_error, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(sample_id) DO UPDATE SET
                    attempt = excluded.attempt, next_retry_at = excluded.next_retry_at,
                    last_error = excluded.last_error, updated_at = excluded.updated_at
                """,
                (int(sample_id), max(0, int(attempt)), str(next_retry_at or ""), str(last_error or "")[:1000], updated_at),
            )

    def clear_review_automation_retry(self, sample_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM review_automation_retries WHERE sample_id = ?", (int(sample_id),))

    # ---- price alerts -------------------------------------------------

    def add_price_alert(
        self,
        code: str,
        name: str,
        kind: str,
        threshold: float,
        note: str = "",
        repeat: bool = False,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO price_alerts(code, name, kind, threshold, note, enabled, repeat, created_at)
                VALUES(?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (code, name, kind, float(threshold), note, 1 if repeat else 0, datetime.now().isoformat()),
            )
            return int(cursor.lastrowid)

    def list_price_alerts(self, enabled_only: bool = False) -> list[PriceAlertRecord]:
        query = "SELECT * FROM price_alerts"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY id DESC"
        with self.connect() as conn:
            rows = conn.execute(query).fetchall()
        records: list[PriceAlertRecord] = []
        for row in rows:
            try:
                created_at = datetime.fromisoformat(row["created_at"])
            except (ValueError, TypeError):
                created_at = datetime.now()
            records.append(
                PriceAlertRecord(
                    id=row["id"],
                    code=row["code"],
                    name=row["name"],
                    kind=row["kind"],
                    threshold=row["threshold"],
                    note=row["note"],
                    enabled=bool(row["enabled"]),
                    repeat=bool(row["repeat"]),
                    created_at=created_at,
                    last_triggered_at=row["last_triggered_at"],
                    trigger_count=row["trigger_count"],
                    last_state=row["last_state"],
                )
            )
        return records

    def set_price_alert_enabled(self, alert_id: int, enabled: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE price_alerts SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, alert_id),
            )

    def update_price_alert(
        self,
        alert_id: int,
        *,
        code: str,
        name: str,
        kind: str,
        threshold: float,
        note: str = "",
        repeat: bool = False,
    ) -> bool:
        """原地更新预警，保留 ID 与触发历史。

        条件改变后必须重新武装边沿触发状态；历史记录仍保留为当时条件的
        审计证据，不能通过删除后重建来替代编辑。
        """
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE price_alerts
                SET code = ?, name = ?, kind = ?, threshold = ?, note = ?, repeat = ?,
                    last_state = 0, last_triggered_at = '', trigger_count = 0
                WHERE id = ?
                """,
                (code, name, kind, float(threshold), note, 1 if repeat else 0, int(alert_id)),
            )
            return cursor.rowcount > 0

    def mark_price_alert_triggered(self, alert_id: int, triggered_at: str, disable: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE price_alerts
                SET last_triggered_at = ?,
                    trigger_count = trigger_count + 1,
                    last_state = 1,
                    enabled = CASE WHEN ? THEN 0 ELSE enabled END
                WHERE id = ?
                """,
                (triggered_at, 1 if disable else 0, alert_id),
            )

    def set_price_alert_state(self, alert_id: int, state: int, *, expected_state: int | None = None) -> bool:
        """仅更新边沿触发用的 last_state(0/1),不动计数和触发时间。"""
        with self.connect() as conn:
            if expected_state is None:
                cursor = conn.execute(
                    "UPDATE price_alerts SET last_state = ? WHERE id = ?",
                    (1 if state else 0, alert_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE price_alerts SET last_state = ? WHERE id = ? AND last_state = ?",
                    (1 if state else 0, alert_id, 1 if expected_state else 0),
                )
            return cursor.rowcount > 0

    def claim_price_alert_trigger(self, alert_id: int, triggered_at: str, disable: bool) -> bool:
        """原子认领一次边沿触发，避免多开软件重复写历史和通知。"""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE price_alerts
                SET last_triggered_at = ?,
                    trigger_count = trigger_count + 1,
                    last_state = 1,
                    enabled = CASE WHEN ? THEN 0 ELSE enabled END
                WHERE id = ? AND enabled = 1 AND last_state = 0
                """,
                (triggered_at, 1 if disable else 0, int(alert_id)),
            )
            return cursor.rowcount == 1

    def delete_price_alert(self, alert_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM price_alerts WHERE id = ?", (alert_id,))

    def get_price_alert(self, alert_id: int) -> PriceAlertRecord | None:
        return next((item for item in self.list_price_alerts() if item.id == int(alert_id)), None)

    def add_alert_history(
        self,
        alert_id: int,
        code: str,
        name: str,
        kind: str,
        threshold: float,
        price: float | None,
        change_pct: float | None,
        message: str,
        created_at: str | None = None,
        quote_source: str = "",
        quote_at: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_history(alert_id, code, name, kind, threshold, price, change_pct, message, created_at, quote_source, quote_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id, code, name, kind, float(threshold), price, change_pct, message,
                    created_at or datetime.now().isoformat(), str(quote_source or ""), str(quote_at or ""),
                ),
            )

    def list_alert_history(self, limit: int = 200, since: datetime | None = None) -> list[AlertHistoryRecord]:
        query = "SELECT * FROM alert_history"
        params: list[Any] = []
        if since is not None:
            query += " WHERE created_at >= ?"
            params.append(since.isoformat())
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        records: list[AlertHistoryRecord] = []
        for row in rows:
            try:
                created_at = datetime.fromisoformat(row["created_at"])
            except (ValueError, TypeError):
                created_at = datetime.now()
            records.append(
                AlertHistoryRecord(
                    id=row["id"],
                    alert_id=row["alert_id"],
                    code=row["code"],
                    name=row["name"],
                    kind=row["kind"],
                    threshold=row["threshold"],
                    price=row["price"],
                    change_pct=row["change_pct"],
                    message=row["message"],
                    created_at=created_at,
                    quote_source=row["quote_source"] or "",
                    quote_at=row["quote_at"] or "",
                )
            )
        return records

    def clear_alert_history(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM alert_history")

    # ---- operation audit --------------------------------------------

    def add_audit(
        self,
        entity_type: str,
        entity_id: int | str,
        action: str,
        actor: str,
        *,
        before: Any = None,
        after: Any = None,
        reason: str = "",
    ) -> int:
        """记录业务操作快照；调用方传入普通数据，不传 SQLite 行对象。"""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO operation_audit(
                    entity_type, entity_id, action, actor, before_json, after_json, reason, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(entity_type), str(entity_id), str(action), str(actor or "unknown"),
                    json.dumps(before if before is not None else {}, ensure_ascii=False, default=str),
                    json.dumps(after if after is not None else {}, ensure_ascii=False, default=str),
                    str(reason or ""), datetime.now().isoformat(timespec="seconds"),
                ),
            )
            return int(cursor.lastrowid)

    def list_audit(self, entity_type: str = "", limit: int = 500) -> list[AuditRecord]:
        sql = "SELECT * FROM operation_audit"
        params: list[Any] = []
        if entity_type:
            sql += " WHERE entity_type = ?"
            params.append(entity_type)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            AuditRecord(
                id=int(row["id"]), entity_type=row["entity_type"], entity_id=row["entity_id"],
                action=row["action"], actor=row["actor"], before_json=row["before_json"],
                after_json=row["after_json"], reason=row["reason"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def cleanup_alert_history(self, keep_days: int = 3) -> int:
        """删除超过保留天数的预警记录，仅保留最近 keep_days 个自然日。

        keep_days=3 表示保留今天及之前两天，删除「今天往前第 3 天 00:00」之前的记录。
        ISO 格式时间字符串按字典序可比，因此用简单的 < 比较即可。
        """
        cutoff = (datetime.now() - timedelta(days=max(0, keep_days - 1))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM alert_history WHERE created_at < ?", (cutoff.isoformat(),))
            return cursor.rowcount

    def add_fetch_log(
        self,
        action: str,
        target: str = "",
        source: str = "",
        status: str = "info",
        message: str = "",
        duration_ms: int = 0,
        keep: int = 500,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO fetch_logs(action, target, source, status, message, duration_ms, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action[:80],
                    target[:80],
                    source[:80],
                    status[:20],
                    message[:500],
                    int(duration_ms or 0),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.execute(
                """
                DELETE FROM fetch_logs
                WHERE id NOT IN (
                    SELECT id FROM fetch_logs ORDER BY id DESC LIMIT ?
                )
                """,
                (max(1, keep),),
            )

    def list_fetch_logs(self, limit: int = 200) -> list[FetchLogRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, action, target, source, status, message, duration_ms, created_at
                FROM fetch_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [
            FetchLogRecord(
                id=row["id"],
                action=row["action"],
                target=row["target"],
                source=row["source"],
                status=row["status"],
                message=row["message"],
                duration_ms=row["duration_ms"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def clear_fetch_logs(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM fetch_logs")

    def fetch_log_summary(self, limit: int = 500) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM (
                    SELECT status FROM fetch_logs ORDER BY id DESC LIMIT ?
                )
                GROUP BY status
                """,
                (max(1, limit),),
            ).fetchall()
        summary = {"success": 0, "failed": 0, "cache": 0, "info": 0}
        for row in rows:
            summary[row["status"]] = int(row["count"])
        return summary

    def add_tracking_task(
        self,
        code: str,
        name: str,
        task_type: str,
        entry_price: float | None,
        fail_price: float | None,
        target_price: float | None,
        horizon_days: int,
        due_date: str,
        note: str = "",
    ) -> int | None:
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT id
                FROM tracking_tasks
                WHERE code = ? AND task_type = ? AND status = 'open'
                ORDER BY id DESC
                LIMIT 1
                """,
                (code, task_type),
            ).fetchone()
            if existing:
                return None
            cursor = conn.execute(
                """
                INSERT INTO tracking_tasks(
                    code, name, task_type, status, entry_price, fail_price, target_price,
                    horizon_days, created_at, due_date, note, result, last_review
                )
                VALUES(?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, '', '')
                """,
                (
                    code,
                    name,
                    task_type,
                    entry_price,
                    fail_price,
                    target_price,
                    max(1, int(horizon_days or 5)),
                    datetime.now().isoformat(timespec="seconds"),
                    due_date,
                    self._truncate_note(note),
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _truncate_note(note: str) -> str:
        text = str(note or "")
        if len(text) <= NOTE_LIMIT:
            return text
        keep = max(0, NOTE_LIMIT - len(NOTE_TRUNCATED_SUFFIX))
        return text[:keep] + NOTE_TRUNCATED_SUFFIX

    def list_tracking_tasks(self, include_closed: bool = True) -> list[TrackingTaskRecord]:
        sql = """
            SELECT * FROM tracking_tasks
            {where}
            ORDER BY status = 'open' DESC, id DESC
        """
        where = "" if include_closed else "WHERE status = 'open'"
        with self.connect() as conn:
            rows = conn.execute(sql.format(where=where)).fetchall()
        return [
            TrackingTaskRecord(
                id=row["id"],
                code=row["code"],
                name=row["name"],
                task_type=row["task_type"],
                status=row["status"],
                entry_price=row["entry_price"],
                fail_price=row["fail_price"],
                target_price=row["target_price"],
                horizon_days=row["horizon_days"],
                created_at=datetime.fromisoformat(row["created_at"]),
                due_date=row["due_date"],
                note=row["note"],
                result=row["result"],
                last_review=row["last_review"],
            )
            for row in rows
        ]

    def update_tracking_task(self, task_id: int, status: str, result: str, last_review: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tracking_tasks
                SET status = ?, result = ?, last_review = ?
                WHERE id = ?
                """,
                (status, result[:500], last_review[:1000], task_id),
            )

    def update_tracking_task_if_open(self, task_id: int, status: str, result: str, last_review: str) -> bool:
        """只允许一个进程从 open 原子推进观察任务，避免重复改变结论。"""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tracking_tasks
                SET status = ?, result = ?, last_review = ?
                WHERE id = ? AND status = 'open'
                """,
                (status, result[:500], last_review[:1000], int(task_id)),
            )
            return cursor.rowcount == 1

    def delete_tracking_task(self, task_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM tracking_tasks WHERE id = ?", (task_id,))
