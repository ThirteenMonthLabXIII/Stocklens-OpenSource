"""Immutable, point-in-time data snapshot contracts.

The V1 factor table is intentionally a current materialized cache.  This
module provides the durable contract that formal screening and research runs
can use when they need reproducible, point-in-time evidence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from stock_tool.core.v1_database import V1Database


SnapshotQuality = Literal["verified", "partial", "unverified", "blocked"]
SNAPSHOT_QUALITY_STATUSES = frozenset({"verified", "partial", "unverified", "blocked"})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("时间字段不能为空")
    if len(text) == 10:
        text += "T00:00:00"
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # Legacy date-only rows are interpreted as UTC rather than the host's
        # local zone, keeping replay hashes and cutoff comparisons stable.
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def content_hash(value: Any) -> str:
    """Hash a normalized value for provenance and amendment detection."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def is_manifest_fresh(
    manifest: "DataSnapshotManifest",
    max_age: timedelta,
    *,
    now: datetime | None = None,
) -> bool:
    """Check freshness from manifest creation time, not mutable row timestamps."""

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    created = _parse_time(manifest.created_at).replace(tzinfo=timezone.utc)
    age = current - created
    return timedelta(0) <= age <= max_age


def _manifest_identity(
    *,
    asof_at: str,
    availability_cutoff: str,
    data_source: str,
    universe_id: str,
    universe_version: str,
    dataset_version: str,
    source_policy_version: str,
    facts_digest: str,
    source_versions: tuple[tuple[str, str], ...],
    quality_status: SnapshotQuality,
    quality_summary_json: str,
) -> dict[str, Any]:
    return {
        "asof_at": asof_at,
        "availability_cutoff": availability_cutoff,
        "data_source": data_source,
        "universe_id": universe_id,
        "universe_version": universe_version,
        "dataset_version": dataset_version,
        "source_policy_version": source_policy_version,
        "facts_digest": facts_digest,
        "source_versions": source_versions,
        "quality_status": quality_status,
        "quality_summary_json": quality_summary_json,
    }


@dataclass(frozen=True)
class DataSnapshotManifest:
    """Identity and quality metadata for one immutable data snapshot."""

    snapshot_id: str
    asof_at: str
    availability_cutoff: str
    data_source: str
    universe_id: str
    dataset_version: str
    facts_digest: str
    universe_version: str = ""
    source_policy_version: str = ""
    source_versions: tuple[tuple[str, str], ...] = ()
    quality_status: SnapshotQuality = "verified"
    quality_summary_json: str = "{}"
    created_at: str = ""

    @classmethod
    def create(
        cls,
        *,
        asof_at: str,
        availability_cutoff: str,
        data_source: str,
        universe_id: str,
        dataset_version: str,
        facts_digest: str,
        universe_version: str = "",
        source_policy_version: str = "",
        source_versions: dict[str, str] | None = None,
        quality_status: SnapshotQuality = "verified",
        quality_summary: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> "DataSnapshotManifest":
        normalized_sources = tuple(sorted((str(k), str(v)) for k, v in (source_versions or {}).items()))
        quality_json = _canonical_json(quality_summary or {})
        identity = _manifest_identity(
            asof_at=asof_at,
            availability_cutoff=availability_cutoff,
            data_source=data_source,
            universe_id=universe_id,
            universe_version=universe_version,
            dataset_version=dataset_version,
            source_policy_version=source_policy_version,
            facts_digest=facts_digest,
            source_versions=normalized_sources,
            quality_status=quality_status,
            quality_summary_json=quality_json,
        )
        snapshot_id = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
        return cls(
            snapshot_id=snapshot_id,
            asof_at=asof_at,
            availability_cutoff=availability_cutoff,
            data_source=data_source,
            universe_id=universe_id,
            universe_version=universe_version,
            dataset_version=dataset_version,
            source_policy_version=source_policy_version,
            facts_digest=facts_digest,
            source_versions=normalized_sources,
            quality_status=quality_status,
            quality_summary_json=quality_json,
            created_at=created_at or _utc_now_iso(),
        )

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            raise ValueError("snapshot_id 不能为空")
        _parse_time(self.asof_at)
        _parse_time(self.availability_cutoff)
        if not self.created_at:
            raise ValueError("created_at 不能为空")
        _parse_time(self.created_at)
        if not self.data_source or not self.universe_id or not self.dataset_version or not self.facts_digest:
            raise ValueError("data_source、universe_id、dataset_version、facts_digest 不能为空")
        if self.quality_status not in SNAPSHOT_QUALITY_STATUSES:
            raise ValueError(f"不支持的数据质量状态：{self.quality_status}")
        json.loads(self.quality_summary_json or "{}")
        expected_id = hashlib.sha256(
            _canonical_json(
                _manifest_identity(
                    asof_at=self.asof_at,
                    availability_cutoff=self.availability_cutoff,
                    data_source=self.data_source,
                    universe_id=self.universe_id,
                    universe_version=self.universe_version,
                    dataset_version=self.dataset_version,
                    source_policy_version=self.source_policy_version,
                    facts_digest=self.facts_digest,
                    source_versions=tuple(sorted(self.source_versions)),
                    quality_status=self.quality_status,
                    quality_summary_json=self.quality_summary_json or "{}",
                )
            ).encode("utf-8")
        ).hexdigest()
        if self.snapshot_id != expected_id:
            raise ValueError("snapshot_id 与 manifest 内容不匹配")


@dataclass(frozen=True)
class DataSnapshotFact:
    """One field observation visible in exactly one snapshot."""

    snapshot_id: str
    code: str
    field_name: str
    value: Any
    effective_at: str
    available_at: str
    source: str
    source_version: str
    fetched_at: str
    source_route: str = "primary"
    quality_status: SnapshotQuality = "verified"
    revision_id: str = ""
    raw_hash: str = ""

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.code or not self.field_name or not self.source:
            raise ValueError("snapshot_id、code、field_name、source 不能为空")
        _parse_time(self.effective_at)
        _parse_time(self.available_at)
        _parse_time(self.fetched_at)
        if self.quality_status not in SNAPSHOT_QUALITY_STATUSES:
            raise ValueError(f"不支持的数据质量状态：{self.quality_status}")
        if self.raw_hash and self.raw_hash != content_hash(self.value):
            raise ValueError("raw_hash 与事实值不匹配")

    @property
    def value_json(self) -> str:
        return _canonical_json(self.value)

    @property
    def resolved_raw_hash(self) -> str:
        return self.raw_hash or content_hash(self.value)


def fact_set_digest(facts: list[DataSnapshotFact | dict[str, Any]]) -> str:
    """Hash fact content without snapshot_id, avoiding a circular identity."""

    rows: list[dict[str, Any]] = []
    for fact in facts:
        if isinstance(fact, DataSnapshotFact):
            row = {
                "code": fact.code,
                "field_name": fact.field_name,
                "value_json": fact.value_json,
                "effective_at": fact.effective_at,
                "available_at": fact.available_at,
                "source": fact.source,
                "source_version": fact.source_version,
                "fetched_at": fact.fetched_at,
                "source_route": fact.source_route,
                "quality_status": fact.quality_status,
                "revision_id": fact.revision_id,
                "raw_hash": fact.resolved_raw_hash,
            }
        else:
            value = fact.get("value")
            row = {
                "code": str(fact.get("code", "")),
                "field_name": str(fact.get("field_name", "")),
                "value_json": _canonical_json(value),
                "effective_at": str(fact.get("effective_at", "")),
                "available_at": str(fact.get("available_at", "")),
                "source": str(fact.get("source", "")),
                "source_version": str(fact.get("source_version", "")),
                "fetched_at": str(fact.get("fetched_at", "")),
                "source_route": str(fact.get("source_route", "primary")),
                "quality_status": str(fact.get("quality_status", "verified")),
                "revision_id": str(fact.get("revision_id", "")),
                "raw_hash": str(fact.get("raw_hash") or content_hash(value)),
            }
        rows.append(row)
    rows.sort(key=lambda row: (row["code"], row["field_name"]))
    return content_hash(rows)


@dataclass(frozen=True)
class RefreshEvent:
    """The immutable identity and final state of one refresh attempt."""

    refresh_event_id: str
    asof_at: str
    availability_cutoff: str
    data_source: str
    universe_id: str
    universe_version: str
    dataset_version: str
    source_policy_version: str
    source_versions: tuple[tuple[str, str], ...]
    started_at: str
    finished_at: str
    status: str
    source_route: str
    used_cache: bool
    used_failover: bool
    coverage_json: str
    error_message: str
    snapshot_id: str | None
    created_at: str

    @property
    def coverage(self) -> dict[str, Any]:
        value = json.loads(self.coverage_json or "{}")
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class SnapshotRef:
    """Reference returned after a snapshot is created or resolved."""

    snapshot_id: str
    reused: bool = False


@dataclass(frozen=True)
class ImmutableSnapshot:
    """A read-only manifest plus its append-only facts."""

    manifest: DataSnapshotManifest
    facts: tuple[DataSnapshotFact, ...]

    @property
    def snapshot_id(self) -> str:
        return self.manifest.snapshot_id


@dataclass(frozen=True)
class SnapshotQualityAssessment:
    """Quality gate result for a persisted immutable snapshot."""

    status: str
    snapshot_id: str
    required_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    invalid_available_facts: int = 0
    unverified_facts: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "snapshot_id": self.snapshot_id,
            "required_fields": list(self.required_fields),
            "missing_fields": list(self.missing_fields),
            "invalid_available_facts": self.invalid_available_facts,
            "unverified_facts": self.unverified_facts,
            "note": self.note,
        }


class DataSnapshotStore:
    """Persistence adapter for append-only snapshot manifests and facts."""

    def __init__(self, database: V1Database) -> None:
        self.database = database

    def create_snapshot(
        self,
        manifest: DataSnapshotManifest,
        facts: list[DataSnapshotFact],
    ) -> None:
        with self.database.connect() as conn:
            self.insert_snapshot(conn, manifest, facts)

    def insert_snapshot(
        self,
        conn: sqlite3.Connection,
        manifest: DataSnapshotManifest,
        facts: list[DataSnapshotFact],
    ) -> None:
        """Insert one snapshot into a caller-owned transaction."""

        if any(fact.snapshot_id != manifest.snapshot_id for fact in facts):
            raise ValueError("事实记录的 snapshot_id 必须与 manifest 一致")
        keys = [(fact.code, fact.field_name) for fact in facts]
        if len(keys) != len(set(keys)):
            raise ValueError("同一个快照内不能重复写入同一股票字段")
        if manifest.facts_digest != fact_set_digest(facts):
            raise ValueError("facts_digest 与事实集合不匹配")
        cutoff = _parse_time(manifest.availability_cutoff)
        for fact in facts:
            if _parse_time(fact.available_at) > cutoff:
                raise ValueError(
                    f"事实 {fact.code}.{fact.field_name} 的 available_at 晚于快照截止时间"
                )

        existing = conn.execute(
            "SELECT * FROM data_snapshot_manifests WHERE snapshot_id = ?",
            (manifest.snapshot_id,),
        ).fetchone()
        if existing is not None:
            expected_manifest = {
                "asof_at": manifest.asof_at,
                "availability_cutoff": manifest.availability_cutoff,
                "data_source": manifest.data_source,
                "universe_id": manifest.universe_id,
                "universe_version": manifest.universe_version,
                "dataset_version": manifest.dataset_version,
                "source_policy_version": manifest.source_policy_version,
                "source_versions_json": _canonical_json(dict(manifest.source_versions)),
                "facts_digest": manifest.facts_digest,
                "quality_status": manifest.quality_status,
                "quality_summary_json": manifest.quality_summary_json,
            }
            for column, expected in expected_manifest.items():
                if str(existing[column]) != str(expected):
                    raise ValueError("snapshot_id 内容哈希冲突：manifest 内容不一致")
            existing_facts = conn.execute(
                """
                SELECT code, field_name, value_json, effective_at, available_at,
                       source, source_version, source_route, fetched_at,
                       quality_status, revision_id, raw_hash
                FROM data_snapshot_facts
                WHERE snapshot_id = ?
                ORDER BY code, field_name
                """,
                (manifest.snapshot_id,),
            ).fetchall()
            expected_facts = [
                (
                    fact.code,
                    fact.field_name,
                    fact.value_json,
                    fact.effective_at,
                    fact.available_at,
                    fact.source,
                    fact.source_version,
                    fact.source_route,
                    fact.fetched_at,
                    fact.quality_status,
                    fact.revision_id,
                    fact.resolved_raw_hash,
                )
                for fact in sorted(facts, key=lambda item: (item.code, item.field_name))
            ]
            if [tuple(row) for row in existing_facts] != expected_facts:
                raise ValueError("snapshot_id 内容哈希冲突：事实内容不一致")
            return

        conn.execute(
            """
            INSERT INTO data_snapshot_manifests(
                snapshot_id, asof_at, availability_cutoff, data_source,
                universe_id, universe_version, dataset_version,
                source_policy_version, source_versions_json,
                facts_digest, quality_status, quality_summary_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manifest.snapshot_id,
                manifest.asof_at,
                manifest.availability_cutoff,
                manifest.data_source,
                manifest.universe_id,
                manifest.universe_version,
                manifest.dataset_version,
                manifest.source_policy_version,
                _canonical_json(dict(manifest.source_versions)),
                manifest.facts_digest,
                manifest.quality_status,
                manifest.quality_summary_json,
                manifest.created_at,
            ),
        )
        conn.executemany(
            """
            INSERT INTO data_snapshot_facts(
                snapshot_id, code, field_name, value_json, effective_at,
                available_at, source, source_version, fetched_at,
                source_route, quality_status, revision_id, raw_hash
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    fact.snapshot_id,
                    fact.code,
                    fact.field_name,
                    fact.value_json,
                    fact.effective_at,
                    fact.available_at,
                    fact.source,
                    fact.source_version,
                    fact.fetched_at,
                    fact.source_route,
                    fact.quality_status,
                    fact.revision_id,
                    fact.resolved_raw_hash,
                )
                for fact in facts
            ],
        )

    def latest_manifest(self) -> DataSnapshotManifest | None:
        with self.database.connect() as conn:
            rows = conn.execute("SELECT * FROM data_snapshot_manifests").fetchall()
        if not rows:
            return None
        row = max(rows, key=lambda item: (_parse_time(item["created_at"]), item["snapshot_id"]))
        return self._manifest_from_row(row)

    def get_manifest(self, snapshot_id: str) -> DataSnapshotManifest | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM data_snapshot_manifests WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return self._manifest_from_row(row)

    @staticmethod
    def _manifest_from_row(row: sqlite3.Row) -> DataSnapshotManifest:
        return DataSnapshotManifest(
            snapshot_id=row["snapshot_id"],
            asof_at=row["asof_at"],
            availability_cutoff=row["availability_cutoff"],
            data_source=row["data_source"],
            universe_id=row["universe_id"],
            universe_version=row["universe_version"] if "universe_version" in row.keys() else "",
            dataset_version=row["dataset_version"],
            source_policy_version=row["source_policy_version"] if "source_policy_version" in row.keys() else "",
            facts_digest=row["facts_digest"],
            source_versions=tuple(sorted(json.loads(row["source_versions_json"] or "{}").items())),
            quality_status=row["quality_status"],
            quality_summary_json=row["quality_summary_json"],
            created_at=row["created_at"],
        )

    def fact_values_by_code(self, snapshot_id: str) -> dict[str, dict[str, Any]]:
        """Return immutable fact values in a shape suitable for typed adapters."""

        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT code, field_name, value_json
                FROM data_snapshot_facts
                WHERE snapshot_id = ?
                ORDER BY code, field_name
                """,
                (snapshot_id,),
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            result.setdefault(row["code"], {})[row["field_name"]] = json.loads(row["value_json"])
        return result

    def fact_lineage_by_code(self, snapshot_id: str) -> dict[str, dict[str, dict[str, Any]]]:
        """Return field-level source and cutoff metadata for candidate audits."""

        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT code, field_name, source, source_version, effective_at,
                       available_at, source_route, quality_status, raw_hash
                FROM data_snapshot_facts
                WHERE snapshot_id = ?
                ORDER BY code, field_name
                """,
                (snapshot_id,),
            ).fetchall()
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(str(row["code"]), {})[str(row["field_name"])] = {
                "source": str(row["source"] or ""),
                "source_version": str(row["source_version"] or ""),
                "effective_at": str(row["effective_at"] or ""),
                "available_at": str(row["available_at"] or ""),
                "source_route": str(row["source_route"] or "primary"),
                "quality_status": str(row["quality_status"] or "unverified"),
                "raw_hash": str(row["raw_hash"] or ""),
            }
        return result

    def list_facts(self, snapshot_id: str) -> list[DataSnapshotFact]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM data_snapshot_facts
                WHERE snapshot_id = ?
                ORDER BY code, field_name
                """,
                (snapshot_id,),
            ).fetchall()
        return [
            DataSnapshotFact(
                snapshot_id=row["snapshot_id"],
                code=row["code"],
                field_name=row["field_name"],
                value=json.loads(row["value_json"]),
                effective_at=row["effective_at"],
                available_at=row["available_at"],
                source=row["source"],
                source_version=row["source_version"],
                fetched_at=row["fetched_at"],
                source_route=row["source_route"] if "source_route" in row.keys() else "primary",
                quality_status=row["quality_status"],
                revision_id=row["revision_id"],
                raw_hash=row["raw_hash"],
            )
            for row in rows
        ]

    def get_snapshot(self, snapshot_id: str) -> ImmutableSnapshot | None:
        """Load an immutable snapshot and all facts, or return ``None``."""

        manifest = self.get_manifest(snapshot_id)
        if manifest is None:
            return None
        return ImmutableSnapshot(manifest=manifest, facts=tuple(self.list_facts(snapshot_id)))

    def record_refresh_attempt(
        self,
        *,
        asof_at: str,
        availability_cutoff: str,
        data_source: str,
        universe_id: str,
        source_policy_version: str,
        dataset_version: str = "",
        universe_version: str = "",
        source_versions: dict[str, str] | None = None,
        started_at: str | None = None,
        source_route: str = "primary",
        used_cache: bool = False,
        used_failover: bool = False,
    ) -> RefreshEvent:
        """Create a uniquely identifiable refresh attempt before fetching data."""

        for value in (asof_at, availability_cutoff, started_at or _utc_now_iso()):
            _parse_time(value)
        if not data_source or not universe_id or not source_policy_version:
            raise ValueError("data_source、universe_id、source_policy_version 不能为空")
        if not source_route:
            raise ValueError("source_route 不能为空")
        event = RefreshEvent(
            refresh_event_id=uuid.uuid4().hex,
            asof_at=asof_at,
            availability_cutoff=availability_cutoff,
            data_source=data_source,
            universe_id=universe_id,
            universe_version=universe_version,
            dataset_version=dataset_version or source_policy_version,
            source_policy_version=source_policy_version,
            source_versions=tuple(sorted((str(k), str(v)) for k, v in (source_versions or {}).items())),
            started_at=started_at or _utc_now_iso(),
            finished_at="",
            status="started",
            source_route=source_route,
            used_cache=bool(used_cache),
            used_failover=bool(used_failover),
            coverage_json="{}",
            error_message="",
            snapshot_id=None,
            created_at=started_at or _utc_now_iso(),
        )
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO snapshot_ingestion_events(
                    refresh_event_id, asof_at, availability_cutoff, data_source,
                    universe_id, universe_version, dataset_version,
                    source_policy_version, source_versions_json,
                    started_at, finished_at, status,
                    source_route, used_cache, used_failover, coverage_json,
                    error_message, snapshot_id, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.refresh_event_id,
                    event.asof_at,
                    event.availability_cutoff,
                    event.data_source,
                    event.universe_id,
                    event.universe_version,
                    event.dataset_version,
                    event.source_policy_version,
                    _canonical_json(dict(event.source_versions)),
                    event.started_at,
                    event.finished_at,
                    event.status,
                    event.source_route,
                    int(event.used_cache),
                    int(event.used_failover),
                    event.coverage_json,
                    event.error_message,
                    event.snapshot_id,
                    event.created_at,
                ),
            )
        return event

    def update_refresh_attempt_context(
        self,
        refresh_event_id: str,
        *,
        asof_at: str | None = None,
        availability_cutoff: str | None = None,
        universe_version: str | None = None,
        dataset_version: str | None = None,
        source_versions: dict[str, str] | None = None,
    ) -> RefreshEvent:
        """Fill request context discovered before a refresh is finalized."""

        event = self.get_refresh_event(refresh_event_id)
        if event is None:
            raise KeyError(f"refresh_event_id 不存在：{refresh_event_id}")
        if event.status != "started":
            raise ValueError("只有 started 刷新事件可以补充请求上下文")
        if asof_at is not None:
            _parse_time(asof_at)
        if availability_cutoff is not None:
            _parse_time(availability_cutoff)
        resolved_sources = (
            tuple(sorted((str(k), str(v)) for k, v in source_versions.items()))
            if source_versions is not None
            else event.source_versions
        )
        with self.database.connect() as conn:
            conn.execute(
                """
                UPDATE snapshot_ingestion_events
                SET asof_at = COALESCE(?, asof_at),
                    availability_cutoff = COALESCE(?, availability_cutoff),
                    universe_version = COALESCE(?, universe_version),
                    dataset_version = COALESCE(?, dataset_version),
                    source_versions_json = ?
                WHERE refresh_event_id = ? AND status = 'started'
                """,
                (
                    asof_at,
                    availability_cutoff,
                    universe_version,
                    dataset_version,
                    _canonical_json(dict(resolved_sources)),
                    refresh_event_id,
                ),
            )
        updated = self.get_refresh_event(refresh_event_id)
        if updated is None:
            raise RuntimeError("刷新事件更新后无法读取")
        return updated

    def finalize_refresh(
        self,
        refresh_event_id: str,
        facts: list[DataSnapshotFact | dict[str, Any]] | None = None,
        *,
        status: str = "success",
        quality_status: SnapshotQuality | None = None,
        quality_summary: dict[str, Any] | None = None,
        coverage: dict[str, Any] | None = None,
        source_versions: dict[str, str] | None = None,
        source_route: str | None = None,
        used_cache: bool | None = None,
        used_failover: bool | None = None,
        error_message: str = "",
        finished_at: str | None = None,
        after_snapshot: Callable[[sqlite3.Connection], None] | None = None,
    ) -> SnapshotRef | None:
        """Finalize a refresh and atomically connect it to a snapshot.

        Failed attempts are retained without producing a snapshot.  Successful
        retries with identical normalized facts reuse the existing snapshot ID.
        """

        event = self.get_refresh_event(refresh_event_id)
        if event is None:
            raise KeyError(f"refresh_event_id 不存在：{refresh_event_id}")
        if event.status != "started":
            if event.status == "failed":
                return None
            if event.snapshot_id:
                return SnapshotRef(event.snapshot_id, reused=True)
            raise ValueError(f"刷新事件已完成但没有 snapshot_id：{refresh_event_id}")
        final_time = finished_at or _utc_now_iso()
        _parse_time(final_time)
        if status not in {"success", "partial", "failed"}:
            raise ValueError(f"不支持的刷新结束状态：{status}")
        route = source_route or event.source_route
        resolved_cache = event.used_cache if used_cache is None else bool(used_cache)
        resolved_failover = event.used_failover if used_failover is None else bool(used_failover)
        resolved_sources = tuple(
            sorted(
                (str(k), str(v))
                for k, v in (
                    source_versions
                    if source_versions is not None
                    else dict(event.source_versions)
                ).items()
            )
        )

        if status == "failed":
            with self.database.connect() as conn:
                conn.execute(
                    """
                    UPDATE snapshot_ingestion_events
                    SET finished_at = ?, status = 'failed', source_route = ?,
                        used_cache = ?, used_failover = ?, coverage_json = ?,
                        error_message = ?, snapshot_id = NULL
                    WHERE refresh_event_id = ? AND status = 'started'
                    """,
                    (
                        final_time,
                        route,
                        int(resolved_cache),
                        int(resolved_failover),
                        _canonical_json(coverage or {}),
                        str(error_message or "刷新失败"),
                        refresh_event_id,
                    ),
                )
            return None

        payloads: list[dict[str, Any]] = []
        for fact in facts or []:
            if isinstance(fact, DataSnapshotFact):
                payloads.append(
                    {
                        "code": fact.code,
                        "field_name": fact.field_name,
                        "value": fact.value,
                        "effective_at": fact.effective_at,
                        "available_at": fact.available_at,
                        "source": fact.source,
                        "source_version": fact.source_version,
                        "fetched_at": fact.fetched_at,
                        "source_route": fact.source_route,
                        "quality_status": fact.quality_status,
                        "revision_id": fact.revision_id,
                        "raw_hash": fact.resolved_raw_hash,
                    }
                )
            else:
                payloads.append(dict(fact))
        resolved_quality: SnapshotQuality = quality_status or ("verified" if status == "success" else "partial")
        if resolved_quality not in SNAPSHOT_QUALITY_STATUSES:
            raise ValueError(f"不支持的数据快照质量状态：{resolved_quality}")
        for payload in payloads:
            payload.setdefault("source_route", route)
            payload.setdefault("quality_status", resolved_quality)
        manifest = DataSnapshotManifest.create(
            asof_at=event.asof_at,
            availability_cutoff=event.availability_cutoff,
            data_source=event.data_source,
            universe_id=event.universe_id,
            universe_version=event.universe_version,
            dataset_version=event.dataset_version,
            source_policy_version=event.source_policy_version,
            facts_digest=fact_set_digest(payloads),
            source_versions=dict(resolved_sources),
            quality_status=resolved_quality,
            quality_summary=quality_summary or {},
            created_at=final_time,
        )
        normalized_facts = [
            DataSnapshotFact(snapshot_id=manifest.snapshot_id, **payload)
            for payload in payloads
        ]
        try:
            with self.database.connect() as conn:
                existing = conn.execute(
                    "SELECT 1 FROM data_snapshot_manifests WHERE snapshot_id = ?",
                    (manifest.snapshot_id,),
                ).fetchone()
                self.insert_snapshot(conn, manifest, normalized_facts)
                if after_snapshot is not None:
                    after_snapshot(conn)
                conn.execute(
                    """
                    UPDATE snapshot_ingestion_events
                    SET finished_at = ?, status = ?, source_route = ?,
                        used_cache = ?, used_failover = ?, coverage_json = ?,
                        error_message = ?, snapshot_id = ?
                    WHERE refresh_event_id = ? AND status = 'started'
                    """,
                    (
                        final_time,
                        status,
                        route,
                        int(resolved_cache),
                        int(resolved_failover),
                        _canonical_json(coverage or {}),
                        str(error_message or ""),
                        manifest.snapshot_id,
                        refresh_event_id,
                    ),
                )
            return SnapshotRef(manifest.snapshot_id, reused=existing is not None)
        except Exception as exc:
            self._mark_refresh_failed(refresh_event_id, final_time, str(exc))
            raise

    def _mark_refresh_failed(self, refresh_event_id: str, finished_at: str, message: str) -> None:
        with self.database.connect() as conn:
            conn.execute(
                """
                UPDATE snapshot_ingestion_events
                SET finished_at = ?, status = 'failed', error_message = ?
                WHERE refresh_event_id = ? AND status = 'started'
                """,
                (finished_at, message[:2000], refresh_event_id),
            )

    def get_refresh_event(self, refresh_event_id: str) -> RefreshEvent | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM snapshot_ingestion_events WHERE refresh_event_id = ?",
                (refresh_event_id,),
            ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def refresh_event_id_for_snapshot(self, snapshot_id: str) -> str:
        """Return the immutable refresh event that produced a snapshot."""
        with self.database.connect() as conn:
            row = conn.execute(
                """
                SELECT refresh_event_id
                FROM snapshot_ingestion_events
                WHERE snapshot_id = ?
                ORDER BY finished_at DESC, created_at DESC
                LIMIT 1
                """,
                (str(snapshot_id),),
            ).fetchone()
        return str(row["refresh_event_id"]) if row is not None else ""

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> RefreshEvent:
        return RefreshEvent(
            refresh_event_id=row["refresh_event_id"],
            asof_at=row["asof_at"],
            availability_cutoff=row["availability_cutoff"],
            data_source=row["data_source"],
            universe_id=row["universe_id"],
            universe_version=row["universe_version"],
            dataset_version=row["dataset_version"],
            source_policy_version=row["source_policy_version"],
            source_versions=tuple(sorted(json.loads(row["source_versions_json"] or "{}").items())),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            source_route=row["source_route"],
            used_cache=bool(row["used_cache"]),
            used_failover=bool(row["used_failover"]),
            coverage_json=row["coverage_json"],
            error_message=row["error_message"],
            snapshot_id=row["snapshot_id"],
            created_at=row["created_at"],
        )

    def resolve_snapshot_for_cutoff(
        self,
        cutoff: str,
        universe_id: str,
        *,
        require_verified: bool = True,
    ) -> SnapshotRef | None:
        """Resolve the newest eligible snapshot without consulting mutable cache rows."""

        cutoff_time = _parse_time(cutoff)
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM data_snapshot_manifests WHERE universe_id = ?",
                (universe_id,),
            ).fetchall()
        eligible = [
            row
            for row in rows
            if _parse_time(row["availability_cutoff"]) <= cutoff_time
            and (not require_verified or row["quality_status"] == "verified")
        ]
        if not eligible:
            return None
        row = max(
            eligible,
            key=lambda item: (
                _parse_time(item["availability_cutoff"]),
                _parse_time(item["created_at"]),
                item["snapshot_id"],
            ),
        )
        return SnapshotRef(row["snapshot_id"], reused=True)

    def assess_snapshot_quality(
        self,
        snapshot_id: str,
        contract: Any,
    ) -> SnapshotQualityAssessment:
        """Apply a field-level quality contract to one persisted snapshot."""

        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            return SnapshotQualityAssessment("blocked", snapshot_id, note="快照不存在")
        if isinstance(contract, dict):
            required_fields = tuple(str(item) for item in contract.get("required_fields", ()))
            expected_codes = tuple(str(item) for item in contract.get("codes", contract.get("universe_codes", ())))
            mode = str(contract.get("mode", "formal"))
        else:
            required_fields = tuple(str(item) for item in getattr(contract, "required_fields", ()))
            expected_codes = tuple(
                str(item)
                for item in getattr(contract, "codes", getattr(contract, "universe_codes", ()))
            )
            mode = str(getattr(contract, "mode", "formal"))
        facts_by_code: dict[str, set[str]] = {}
        for fact in snapshot.facts:
            facts_by_code.setdefault(fact.code, set()).add(fact.field_name)
        missing: list[str] = []
        codes = expected_codes or tuple(sorted(facts_by_code))
        for code in codes:
            for field in required_fields:
                if field not in facts_by_code.get(code, set()):
                    missing.append(f"{code}.{field}")
        cutoff = _parse_time(snapshot.manifest.availability_cutoff)
        invalid_available = sum(
            1
            for fact in snapshot.facts
            if _parse_time(fact.available_at) > cutoff
            and (not required_fields or fact.field_name in required_fields)
        )
        unverified = sum(
            1
            for fact in snapshot.facts
            if fact.quality_status != "verified"
            and (not required_fields or fact.field_name in required_fields)
        )
        # A manifest may be partial because an optional field is absent.  The
        # template contract decides whether that optional gap blocks formal
        # research; only required-field quality issues do.
        has_quality_issue = bool(invalid_available or unverified)
        if mode == "review":
            status = "review_only"
            note = "当前快照仅供历史复核，不得进入正式筛选或绩效统计。"
        elif missing or has_quality_issue:
            status = "blocked"
            note = "正式数据质量闸门未通过，禁止进入正式筛选或绩效统计。"
        else:
            status = "verified"
            note = "快照满足声明字段和可获得时间契约。"
        return SnapshotQualityAssessment(
            status=status,
            snapshot_id=snapshot_id,
            required_fields=required_fields,
            missing_fields=tuple(missing),
            invalid_available_facts=invalid_available,
            unverified_facts=unverified,
            note=note,
        )
