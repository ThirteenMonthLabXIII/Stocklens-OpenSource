from __future__ import annotations

from research._testkit import raises, run_tests
from research.experiment_identity import ExperimentManifest, ResearchRun


def _create(**overrides):
    values = {
        "data_snapshot_id": "snapshot-1",
        "refresh_event_id": "refresh-1",
        "universe_version": "universe-1",
        "formula_version": "baseline-v1",
        "model_version": "fixed-baseline",
        "cost_parameter_version": "cost-v1",
        "train_start": "2019-01-01",
        "train_end": "2021-12-31",
        "validation_start": "2022-01-01",
        "validation_end": "2022-12-31",
        "test_start": "2023-01-01",
        "test_end": "2023-12-31",
        "code_environment_digest": "env-1",
        "random_seed": 7,
        "output_payload": {"rows": [1, 2, 3]},
        "metrics": {"rank_ic": 0.12},
        "artifact_paths": ["out/metrics.json"],
        "created_at": "2026-07-23T00:00:00+00:00",
    }
    values.update(overrides)
    return ExperimentManifest.create(**values)


def test_manifest_is_deterministic_and_binds_all_versions():
    first = _create()
    second = _create(created_at="2026-07-24T00:00:00+00:00")
    assert first.experiment_id == second.experiment_id
    assert first.metrics["rank_ic"] == 0.12
    assert ResearchRun is ExperimentManifest


def test_manifest_rejects_overlapping_windows():
    with raises(ValueError):
        _create(validation_start="2021-06-01")


def test_manifest_rejects_tampering():
    manifest = _create()
    with raises(ValueError):
        ExperimentManifest(
            experiment_id="0" * 64,
            data_snapshot_id=manifest.data_snapshot_id,
            refresh_event_id=manifest.refresh_event_id,
            universe_version=manifest.universe_version,
            formula_version=manifest.formula_version,
            model_version=manifest.model_version,
            cost_parameter_version=manifest.cost_parameter_version,
            train_start=manifest.train_start,
            train_end=manifest.train_end,
            validation_start=manifest.validation_start,
            validation_end=manifest.validation_end,
            test_start=manifest.test_start,
            test_end=manifest.test_end,
            code_environment_digest=manifest.code_environment_digest,
            random_seed=manifest.random_seed,
            output_digest=manifest.output_digest,
            metrics_json=manifest.metrics_json,
            artifact_paths=manifest.artifact_paths,
            created_at=manifest.created_at,
        )


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
