"""Build the strict evidence package used by a batch screening run.

The Qt layer owns presentation, while this module keeps the screening-to-Agent
seam deterministic and testable.  Only the selected candidate records and
snapshot-global facts cross the seam; legacy trade-reference fields never do.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from stock_tool.core.data_snapshot import DataSnapshotFact, DataSnapshotManifest
from stock_tool.core.research_discussion import build_evidence_package
from stock_tool.core.screening_contract import stable_hash


def _code(value: Any) -> str:
    return str(value or "").strip().zfill(6)


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    else:
        values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _quality_summary(manifest: DataSnapshotManifest, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    try:
        supplied = json.loads(manifest.quality_summary_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        supplied = {}
    if not isinstance(supplied, Mapping):
        supplied = {}
    return {
        "snapshot_status": manifest.quality_status,
        "snapshot_asof": manifest.asof_at,
        "availability_cutoff": manifest.availability_cutoff,
        "data_source": manifest.data_source,
        "quality_summary": dict(supplied),
        "candidate_count": len(rows),
        "candidate_gap_count": sum(
            bool(row.get("missing_fields") or row.get("stale_fields")) for row in rows
        ),
    }


def build_screening_evidence_package(
    *,
    manifest: DataSnapshotManifest,
    facts: Iterable[DataSnapshotFact | Mapping[str, Any]],
    rows: Iterable[Mapping[str, Any]],
    refresh_event_id: str = "",
    lens_version: str = "candidate_lens.v1",
    batch_range: tuple[int, int] | list[int] | None = None,
) -> dict[str, Any]:
    """Create one strict package covering the complete deterministic result set.

    The package is deliberately bounded by the deterministic result rows.  It
    includes ``__market__``/``__industry__`` facts for the market Agent and
    facts for selected candidates only; unrelated universe facts are excluded.
    """

    if not isinstance(manifest, DataSnapshotManifest):
        raise TypeError("筛选证据包必须绑定 DataSnapshotManifest")
    normalized_rows: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ValueError(f"筛选候选行 {index} 必须是对象")
        code = _code(raw.get("code"))
        if not code or code == "000000":
            raise ValueError(f"筛选候选行 {index} 缺少 code")
        normalized_rows.append(
            {
                "candidate_id": _code(raw.get("candidate_id") or code),
                "code": code,
                "name": str(raw.get("name") or code),
                "lens_ids": _text_list(raw.get("lens_ids")),
                "eligibility_status": str(raw.get("eligibility_status") or "eligible"),
                "reason_codes": _text_list(raw.get("reason_codes")),
                "missing_fields": _text_list(raw.get("missing_fields")),
                "stale_fields": _text_list(raw.get("stale_fields")),
                "candidate_digest": str(raw.get("candidate_digest") or ""),
                "data_snapshot_id": str(raw.get("data_snapshot_id") or manifest.snapshot_id),
            }
        )
    if not normalized_rows:
        raise ValueError("没有候选行，不能构造筛选证据包")
    if len(normalized_rows) > 30:
        raise ValueError("正式筛选证据包最多包含 30 只候选")
    codes = {row["code"] for row in normalized_rows}

    normalized_facts: list[DataSnapshotFact | Mapping[str, Any]] = []
    for fact in facts:
        raw_code = _code(fact.code if isinstance(fact, DataSnapshotFact) else fact.get("code"))
        if raw_code in codes or raw_code in {"__market__", "__industry__"}:
            normalized_facts.append(fact)
    digest = stable_hash(normalized_rows)
    return build_evidence_package(
        data_snapshot_id=manifest.snapshot_id,
        refresh_event_id=str(refresh_event_id or f"snapshot:{manifest.snapshot_id}"),
        formula_version=str(lens_version),
        snapshot_metadata=manifest,
        facts=list(normalized_facts),
        candidate_records=normalized_rows,
        candidate_set_digest=digest,
        data_quality_summary=_quality_summary(manifest, normalized_rows),
        batch_range=batch_range,
    )


__all__ = ["build_screening_evidence_package"]
