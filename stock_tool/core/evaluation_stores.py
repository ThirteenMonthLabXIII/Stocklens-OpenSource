"""阶段7：数据质量、Agent 过程和 Candidate Lens 评价存储。

本模块故意把评价和候选生成、Agent 执行解耦：

* 旧的 ``observation_samples``/``observation_outcomes`` 仍是兼容事实表；
* formal、review_only、exploratory、legacy 在读取时严格分开；
* 评价只消费已经保存的样本、Agent 执行信封和不可变快照，不参与候选排序；
* 缺数、过期、人工关闭等样本保留，但只能带着排除原因进入统计。

阶段7表是附加表，由 Store 懒创建，因此不改变 V1 schema version，旧数据库可以直接读取。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping

from stock_tool.core.data_snapshot import DataSnapshotFact, fact_set_digest


EVALUATION_MODES = ("formal", "review_only", "exploratory", "legacy")
SAMPLE_CLASS_TO_MODE = {
    "formal": "formal",
    "review": "review_only",
    "exploratory": "exploratory",
    "legacy": "legacy",
}


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _dict(value: Any) -> dict[str, Any]:
    parsed = _json(value, {})
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _list(value: Any) -> list[Any]:
    parsed = _json(value, [])
    return list(parsed) if isinstance(parsed, list) else []


def _float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _mean(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 6) if clean else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _naive_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _date_text(value: Any) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else text


def _in_date_range(value: Any, start_date: str, end_date: str) -> bool:
    current = _date_text(value)
    return bool(current) and (not start_date or current >= _date_text(start_date)) and (not end_date or current <= _date_text(end_date))


class EvaluationStoreError(ValueError):
    """Raised when an evaluation input or mode is invalid."""


class _DatabaseMixin:
    def __init__(self, database: Any) -> None:
        self.database = database
        self.database_path = str(getattr(database, "db_path", database))

    def _connect(self):
        connect = getattr(self.database, "connect", None)
        if callable(connect):
            return connect()
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _table_exists(self, connection: sqlite3.Connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None


@dataclass(frozen=True)
class ObservationEvaluation:
    """A normalized observation row used by all Lens evaluations."""

    sample_id: int
    run_id: int
    mode: str
    code: str
    name: str
    industry: str
    horizon_days: int
    sample_date: str
    due_date: str
    lens_ids: tuple[str, ...]
    lens_versions: dict[str, str]
    regime: str
    snapshot_id: str
    outcome_present: bool
    data_quality: str
    valid_outcome: bool
    return_pct: float | None
    excess_vs_hs300_pct: float | None
    excess_vs_industry_pct: float | None
    max_drawdown_pct: float | None
    exclusion_reason: str
    exclusion_detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "run_id": self.run_id,
            "mode": self.mode,
            "code": self.code,
            "name": self.name,
            "industry": self.industry,
            "horizon_days": self.horizon_days,
            "sample_date": self.sample_date,
            "due_date": self.due_date,
            "lens_ids": list(self.lens_ids),
            "lens_versions": dict(self.lens_versions),
            "regime": self.regime,
            "snapshot_id": self.snapshot_id,
            "outcome_present": self.outcome_present,
            "data_quality": self.data_quality,
            "valid_outcome": self.valid_outcome,
            "return_pct": self.return_pct,
            "excess_vs_hs300_pct": self.excess_vs_hs300_pct,
            "excess_vs_industry_pct": self.excess_vs_industry_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "exclusion_reason": self.exclusion_reason,
            "exclusion_detail": self.exclusion_detail,
        }


class ObservationOutcomeStore(_DatabaseMixin):
    """Read and annotate observation outcomes without replacing legacy tables."""

    def __init__(self, database: Any) -> None:
        super().__init__(database)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS observation_evaluation_exclusions (
                    exclusion_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sample_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    reason_detail TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT 'system:stage7',
                    created_at TEXT NOT NULL,
                    UNIQUE(sample_id, reason_code)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_observation_evaluation_exclusions_sample
                ON observation_evaluation_exclusions(sample_id, created_at DESC)
                """
            )

    @staticmethod
    def _validate_mode(mode: str | None) -> str | None:
        if mode in (None, ""):
            return None
        normalized = str(mode).strip()
        if normalized not in EVALUATION_MODES:
            raise EvaluationStoreError(f"不支持的评价模式：{mode}")
        return normalized

    @staticmethod
    def _lens_data(breakdown: dict[str, Any], metrics: dict[str, Any], run_context: dict[str, Any]) -> tuple[tuple[str, ...], dict[str, str]]:
        raw_ids: Any = breakdown.get("lens_ids")
        if not raw_ids and isinstance(breakdown.get("candidate_contract"), Mapping):
            raw_ids = breakdown["candidate_contract"].get("lens_ids")
        if not raw_ids:
            raw_ids = metrics.get("lens_ids")
        lens_ids = [str(item).strip() for item in (raw_ids or []) if str(item).strip()]
        versions: dict[str, str] = {}
        evaluations = breakdown.get("lens_evaluations")
        if isinstance(evaluations, Mapping):
            matched: list[str] = []
            for lens_id, evaluation in evaluations.items():
                if not isinstance(evaluation, Mapping):
                    continue
                if str(evaluation.get("status") or "") == "matched" and str(lens_id).strip():
                    matched.append(str(lens_id).strip())
                version = str(evaluation.get("lens_version") or "").strip()
                if version:
                    versions[str(lens_id)] = version
            if not lens_ids:
                lens_ids = matched
        context_versions = run_context.get("lens_versions")
        if isinstance(context_versions, Mapping):
            for key, value in context_versions.items():
                versions.setdefault(str(key), str(value))
        return tuple(dict.fromkeys(lens_ids)), versions

    def _explicit_exclusions(self) -> dict[int, tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sample_id, reason_code, reason_detail
                FROM observation_evaluation_exclusions
                ORDER BY created_at DESC, exclusion_id DESC
                """
            ).fetchall()
        result: dict[int, tuple[str, str]] = {}
        for row in rows:
            sample_id = int(row["sample_id"])
            result.setdefault(sample_id, (str(row["reason_code"]), str(row["reason_detail"] or "")))
        return result

    def record_exclusion(
        self,
        sample_id: int,
        reason_code: str,
        *,
        mode: str,
        actor: str = "system:stage7",
        reason_detail: str = "",
        created_at: str | None = None,
    ) -> None:
        mode = self._validate_mode(mode) or "legacy"
        reason_code = str(reason_code or "").strip()
        if not reason_code:
            raise EvaluationStoreError("排除原因不能为空")
        actor = str(actor or "").strip()
        if not actor:
            raise EvaluationStoreError("排除操作 actor 不能为空")
        if reason_code == "human_closed" and not (actor.startswith("human:") or actor.startswith("user:")):
            raise EvaluationStoreError("human_closed 必须由 human:<id> 或 user:<id> 记录")
        with self._connect() as connection:
            sample = connection.execute(
                "SELECT sample_class FROM observation_samples WHERE id = ?",
                (int(sample_id),),
            ).fetchone()
            if sample is None:
                raise EvaluationStoreError(f"找不到观察样本：{sample_id}")
            actual_mode = SAMPLE_CLASS_TO_MODE.get(str(sample["sample_class"] or "legacy"), "legacy")
            if actual_mode != mode:
                raise EvaluationStoreError(f"样本 {sample_id} 属于 {actual_mode}，不能按 {mode} 写入排除原因")
            connection.execute(
                """
                INSERT INTO observation_evaluation_exclusions(
                    sample_id, mode, reason_code, reason_detail, actor, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(sample_id, reason_code) DO UPDATE SET
                    mode = excluded.mode,
                    reason_detail = excluded.reason_detail,
                    actor = excluded.actor,
                    created_at = excluded.created_at
                """,
                (int(sample_id), mode, reason_code, str(reason_detail or "")[:1000], actor, created_at or _now()),
            )

    def list_exclusions(self, *, mode: str | None = None) -> list[dict[str, Any]]:
        mode = self._validate_mode(mode)
        where = " WHERE mode = ?" if mode else ""
        params: tuple[Any, ...] = (mode,) if mode else ()
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT sample_id, mode, reason_code, reason_detail, actor, created_at FROM observation_evaluation_exclusions{where} ORDER BY created_at DESC, exclusion_id DESC",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_outcomes(
        self,
        *,
        mode: str | None = None,
        lens_id: str | None = None,
        horizon_days: int | None = None,
        start_date: str = "",
        end_date: str = "",
        include_unsettled: bool = True,
    ) -> list[ObservationEvaluation]:
        mode = self._validate_mode(mode)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.id AS sample_id, s.run_id, s.horizon_days, s.sample_date, s.due_date,
                       s.code, s.name, s.industry, s.sample_class, s.metrics_json,
                       s.score_breakdown_json, o.id AS outcome_id, o.return_pct,
                       o.excess_vs_hs300_pct, o.excess_vs_industry_pct, o.max_drawdown_pct,
                       o.data_quality, o.data_quality_note,
                       r.scoring_context_json, c.regime
                FROM observation_samples s
                JOIN query_runs r ON r.id = s.run_id
                LEFT JOIN strategy_run_contexts c ON c.run_id = s.run_id
                LEFT JOIN observation_outcomes o ON o.sample_id = s.id
                ORDER BY s.sample_date ASC, s.id ASC
                """
            ).fetchall()
        explicit = self._explicit_exclusions()
        result: list[ObservationEvaluation] = []
        for row in rows:
            sample_mode = SAMPLE_CLASS_TO_MODE.get(str(row["sample_class"] or "legacy"), "legacy")
            if mode and sample_mode != mode:
                continue
            if horizon_days is not None and int(row["horizon_days"]) != int(horizon_days):
                continue
            if not _in_date_range(row["sample_date"], start_date, end_date):
                continue
            outcome_present = row["outcome_id"] is not None
            if not include_unsettled and not outcome_present:
                continue
            metrics = _dict(row["metrics_json"])
            breakdown = _dict(row["score_breakdown_json"])
            run_context = _dict(row["scoring_context_json"])
            lens_ids, lens_versions = self._lens_data(breakdown, metrics, run_context)
            quality = str(row["data_quality"] or "pending") if outcome_present else "pending"
            stale = bool(metrics.get("stale_fields")) or str(metrics.get("data_quality_status") or "").lower() in {
                "stale", "blocked", "partial", "unverified"
            }
            explicit_reason, explicit_detail = explicit.get(int(row["sample_id"]), ("", ""))
            if explicit_reason:
                exclusion_reason, exclusion_detail = explicit_reason, explicit_detail
            elif quality != "ok" and outcome_present:
                exclusion_reason = f"data_quality:{quality}"
                exclusion_detail = str(row["data_quality_note"] or "")
            elif stale:
                exclusion_reason, exclusion_detail = "stale_snapshot", "样本关联快照或字段已过期/不可验证。"
            elif str(metrics.get("selection_state") or "").lower() == "closed":
                exclusion_reason, exclusion_detail = "human_closed", "候选状态已关闭，不能进入有效表现统计。"
            elif not outcome_present:
                exclusion_reason, exclusion_detail = "unsettled", "观察周期尚未完成结算。"
            else:
                exclusion_reason = exclusion_detail = ""
            valid = (
                outcome_present
                and quality == "ok"
                and _float(row["return_pct"]) is not None
                and not stale
                and not exclusion_reason
            )
            result.append(
                ObservationEvaluation(
                    sample_id=int(row["sample_id"]),
                    run_id=int(row["run_id"]),
                    mode=sample_mode,
                    code=str(row["code"] or "").zfill(6),
                    name=str(row["name"] or ""),
                    industry=str(row["industry"] or ""),
                    horizon_days=int(row["horizon_days"]),
                    sample_date=_date_text(row["sample_date"]),
                    due_date=_date_text(row["due_date"]),
                    lens_ids=lens_ids,
                    lens_versions=lens_versions,
                    regime=str(row["regime"] or run_context.get("regime") or "uncertain"),
                    snapshot_id=str(run_context.get("data_snapshot_id") or ""),
                    outcome_present=outcome_present,
                    data_quality=quality,
                    valid_outcome=valid,
                    return_pct=_float(row["return_pct"]),
                    excess_vs_hs300_pct=_float(row["excess_vs_hs300_pct"]),
                    excess_vs_industry_pct=_float(row["excess_vs_industry_pct"]),
                    max_drawdown_pct=_float(row["max_drawdown_pct"]),
                    exclusion_reason=exclusion_reason,
                    exclusion_detail=exclusion_detail,
                )
            )
        if lens_id:
            wanted = str(lens_id).strip()
            result = [item for item in result if wanted in item.lens_ids]
        return result

    def mode_counts(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for mode in EVALUATION_MODES:
            rows = self.list_outcomes(mode=mode)
            result[mode] = {
                "sample_count": len(rows),
                "settled_count": sum(item.outcome_present for item in rows),
                "valid_settled_count": sum(item.valid_outcome for item in rows),
                "excluded_count": sum(bool(item.exclusion_reason and item.exclusion_reason != "unsettled") for item in rows),
            }
        return result


@dataclass(frozen=True)
class DataQualityEvaluation:
    start_date: str
    end_date: str
    snapshot_count: int
    refresh_event_count: int
    metrics: dict[str, float | int | None]
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "snapshot_count": self.snapshot_count,
            "refresh_event_count": self.refresh_event_count,
            "metrics": dict(self.metrics),
            "notes": list(self.notes),
        }


class DataQualityEvaluationStore(_DatabaseMixin):
    """Evaluate freshness, completeness, source and snapshot reproducibility."""

    def evaluate(
        self,
        *,
        start_date: str = "",
        end_date: str = "",
        now: datetime | None = None,
    ) -> DataQualityEvaluation:
        current = _naive_datetime(now or datetime.now(timezone.utc)) or datetime.now(timezone.utc).replace(tzinfo=None)
        with self._connect() as connection:
            if not self._table_exists(connection, "data_snapshot_manifests"):
                return DataQualityEvaluation(start_date, end_date, 0, 0, {}, ("快照表不存在，无法评价数据质量。",))
            manifests = [
                dict(row)
                for row in connection.execute("SELECT * FROM data_snapshot_manifests ORDER BY created_at ASC").fetchall()
                if _in_date_range(row["created_at"] or row["asof_at"], start_date, end_date)
            ]
            events = []
            if self._table_exists(connection, "snapshot_ingestion_events"):
                events = [
                    dict(row)
                    for row in connection.execute("SELECT * FROM snapshot_ingestion_events ORDER BY created_at ASC").fetchall()
                    if _in_date_range(row["created_at"] or row["asof_at"], start_date, end_date)
                ]
            facts = []
            if self._table_exists(connection, "data_snapshot_facts"):
                facts = [dict(row) for row in connection.execute("SELECT * FROM data_snapshot_facts").fetchall()]
        notes: list[str] = []
        latest = max((item for item in manifests if item.get("asof_at")), key=lambda item: str(item.get("asof_at")), default=None)
        latest_age = None
        if latest:
            latest_dt = _naive_datetime(latest.get("asof_at"))
            if latest_dt:
                latest_age = round(max(0.0, (current - latest_dt).total_seconds() / 3600), 6)
        else:
            notes.append("没有可评价的快照时间。")

        expected_total = missing_total = 0
        for manifest in manifests:
            quality = _dict(manifest.get("quality_summary_json"))
            expected = _float(
                quality.get("expected_fact_count")
                if quality.get("expected_fact_count") is not None
                else quality.get("expected_fields")
            )
            missing = _float(
                quality.get("missing_fact_count")
                if quality.get("missing_fact_count") is not None
                else quality.get("missing_field_count")
            )
            if expected is not None and missing is not None and expected > 0:
                expected_total += int(expected)
                missing_total += int(max(0, missing))
        if not expected_total:
            notes.append("快照质量摘要没有同时提供 expected_fact_count 与 missing_fact_count，字段缺失率暂不可计算。")

        stable_events = sum(str(item.get("status") or "") == "success" for item in events)
        selected_snapshot_ids = {str(item.get("snapshot_id") or "") for item in manifests}
        selected_facts = [item for item in facts if str(item.get("snapshot_id") or "") in selected_snapshot_ids]
        facts_by_snapshot: dict[str, list[DataSnapshotFact]] = {}
        for item in selected_facts:
            try:
                fact = DataSnapshotFact(
                    snapshot_id=str(item.get("snapshot_id") or ""),
                    code=str(item.get("code") or ""),
                    field_name=str(item.get("field_name") or ""),
                    value=_json(item.get("value_json"), None),
                    effective_at=str(item.get("effective_at") or ""),
                    available_at=str(item.get("available_at") or ""),
                    source=str(item.get("source") or ""),
                    source_version=str(item.get("source_version") or ""),
                    fetched_at=str(item.get("fetched_at") or ""),
                    source_route=str(item.get("source_route") or "primary"),
                    quality_status=str(item.get("quality_status") or "verified"),
                    revision_id=str(item.get("revision_id") or ""),
                    raw_hash=str(item.get("raw_hash") or ""),
                )
            except (TypeError, ValueError):
                continue
            facts_by_snapshot.setdefault(fact.snapshot_id, []).append(fact)
        reproducible = 0
        mismatched = 0
        for manifest in manifests:
            expected_digest = str(manifest.get("facts_digest") or "").strip()
            snapshot_id = str(manifest.get("snapshot_id") or "")
            if not expected_digest:
                continue
            actual_digest = fact_set_digest(facts_by_snapshot.get(snapshot_id, []))
            if actual_digest == expected_digest:
                reproducible += 1
            else:
                mismatched += 1
        if mismatched:
            notes.append(f"{mismatched} 个快照的事实集合摘要与当前事实表不一致，不能视为可复现。")
        cached = sum(bool(item.get("used_cache")) for item in events)
        failover = sum(bool(item.get("used_failover")) for item in events)
        revised = sum(bool(str(item.get("revision_id") or "").strip()) for item in selected_facts)
        metrics: dict[str, float | int | None] = {
            "latest_snapshot_age_hours": latest_age,
            "field_missing_rate": _ratio(missing_total, expected_total) if expected_total else None,
            "source_stability_rate": _ratio(stable_events, len(events)),
            "snapshot_reproducibility_rate": _ratio(reproducible, len(manifests)),
            "cache_usage_rate": _ratio(cached, len(events)),
            "failover_usage_rate": _ratio(failover, len(events)),
            "revision_rate": _ratio(revised, len(selected_facts)),
        }
        return DataQualityEvaluation(start_date, end_date, len(manifests), len(events), metrics, tuple(notes))


def _execution_diagnostics(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    result = payload.get("result")
    result = dict(result) if isinstance(result, Mapping) else {}
    diagnostics = result.get("diagnostics")
    return result, dict(diagnostics) if isinstance(diagnostics, Mapping) else {}


def _json_compliant(payload: Mapping[str, Any], result: Mapping[str, Any], diagnostics: Mapping[str, Any]) -> bool | None:
    if str(payload.get("status") or "") == "skipped":
        return None
    failure_type = str(diagnostics.get("failure_type") or "").lower()
    if failure_type in {"jsondecodeerror", "json_error", "valueerror", "parseerror"}:
        return False
    raw = str(payload.get("raw_response") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return isinstance(parsed, Mapping)
    return bool(result and payload.get("normalized_response")) if str(payload.get("status") or "") in {"ok", "degraded"} else None


def _coverage_ratio(result: Mapping[str, Any]) -> float | None:
    coverage = result.get("coverage")
    if not isinstance(coverage, Mapping):
        return None
    explicit = _float(coverage.get("coverage_rate") or coverage.get("coverage_ratio"))
    if explicit is not None:
        return max(0.0, min(1.0, explicit if explicit <= 1 else explicit / 100))
    by_code = coverage.get("by_code")
    required = list(coverage.get("required_fields") or [])
    if isinstance(by_code, Mapping) and by_code:
        ratios: list[float] = []
        for item in by_code.values():
            if not isinstance(item, Mapping):
                continue
            total = len(required) or len(item.get("available_fields") or []) + len(item.get("missing_fields") or [])
            missing = len(item.get("missing_fields") or [])
            if total:
                ratios.append(max(0.0, min(1.0, 1 - missing / total)))
        return _mean(ratios)
    fact_count = _float(coverage.get("fact_count"))
    if fact_count is not None and required:
        return max(0.0, min(1.0, fact_count / len(required)))
    return None


def _cost_usd(cost_note: Any) -> float | None:
    text = str(cost_note or "")
    if not text:
        return None
    lines = [line for line in text.splitlines() if "$" in line]
    selected = next((line for line in reversed(lines) if "按返回用量估算费用" in line), None)
    selected = selected or next((line for line in lines if "估算费用" in line), None)
    match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", selected or "")
    return float(match.group(1)) if match else None


class AgentEvaluationStore(_DatabaseMixin):
    """Aggregate durable Agent execution envelopes and F-layer conflicts."""

    def _rows(self, *, start_date: str = "", end_date: str = "") -> list[dict[str, Any]]:
        with self._connect() as connection:
            if not self._table_exists(connection, "formal_agent_run_history"):
                return []
            rows = connection.execute(
                "SELECT agent_id, status, outcome, created_at, payload_json FROM formal_agent_run_history ORDER BY created_at ASC"
            ).fetchall()
        return [dict(row) for row in rows if _in_date_range(row["created_at"], start_date, end_date)]

    def _conflicts(self, *, start_date: str = "", end_date: str = "") -> dict[str, int]:
        with self._connect() as connection:
            if not self._table_exists(connection, "formal_evidence_runs"):
                return {}
            rows = connection.execute("SELECT run_id, created_at, aggregation_json FROM formal_evidence_runs").fetchall()
        result: dict[str, int] = {}
        for row in rows:
            if not _in_date_range(row["created_at"], start_date, end_date):
                continue
            aggregation = _dict(row["aggregation_json"])
            total = 0
            for summary in aggregation.get("summaries") or []:
                if isinstance(summary, Mapping):
                    total += len(summary.get("conflicts") or [])
            result[str(row["run_id"])] = total
        return result

    def summarize(
        self,
        *,
        agent_id: str | None = None,
        start_date: str = "",
        end_date: str = "",
    ) -> dict[str, Any]:
        rows = self._rows(start_date=start_date, end_date=end_date)
        if agent_id:
            rows = [row for row in rows if str(row["agent_id"]) == str(agent_id)]
        conflict_by_run = self._conflicts(start_date=start_date, end_date=end_date)
        status_counts: dict[str, int] = {}
        outcome_counts: dict[str, int] = {}
        attempted = 0
        operational_success = strict_success = timeouts = 0
        json_values: list[bool] = []
        evidence_values: list[bool] = []
        unsupported = total_claims = 0
        coverage_values: list[float] = []
        latency_values: list[float] = []
        total_cost = 0.0
        cost_count = 0
        for row in rows:
            payload = _dict(row["payload_json"])
            result, diagnostics = _execution_diagnostics(payload)
            status = str(payload.get("status") or row["status"] or "failed")
            outcome = str(payload.get("outcome") or row["outcome"] or status)
            status_counts[status] = status_counts.get(status, 0) + 1
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            if status == "skipped":
                continue
            attempted += 1
            if status in {"ok", "degraded"}:
                operational_success += 1
            if status == "ok":
                strict_success += 1
            failure_type = str(diagnostics.get("failure_type") or "").lower()
            if failure_type == "timeout" or "超时" in str(payload.get("error") or ""):
                timeouts += 1
            compliant = _json_compliant(payload, result, diagnostics)
            if compliant is not None:
                json_values.append(compliant)
            if status in {"ok", "degraded"}:
                invalid_ids = list(diagnostics.get("invalid_claim_ids") or [])
                evidence_values.append(bool(result) and outcome != "evidence_invalid" and not invalid_ids)
                if result:
                    invalid_count = len(invalid_ids)
                    valid_count = len(result.get("claims") or []) + len(result.get("counter_claims") or [])
                    unsupported += invalid_count
                    total_claims += invalid_count + valid_count
            coverage = _coverage_ratio(result)
            if coverage is not None:
                coverage_values.append(coverage)
            latency = _float(payload.get("latency_ms"))
            if latency is not None:
                latency_values.append(latency)
            if not bool(payload.get("cache_hit")):
                cost = _cost_usd(payload.get("cost_note"))
                if cost is not None:
                    total_cost += cost
                    cost_count += 1
        return {
            "agent_id": agent_id or "all",
            "execution_count": len(rows),
            "attempted_count": attempted,
            "status_counts": status_counts,
            "outcome_counts": outcome_counts,
            "success_rate": _ratio(operational_success, attempted),
            "strict_success_rate": _ratio(strict_success, attempted),
            "timeout_rate": _ratio(timeouts, attempted),
            "json_compliance_rate": _ratio(sum(json_values), len(json_values)),
            "evidence_reference_valid_rate": _ratio(sum(evidence_values), len(evidence_values)),
            "unsupported_claim_rate": _ratio(unsupported, total_claims),
            "coverage": {"average": _mean(coverage_values), "evaluated_count": len(coverage_values)},
            "conflict_count": sum(conflict_by_run.values()),
            "average_latency_ms": _mean(latency_values),
            "latency_observation_count": len(latency_values),
            "total_cost_usd": round(total_cost, 8) if cost_count else None,
            "cost_observation_count": cost_count,
            "cost_note_missing_count": max(0, attempted - cost_count),
        }

    def summarize_by_agent(self, *, start_date: str = "", end_date: str = "") -> list[dict[str, Any]]:
        from stock_tool.core.multi_agent_orchestrator import FIVE_AGENT_IDS

        agent_ids = sorted(
            set(FIVE_AGENT_IDS)
            | {str(row["agent_id"]) for row in self._rows(start_date=start_date, end_date=end_date)}
        )
        return [self.summarize(agent_id=agent_id, start_date=start_date, end_date=end_date) for agent_id in agent_ids]


def _lens_metrics(rows: list[ObservationEvaluation]) -> dict[str, Any]:
    settled = [row for row in rows if row.outcome_present]
    valid = [row for row in rows if row.valid_outcome]
    drawdowns = [row.max_drawdown_pct for row in valid if row.max_drawdown_pct is not None]
    return {
        "sample_count": len(rows),
        "settled_count": len(settled),
        "valid_settled_count": len(valid),
        "sample_coverage_rate": _ratio(len(settled), len(rows)),
        "valid_sample_coverage_rate": _ratio(len(valid), len(rows)),
        "avg_return_pct": _mean(row.return_pct for row in valid),
        "avg_excess_vs_hs300_pct": _mean(row.excess_vs_hs300_pct for row in valid),
        "avg_excess_vs_industry_pct": _mean(row.excess_vs_industry_pct for row in valid),
        "avg_max_drawdown_pct": _mean(row.max_drawdown_pct for row in valid),
        "worst_max_drawdown_pct": min(drawdowns) if drawdowns else None,
        "excluded_reasons": {
            reason: sum(1 for row in rows if row.exclusion_reason == reason)
            for reason in sorted({row.exclusion_reason for row in rows if row.exclusion_reason and row.exclusion_reason != "unsettled"})
        },
    }


class LensEvaluationStore:
    """Evaluate Lens outcomes by horizon, market regime and chronological holdout."""

    def __init__(self, outcome_store: ObservationOutcomeStore | Any) -> None:
        self.outcome_store = outcome_store if isinstance(outcome_store, ObservationOutcomeStore) else ObservationOutcomeStore(outcome_store)

    def evaluate(
        self,
        *,
        mode: str = "formal",
        lens_ids: Iterable[str] | None = None,
        horizons: Iterable[int] = (3, 5, 10, 20),
        start_date: str = "",
        end_date: str = "",
    ) -> list[dict[str, Any]]:
        rows = self.outcome_store.list_outcomes(mode=mode, start_date=start_date, end_date=end_date)
        requested = {str(item).strip() for item in (lens_ids or ()) if str(item).strip()}
        available = sorted({lens_id for row in rows for lens_id in row.lens_ids})
        target_lenses = sorted(requested or set(available))
        result: list[dict[str, Any]] = []
        for lens_id in target_lenses:
            lens_rows = [row for row in rows if lens_id in row.lens_ids]
            versions = sorted({row.lens_versions.get(lens_id, "") for row in lens_rows if row.lens_versions.get(lens_id, "")})
            for horizon in horizons:
                period_rows = [row for row in lens_rows if row.horizon_days == int(horizon)]
                if not period_rows:
                    continue
                valid_rows = [row for row in period_rows if row.valid_outcome]
                by_regime = {
                    regime: _lens_metrics([row for row in valid_rows if row.regime == regime])
                    for regime in sorted({row.regime for row in valid_rows})
                }
                ordered = sorted(valid_rows, key=lambda row: (row.sample_date, row.sample_id))
                split = int(len(ordered) * 0.7)
                if len(ordered) < 4 or split <= 0 or split >= len(ordered):
                    oos = {
                        "available": False,
                        "reason": "有效样本不足以进行 70/30 时间切分。",
                        "in_sample_count": 0,
                        "out_of_sample_count": 0,
                    }
                else:
                    in_sample = _lens_metrics(ordered[:split])
                    out_sample = _lens_metrics(ordered[split:])
                    in_excess = in_sample.get("avg_excess_vs_hs300_pct")
                    out_excess = out_sample.get("avg_excess_vs_hs300_pct")
                    oos = {
                        "available": True,
                        "split": "chronological_70_30",
                        "in_sample_count": split,
                        "out_of_sample_count": len(ordered) - split,
                        "in_sample": in_sample,
                        "out_of_sample": out_sample,
                        "same_excess_sign": (in_excess is not None and out_excess is not None and (in_excess == 0 or out_excess == 0 or (in_excess > 0) == (out_excess > 0))),
                        "absolute_excess_delta_pct": round(out_excess - in_excess, 6) if in_excess is not None and out_excess is not None else None,
                    }
                result.append(
                    {
                        "mode": mode,
                        "lens_id": lens_id,
                        "lens_versions": versions,
                        "horizon_days": int(horizon),
                        **_lens_metrics(period_rows),
                        "by_regime": by_regime,
                        "out_of_sample": oos,
                    }
                )
        return result

    def evaluate_all_modes(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        return {mode: self.evaluate(mode=mode, **kwargs) for mode in EVALUATION_MODES}


__all__ = [
    "AgentEvaluationStore",
    "DataQualityEvaluation",
    "DataQualityEvaluationStore",
    "EVALUATION_MODES",
    "EvaluationStoreError",
    "LensEvaluationStore",
    "ObservationEvaluation",
    "ObservationOutcomeStore",
]
