"""Durable storage and verification for complete F-layer replay."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping
from datetime import datetime, timezone
from contextlib import closing

from stock_tool.core.evidence_contracts import contract_hash
from stock_tool.core.evidence_summary import EvidenceAggregationResult
from stock_tool.core.research_discussion import validate_evidence_package


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class EvidenceRunRecord:
    """A self-contained package, Agent execution envelope and F summary."""

    run_id: str
    snapshot_id: str
    candidate_set_digest: str
    evidence_package_hash: str
    package: dict[str, Any]
    aggregation: EvidenceAggregationResult
    orchestration: Any | None
    created_at: str


@dataclass(frozen=True)
class EvidencePackageRecord:
    """A persisted F-layer input that may still need Agent execution."""

    package_id: str
    run_id: str
    snapshot_id: str
    candidate_set_digest: str
    evidence_package_hash: str
    package: dict[str, Any]
    status: str
    error: str
    created_at: str
    updated_at: str

    @property
    def lens_version(self) -> str:
        explicit_version = str(self.package.get("lens_version") or "")
        if explicit_version:
            return explicit_version
        formula_version = str(self.package.get("formula_version") or "")
        if formula_version:
            return formula_version
        metadata = self.package.get("lens_definition")
        if isinstance(metadata, Mapping):
            return str(metadata.get("lens_version") or "")
        if isinstance(metadata, list):
            versions = sorted(
                {
                    str(item.get("lens_version") or "")
                    for item in metadata
                    if isinstance(item, Mapping) and str(item.get("lens_version") or "")
                }
            )
            if versions:
                return "+".join(versions)
        return ""

    @property
    def candidate_count(self) -> int:
        records = self.package.get("candidate_records")
        return len(records) if isinstance(records, list) else 0

    @property
    def batch_range(self) -> tuple[int, int]:
        value = self.package.get("batch_range")
        if isinstance(value, (list, tuple)) and len(value) == 2:
            try:
                start, end = int(value[0]), int(value[1])
            except (TypeError, ValueError):
                pass
            else:
                if start >= 1 and end >= start:
                    return start, end
        count = self.candidate_count
        return (1, count)

    def retry_metadata(self) -> dict[str, Any]:
        """Return identity metadata suitable for a restart-time retry UI."""

        return {
            "package_id": self.package_id,
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "candidate_set_digest": self.candidate_set_digest,
            "evidence_package_hash": self.evidence_package_hash,
            "lens_version": self.lens_version,
            "batch_range": list(self.batch_range),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class EvidenceReplayError(ValueError):
    """Raised when a persisted evidence run cannot be verified."""


class SqliteEvidenceRunStore:
    """Persist strict evidence packages and their F-layer result atomically."""

    def __init__(self, database_path: str) -> None:
        self.database_path = str(database_path)
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS formal_evidence_runs (
                    run_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    candidate_set_digest TEXT NOT NULL,
                    evidence_package_hash TEXT NOT NULL,
                    summary_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_package_json TEXT NOT NULL,
                    orchestration_json TEXT NOT NULL DEFAULT '{}',
                    orchestration_hash TEXT NOT NULL DEFAULT '',
                    aggregation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_package_queue (
                    package_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL DEFAULT '',
                    snapshot_id TEXT NOT NULL,
                    candidate_set_digest TEXT NOT NULL,
                    evidence_package_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    evidence_package_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_evidence_package_queue_lookup
                ON evidence_package_queue(snapshot_id, candidate_set_digest, updated_at DESC)
                """
            )
            columns = {
                str(item[1])
                for item in connection.execute("PRAGMA table_info(formal_evidence_runs)").fetchall()
            }
            if "orchestration_hash" not in columns:
                connection.execute(
                    "ALTER TABLE formal_evidence_runs ADD COLUMN orchestration_hash TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_formal_evidence_runs_lookup
                ON formal_evidence_runs(snapshot_id, candidate_set_digest, created_at DESC)
                """
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _orchestration_payload(orchestration: Any | None) -> dict[str, Any]:
        if orchestration is None:
            return {}
        as_dict = getattr(orchestration, "as_dict", None)
        payload = as_dict() if callable(as_dict) else orchestration
        if not isinstance(payload, Mapping):
            raise EvidenceReplayError("Agent 编排结果必须可序列化")
        return dict(payload)

    @staticmethod
    def _package_id(package: Mapping[str, Any]) -> str:
        return contract_hash(
            {
                "snapshot_id": str(package.get("data_snapshot_id") or ""),
                "candidate_set_digest": str(package.get("candidate_set_digest") or ""),
                "evidence_package_hash": str(package.get("evidence_package_hash") or ""),
            }
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def save_package(
        self,
        package: Mapping[str, Any],
        *,
        run_id: str = "",
        status: str = "pending",
        error: str = "",
    ) -> EvidencePackageRecord:
        """Persist the immutable package before checking API credentials.

        This queue is intentionally separate from ``formal_evidence_runs``:
        F-layer persistence still requires an aggregation, while a missing key
        must leave a retryable package on disk.
        """

        verified_package = validate_evidence_package(dict(package))
        if status not in {"pending", "skipped", "partial", "failed", "completed"}:
            raise EvidenceReplayError(f"未知证据包队列状态：{status}")
        now = self._now()
        package_id = self._package_id(verified_package)
        package_json = _canonical_json(verified_package)
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            connection.execute(
                """
                INSERT INTO evidence_package_queue(
                    package_id, run_id, snapshot_id, candidate_set_digest,
                    evidence_package_hash, status, error, evidence_package_json,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(package_id) DO UPDATE SET
                    run_id = CASE WHEN excluded.run_id <> '' THEN excluded.run_id ELSE evidence_package_queue.run_id END,
                    status = excluded.status,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    package_id,
                    str(run_id or ""),
                    str(verified_package["data_snapshot_id"]),
                    str(verified_package["candidate_set_digest"]),
                    str(verified_package["evidence_package_hash"]),
                    status,
                    str(error or "")[:2000],
                    package_json,
                    now,
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return EvidencePackageRecord(
            package_id=package_id,
            run_id=str(run_id or ""),
            snapshot_id=str(verified_package["data_snapshot_id"]),
            candidate_set_digest=str(verified_package["candidate_set_digest"]),
            evidence_package_hash=str(verified_package["evidence_package_hash"]),
            package=verified_package,
            status=status,
            error=str(error or ""),
            created_at=now,
            updated_at=now,
        )

    def mark_package(self, package: Mapping[str, Any] | str, *, status: str, run_id: str = "", error: str = "") -> None:
        package_id = str(package if isinstance(package, str) else self._package_id(package))
        if status not in {"pending", "skipped", "partial", "failed", "completed"}:
            raise EvidenceReplayError(f"未知证据包队列状态：{status}")
        now = self._now()
        with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
            connection.execute(
                """
                UPDATE evidence_package_queue
                SET run_id = CASE WHEN ? <> '' THEN ? ELSE run_id END,
                    status = ?, error = ?, updated_at = ?
                WHERE package_id = ?
                """,
                (str(run_id or ""), str(run_id or ""), status, str(error or "")[:2000], now, package_id),
            )
            connection.commit()

    def _package_from_row(self, row: sqlite3.Row | tuple[Any, ...]) -> EvidencePackageRecord:
        values = {key: row[key] for key in row.keys()} if isinstance(row, sqlite3.Row) else {
            "package_id": row[0], "run_id": row[1], "snapshot_id": row[2],
            "candidate_set_digest": row[3], "evidence_package_hash": row[4],
            "status": row[5], "error": row[6], "evidence_package_json": row[7],
            "created_at": row[8], "updated_at": row[9],
        }
        try:
            package = validate_evidence_package(json.loads(values["evidence_package_json"]))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise EvidenceReplayError(f"证据包队列回放校验失败：{exc}") from exc
        if self._package_id(package) != str(values["package_id"]):
            raise EvidenceReplayError("证据包队列身份哈希不一致")
        return EvidencePackageRecord(
            package_id=str(values["package_id"]), run_id=str(values["run_id"] or ""),
            snapshot_id=str(values["snapshot_id"]), candidate_set_digest=str(values["candidate_set_digest"]),
            evidence_package_hash=str(values["evidence_package_hash"]), package=package,
            status=str(values["status"]), error=str(values["error"] or ""),
            created_at=str(values["created_at"]), updated_at=str(values["updated_at"]),
        )

    def latest_package(self, *, snapshot_id: str, candidate_set_digest: str) -> EvidencePackageRecord | None:
        with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT package_id, run_id, snapshot_id, candidate_set_digest, evidence_package_hash, status, error, evidence_package_json, created_at, updated_at FROM evidence_package_queue WHERE snapshot_id = ? AND candidate_set_digest = ? ORDER BY updated_at DESC, package_id DESC LIMIT 1",
                (str(snapshot_id), str(candidate_set_digest)),
            ).fetchone()
        return self._package_from_row(row) if row is not None else None

    def list_retryable(self, *, limit: int = 100) -> list[EvidencePackageRecord]:
        with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT package_id, run_id, snapshot_id, candidate_set_digest, evidence_package_hash, status, error, evidence_package_json, created_at, updated_at FROM evidence_package_queue WHERE status IN ('pending', 'skipped', 'partial', 'failed') ORDER BY updated_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._package_from_row(row) for row in rows]

    def list_retryable_batches(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """List validated retry batches with stable identity metadata."""

        records = self.list_retryable(limit=limit)
        try:
            from stock_tool.core.multi_agent_orchestrator import AGENT_DEFINITIONS

            default_prompts = {
                agent_id: definition.prompt_version
                for agent_id, definition in AGENT_DEFINITIONS.items()
            }
        except Exception:
            default_prompts = {}
        metadata: list[dict[str, Any]] = []
        for record in records:
            item = record.retry_metadata()
            item["prompt_versions"] = dict(default_prompts)
            item["model_versions"] = {}
            with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
                row = connection.execute(
                    "SELECT orchestration_json FROM formal_evidence_runs "
                    "WHERE snapshot_id = ? AND candidate_set_digest = ? "
                    "ORDER BY created_at DESC, run_id DESC LIMIT 1",
                    (record.snapshot_id, record.candidate_set_digest),
                ).fetchone()
            if row and row[0]:
                try:
                    payload = json.loads(row[0])
                    executions = payload.get("executions") if isinstance(payload, Mapping) else ()
                    for execution in executions or ():
                        if not isinstance(execution, Mapping):
                            continue
                        agent_id = str(execution.get("agent_id") or "")
                        if agent_id and execution.get("prompt_version"):
                            item["prompt_versions"][agent_id] = str(execution["prompt_version"])
                        if agent_id and execution.get("model_version"):
                            item["model_versions"][agent_id] = str(execution["model_version"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            metadata.append(item)
        return metadata

    def save(
        self,
        package: Mapping[str, Any],
        aggregation: EvidenceAggregationResult,
        *,
        orchestration: Any | None = None,
    ) -> EvidenceRunRecord:
        verified_package = validate_evidence_package(dict(package))
        if not isinstance(aggregation, EvidenceAggregationResult):
            raise EvidenceReplayError("F 层结果类型不正确")
        if not aggregation.candidate_set_preserved:
            raise EvidenceReplayError("候选集合未保持不变，拒绝保存 F 层回放")
        if aggregation.snapshot_id != str(verified_package["data_snapshot_id"]):
            raise EvidenceReplayError("F 层 snapshot_id 与证据包不一致")
        if aggregation.candidate_set_digest != str(verified_package["candidate_set_digest"]):
            raise EvidenceReplayError("F 层 candidate_set_digest 与证据包不一致")
        if not aggregation.run_id:
            raise EvidenceReplayError("F 层 run_id 不能为空")
        orchestration_payload = self._orchestration_payload(orchestration)
        if orchestration_payload:
            if str(orchestration_payload.get("run_id") or "") != aggregation.run_id:
                raise EvidenceReplayError("编排结果与 F 层 run_id 不一致")
            if orchestration_payload.get("candidate_set_preserved") is False:
                raise EvidenceReplayError("编排结果报告候选集合发生变化")
        aggregation_payload = aggregation.as_dict()
        package_json = _canonical_json(verified_package)
        orchestration_json = _canonical_json(orchestration_payload)
        orchestration_hash = contract_hash(orchestration_payload) if orchestration_payload else ""
        aggregation_json = _canonical_json(aggregation_payload)
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            connection.execute(
                """
                INSERT OR REPLACE INTO formal_evidence_runs(
                    run_id, snapshot_id, candidate_set_digest, evidence_package_hash,
                    summary_hash, status, evidence_package_json, orchestration_json,
                    orchestration_hash, aggregation_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    aggregation.run_id,
                    aggregation.snapshot_id,
                    aggregation.candidate_set_digest,
                    str(verified_package["evidence_package_hash"]),
                    str(aggregation_payload["summary_hash"]),
                    aggregation.status,
                    package_json,
                    orchestration_json,
                    orchestration_hash,
                    aggregation_json,
                    aggregation.created_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return EvidenceRunRecord(
            run_id=aggregation.run_id,
            snapshot_id=aggregation.snapshot_id,
            candidate_set_digest=aggregation.candidate_set_digest,
            evidence_package_hash=str(verified_package["evidence_package_hash"]),
            package=verified_package,
            aggregation=aggregation,
            orchestration=orchestration,
            created_at=aggregation.created_at,
        )

    def _from_row(self, row: sqlite3.Row | tuple[Any, ...]) -> EvidenceRunRecord:
        values = {key: row[key] for key in row.keys()} if isinstance(row, sqlite3.Row) else {
            "run_id": row[0],
            "snapshot_id": row[1],
            "candidate_set_digest": row[2],
            "evidence_package_hash": row[3],
            "summary_hash": row[4],
            "status": row[5],
            "evidence_package_json": row[6],
            "orchestration_json": row[7],
            "orchestration_hash": row[8],
            "aggregation_json": row[9],
            "created_at": row[10],
        }
        try:
            package = validate_evidence_package(json.loads(values["evidence_package_json"]))
            aggregation = EvidenceAggregationResult.from_dict(json.loads(values["aggregation_json"]))
            if str(package["evidence_package_hash"]) != str(values["evidence_package_hash"]):
                raise EvidenceReplayError("持久化证据包哈希不一致")
            if aggregation.summary_hash != str(values["summary_hash"]):
                raise EvidenceReplayError("持久化 F 层摘要哈希不一致")
            if aggregation.run_id != str(values["run_id"]):
                raise EvidenceReplayError("持久化 F 层 run_id 不一致")
            if aggregation.snapshot_id != str(values["snapshot_id"]):
                raise EvidenceReplayError("持久化 F 层 snapshot_id 不一致")
            if aggregation.candidate_set_digest != str(values["candidate_set_digest"]):
                raise EvidenceReplayError("持久化 F 层候选集哈希不一致")
            orchestration_payload = json.loads(values["orchestration_json"] or "{}")
            orchestration = None
            if orchestration_payload:
                stored_orchestration_hash = str(values.get("orchestration_hash") or "")
                if stored_orchestration_hash and contract_hash(orchestration_payload) != stored_orchestration_hash:
                    raise EvidenceReplayError("持久化 Agent 编排哈希不一致")
                from stock_tool.core.multi_agent_orchestrator import AgentOrchestrationResult

                orchestration = AgentOrchestrationResult.from_dict(orchestration_payload)
                if orchestration.run_id != aggregation.run_id:
                    raise EvidenceReplayError("持久化 Agent 编排 run_id 不一致")
                if orchestration.snapshot_id != aggregation.snapshot_id:
                    raise EvidenceReplayError("持久化 Agent 编排 snapshot_id 不一致")
                if orchestration.candidate_set_digest != aggregation.candidate_set_digest:
                    raise EvidenceReplayError("持久化 Agent 编排候选集哈希不一致")
                if tuple(orchestration.candidate_records) != tuple(package["candidate_records"]):
                    raise EvidenceReplayError("持久化 Agent 编排候选记录不一致")
                expected_snapshot_id = str(package["data_snapshot_id"])
                expected_candidate_digest = str(package["candidate_set_digest"])
                expected_package_hash = str(package["evidence_package_hash"])
                for execution in orchestration.executions:
                    if execution.run_id != orchestration.run_id:
                        raise EvidenceReplayError("持久化 Agent execution run_id 不一致")
                    if execution.snapshot_id != expected_snapshot_id:
                        raise EvidenceReplayError("持久化 Agent execution snapshot_id 不一致")
                    if execution.candidate_set_digest != expected_candidate_digest:
                        raise EvidenceReplayError("持久化 Agent execution 候选集哈希不一致")
                    if execution.evidence_package_hash != expected_package_hash:
                        raise EvidenceReplayError("持久化 Agent execution 证据包哈希不一致")
                    if execution.result is not None and execution.result.run_id != orchestration.run_id:
                        raise EvidenceReplayError("持久化 AgentResult run_id 不一致")
        except EvidenceReplayError:
            raise
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise EvidenceReplayError(f"持久化证据回放校验失败：{exc}") from exc
        return EvidenceRunRecord(
            run_id=str(values["run_id"]),
            snapshot_id=str(values["snapshot_id"]),
            candidate_set_digest=str(values["candidate_set_digest"]),
            evidence_package_hash=str(values["evidence_package_hash"]),
            package=package,
            aggregation=aggregation,
            orchestration=orchestration,
            created_at=str(values["created_at"]),
        )

    def get(self, run_id: str) -> EvidenceRunRecord | None:
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT run_id, snapshot_id, candidate_set_digest, evidence_package_hash, summary_hash, status, evidence_package_json, orchestration_json, orchestration_hash, aggregation_json, created_at FROM formal_evidence_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
        finally:
            connection.close()
        return self._from_row(row) if row is not None else None

    def latest(self, *, snapshot_id: str, candidate_set_digest: str) -> EvidenceRunRecord | None:
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT run_id, snapshot_id, candidate_set_digest, evidence_package_hash, summary_hash, status, evidence_package_json, orchestration_json, orchestration_hash, aggregation_json, created_at FROM formal_evidence_runs WHERE snapshot_id = ? AND candidate_set_digest = ? ORDER BY created_at DESC, run_id DESC LIMIT 1",
                (str(snapshot_id), str(candidate_set_digest)),
            ).fetchone()
        finally:
            connection.close()
        return self._from_row(row) if row is not None else None


__all__ = [
    "EvidenceReplayError",
    "EvidencePackageRecord",
    "EvidenceRunRecord",
    "SqliteEvidenceRunStore",
]
