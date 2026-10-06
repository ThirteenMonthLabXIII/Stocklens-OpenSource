from __future__ import annotations

import math

from research.template_diagnostics import (
    METRIC_FIELDS,
    ParameterAttempt,
    build_template_diagnostics_report,
    pairwise_jaccard,
)


def _metrics(value: float) -> dict[str, float]:
    return {
        "gross_return": value,
        "net_return_after_cost": value - 0.01,
        "benchmark_excess": value - 0.02,
        "max_drawdown": -0.1,
        "turnover": 1.2,
        "sample_size": 12,
        "data_completeness": 0.98,
    }


def main() -> int:
    pools = {
        "a_share.main_inflow_dip": ["600000", "600001", "600001"],
        "a_share.strong_pullback": ["600001", "600002"],
        "a_share.high_growth": ["600002", "600003"],
    }
    overlap = pairwise_jaccard(pools, template_versions={key: "v1" for key in pools})
    assert len(overlap) == 3
    first = overlap[0]
    assert first["left_template_id"] == "a_share.high_growth"
    assert first["right_template_id"] == "a_share.main_inflow_dip"
    assert math.isclose(first["jaccard"], 0.0)

    attempts = [
        ParameterAttempt("train-001", "a_share.main_inflow_dip", "train", {"pe": 60}, _metrics(0.12)),
        ParameterAttempt("validate-001", "a_share.main_inflow_dip", "validate", {"pe": 50}, _metrics(0.08)),
        ParameterAttempt("holdout-001", "a_share.main_inflow_dip", "holdout", {"pe": 50}, _metrics(0.06)),
    ]
    report = build_template_diagnostics_report(
        pools,
        attempts,
        template_versions={key: "v1" for key in pools},
        snapshot_id="snap-001",
        snapshot_asof="2026-07-22",
        selected_attempt_id="validate-001",
        holdout_attempt_ids=["holdout-001"],
    )
    sensitivity = report["parameter_sensitivity"]
    assert sensitivity["attempt_count"] == 3
    assert sensitivity["selection"]["eligible_attempt_count"] == 2
    assert sensitivity["holdout"] == {
        "attempt_ids": ["holdout-001"],
        "isolated": True,
        "selection_forbidden": True,
    }
    assert set(sensitivity["attempts"][0]["metrics"]) == set(METRIC_FIELDS)
    assert report["report_hash"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
