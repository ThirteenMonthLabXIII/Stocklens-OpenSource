"""Research-only diagnostics for template overlap and parameter sensitivity.

The production Qt chain owns template execution.  This module deliberately
accepts serialized candidate pools and parameter attempts so research can
compare templates without importing or mutating the production runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
METRIC_FIELDS = (
    "gross_return",
    "net_return_after_cost",
    "benchmark_excess",
    "max_drawdown",
    "turnover",
    "sample_size",
    "data_completeness",
)
VALID_SPLITS = {"train", "validate", "holdout"}


def _json_default(value: Any) -> str:
    return str(value)


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ordered_codes(values: Iterable[Any]) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


@dataclass(frozen=True)
class ParameterAttempt:
    """One immutable parameter attempt that must remain in the ledger."""

    attempt_id: str
    template_id: str
    split: str
    parameters: Mapping[str, Any]
    metrics: Mapping[str, Any]
    status: str = "observed"
    selected: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        metrics = {field: self.metrics.get(field) for field in METRIC_FIELDS}
        extras = {
            str(key): value
            for key, value in self.metrics.items()
            if str(key) not in METRIC_FIELDS
        }
        return {
            "attempt_id": self.attempt_id,
            "template_id": self.template_id,
            "split": self.split,
            "parameters": dict(self.parameters),
            "metrics": metrics,
            "metric_extras": extras,
            "status": self.status,
            "selected": self.selected,
            "note": self.note,
            "attempt_hash": stable_hash(
                {
                    "attempt_id": self.attempt_id,
                    "template_id": self.template_id,
                    "split": self.split,
                    "parameters": dict(self.parameters),
                    "metrics": metrics,
                }
            ),
        }


def _validate_attempts(attempts: Sequence[ParameterAttempt]) -> None:
    seen: set[str] = set()
    for attempt in attempts:
        if not attempt.attempt_id:
            raise ValueError("parameter attempt_id 不能为空")
        if attempt.attempt_id in seen:
            raise ValueError(f"parameter attempt_id 重复：{attempt.attempt_id}")
        seen.add(attempt.attempt_id)
        if attempt.split not in VALID_SPLITS:
            raise ValueError(f"不支持的研究切分：{attempt.split}")
        if attempt.split == "holdout" and attempt.selected:
            raise ValueError("holdout 参数尝试不得直接成为最终选择")


def pairwise_jaccard(
    candidate_pools: Mapping[str, Iterable[Any]],
    *,
    template_versions: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return deterministic pairwise candidate-pool overlap rows."""

    normalized = {
        str(template_id): set(_ordered_codes(codes))
        for template_id, codes in candidate_pools.items()
    }
    versions = template_versions or {}
    rows: list[dict[str, Any]] = []
    for left_id, right_id in itertools.combinations(sorted(normalized), 2):
        left = normalized[left_id]
        right = normalized[right_id]
        intersection = left & right
        union = left | right
        rows.append(
            {
                "left_template_id": left_id,
                "left_template_version": versions.get(left_id),
                "right_template_id": right_id,
                "right_template_version": versions.get(right_id),
                "left_count": len(left),
                "right_count": len(right),
                "intersection_count": len(intersection),
                "union_count": len(union),
                "jaccard": len(intersection) / len(union) if union else 1.0,
            }
        )
    return rows


def build_parameter_sensitivity_report(
    attempts: Sequence[ParameterAttempt],
    *,
    selected_attempt_id: str | None = None,
    holdout_attempt_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a complete attempt ledger with an explicit holdout isolation gate."""

    attempts = tuple(attempts)
    _validate_attempts(attempts)
    by_id = {attempt.attempt_id: attempt for attempt in attempts}
    holdout_ids = {str(attempt_id) for attempt_id in holdout_attempt_ids}
    unknown_holdout = sorted(holdout_ids - set(by_id))
    if unknown_holdout:
        raise ValueError(f"holdout 参数尝试不存在：{unknown_holdout}")
    inferred_holdout = {attempt.attempt_id for attempt in attempts if attempt.split == "holdout"}
    if inferred_holdout != holdout_ids:
        raise ValueError(
            "holdout_attempt_ids 必须与 split=holdout 的全部尝试一致："
            f"expected={sorted(inferred_holdout)} actual={sorted(holdout_ids)}"
        )
    if selected_attempt_id is not None:
        selected_attempt = by_id.get(selected_attempt_id)
        if selected_attempt is None:
            raise ValueError(f"最终选择的参数尝试不存在：{selected_attempt_id}")
        if selected_attempt_id in holdout_ids:
            raise ValueError("最终参数选择不得来自 holdout")
    else:
        selected_attempt = None

    split_rows = {
        split: [attempt.attempt_id for attempt in attempts if attempt.split == split]
        for split in sorted(VALID_SPLITS)
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "metric_fields": list(METRIC_FIELDS),
        "attempt_count": len(attempts),
        "attempts": [attempt.as_dict() for attempt in attempts],
        "attempts_by_split": split_rows,
        "selection": {
            "selected_attempt_id": selected_attempt_id,
            "eligible_attempt_count": len(attempts) - len(holdout_ids),
            "source": "non_holdout_only",
        },
        "holdout": {
            "attempt_ids": sorted(holdout_ids),
            "isolated": selected_attempt_id not in holdout_ids,
            "selection_forbidden": True,
        },
    }


def build_template_diagnostics_report(
    candidate_pools: Mapping[str, Iterable[Any]],
    attempts: Sequence[ParameterAttempt],
    *,
    template_versions: Mapping[str, str] | None = None,
    snapshot_id: str | None = None,
    snapshot_asof: str | None = None,
    selected_attempt_id: str | None = None,
    holdout_attempt_ids: Iterable[str] = (),
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Combine overlap and sensitivity evidence into one research artifact."""

    overlap_rows = pairwise_jaccard(candidate_pools, template_versions=template_versions)
    sensitivity = build_parameter_sensitivity_report(
        attempts,
        selected_attempt_id=selected_attempt_id,
        holdout_attempt_ids=holdout_attempt_ids,
    )
    candidate_counts = {
        str(template_id): len(set(_ordered_codes(codes)))
        for template_id, codes in sorted(candidate_pools.items())
    }
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": "research_only",
        "snapshot_id": snapshot_id,
        "snapshot_asof": snapshot_asof,
        "template_versions": dict(sorted((template_versions or {}).items())),
        "candidate_counts": candidate_counts,
        "pairwise_overlap": overlap_rows,
        "parameter_sensitivity": sensitivity,
    }
    if generated_at:
        report["generated_at"] = generated_at
    report["report_hash"] = stable_hash(report)
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render an audit-friendly compact report without ranking a winner."""

    lines = [
        "# 模板候选池与参数敏感性诊断",
        "",
        f"- scope: `{report.get('scope', 'research_only')}`",
        f"- snapshot: `{report.get('snapshot_id') or 'unknown'}` / `{report.get('snapshot_asof') or 'unknown'}`",
        f"- report hash: `{report.get('report_hash', '')}`",
        "",
        "## 候选池重叠",
        "",
        "| 模板A | 模板B | A数量 | B数量 | 交集 | 并集 | Jaccard |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report.get("pairwise_overlap", []):
        lines.append(
            "| {left_template_id} | {right_template_id} | {left_count} | {right_count} | "
            "{intersection_count} | {union_count} | {jaccard:.4f} |".format(**row)
        )
    sensitivity = report.get("parameter_sensitivity", {})
    holdout = sensitivity.get("holdout", {})
    lines.extend(
        [
            "",
            "## 参数尝试留痕",
            "",
            f"- attempts: `{sensitivity.get('attempt_count', 0)}`",
            f"- selected: `{sensitivity.get('selection', {}).get('selected_attempt_id') or 'none'}`",
            f"- holdout isolated: `{holdout.get('isolated', False)}`",
            "",
            "每次参数尝试均保留；holdout 只用于最终隔离评估，不参与参数选择。",
        ]
    )
    return "\n".join(lines) + "\n"


def _load_attempts(payload: Iterable[Mapping[str, Any]]) -> list[ParameterAttempt]:
    return [
        ParameterAttempt(
            attempt_id=str(item["attempt_id"]),
            template_id=str(item["template_id"]),
            split=str(item["split"]),
            parameters=dict(item.get("parameters", {})),
            metrics=dict(item.get("metrics", {})),
            status=str(item.get("status", "observed")),
            selected=bool(item.get("selected", False)),
            note=str(item.get("note", "")),
        )
        for item in payload
    ]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成研究侧模板重叠与参数敏感性报告")
    parser.add_argument("input", type=Path, help="包含 candidate_pools 与 attempts 的 JSON")
    parser.add_argument("--output", type=Path, default=Path("research/out/template_diagnostics.json"))
    parser.add_argument("--markdown", type=Path, default=Path("research/out/template_diagnostics.md"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    report = build_template_diagnostics_report(
        payload.get("candidate_pools", {}),
        _load_attempts(payload.get("attempts", [])),
        template_versions=payload.get("template_versions"),
        snapshot_id=payload.get("snapshot_id"),
        snapshot_asof=payload.get("snapshot_asof"),
        selected_attempt_id=payload.get("selected_attempt_id"),
        holdout_attempt_ids=payload.get("holdout_attempt_ids", []),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(f"[OK] 模板诊断报告已生成：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
