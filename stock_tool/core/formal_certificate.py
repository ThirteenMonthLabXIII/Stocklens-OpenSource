"""Formal screening identity and observation-sample gates.

The ordinary screening result is useful for exploration, but it is not by
itself a reproducible research credential.  This module makes the credential
explicit and keeps the database write path honest: a formal observation must
point to a verified certificate for the same run, snapshot and refresh event.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from stock_tool.core.screening_contract import stable_hash
from stock_tool.core.v1_database import V1Database


RESEARCH_MODES = frozenset({"formal", "review_only", "exploratory"})
SAMPLE_CLASSES = frozenset({"formal", "review", "exploratory", "legacy"})


class FormalObservationBlocked(RuntimeError):
    """Raised when a formal observation would bypass its research credential."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class UniverseDefinition:
    universe_definition_id: str
    universe_version: str
    members_digest: str
    selection_scope: str
    selection_policy_version: str
    selected_limit: int | None = None
    truncation_reason: str = ""
    member_status_policy: str = ""

    def __post_init__(self) -> None:
        required = {
            "universe_definition_id": self.universe_definition_id,
            "universe_version": self.universe_version,
            "members_digest": self.members_digest,
            "selection_scope": self.selection_scope,
            "selection_policy_version": self.selection_policy_version,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"股票池身份缺少字段：{', '.join(missing)}")
        if self.selected_limit is not None and int(self.selected_limit) < 1:
            raise ValueError("selected_limit 必须为正整数")

    def as_dict(self) -> dict[str, Any]:
        return {
            "universe_definition_id": self.universe_definition_id,
            "universe_version": self.universe_version,
            "members_digest": self.members_digest,
            "selection_scope": self.selection_scope,
            "selection_policy_version": self.selection_policy_version,
            "selected_limit": self.selected_limit,
            "truncation_reason": self.truncation_reason,
            "member_status_policy": self.member_status_policy,
        }

    @classmethod
    def from_members(
        cls,
        *,
        universe_definition_id: str,
        universe_version: str,
        members: Iterable[str],
        selection_scope: str,
        selection_policy_version: str,
        selected_limit: int | None = None,
        truncation_reason: str = "",
        member_status_policy: str = "",
    ) -> "UniverseDefinition":
        normalized = sorted({str(code).zfill(6) for code in members if str(code).strip()})
        if not normalized:
            raise ValueError("正式股票池不能没有成员")
        return cls(
            universe_definition_id=universe_definition_id,
            universe_version=universe_version,
            members_digest=stable_hash(normalized),
            selection_scope=selection_scope,
            selection_policy_version=selection_policy_version,
            selected_limit=selected_limit,
            truncation_reason=truncation_reason,
            member_status_policy=member_status_policy,
        )


def screening_result_digest(results: Iterable[Any]) -> str:
    """Hash only deterministic selection identity, not volatile display text."""

    rows: list[dict[str, Any]] = []
    for index, candidate in enumerate(results, start=1):
        quote = getattr(candidate, "quote", None)
        rows.append(
            {
                "rank": index,
                "code": str(getattr(quote, "code", "")).zfill(6),
                "local_score": getattr(candidate, "local_score", None),
                "eligibility_status": (getattr(candidate, "score_breakdown", {}) or {}).get("eligibility_status"),
            }
        )
    return stable_hash(rows)


@dataclass(frozen=True)
class FormalScreeningCertificate:
    certificate_id: str
    run_id: int
    data_snapshot_id: str
    refresh_event_id: str
    universe: UniverseDefinition
    selection_scope: str
    formula_version: str
    execution_mode: str
    quality_status: str
    quality_summary: dict[str, Any]
    condition_hash: str
    result_digest: str
    issued_at: str
    certificate_hash: str

    @classmethod
    def issue(
        cls,
        *,
        run_id: int,
        data_snapshot_id: str,
        refresh_event_id: str,
        universe: UniverseDefinition,
        selection_scope: str,
        formula_version: str,
        execution_mode: str,
        quality_status: str,
        quality_summary: dict[str, Any],
        condition_hash: str,
        result_digest: str,
        issued_at: str | None = None,
        certificate_id: str | None = None,
    ) -> "FormalScreeningCertificate":
        if execution_mode != "formal":
            raise FormalObservationBlocked("只有 formal 运行可以签发正式筛选凭证")
        if quality_status != "verified":
            raise FormalObservationBlocked("数据质量未达到 verified，不能签发正式筛选凭证")
        required = {
            "data_snapshot_id": data_snapshot_id,
            "refresh_event_id": refresh_event_id,
            "selection_scope": selection_scope,
            "formula_version": formula_version,
            "condition_hash": condition_hash,
            "result_digest": result_digest,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise FormalObservationBlocked(f"正式筛选凭证缺少身份字段：{', '.join(missing)}")
        summary = dict(quality_summary or {})
        count_names = (
            "fact_total",
            "verified_fact_count",
            "partial_fact_count",
            "unverified_fact_count",
            "invalid_fact_count",
            "blocked_fact_count",
        )
        counts: dict[str, int] = {}
        for name in count_names:
            if name not in summary:
                continue
            value = summary[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FormalObservationBlocked(f"正式筛选凭证质量计数无效：{name}")
            counts[name] = value
        status_counts = sum(
            counts.get(name, 0)
            for name in ("verified_fact_count", "partial_fact_count", "unverified_fact_count", "invalid_fact_count", "blocked_fact_count")
        )
        if "fact_total" in counts and status_counts and status_counts != counts["fact_total"]:
            raise FormalObservationBlocked("正式筛选凭证质量计数与 fact_total 不一致")
        if quality_status == "verified":
            if any(counts.get(name, 0) for name in ("partial_fact_count", "unverified_fact_count", "invalid_fact_count", "blocked_fact_count")):
                raise FormalObservationBlocked("verified 凭证不能包含 partial/unverified/invalid/blocked 事实")
            if "fact_total" in counts and "verified_fact_count" in counts and counts["fact_total"] != counts["verified_fact_count"]:
                raise FormalObservationBlocked("verified 凭证的 fact_total 必须全部为 verified")
            if int(summary.get("failed") or 0) != 0:
                raise FormalObservationBlocked("verified 凭证不能包含失败批次")
        if summary.get("required_missing_fields") or summary.get("invalid_fact_count"):
            raise FormalObservationBlocked("正式筛选凭证包含缺失或非法事实")
        issued = issued_at or _now()
        resolved_certificate_id = certificate_id or f"cert-{uuid.uuid4().hex}"
        payload = {
            "certificate_id": resolved_certificate_id,
            "run_id": int(run_id),
            "data_snapshot_id": data_snapshot_id,
            "refresh_event_id": refresh_event_id,
            "universe": universe.as_dict(),
            "selection_scope": selection_scope,
            "formula_version": formula_version,
            "execution_mode": execution_mode,
            "quality_status": quality_status,
            "quality_summary": summary,
            "condition_hash": condition_hash,
            "result_digest": result_digest,
            "issued_at": issued,
        }
        return cls(
            certificate_id=resolved_certificate_id,
            run_id=int(run_id),
            data_snapshot_id=data_snapshot_id,
            refresh_event_id=refresh_event_id,
            universe=universe,
            selection_scope=selection_scope,
            formula_version=formula_version,
            execution_mode=execution_mode,
            quality_status=quality_status,
            quality_summary=summary,
            condition_hash=condition_hash,
            result_digest=result_digest,
            issued_at=issued,
            certificate_hash=stable_hash(payload),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "certificate_id": self.certificate_id,
            "run_id": self.run_id,
            "data_snapshot_id": self.data_snapshot_id,
            "refresh_event_id": self.refresh_event_id,
            **self.universe.as_dict(),
            "selection_scope": self.selection_scope,
            "formula_version": self.formula_version,
            "execution_mode": self.execution_mode,
            "quality_status": self.quality_status,
            "quality_summary": self.quality_summary,
            "condition_hash": self.condition_hash,
            "result_digest": self.result_digest,
            "issued_at": self.issued_at,
            "certificate_hash": self.certificate_hash,
        }


class FormalScreeningCertificateStore:
    """Append-only persistence for formal screening certificates."""

    def __init__(self, database: V1Database) -> None:
        self.database = database

    def save(self, certificate: FormalScreeningCertificate) -> None:
        payload = certificate.as_dict()
        with self.database.connect() as conn:
            manifest = conn.execute(
                "SELECT quality_status, quality_summary_json FROM data_snapshot_manifests WHERE snapshot_id = ?",
                (certificate.data_snapshot_id,),
            ).fetchone()
            if manifest is None:
                raise FormalObservationBlocked("正式凭证绑定的数据快照不存在")
            if manifest["quality_status"] != "verified":
                raise FormalObservationBlocked("正式凭证绑定的数据快照不是 verified")
            manifest_summary = json.loads(manifest["quality_summary_json"] or "{}")
            for name in (
                "fact_total",
                "verified_fact_count",
                "partial_fact_count",
                "unverified_fact_count",
                "invalid_fact_count",
                "blocked_fact_count",
            ):
                if name in manifest_summary and name in certificate.quality_summary:
                    if manifest_summary[name] != certificate.quality_summary[name]:
                        raise FormalObservationBlocked(f"正式凭证与快照质量计数不一致：{name}")
            if any(
                int(manifest_summary.get(name) or 0)
                for name in ("partial_fact_count", "unverified_fact_count", "invalid_fact_count", "blocked_fact_count")
            ):
                raise FormalObservationBlocked("正式凭证绑定的数据快照包含非 verified 事实")
            conn.execute(
                """
                INSERT INTO formal_screening_certificates(
                    certificate_id, run_id, data_snapshot_id, refresh_event_id,
                    universe_definition_id, universe_version, members_digest,
                    selection_scope, selection_policy_version, selected_limit,
                    truncation_reason, member_status_policy, formula_version,
                    execution_mode, quality_status, quality_summary_json,
                    condition_hash, result_digest, issued_at, certificate_hash
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(certificate_id) DO NOTHING
                """,
                (
                    certificate.certificate_id,
                    int(certificate.run_id),
                    certificate.data_snapshot_id,
                    certificate.refresh_event_id,
                    certificate.universe.universe_definition_id,
                    certificate.universe.universe_version,
                    certificate.universe.members_digest,
                    certificate.selection_scope,
                    certificate.universe.selection_policy_version,
                    certificate.universe.selected_limit,
                    certificate.universe.truncation_reason,
                    certificate.universe.member_status_policy,
                    certificate.formula_version,
                    certificate.execution_mode,
                    certificate.quality_status,
                    json.dumps(certificate.quality_summary, ensure_ascii=False, sort_keys=True),
                    certificate.condition_hash,
                    certificate.result_digest,
                    certificate.issued_at,
                    certificate.certificate_hash,
                ),
            )

    def get(self, certificate_id: str) -> FormalScreeningCertificate | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM formal_screening_certificates WHERE certificate_id = ?",
                (str(certificate_id),),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get_for_run(self, run_id: int) -> FormalScreeningCertificate | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM formal_screening_certificates WHERE run_id = ?",
                (int(run_id),),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def require_for_sample(self, run_id: int, certificate_id: str) -> FormalScreeningCertificate:
        certificate = self.get(certificate_id)
        if certificate is None:
            raise FormalObservationBlocked("正式观察样本缺少有效凭证")
        if certificate.run_id != int(run_id):
            raise FormalObservationBlocked("正式凭证与运行记录不一致")
        if certificate.execution_mode != "formal" or certificate.quality_status != "verified":
            raise FormalObservationBlocked("正式凭证状态不允许创建正式观察样本")
        with self.database.connect() as conn:
            run = conn.execute(
                "SELECT scoring_context_json FROM query_runs WHERE id = ?",
                (int(run_id),),
            ).fetchone()
        if run is None:
            raise FormalObservationBlocked("正式凭证对应的运行记录不存在")
        context = json.loads(run["scoring_context_json"] or "{}")
        if not isinstance(context, dict) or context.get("execution_mode") != "formal" or context.get("screening_status") != "verified":
            raise FormalObservationBlocked("运行记录不是 verified formal，禁止创建正式观察样本")
        for field in ("data_snapshot_id", "refresh_event_id"):
            bound = str(context.get(field) or "").strip()
            if bound and bound != str(getattr(certificate, field) or "").strip():
                raise FormalObservationBlocked(f"正式凭证与运行记录的 {field} 不一致")
        return certificate

    @staticmethod
    def _from_row(row: Any) -> FormalScreeningCertificate:
        universe = UniverseDefinition(
            universe_definition_id=row["universe_definition_id"],
            universe_version=row["universe_version"],
            members_digest=row["members_digest"],
            selection_scope=row["selection_scope"],
            selection_policy_version=row["selection_policy_version"],
            selected_limit=row["selected_limit"],
            truncation_reason=row["truncation_reason"],
            member_status_policy=row["member_status_policy"],
        )
        return FormalScreeningCertificate(
            certificate_id=row["certificate_id"],
            run_id=int(row["run_id"]),
            data_snapshot_id=row["data_snapshot_id"],
            refresh_event_id=row["refresh_event_id"],
            universe=universe,
            selection_scope=row["selection_scope"],
            formula_version=row["formula_version"],
            execution_mode=row["execution_mode"],
            quality_status=row["quality_status"],
            quality_summary=json.loads(row["quality_summary_json"] or "{}"),
            condition_hash=row["condition_hash"],
            result_digest=row["result_digest"],
            issued_at=row["issued_at"],
            certificate_hash=row["certificate_hash"],
        )
