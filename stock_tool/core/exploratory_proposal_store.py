"""Durable human-review queue for exploratory CandidateLens proposals."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Iterable, Mapping
from contextlib import closing

from stock_tool.core.evidence_contracts import contract_hash
from stock_tool.core.selection_policy import (
    DEFAULT_SELECTION_POLICY,
    ExploratoryProposal,
    SelectionPolicy,
    SelectionResult,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _actor(actor: str) -> str:
    value = str(actor or "").strip()
    if not value.casefold().startswith(("human:", "user:")):
        raise ValueError("探索建议只能由 human:<id> 或 user:<id> 执行人工操作")
    return value


@dataclass(frozen=True)
class ExploratoryProposalRecord:
    proposal_id: str
    snapshot_id: str
    proposal: ExploratoryProposal
    status: str
    operator: str
    audited_at: str
    rejection_reason: str
    formal_run_id: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.proposal.as_dict(),
            "snapshot_id": self.snapshot_id,
            "status": self.status,
            "operator": self.operator,
            "audited_at": self.audited_at,
            "rejection_reason": self.rejection_reason,
            "formal_run_id": self.formal_run_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SqliteExploratoryProposalStore:
    """Store proposals without ever mutating the deterministic candidate set."""

    STATUSES = frozenset({"pending_human_review", "formalized", "rejected", "evidence_insufficient"})

    def __init__(self, database_path: str) -> None:
        self.database_path = str(database_path)
        with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS exploratory_candidate_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    proposed_lens_id TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    proposal_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    operator TEXT NOT NULL DEFAULT '',
                    audited_at TEXT NOT NULL DEFAULT '',
                    rejection_reason TEXT NOT NULL DEFAULT '',
                    formal_run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_exploratory_candidate_proposals_status
                ON exploratory_candidate_proposals(status, updated_at DESC)
                """
            )
            connection.commit()

    @staticmethod
    def _proposal_id(proposal: ExploratoryProposal) -> str:
        return str(proposal.proposal_id or "").strip()

    @staticmethod
    def _fact_id(snapshot_id: str, code: str, field_name: str) -> str:
        return f"fact-{contract_hash({'snapshot_id': snapshot_id, 'code': code, 'field_name': field_name})[:24]}"

    @classmethod
    def _known_fact_ids(cls, connection: sqlite3.Connection, snapshot_id: str) -> set[str] | None:
        """Return persisted Fact v1 identities when the immutable fact table exists."""

        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'data_snapshot_facts'"
        ).fetchone()
        if table is None:
            return None
        return {
            cls._fact_id(str(snapshot_id), str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT code, field_name FROM data_snapshot_facts WHERE snapshot_id = ?",
                (str(snapshot_id),),
            ).fetchall()
        }

    def save(self, proposal: ExploratoryProposal, *, snapshot_id: str = "") -> ExploratoryProposalRecord:
        if not isinstance(proposal, ExploratoryProposal):
            raise TypeError("proposal 必须是 ExploratoryProposal")
        resolved_snapshot_id = str(snapshot_id or proposal.snapshot_id or "").strip()
        if not resolved_snapshot_id:
            raise ValueError("探索建议必须绑定当前不可变 snapshot_id")
        if not proposal.evidence_refs:
            raise ValueError("探索建议必须包含合法事实引用")
        if proposal.snapshot_id and proposal.snapshot_id != resolved_snapshot_id:
            raise ValueError("proposal.snapshot_id 与 snapshot_id 不一致")
        if not self._proposal_id(proposal):
            raise ValueError("proposal_id 不能为空")
        now = _now()
        payload = replace(proposal, snapshot_id=resolved_snapshot_id).as_dict()
        evidence_refs = tuple(str(item) for item in proposal.evidence_refs if str(item).strip())
        if not evidence_refs:
            raise ValueError("探索建议必须包含合法事实引用")
        with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
            known_fact_ids = self._known_fact_ids(connection, resolved_snapshot_id)
            if known_fact_ids is not None:
                missing = sorted(set(evidence_refs) - known_fact_ids)
                if missing:
                    raise ValueError("探索建议包含不存在的事实引用：" + "、".join(missing[:8]))
            existing = connection.execute(
                "SELECT proposal_json FROM exploratory_candidate_proposals WHERE proposal_id = ?",
                (proposal.proposal_id,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_payload = json.loads(existing[0])
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError("已有探索建议记录损坏，拒绝覆盖") from exc
                if existing_payload != payload:
                    raise ValueError("proposal_id 已存在但内容不一致，拒绝覆盖人工审计记录")
                return self.get(proposal.proposal_id)  # type: ignore[return-value]
            connection.execute(
                """
                INSERT INTO exploratory_candidate_proposals(
                    proposal_id, snapshot_id, source_run_id, code, proposed_lens_id,
                    evidence_refs_json, proposal_json, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending_human_review', ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    snapshot_id = excluded.snapshot_id,
                    source_run_id = excluded.source_run_id,
                    code = excluded.code,
                    proposed_lens_id = excluded.proposed_lens_id,
                    evidence_refs_json = excluded.evidence_refs_json,
                    proposal_json = excluded.proposal_json,
                    status = 'pending_human_review',
                    operator = '', audited_at = '', rejection_reason = '',
                    formal_run_id = '', updated_at = excluded.updated_at
                """,
                (
                    proposal.proposal_id,
                    resolved_snapshot_id,
                    proposal.source_run_id,
                    proposal.code.zfill(6),
                    proposal.proposed_lens_id,
                    _canonical_json(list(proposal.evidence_refs)),
                    _canonical_json(payload),
                    now,
                    now,
                ),
            )
            connection.commit()
        return self.get(proposal.proposal_id)  # type: ignore[return-value]

    submit = save

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ExploratoryProposalRecord:
        payload = json.loads(row["proposal_json"])
        proposal = ExploratoryProposal(
            proposal_id=str(payload["proposal_id"]),
            code=str(payload["code"]).zfill(6),
            proposed_lens_id=str(payload["proposed_lens_id"]),
            source_run_id=str(payload["source_run_id"]),
            evidence_refs=tuple(str(item) for item in payload.get("evidence_refs") or ()),
            proposal_reason=str(payload["proposal_reason"]),
            missing_evidence=tuple(str(item) for item in payload.get("missing_evidence") or ()),
            status="pending_human_review",
            contract_version=str(payload.get("contract_version") or "exploratory_proposal.v1"),
            snapshot_id=str(payload.get("snapshot_id") or row["snapshot_id"]),
        )
        return ExploratoryProposalRecord(
            proposal_id=str(row["proposal_id"]),
            snapshot_id=str(row["snapshot_id"]),
            proposal=proposal,
            status=str(row["status"]),
            operator=str(row["operator"] or ""),
            audited_at=str(row["audited_at"] or ""),
            rejection_reason=str(row["rejection_reason"] or ""),
            formal_run_id=str(row["formal_run_id"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get(self, proposal_id: str) -> ExploratoryProposalRecord | None:
        with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM exploratory_candidate_proposals WHERE proposal_id = ?",
                (str(proposal_id),),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list(self, *, status: str = "pending_human_review", limit: int = 100) -> list[ExploratoryProposalRecord]:
        if status and status not in self.STATUSES:
            raise ValueError(f"未知探索建议状态：{status}")
        query = "SELECT * FROM exploratory_candidate_proposals"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC, proposal_id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
        return [self._from_row(row) for row in rows]

    def list_pending(self, *, limit: int = 100) -> list[ExploratoryProposalRecord]:
        return self.list(status="pending_human_review", limit=limit)

    def _audit_update(
        self,
        proposal_id: str,
        *,
        status: str,
        operator: str,
        reason: str = "",
        formal_run_id: str = "",
    ) -> ExploratoryProposalRecord:
        if status not in self.STATUSES:
            raise ValueError(f"未知探索建议状态：{status}")
        now = _now()
        with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
            connection.execute(
                """
                UPDATE exploratory_candidate_proposals
                SET status = ?, operator = ?, audited_at = ?, rejection_reason = ?,
                    formal_run_id = CASE WHEN ? <> '' THEN ? ELSE formal_run_id END,
                    updated_at = ?
                WHERE proposal_id = ? AND status = 'pending_human_review'
                """,
                (status, operator, now, str(reason or "")[:2000], str(formal_run_id or ""), str(formal_run_id or ""), now, proposal_id),
            )
            connection.commit()
        record = self.get(proposal_id)
        if record is None:
            raise KeyError(f"探索建议不存在：{proposal_id}")
        if record.status != status:
            raise ValueError("探索建议已被其他人工操作处理")
        return record

    def reject(self, proposal_id: str, actor: str, reason: str) -> ExploratoryProposalRecord:
        operator = _actor(actor)
        if not str(reason or "").strip():
            raise ValueError("拒绝探索建议必须填写理由")
        return self._audit_update(proposal_id, status="rejected", operator=operator, reason=reason)

    def attach_formal_run(self, proposal_id: str, run_id: str) -> ExploratoryProposalRecord:
        now = _now()
        with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
            connection.execute(
                "UPDATE exploratory_candidate_proposals SET formal_run_id = ?, updated_at = ? WHERE proposal_id = ? AND status = 'formalized'",
                (str(run_id or ""), now, str(proposal_id)),
            )
            connection.commit()
        record = self.get(proposal_id)
        if record is None:
            raise KeyError(f"探索建议不存在：{proposal_id}")
        return record

    def mark_formalization_failure(
        self,
        proposal_id: str,
        actor: str,
        reason: str,
    ) -> ExploratoryProposalRecord:
        """Close a pre-audited proposal when the persisted formal run fails its gates."""

        operator = _actor(actor)
        if not str(reason or "").strip():
            raise ValueError("正式复核失败必须填写理由")
        now = _now()
        with closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
            connection.execute(
                """
                UPDATE exploratory_candidate_proposals
                SET status = 'evidence_insufficient', operator = ?, audited_at = ?,
                    rejection_reason = ?, updated_at = ?
                WHERE proposal_id = ? AND status = 'formalized' AND formal_run_id = ''
                """,
                (operator, now, str(reason)[:2000], now, str(proposal_id)),
            )
            connection.commit()
        record = self.get(proposal_id)
        if record is None:
            raise KeyError(f"探索建议不存在：{proposal_id}")
        if record.status != "evidence_insufficient":
            raise ValueError("探索建议已完成正式化，不能覆盖已有正式运行记录")
        return record

    def formalize(
        self,
        proposal_id: str,
        *,
        actor: str,
        snapshot_id: str,
        item: Any | None = None,
        snapshot: Any | None = None,
        availability_cutoff: str = "",
        policy: SelectionPolicy | None = None,
        fact_ids: Iterable[str] | None = None,
        formal_run_id: str = "",
        **kwargs: Any,
    ) -> tuple[ExploratoryProposalRecord, SelectionResult]:
        operator = _actor(actor)
        record = self.get(proposal_id)
        if record is None:
            raise KeyError(f"探索建议不存在：{proposal_id}")
        if record.status != "pending_human_review":
            raise ValueError("探索建议已不在待复核状态")
        if str(snapshot_id or "") != record.snapshot_id:
            raise ValueError("正式复核必须使用原始不可变 snapshot_id")
        item = snapshot if snapshot is not None else item
        if item is None:
            raise ValueError("正式复核缺少原始不可变快照")
        item_code = item.get("code") if isinstance(item, Mapping) else getattr(item, "code", "")
        if str(item_code or "").zfill(6) != record.proposal.code.zfill(6):
            raise ValueError("正式复核快照与探索建议股票代码不一致")
        if fact_ids is not None:
            valid_ids = {str(value) for value in fact_ids}
            missing = [ref for ref in record.proposal.evidence_refs if ref not in valid_ids]
            if missing:
                audited = self._audit_update(
                    proposal_id,
                    status="evidence_insufficient",
                    operator=operator,
                    reason="证据引用失效：" + "、".join(missing[:8]),
                )
                raise ValueError(audited.rejection_reason)
        proposal = record.proposal
        if not proposal.snapshot_id:
            proposal = replace(proposal, snapshot_id=record.snapshot_id)
        result = (policy or DEFAULT_SELECTION_POLICY).formalize_exploratory_proposal(
            proposal,
            item,
            actor=operator,
            availability_cutoff=availability_cutoff,
            **kwargs,
        )
        if result.status == "candidate":
            status = "formalized"
            reason = "已通过 CandidateLens 正式复核；仍需单独人工加入观察。"
        else:
            status = "evidence_insufficient" if result.status in {"insufficient", "stale"} else "rejected"
            reason = "；".join(result.reason_codes) or "CandidateLens 正式复核未通过"
        audited = self._audit_update(
            proposal_id,
            status=status,
            operator=operator,
            reason=reason,
            formal_run_id=formal_run_id,
        )
        return audited, result


def proposals_from_agent_result(
    result: Any,
    *,
    snapshot_id: str,
    source_run_id: str,
) -> tuple[ExploratoryProposal, ...]:
    """Read optional exploratory suggestions from Agent diagnostics only.

    Diagnostics are advisory input; persistence still validates the contract
    and places every proposal in ``pending_human_review``.
    """

    payload = result.as_dict() if hasattr(result, "as_dict") else result
    diagnostics = payload.get("diagnostics") if isinstance(payload, Mapping) else {}
    raw_items = diagnostics.get("exploratory_proposals") if isinstance(diagnostics, Mapping) else ()
    proposals: list[ExploratoryProposal] = []
    for index, raw in enumerate(raw_items or ()):
        if not isinstance(raw, Mapping):
            continue
        proposal_id = str(raw.get("proposal_id") or contract_hash({"run": source_run_id, "index": index, "raw": raw}))
        raw_code = str(raw.get("code") or raw.get("symbol") or "").strip()
        if not raw_code or not raw_code.isdigit() or len(raw_code) > 6:
            continue
        try:
            proposals.append(
                ExploratoryProposal(
                    proposal_id=proposal_id,
                    code=raw_code.zfill(6),
                    proposed_lens_id=str(raw.get("proposed_lens_id") or raw.get("lens_id") or ""),
                    source_run_id=str(source_run_id),
                    evidence_refs=tuple(str(item) for item in raw.get("evidence_refs") or ()),
                    proposal_reason=str(raw.get("proposal_reason") or raw.get("reason") or ""),
                    missing_evidence=tuple(str(item) for item in raw.get("missing_evidence") or ()),
                    snapshot_id=str(snapshot_id),
                )
            )
        except ValueError:
            # Malformed Agent suggestions are not candidates and are not put
            # in the human queue; the five-Agent batch itself remains intact.
            continue
    return tuple(proposals)


__all__ = [
    "ExploratoryProposalRecord",
    "SqliteExploratoryProposalStore",
    "proposals_from_agent_result",
]
