"""Phase 5 G layer: local candidate state and auditable transitions.

The G layer is intentionally independent from Agent execution.  It may use
the F summary as an input signal for ``insufficient`` or ``conflicted``, but
it never lets an Agent create a candidate, remove a candidate, or enter a
watch state.  Entering ``watch`` requires an explicit human action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import json
import re
import sqlite3
import threading
from typing import Any, Mapping, Protocol

from stock_tool.core.evidence_contracts import CANDIDATE_STATUSES


CANDIDATE_STATE_CONTRACT_VERSION = "candidate_state.v1"
LOCAL_RULE_ACTOR = "local_rule"
MANUAL_WATCH_ACTION = "manual_add_watch"

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "eligible": frozenset({"candidate", "insufficient", "conflicted", "excluded", "stale", "invalidated", "closed"}),
    "candidate": frozenset({"watch", "insufficient", "conflicted", "excluded", "stale", "invalidated", "closed"}),
    "insufficient": frozenset({"stale", "invalidated", "closed"}),
    "conflicted": frozenset({"stale", "invalidated", "closed"}),
    "watch": frozenset({"stale", "invalidated", "closed"}),
    "excluded": frozenset({"closed"}),
    "stale": frozenset({"invalidated", "closed"}),
    "invalidated": frozenset({"closed"}),
    "closed": frozenset(),
}
_AUTOMATED_ACTOR_MARKERS = frozenset(
    {
        "ai",
        "agent",
        "assistant",
        "automated",
        "automation",
        "bot",
        "f_layer",
        "g_layer",
        "local",
        "local_rule",
        "orchestration",
        "rule",
        "scheduler",
        "system",
        "workflow",
    }
)
_HUMAN_ACTOR_PREFIXES = ("human:", "user:")


class CandidateStateError(ValueError):
    """Raised when a local candidate state contract is violated."""


def _required_text(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise CandidateStateError(f"{name} 不能为空")
    return text


def _text_tuple(name: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        raise CandidateStateError(f"{name} 必须是字符串列表")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_time(name: str, value: str) -> str:
    text = _required_text(name, value)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandidateStateError(f"{name} 必须是 ISO 时间") from exc
    return text


def _candidate_id(record: Mapping[str, Any]) -> str:
    return _required_text("candidate_id", record.get("candidate_id") or record.get("code"))


def _code(record: Mapping[str, Any]) -> str:
    return _required_text("code", record.get("code"))


def _record_status(record: Mapping[str, Any]) -> str:
    raw = str(record.get("eligibility_status") or "eligible").strip()
    if raw == "ineligible":
        raw = "excluded"
    if raw not in CANDIDATE_STATUSES:
        raise CandidateStateError(f"候选记录包含未知 eligibility_status：{raw}")
    if raw == "eligible":
        return "eligible"
    return raw


def _summary_dict(summary: Any) -> dict[str, Any] | None:
    if summary is None:
        return None
    if isinstance(summary, Mapping):
        return dict(summary)
    as_dict = getattr(summary, "as_dict", None)
    if callable(as_dict):
        value = as_dict()
        if isinstance(value, Mapping):
            return dict(value)
    raise CandidateStateError("EvidenceSummary 类型不合法")


def _summary_matches(summary: dict[str, Any] | None, record: Mapping[str, Any]) -> bool:
    if summary is None:
        return False
    summary_id = str(summary.get("candidate_id") or "").strip()
    summary_code = str(summary.get("code") or "").strip()
    if summary_id and summary_id != _candidate_id(record):
        return False
    if summary_code and summary_code != _code(record):
        return False
    return bool(summary_id or summary_code)


def _state_reasons(record: Mapping[str, Any], summary: dict[str, Any] | None, target: str) -> tuple[str, ...]:
    reasons = list(_text_tuple("reason_codes", record.get("reason_codes") or ()))
    missing = _text_tuple("missing_fields", record.get("missing_fields") or ())
    stale = _text_tuple("stale_fields", record.get("stale_fields") or ())
    if target == "candidate":
        reasons.append("STATE_CANDIDATE")
    elif target == "insufficient":
        reasons.append("STATE_INSUFFICIENT_DATA")
    elif target == "conflicted":
        reasons.append("STATE_CONFLICTED_EVIDENCE")
    elif target == "stale":
        reasons.append("STATE_STALE_DATA")
    elif target == "excluded":
        reasons.append("STATE_EXCLUDED")
    if missing:
        reasons.append("MISSING_FIELDS_PRESENT")
    if stale:
        reasons.append("STALE_FIELDS_PRESENT")
    if summary:
        if str(summary.get("status") or "") == "failed":
            reasons.append("AGENT_UNAVAILABLE")
        if summary.get("unresolved_questions"):
            reasons.append("EVIDENCE_GAP")
        if summary.get("conflicts"):
            reasons.append("EVIDENCE_CONFLICT")
    return tuple(dict.fromkeys(reasons))


def _target_for_record(record: Mapping[str, Any], summary: dict[str, Any] | None) -> str:
    base = _record_status(record)
    if base in {"excluded", "insufficient", "conflicted", "stale", "invalidated", "closed", "watch"}:
        return base
    if _text_tuple("stale_fields", record.get("stale_fields") or ()):
        return "stale"
    if _text_tuple("missing_fields", record.get("missing_fields") or ()):
        return "insufficient"
    if summary and _summary_matches(summary, record):
        if summary.get("conflicts"):
            return "conflicted"
        # A failed Agent keeps the deterministic candidate visible.  It is
        # an unavailable research overlay, not a local exclusion signal.
        if str(summary.get("status") or "") != "failed" and summary.get("unresolved_questions"):
            return "insufficient"
    return "candidate"


def _target_with_prior(
    record: Mapping[str, Any],
    summary: dict[str, Any] | None,
    prior_status: str | None,
) -> str:
    fresh_target = _target_for_record(record, summary)
    if not prior_status:
        if fresh_target == "watch":
            raise CandidateStateError("初次建立状态不能自动进入 watch，必须使用人工动作")
        return "candidate" if fresh_target == "eligible" else fresh_target
    if prior_status not in CANDIDATE_STATUSES:
        raise CandidateStateError(f"已有状态未知：{prior_status}")
    if prior_status in {"closed", "invalidated"}:
        return prior_status
    if prior_status == "watch":
        return "stale" if fresh_target == "stale" else "watch"
    if prior_status in {"insufficient", "conflicted"}:
        # No automatic promotion from a blocked/conflicted state.  A future
        # explicit local re-screen can create a new candidate identity.
        if fresh_target == "stale":
            return "stale"
        return prior_status
    return fresh_target


@dataclass(frozen=True)
class CandidateState:
    candidate_id: str
    code: str
    status: str
    lens_ids: tuple[str, ...] = ()
    candidate_digest: str = ""
    reason_codes: tuple[str, ...] = ()
    snapshot_id: str = ""
    updated_at: str = ""
    contract_version: str = CANDIDATE_STATE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CANDIDATE_STATE_CONTRACT_VERSION:
            raise CandidateStateError(f"不支持的候选状态契约版本：{self.contract_version}")
        _required_text("candidate_id", self.candidate_id)
        _required_text("code", self.code)
        if self.status not in CANDIDATE_STATUSES:
            raise CandidateStateError(f"不支持的候选状态：{self.status}")
        _required_text("snapshot_id", self.snapshot_id)
        _validate_time("updated_at", self.updated_at)
        object.__setattr__(self, "lens_ids", _text_tuple("lens_ids", self.lens_ids))
        object.__setattr__(self, "reason_codes", _text_tuple("reason_codes", self.reason_codes))

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "candidate_id": self.candidate_id,
            "code": self.code,
            "status": self.status,
            "lens_ids": list(self.lens_ids),
            "candidate_digest": self.candidate_digest,
            "reason_codes": list(self.reason_codes),
            "snapshot_id": self.snapshot_id,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class CandidateStateTransition:
    candidate_id: str
    from_status: str
    to_status: str
    reason_codes: tuple[str, ...]
    actor: str
    snapshot_id: str
    created_at: str
    contract_version: str = CANDIDATE_STATE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CANDIDATE_STATE_CONTRACT_VERSION:
            raise CandidateStateError(f"不支持的状态迁移契约版本：{self.contract_version}")
        _required_text("candidate_id", self.candidate_id)
        if self.from_status not in CANDIDATE_STATUSES or self.to_status not in CANDIDATE_STATUSES:
            raise CandidateStateError("状态迁移包含未知状态")
        if self.from_status == self.to_status:
            raise CandidateStateError("状态迁移的前后状态不能相同")
        if self.to_status not in _ALLOWED_TRANSITIONS[self.from_status]:
            raise CandidateStateError(f"不允许的候选状态迁移：{self.from_status} -> {self.to_status}")
        if not _text_tuple("reason_codes", self.reason_codes):
            raise CandidateStateError("状态迁移必须有 reason_codes")
        _required_text("actor", self.actor)
        _required_text("snapshot_id", self.snapshot_id)
        _validate_time("created_at", self.created_at)
        object.__setattr__(self, "reason_codes", _text_tuple("reason_codes", self.reason_codes))

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "candidate_id": self.candidate_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "reason_codes": list(self.reason_codes),
            "actor": self.actor,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CandidateStateBatch:
    snapshot_id: str
    states: tuple[CandidateState, ...]
    transitions: tuple[CandidateStateTransition, ...]
    candidate_ids: tuple[str, ...] = ()
    candidate_set_preserved: bool = True

    def __post_init__(self) -> None:
        if not self.candidate_set_preserved:
            raise CandidateStateError("G 层不允许改变候选集合")
        _required_text("snapshot_id", self.snapshot_id)
        state_ids = tuple(item.candidate_id for item in self.states)
        ids = tuple(self.candidate_ids) or state_ids
        if ids != state_ids:
            raise CandidateStateError("G 层状态集合与候选集合不一致")
        object.__setattr__(self, "candidate_ids", ids)

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "candidate_ids": list(self.candidate_ids),
            "states": [item.as_dict() for item in self.states],
            "transitions": [item.as_dict() for item in self.transitions],
            "candidate_set_preserved": self.candidate_set_preserved,
        }


class CandidateStateStore(Protocol):
    def get(self, candidate_id: str) -> CandidateState | None: ...
    def save_state(self, state: CandidateState) -> None: ...
    def append_transition(self, transition: CandidateStateTransition) -> None: ...
    def commit_transition(self, state: CandidateState, transition: CandidateStateTransition) -> None: ...
    def list_transitions(self, candidate_id: str | None = None) -> list[CandidateStateTransition]: ...


class MemoryCandidateStateStore:
    def __init__(self) -> None:
        self._states: dict[str, CandidateState] = {}
        self._transitions: list[CandidateStateTransition] = []
        self._lock = threading.Lock()

    def get(self, candidate_id: str) -> CandidateState | None:
        with self._lock:
            return self._states.get(str(candidate_id))

    def save_state(self, state: CandidateState) -> None:
        with self._lock:
            self._states[state.candidate_id] = state

    def append_transition(self, transition: CandidateStateTransition) -> None:
        with self._lock:
            self._transitions.append(transition)

    def commit_transition(self, state: CandidateState, transition: CandidateStateTransition) -> None:
        with self._lock:
            self._transitions.append(transition)
            self._states[state.candidate_id] = state

    def list_transitions(self, candidate_id: str | None = None) -> list[CandidateStateTransition]:
        with self._lock:
            items = list(self._transitions)
        if candidate_id is None:
            return items
        return [item for item in items if item.candidate_id == str(candidate_id)]


class SqliteCandidateStateStore:
    """Durable state plus append-only transition audit in a SQLite database."""

    def __init__(self, database_path: str) -> None:
        self.database_path = str(database_path)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_states (
                    candidate_id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lens_ids_json TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    contract_version TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_state_transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    contract_version TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidate_state_transitions_candidate "
                "ON candidate_state_transitions(candidate_id, created_at)"
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, candidate_id: str) -> CandidateState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT candidate_id, code, status, lens_ids_json, candidate_digest, reason_codes_json, snapshot_id, updated_at, contract_version FROM candidate_states WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
        if row is None:
            return None
        return CandidateState(
            candidate_id=row[0],
            code=row[1],
            status=row[2],
            lens_ids=tuple(json.loads(row[3])),
            candidate_digest=row[4],
            reason_codes=tuple(json.loads(row[5])),
            snapshot_id=row[6],
            updated_at=row[7],
            contract_version=row[8],
        )

    def save_state(self, state: CandidateState) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO candidate_states(
                    candidate_id, code, status, lens_ids_json, candidate_digest,
                    reason_codes_json, snapshot_id, updated_at, contract_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.candidate_id,
                    state.code,
                    state.status,
                    json.dumps(list(state.lens_ids), ensure_ascii=False),
                    state.candidate_digest,
                    json.dumps(list(state.reason_codes), ensure_ascii=False),
                    state.snapshot_id,
                    state.updated_at,
                    state.contract_version,
                ),
            )

    def append_transition(self, transition: CandidateStateTransition) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO candidate_state_transitions(
                    candidate_id, from_status, to_status, reason_codes_json,
                    actor, snapshot_id, created_at, contract_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition.candidate_id,
                    transition.from_status,
                    transition.to_status,
                    json.dumps(list(transition.reason_codes), ensure_ascii=False),
                    transition.actor,
                    transition.snapshot_id,
                    transition.created_at,
                    transition.contract_version,
                ),
            )

    def commit_transition(self, state: CandidateState, transition: CandidateStateTransition) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO candidate_states(
                    candidate_id, code, status, lens_ids_json, candidate_digest,
                    reason_codes_json, snapshot_id, updated_at, contract_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.candidate_id,
                    state.code,
                    state.status,
                    json.dumps(list(state.lens_ids), ensure_ascii=False),
                    state.candidate_digest,
                    json.dumps(list(state.reason_codes), ensure_ascii=False),
                    state.snapshot_id,
                    state.updated_at,
                    state.contract_version,
                ),
            )
            connection.execute(
                """
                INSERT INTO candidate_state_transitions(
                    candidate_id, from_status, to_status, reason_codes_json,
                    actor, snapshot_id, created_at, contract_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition.candidate_id,
                    transition.from_status,
                    transition.to_status,
                    json.dumps(list(transition.reason_codes), ensure_ascii=False),
                    transition.actor,
                    transition.snapshot_id,
                    transition.created_at,
                    transition.contract_version,
                ),
            )

    def list_transitions(self, candidate_id: str | None = None) -> list[CandidateStateTransition]:
        query = (
            "SELECT candidate_id, from_status, to_status, reason_codes_json, actor, snapshot_id, created_at, contract_version "
            "FROM candidate_state_transitions"
        )
        params: tuple[Any, ...] = ()
        if candidate_id is not None:
            query += " WHERE candidate_id = ?"
            params = (str(candidate_id),)
        query += " ORDER BY transition_id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            CandidateStateTransition(
                candidate_id=row[0],
                from_status=row[1],
                to_status=row[2],
                reason_codes=tuple(json.loads(row[3])),
                actor=row[4],
                snapshot_id=row[5],
                created_at=row[6],
                contract_version=row[7],
            )
            for row in rows
        ]


def transition_candidate_state(
    current: CandidateState,
    to_status: str,
    *,
    reason_codes: Any,
    actor: str,
    snapshot_id: str,
    created_at: str | None = None,
    manual: bool = False,
) -> CandidateStateTransition:
    """Create one legal, auditable transition; no implicit promotion exists."""

    target = str(to_status or "").strip()
    if target == "watch":
        if manual is not True:
            raise CandidateStateError("进入 watch 必须是明确的人工动作")
        normalized_actor = str(actor or "").casefold()
        if not normalized_actor.startswith(_HUMAN_ACTOR_PREFIXES):
            raise CandidateStateError("加入 watch 的 actor 必须明确标记为 human: 或 user:")
        actor_tokens = set(token for token in re.split(r"[^a-z0-9]+", normalized_actor) if token)
        if actor_tokens & _AUTOMATED_ACTOR_MARKERS:
            raise CandidateStateError("AI、Agent 或本地自动规则不能执行加入观察动作")
        if current.status != "candidate":
            raise CandidateStateError("只有 candidate 可以人工加入 watch")
    return CandidateStateTransition(
        candidate_id=current.candidate_id,
        from_status=current.status,
        to_status=target,
        reason_codes=_text_tuple("reason_codes", reason_codes),
        actor=actor,
        snapshot_id=snapshot_id,
        created_at=created_at or _now(),
    )


class CandidateStateService:
    """Resolve local states and persist transitions without AI side effects."""

    def __init__(self, store: CandidateStateStore) -> None:
        self.store = store

    def resolve_one(
        self,
        record: Mapping[str, Any],
        summary: Any | None,
        *,
        snapshot_id: str,
        actor: str = LOCAL_RULE_ACTOR,
        created_at: str | None = None,
    ) -> tuple[CandidateState, CandidateStateTransition | None]:
        snapshot_id = _required_text("snapshot_id", snapshot_id)
        now = created_at or _now()
        _validate_time("created_at", now)
        candidate_id = _candidate_id(record)
        code = _code(record)
        summary_value = _summary_dict(summary)
        existing = self.store.get(candidate_id)
        prior_status = existing.status if existing else None
        target = _target_with_prior(record, summary_value, prior_status)
        reasons = _state_reasons(record, summary_value, target)
        current = existing or CandidateState(
            candidate_id=candidate_id,
            code=code,
            status=_record_status(record),
            lens_ids=tuple(str(item) for item in (record.get("lens_ids") or ()) if str(item).strip()),
            candidate_digest=str(record.get("candidate_digest") or ""),
            reason_codes=tuple(_text_tuple("reason_codes", record.get("reason_codes") or ())),
            snapshot_id=snapshot_id,
            updated_at=now,
        )
        transition: CandidateStateTransition | None = None
        if current.status != target:
            transition = transition_candidate_state(
                current,
                target,
                reason_codes=reasons or (f"STATE_{target.upper()}",),
                actor=actor,
                snapshot_id=snapshot_id,
                created_at=now,
            )
        state = CandidateState(
            candidate_id=candidate_id,
            code=code,
            status=target,
            lens_ids=tuple(str(item) for item in (record.get("lens_ids") or ()) if str(item).strip()),
            candidate_digest=str(record.get("candidate_digest") or ""),
            reason_codes=reasons or current.reason_codes or (f"STATE_{target.upper()}",),
            snapshot_id=snapshot_id,
            updated_at=now,
        )
        if transition:
            commit = getattr(self.store, "commit_transition", None)
            if callable(commit):
                commit(state, transition)
            else:  # pragma: no cover - compatibility for external stores
                self.store.append_transition(transition)
                self.store.save_state(state)
        else:
            self.store.save_state(state)
        return state, transition

    def resolve_batch(
        self,
        records: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        summaries: Any,
        *,
        snapshot_id: str,
        actor: str = LOCAL_RULE_ACTOR,
        created_at: str | None = None,
    ) -> CandidateStateBatch:
        if not isinstance(records, (list, tuple)):
            raise CandidateStateError("候选记录必须是列表")
        summary_items = summaries.summaries if hasattr(summaries, "summaries") else summaries
        if summary_items is None:
            summary_items = ()
        summary_by_id: dict[str, Any] = {}
        for summary in summary_items:
            item = _summary_dict(summary)
            if item is None:
                continue
            key = str(item.get("candidate_id") or item.get("code") or "").strip()
            if key:
                summary_by_id[key] = summary
                if item.get("code"):
                    summary_by_id[str(item["code"])] = summary
        states: list[CandidateState] = []
        transitions: list[CandidateStateTransition] = []
        seen: set[str] = set()
        for record in records:
            candidate_id = _candidate_id(record)
            if candidate_id in seen:
                raise CandidateStateError(f"候选记录包含重复 candidate_id：{candidate_id}")
            seen.add(candidate_id)
            state, transition = self.resolve_one(
                record,
                summary_by_id.get(candidate_id) or summary_by_id.get(_code(record)),
                snapshot_id=snapshot_id,
                actor=actor,
                created_at=created_at,
            )
            states.append(state)
            if transition:
                transitions.append(transition)
        return CandidateStateBatch(
            snapshot_id=_required_text("snapshot_id", snapshot_id),
            states=tuple(states),
            transitions=tuple(transitions),
            candidate_ids=tuple(_candidate_id(record) for record in records),
            candidate_set_preserved=True,
        )

    def add_to_watch(
        self,
        candidate_id: str,
        *,
        actor: str,
        reason_codes: Any,
        snapshot_id: str,
        created_at: str | None = None,
    ) -> tuple[CandidateState, CandidateStateTransition]:
        current = self.store.get(_required_text("candidate_id", candidate_id))
        if current is None:
            raise CandidateStateError(f"找不到候选状态：{candidate_id}")
        transition = transition_candidate_state(
            current,
            "watch",
            reason_codes=reason_codes or (MANUAL_WATCH_ACTION,),
            actor=actor,
            snapshot_id=snapshot_id,
            created_at=created_at,
            manual=True,
        )
        state = CandidateState(
            candidate_id=current.candidate_id,
            code=current.code,
            status="watch",
            lens_ids=current.lens_ids,
            candidate_digest=current.candidate_digest,
            reason_codes=transition.reason_codes,
            snapshot_id=snapshot_id,
            updated_at=transition.created_at,
        )
        commit = getattr(self.store, "commit_transition", None)
        if callable(commit):
            commit(state, transition)
        else:  # pragma: no cover - compatibility for external stores
            self.store.append_transition(transition)
            self.store.save_state(state)
        return state, transition


__all__ = [
    "CANDIDATE_STATE_CONTRACT_VERSION",
    "CandidateState",
    "CandidateStateBatch",
    "CandidateStateError",
    "CandidateStateService",
    "CandidateStateStore",
    "CandidateStateTransition",
    "LOCAL_RULE_ACTOR",
    "MANUAL_WATCH_ACTION",
    "MemoryCandidateStateStore",
    "SqliteCandidateStateStore",
    "transition_candidate_state",
]
