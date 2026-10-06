"""Stable identities for reproducible research runs.

The identity deliberately combines the research code revision, ETL run
metadata, and content fingerprints of the point-in-time panel tables.  A
metric report without this identity is not sufficient to reproduce or audit a
historical experiment.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.panel_db import ResearchPanelDB


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class ExperimentManifest:
    """Identity contract for one reproducible research run."""

    experiment_id: str
    data_snapshot_id: str
    refresh_event_id: str
    universe_version: str
    formula_version: str
    model_version: str
    cost_parameter_version: str
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    test_start: str
    test_end: str
    code_environment_digest: str
    random_seed: int | None
    output_digest: str
    metrics_json: str
    artifact_paths: tuple[str, ...]
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        data_snapshot_id: str,
        refresh_event_id: str,
        universe_version: str,
        formula_version: str,
        model_version: str,
        cost_parameter_version: str,
        train_start: str,
        train_end: str,
        validation_start: str,
        validation_end: str,
        test_start: str,
        test_end: str,
        code_environment_digest: str,
        random_seed: int | None = None,
        output_payload: Any = None,
        output_digest: str = "",
        metrics: dict[str, Any] | None = None,
        artifact_paths: tuple[str, ...] | list[str] = (),
        created_at: str | None = None,
    ) -> "ExperimentManifest":
        values = {
            "data_snapshot_id": data_snapshot_id,
            "refresh_event_id": refresh_event_id,
            "universe_version": universe_version,
            "formula_version": formula_version,
            "model_version": model_version,
            "cost_parameter_version": cost_parameter_version,
            "train_start": train_start,
            "train_end": train_end,
            "validation_start": validation_start,
            "validation_end": validation_end,
            "test_start": test_start,
            "test_end": test_end,
            "code_environment_digest": code_environment_digest,
            "random_seed": random_seed,
            "output_digest": output_digest or hashlib.sha256(_canonical_json(output_payload).encode("utf-8")).hexdigest(),
            "metrics_json": _canonical_json(metrics or {}),
            "artifact_paths": tuple(sorted(str(path) for path in artifact_paths)),
        }
        cls._validate_values(values)
        identity = hashlib.sha256(_canonical_json(values).encode("utf-8")).hexdigest()
        return cls(
            experiment_id=identity,
            created_at=created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **values,
        )

    @property
    def metrics(self) -> dict[str, Any]:
        value = json.loads(self.metrics_json or "{}")
        return value if isinstance(value, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "data_snapshot_id": self.data_snapshot_id,
            "refresh_event_id": self.refresh_event_id,
            "universe_version": self.universe_version,
            "formula_version": self.formula_version,
            "model_version": self.model_version,
            "cost_parameter_version": self.cost_parameter_version,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "validation_start": self.validation_start,
            "validation_end": self.validation_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "code_environment_digest": self.code_environment_digest,
            "random_seed": self.random_seed,
            "output_digest": self.output_digest,
            "metrics": self.metrics,
            "artifact_paths": list(self.artifact_paths),
            "created_at": self.created_at,
        }

    def __post_init__(self) -> None:
        values = {
            "data_snapshot_id": self.data_snapshot_id,
            "refresh_event_id": self.refresh_event_id,
            "universe_version": self.universe_version,
            "formula_version": self.formula_version,
            "model_version": self.model_version,
            "cost_parameter_version": self.cost_parameter_version,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "validation_start": self.validation_start,
            "validation_end": self.validation_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "code_environment_digest": self.code_environment_digest,
            "random_seed": self.random_seed,
            "output_digest": self.output_digest,
            "metrics_json": self.metrics_json,
            "artifact_paths": tuple(self.artifact_paths),
        }
        self._validate_values(values)
        expected_id = hashlib.sha256(_canonical_json(values).encode("utf-8")).hexdigest()
        if self.experiment_id != expected_id:
            raise ValueError("experiment_id 与实验身份内容不匹配")
        json.loads(self.metrics_json or "{}")

    @staticmethod
    def _validate_values(values: dict[str, Any]) -> None:
        required = (
            "data_snapshot_id",
            "refresh_event_id",
            "universe_version",
            "formula_version",
            "model_version",
            "cost_parameter_version",
            "code_environment_digest",
        )
        if any(not str(values.get(key) or "").strip() for key in required):
            raise ValueError("实验身份缺少快照、刷新事件、版本或环境摘要")
        if values.get("random_seed") is not None and not isinstance(values["random_seed"], int):
            raise ValueError("random_seed 必须是整数")
        json.loads(values.get("metrics_json") or "{}")
        intervals = (
            ("train_start", "train_end"),
            ("validation_start", "validation_end"),
            ("test_start", "test_end"),
        )
        parsed: dict[str, datetime] = {}
        for start, end in intervals:
            for field in (start, end):
                text = str(values.get(field) or "").strip()
                if not text:
                    raise ValueError(f"实验区间缺少 {field}")
                try:
                    parsed[field] = datetime.fromisoformat(text)
                except ValueError as exc:
                    raise ValueError(f"实验区间不是 ISO 日期：{field}") from exc
            if parsed[start] > parsed[end]:
                raise ValueError(f"实验区间起止顺序错误：{start} > {end}")
        if parsed["train_end"] >= parsed["validation_start"]:
            raise ValueError("训练区间必须严格早于验证区间")
        if parsed["validation_end"] >= parsed["test_start"]:
            raise ValueError("验证区间必须严格早于测试区间")


ResearchRun = ExperimentManifest


def data_generation_fingerprint(db: ResearchPanelDB) -> dict[str, Any]:
    """Return the code/data provenance used by a research experiment."""
    research_root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    files = sorted(p for p in research_root.glob("*.py") if p.is_file())
    file_hashes = {}
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        file_hashes[path.name] = digest[:12]
        h.update(path.name.encode("utf-8"))
        h.update(digest.encode("ascii"))

    run_params = db.query_df(
        "SELECT key, value, updated_at FROM run_metadata WHERE key LIKE 'run_params:%' ORDER BY key"
    ).to_dict("records")
    debt_decision_text = db.get_metadata("debt_ratio_scale_decision", "")
    try:
        debt_decision = json.loads(debt_decision_text) if debt_decision_text else {}
    except json.JSONDecodeError:
        debt_decision = {"raw": debt_decision_text}

    run_ids = db.query_df(
        """
        SELECT api, run_id, MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS log_rows
        FROM etl_log
        GROUP BY api, run_id
        ORDER BY first_ts, api, run_id
        """
    ).to_dict("records")
    table_fingerprints = {}
    for table in ("universe", "trade_calendar", "industry_map", "fundamentals_pit", "prices_daily", "adjust_factor"):
        table_fingerprints[table] = db.table_fingerprint(table)

    cleanup_path = research_root / "out" / "phase_b_old_panel_summary.json"
    cleanup = {}
    if cleanup_path.exists():
        try:
            cleanup = json.loads(cleanup_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            cleanup = {"error": str(exc), "path": str(cleanup_path)}

    return {
        "etl_code_version_sha256": h.hexdigest(),
        "etl_code_files": file_hashes,
        "run_params": run_params,
        "debt_ratio_scale_decision": debt_decision,
        "old_panel_summary": cleanup,
        "task_run_ids": run_ids,
        "table_fingerprints": table_fingerprints,
        "unit_tests_are_not_sample_rerun": True,
        "sample_rerun_evidence": "etl_log/task_run_ids plus table counts/fingerprints in this report",
    }
