from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable


V1_DB_FILENAME = "stock_tool_v1.sqlite3"
V1_SCHEMA_VERSION = 16

FACTOR_SNAPSHOT_COLUMNS = {
    "code": "TEXT PRIMARY KEY",
    "name": "TEXT NOT NULL",
    "price": "REAL",
    "pe": "REAL",
    "pe_ttm": "REAL",
    "pb": "REAL",
    "market_cap_yi": "REAL",
    "roe": "REAL",
    "gross_margin": "REAL",
    "net_margin": "REAL",
    "debt_ratio": "REAL",
    "revenue_growth": "REAL",
    "profit_growth": "REAL",
    "trend_20d": "REAL",
    "trend_60d": "REAL",
    "volume_ratio": "REAL",
    "turnover": "REAL",
    "box_score": "REAL",
    "box_upper": "REAL",
    "box_lower": "REAL",
    "box_range_pct": "REAL",
    "box_position_pct": "REAL",
    "box_position_label": "TEXT NOT NULL DEFAULT ''",
    "box_days": "INTEGER NOT NULL DEFAULT 0",
    "box_ma20_slope_pct": "REAL",
    "box_boundary_touch_count": "INTEGER NOT NULL DEFAULT 0",
    "box_is_sideways": "INTEGER NOT NULL DEFAULT 0",
    "box_summary": "TEXT NOT NULL DEFAULT ''",
    "industry": "TEXT NOT NULL DEFAULT ''",
    "industry_strength": "REAL",
    "industry_rank": "INTEGER",
    "industry_sample_count": "INTEGER",
    "industry_updated_at": "TEXT NOT NULL DEFAULT ''",
    "technical_label": "TEXT NOT NULL DEFAULT ''",
    "technical_status": "TEXT NOT NULL DEFAULT ''",
    "technical_summary": "TEXT NOT NULL DEFAULT ''",
    "risk_flags_json": "TEXT NOT NULL DEFAULT '[]'",
    "data_date": "TEXT NOT NULL DEFAULT ''",
    "quote_updated_at": "TEXT NOT NULL DEFAULT ''",
    "report_date": "TEXT NOT NULL DEFAULT ''",
    "financial_updated_at": "TEXT NOT NULL DEFAULT ''",
    "history_updated_at": "TEXT NOT NULL DEFAULT ''",
    "pe_ttm_updated_at": "TEXT NOT NULL DEFAULT ''",
    "pe_ttm_source": "TEXT NOT NULL DEFAULT ''",
    "pb_updated_at": "TEXT NOT NULL DEFAULT ''",
    "pb_source": "TEXT NOT NULL DEFAULT ''",
    "updated_at": "TEXT NOT NULL DEFAULT ''",
    "missing_fields": "TEXT NOT NULL DEFAULT ''",
}

FACTOR_SNAPSHOT_ALTER_COLUMNS = {
    column: (
        definition
        .replace(" PRIMARY KEY", "")
        .replace(" NOT NULL", "")
    )
    for column, definition in FACTOR_SNAPSHOT_COLUMNS.items()
}

STRATEGY_PRESET_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "name": "TEXT NOT NULL UNIQUE",
    "user_text": "TEXT NOT NULL",
    "query_spec_json": "TEXT NOT NULL",
    "summary": "TEXT NOT NULL",
    "parse_source": "TEXT NOT NULL DEFAULT ''",
    "ai_repair_retry": "INTEGER NOT NULL DEFAULT 0",
    "template_id": "TEXT NOT NULL DEFAULT ''",
    "template_version": "TEXT NOT NULL DEFAULT ''",
    "universe_id": "TEXT NOT NULL DEFAULT ''",
    "config_hash": "TEXT NOT NULL DEFAULT ''",
    "created_at": "TEXT NOT NULL",
    "updated_at": "TEXT NOT NULL",
    "last_run_at": "TEXT NOT NULL DEFAULT ''",
    "run_count": "INTEGER NOT NULL DEFAULT 0",
}
STRATEGY_PRESET_VERSION_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "preset_id": "INTEGER NOT NULL",
    "preset_name": "TEXT NOT NULL",
    "version_no": "INTEGER NOT NULL",
    "user_text": "TEXT NOT NULL",
    "query_spec_json": "TEXT NOT NULL",
    "summary": "TEXT NOT NULL",
    "parse_source": "TEXT NOT NULL DEFAULT ''",
    "ai_repair_retry": "INTEGER NOT NULL DEFAULT 0",
    "template_id": "TEXT NOT NULL DEFAULT ''",
    "template_version": "TEXT NOT NULL DEFAULT ''",
    "universe_id": "TEXT NOT NULL DEFAULT ''",
    "config_hash": "TEXT NOT NULL DEFAULT ''",
    "change_note": "TEXT NOT NULL DEFAULT ''",
    "created_at": "TEXT NOT NULL",
}
QUERY_RUN_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "preset_id": "INTEGER",
    "preset_name": "TEXT NOT NULL DEFAULT ''",
    "source_label": "TEXT NOT NULL",
    "user_text": "TEXT NOT NULL",
    "query_spec_json": "TEXT NOT NULL",
    "summary": "TEXT NOT NULL",
    "data_source": "TEXT NOT NULL",
    "scanned": "INTEGER NOT NULL",
    "matched": "INTEGER NOT NULL",
    "skipped": "INTEGER NOT NULL",
    "failed": "INTEGER NOT NULL",
    "note": "TEXT NOT NULL DEFAULT ''",
    "template_id": "TEXT NOT NULL DEFAULT ''",
    "template_version": "TEXT NOT NULL DEFAULT ''",
    "universe_id": "TEXT NOT NULL DEFAULT ''",
    "universe_hash": "TEXT NOT NULL DEFAULT ''",
    "config_hash": "TEXT NOT NULL DEFAULT ''",
    "scoring_context_json": "TEXT NOT NULL DEFAULT '{}'",
    "created_at": "TEXT NOT NULL",
}
QUERY_RUN_RESULT_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "run_id": "INTEGER NOT NULL",
    "rank_order": "INTEGER NOT NULL",
    "code": "TEXT NOT NULL",
    "name": "TEXT NOT NULL",
    "local_score": "REAL",
    "reason": "TEXT NOT NULL DEFAULT ''",
    "risk_flags_json": "TEXT NOT NULL DEFAULT '[]'",
    "metrics_json": "TEXT NOT NULL DEFAULT '{}'",
    "score_breakdown_json": "TEXT NOT NULL DEFAULT '{}'",
}


def _alter_columns(columns: dict[str, str]) -> dict[str, str]:
    return {
        column: definition.replace(" PRIMARY KEY", "").replace(" AUTOINCREMENT", "").replace(" UNIQUE", "").replace(" NOT NULL", "")
        for column, definition in columns.items()
    }


def v1_db_path_for(data_dir: Path) -> Path:
    return data_dir / V1_DB_FILENAME


class V1Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        self._open_connection()
        self.init_schema()

    def _open_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("V1Database 已关闭")
        if self._conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
        return self._conn

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        with self._lock:
            conn = self._open_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            conn = self._conn
            self._conn = None
            self._closed = True
            if conn is not None:
                try:
                    conn.commit()
                finally:
                    conn.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS factor_snapshots (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    price REAL,
                    pe REAL,
                    pe_ttm REAL,
                    pb REAL,
                    market_cap_yi REAL,
                    roe REAL,
                    gross_margin REAL,
                    net_margin REAL,
                    debt_ratio REAL,
                    revenue_growth REAL,
                    profit_growth REAL,
                    trend_20d REAL,
                    trend_60d REAL,
                    box_score REAL,
                    box_upper REAL,
                    box_lower REAL,
                    box_range_pct REAL,
                    box_position_pct REAL,
                    box_position_label TEXT NOT NULL DEFAULT '',
                    box_days INTEGER NOT NULL DEFAULT 0,
                    box_ma20_slope_pct REAL,
                    box_boundary_touch_count INTEGER NOT NULL DEFAULT 0,
                    box_is_sideways INTEGER NOT NULL DEFAULT 0,
                    box_summary TEXT NOT NULL DEFAULT '',
                    industry TEXT NOT NULL DEFAULT '',
                    industry_strength REAL,
                    industry_rank INTEGER,
                    industry_sample_count INTEGER,
                    industry_updated_at TEXT NOT NULL DEFAULT '',
                    technical_label TEXT NOT NULL DEFAULT '',
                    technical_status TEXT NOT NULL DEFAULT '',
                    technical_summary TEXT NOT NULL DEFAULT '',
                    risk_flags_json TEXT NOT NULL DEFAULT '[]',
                    data_date TEXT NOT NULL DEFAULT '',
                    quote_updated_at TEXT NOT NULL DEFAULT '',
                    report_date TEXT NOT NULL DEFAULT '',
                    financial_updated_at TEXT NOT NULL DEFAULT '',
                    history_updated_at TEXT NOT NULL DEFAULT '',
                    pe_ttm_updated_at TEXT NOT NULL DEFAULT '',
                    pe_ttm_source TEXT NOT NULL DEFAULT '',
                    pb_updated_at TEXT NOT NULL DEFAULT '',
                    pb_source TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    missing_fields TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_table_columns(conn, "factor_snapshots", FACTOR_SNAPSHOT_ALTER_COLUMNS)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_factor_snapshots_updated_at ON factor_snapshots(updated_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS data_snapshot_manifests (
                    snapshot_id TEXT PRIMARY KEY,
                    asof_at TEXT NOT NULL,
                    availability_cutoff TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    universe_id TEXT NOT NULL,
                    universe_version TEXT NOT NULL DEFAULT '',
                    dataset_version TEXT NOT NULL,
                    source_policy_version TEXT NOT NULL DEFAULT '',
                    source_versions_json TEXT NOT NULL DEFAULT '{}',
                    facts_digest TEXT NOT NULL DEFAULT '',
                    quality_status TEXT NOT NULL,
                    quality_summary_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            self._ensure_table_columns(
                conn,
                "data_snapshot_manifests",
                {
                    "universe_version": "TEXT NOT NULL DEFAULT ''",
                    "source_policy_version": "TEXT NOT NULL DEFAULT ''",
                    "facts_digest": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS data_snapshot_facts (
                    snapshot_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL DEFAULT '',
                    source_route TEXT NOT NULL DEFAULT 'primary',
                    fetched_at TEXT NOT NULL,
                    quality_status TEXT NOT NULL,
                    revision_id TEXT NOT NULL DEFAULT '',
                    raw_hash TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, code, field_name),
                    FOREIGN KEY(snapshot_id) REFERENCES data_snapshot_manifests(snapshot_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_data_snapshot_facts_available_at "
                "ON data_snapshot_facts(available_at)"
            )
            self._ensure_table_columns(
                conn,
                "data_snapshot_facts",
                {"source_route": "TEXT NOT NULL DEFAULT 'primary'"},
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS protect_data_snapshot_manifests_update
                BEFORE UPDATE ON data_snapshot_manifests
                BEGIN
                    SELECT RAISE(ABORT, 'data_snapshot_manifests is append-only');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS protect_data_snapshot_manifests_delete
                BEFORE DELETE ON data_snapshot_manifests
                BEGIN
                    SELECT RAISE(ABORT, 'data_snapshot_manifests is append-only');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS protect_data_snapshot_facts_update
                BEFORE UPDATE ON data_snapshot_facts
                BEGIN
                    SELECT RAISE(ABORT, 'data_snapshot_facts is append-only');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS protect_data_snapshot_facts_delete
                BEFORE DELETE ON data_snapshot_facts
                BEGIN
                    SELECT RAISE(ABORT, 'data_snapshot_facts is append-only');
                END
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshot_ingestion_events (
                    refresh_event_id TEXT PRIMARY KEY,
                    asof_at TEXT NOT NULL,
                    availability_cutoff TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    universe_id TEXT NOT NULL,
                    universe_version TEXT NOT NULL DEFAULT '',
                    dataset_version TEXT NOT NULL DEFAULT '',
                    source_policy_version TEXT NOT NULL,
                    source_versions_json TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    source_route TEXT NOT NULL DEFAULT 'primary',
                    used_cache INTEGER NOT NULL DEFAULT 0,
                    used_failover INTEGER NOT NULL DEFAULT 0,
                    coverage_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT NOT NULL DEFAULT '',
                    snapshot_id TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(snapshot_id) REFERENCES data_snapshot_manifests(snapshot_id)
                )
                """
            )
            self._ensure_table_columns(
                conn,
                "snapshot_ingestion_events",
                {
                    "data_source": "TEXT NOT NULL DEFAULT ''",
                    "universe_version": "TEXT NOT NULL DEFAULT ''",
                    "dataset_version": "TEXT NOT NULL DEFAULT ''",
                    "source_policy_version": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshot_ingestion_events_cutoff "
                "ON snapshot_ingestion_events(universe_id, availability_cutoff, created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshot_ingestion_events_snapshot "
                "ON snapshot_ingestion_events(snapshot_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_presets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    user_text TEXT NOT NULL,
                    query_spec_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    parse_source TEXT NOT NULL DEFAULT '',
                    ai_repair_retry INTEGER NOT NULL DEFAULT 0,
                    template_id TEXT NOT NULL DEFAULT '',
                    template_version TEXT NOT NULL DEFAULT '',
                    universe_id TEXT NOT NULL DEFAULT '',
                    config_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_at TEXT NOT NULL DEFAULT '',
                    run_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._ensure_table_columns(conn, "strategy_presets", _alter_columns(STRATEGY_PRESET_COLUMNS))
            conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy_presets_updated_at ON strategy_presets(updated_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_preset_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preset_id INTEGER NOT NULL,
                    preset_name TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    user_text TEXT NOT NULL,
                    query_spec_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    parse_source TEXT NOT NULL DEFAULT '',
                    ai_repair_retry INTEGER NOT NULL DEFAULT 0,
                    template_id TEXT NOT NULL DEFAULT '',
                    template_version TEXT NOT NULL DEFAULT '',
                    universe_id TEXT NOT NULL DEFAULT '',
                    config_hash TEXT NOT NULL DEFAULT '',
                    change_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(preset_id, version_no)
                )
                """
            )
            self._ensure_table_columns(conn, "strategy_preset_versions", _alter_columns(STRATEGY_PRESET_VERSION_COLUMNS))
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_preset_versions_preset_id "
                "ON strategy_preset_versions(preset_id, version_no DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_preset_versions_created_at "
                "ON strategy_preset_versions(created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preset_id INTEGER,
                    preset_name TEXT NOT NULL DEFAULT '',
                    source_label TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    query_spec_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    scanned INTEGER NOT NULL,
                    matched INTEGER NOT NULL,
                    skipped INTEGER NOT NULL,
                    failed INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    template_id TEXT NOT NULL DEFAULT '',
                    template_version TEXT NOT NULL DEFAULT '',
                    universe_id TEXT NOT NULL DEFAULT '',
                    universe_hash TEXT NOT NULL DEFAULT '',
                    config_hash TEXT NOT NULL DEFAULT '',
                    scoring_context_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            self._ensure_table_columns(conn, "query_runs", _alter_columns(QUERY_RUN_COLUMNS))
            conn.execute("CREATE INDEX IF NOT EXISTS idx_query_runs_created_at ON query_runs(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_query_runs_preset_id ON query_runs(preset_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_run_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    rank_order INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    local_score REAL,
                    reason TEXT NOT NULL DEFAULT '',
                    risk_flags_json TEXT NOT NULL DEFAULT '[]',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    score_breakdown_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS formal_screening_certificates (
                    certificate_id TEXT PRIMARY KEY,
                    run_id INTEGER NOT NULL UNIQUE,
                    data_snapshot_id TEXT NOT NULL,
                    refresh_event_id TEXT NOT NULL,
                    universe_definition_id TEXT NOT NULL,
                    universe_version TEXT NOT NULL,
                    members_digest TEXT NOT NULL,
                    selection_scope TEXT NOT NULL,
                    selection_policy_version TEXT NOT NULL,
                    selected_limit INTEGER,
                    truncation_reason TEXT NOT NULL DEFAULT '',
                    member_status_policy TEXT NOT NULL DEFAULT '',
                    formula_version TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    quality_status TEXT NOT NULL,
                    quality_summary_json TEXT NOT NULL DEFAULT '{}',
                    condition_hash TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    certificate_hash TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES query_runs(id),
                    FOREIGN KEY(data_snapshot_id) REFERENCES data_snapshot_manifests(snapshot_id),
                    FOREIGN KEY(refresh_event_id) REFERENCES snapshot_ingestion_events(refresh_event_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_formal_certificates_snapshot "
                "ON formal_screening_certificates(data_snapshot_id, issued_at DESC)"
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS validate_formal_certificate_provenance
                BEFORE INSERT ON formal_screening_certificates
                WHEN NOT EXISTS(
                    SELECT 1
                    FROM snapshot_ingestion_events e
                    WHERE e.refresh_event_id = NEW.refresh_event_id
                      AND e.snapshot_id = NEW.data_snapshot_id
                      AND e.status IN ('success', 'partial')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'formal certificate snapshot and refresh event mismatch');
                END
                """
            )
            self._ensure_table_columns(conn, "query_run_results", _alter_columns(QUERY_RUN_RESULT_COLUMNS))
            conn.execute("CREATE INDEX IF NOT EXISTS idx_query_run_results_run_id ON query_run_results(run_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_regime_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asof_date TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(asof_date, source)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_market_regime_snapshots_date "
                "ON market_regime_snapshots(asof_date DESC, source)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS weight_policy_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'shadow',
                    baseline_weights_json TEXT NOT NULL DEFAULT '{}',
                    regime_targets_json TEXT NOT NULL DEFAULT '{}',
                    max_daily_delta REAL NOT NULL DEFAULT 0.03,
                    min_confirm_days INTEGER NOT NULL DEFAULT 3,
                    data_quality_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(name, version)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_weight_policy_versions_status "
                "ON weight_policy_versions(status, name, version)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_run_contexts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL UNIQUE,
                    asof_date TEXT NOT NULL,
                    regime TEXT NOT NULL DEFAULT 'uncertain',
                    regime_confidence REAL NOT NULL DEFAULT 0,
                    weight_policy_id INTEGER,
                    weight_policy_name TEXT NOT NULL DEFAULT '',
                    weight_policy_version TEXT NOT NULL DEFAULT '',
                    effective_weights_json TEXT NOT NULL DEFAULT '{}',
                    shadow INTEGER NOT NULL DEFAULT 0,
                    ai_log_id INTEGER,
                    data_quality_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_run_contexts_regime "
                "ON strategy_run_contexts(asof_date, regime)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS observation_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    result_id INTEGER NOT NULL,
                    run_id INTEGER NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    sample_date TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    entry_price REAL,
                    rank_order INTEGER NOT NULL,
                    local_score REAL,
                    industry TEXT NOT NULL DEFAULT '',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    score_breakdown_json TEXT NOT NULL DEFAULT '{}',
                    ai_score REAL,
                    ai_level TEXT NOT NULL DEFAULT '',
                    sample_class TEXT NOT NULL DEFAULT 'legacy',
                    certificate_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(result_id, horizon_days),
                    FOREIGN KEY(certificate_id) REFERENCES formal_screening_certificates(certificate_id)
                )
                """
            )
            self._ensure_table_columns(
                conn,
                "observation_samples",
                {
                    "sample_class": "TEXT NOT NULL DEFAULT 'legacy'",
                    "certificate_id": "TEXT",
                },
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_observation_samples_due ON observation_samples(due_date, horizon_days)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_observation_samples_run ON observation_samples(run_id, rank_order)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_observation_samples_class ON observation_samples(sample_class, run_id)")
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS validate_formal_observation_sample_insert
                BEFORE INSERT ON observation_samples
                WHEN NEW.sample_class = 'formal'
                BEGIN
                    SELECT RAISE(ABORT, 'formal observation requires certificate')
                    WHERE NEW.certificate_id IS NULL
                       OR NOT EXISTS(
                            SELECT 1
                            FROM formal_screening_certificates c
                            WHERE c.certificate_id = NEW.certificate_id
                              AND c.run_id = NEW.run_id
                              AND c.execution_mode = 'formal'
                              AND c.quality_status = 'verified'
                       );
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS validate_formal_observation_sample_update
                BEFORE UPDATE OF sample_class, certificate_id, run_id ON observation_samples
                WHEN NEW.sample_class = 'formal'
                BEGIN
                    SELECT RAISE(ABORT, 'formal observation requires certificate')
                    WHERE NEW.certificate_id IS NULL
                       OR NOT EXISTS(
                            SELECT 1
                            FROM formal_screening_certificates c
                            WHERE c.certificate_id = NEW.certificate_id
                              AND c.run_id = NEW.run_id
                              AND c.execution_mode = 'formal'
                              AND c.quality_status = 'verified'
                       );
                END
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS observation_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sample_id INTEGER NOT NULL UNIQUE,
                    settled_at TEXT NOT NULL,
                    exit_date TEXT NOT NULL DEFAULT '',
                    exit_price REAL,
                    return_pct REAL,
                    max_favorable_pct REAL,
                    max_drawdown_pct REAL,
                    hs300_return_pct REAL,
                    excess_vs_hs300_pct REAL,
                    industry_return_pct REAL,
                    excess_vs_industry_pct REAL,
                    direction_hit INTEGER,
                    benchmark_hit INTEGER,
                    rating TEXT NOT NULL DEFAULT '',
                    data_quality TEXT NOT NULL DEFAULT 'ok',
                    data_quality_note TEXT NOT NULL DEFAULT '',
                    metrics_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_observation_outcomes_rating ON observation_outcomes(rating, data_quality)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_decision_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_type TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL,
                    input_summary TEXT NOT NULL DEFAULT '',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    cost_json TEXT NOT NULL DEFAULT '{}',
                    adopted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_decision_logs_prompt ON ai_decision_logs(prompt_version, created_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_discussion_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL DEFAULT '',
                    candidate_set_digest TEXT NOT NULL DEFAULT '',
                    data_snapshot_id TEXT NOT NULL,
                    refresh_event_id TEXT NOT NULL,
                    formula_version TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    evidence_package_hash TEXT NOT NULL DEFAULT '',
                    request_hash TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL,
                    model_version TEXT NOT NULL DEFAULT '',
                    response_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    cost_note TEXT NOT NULL DEFAULT '',
                    evidence_package_json TEXT NOT NULL DEFAULT '{}',
                    request_text TEXT NOT NULL DEFAULT '',
                    raw_response TEXT NOT NULL DEFAULT '',
                    normalized_response TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            self._ensure_table_columns(
                conn,
                "research_discussion_audits",
                {
                    "run_id": "TEXT NOT NULL DEFAULT ''",
                    "evidence_package_hash": "TEXT NOT NULL DEFAULT ''",
                    "request_hash": "TEXT NOT NULL DEFAULT ''",
                    "evidence_package_json": "TEXT NOT NULL DEFAULT '{}'",
                    "request_text": "TEXT NOT NULL DEFAULT ''",
                    "raw_response": "TEXT NOT NULL DEFAULT ''",
                    "normalized_response": "TEXT NOT NULL DEFAULT '{}'",
                },
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_discussion_audits_evidence "
                "ON research_discussion_audits(data_snapshot_id, refresh_event_id, created_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS professional_formula_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'shadow',
                    dimension_weights_json TEXT NOT NULL DEFAULT '{}',
                    field_weights_json TEXT NOT NULL DEFAULT '{}',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    validation_json TEXT NOT NULL DEFAULT '{}',
                    ai_log_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    activated_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(name, version)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_professional_formula_candidates_status "
                "ON professional_formula_candidates(status, updated_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS professional_weight_ranges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    dimension TEXT NOT NULL,
                    field_name TEXT NOT NULL DEFAULT '',
                    min_weight REAL NOT NULL,
                    max_weight REAL NOT NULL,
                    center_weight REAL NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    data_quality_score REAL NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_professional_weight_ranges_lookup "
                "ON professional_weight_ranges(scope, dimension, field_name, created_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS professional_formula_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    source_candidate_id INTEGER,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_professional_formula_memory_type "
                "ON professional_formula_memory(memory_type, created_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS formula_governance_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id INTEGER,
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
                "CREATE INDEX IF NOT EXISTS idx_formula_governance_audit_candidate "
                "ON formula_governance_audit(candidate_id, created_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS exploratory_candidate_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    proposed_lens_id TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    proposal_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_human_review',
                    operator TEXT NOT NULL DEFAULT '',
                    audited_at TEXT NOT NULL DEFAULT '',
                    rejection_reason TEXT NOT NULL DEFAULT '',
                    formal_run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exploratory_candidate_proposals_status "
                "ON exploratory_candidate_proposals(status, updated_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS universe_snapshots (
                    universe_id TEXT NOT NULL,
                    asof_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    membership_hash TEXT NOT NULL,
                    member_count INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'complete',
                    message TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(universe_id, asof_date, source)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS universe_memberships (
                    universe_id TEXT NOT NULL,
                    asof_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(universe_id, asof_date, source, code)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_universe_memberships_lookup ON universe_memberships(universe_id, asof_date)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS factor_update_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    universe_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total INTEGER NOT NULL DEFAULT 0,
                    success INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS factor_update_items (
                    job_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(job_id, code)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_factor_update_items_status ON factor_update_items(job_id, status)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mvp_shadow_runs (
                    run_date TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    universe_id TEXT NOT NULL,
                    template_version TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    universe_hash TEXT NOT NULL DEFAULT '',
                    result_hash TEXT NOT NULL DEFAULT '',
                    result_count INTEGER NOT NULL DEFAULT 0,
                    score_min REAL,
                    score_max REAL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_date, template_id, universe_id, config_hash)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mvp_shadow_runs_gate ON mvp_shadow_runs(config_hash, run_date, status)")
            self._seed_initial_strategy_versions(conn)
            conn.execute("PRAGMA user_version = %d" % V1_SCHEMA_VERSION)

    def _ensure_table_columns(self, conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] for row in rows}
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _seed_initial_strategy_versions(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO strategy_preset_versions(
                preset_id, preset_name, version_no, user_text, query_spec_json,
                summary, parse_source, ai_repair_retry, template_id,
                template_version, universe_id, config_hash, change_note, created_at
            )
            SELECT
                id, name, 1, user_text, query_spec_json, summary, parse_source,
                ai_repair_retry, template_id, template_version, universe_id,
                config_hash, 'v2 数据库升级自动补档', created_at
            FROM strategy_presets
            WHERE NOT EXISTS (
                SELECT 1
                FROM strategy_preset_versions
                WHERE strategy_preset_versions.preset_id = strategy_presets.id
            )
            """
        )
