from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research._testkit import run_tests
from research.panel_db import OUT_DIR
from research.rigor_battery import development_shrink_gate, development_shrink_lambda, shrink_product_weights, smoothstep_ramp, write_probe_log
from research.v2_experiment import unique_trial_count

TEST_PROBE_LOG = OUT_DIR / "v2_robustness_probes_selftest.jsonl"
TEST_TRIAL_LOG = OUT_DIR / "v2_trial_log_selftest_rigor.jsonl"
V2_SHRUNK_DEFAULT_JSON = OUT_DIR / "v2_shrunk_default.json"


def test_development_shrink_lambda_direction_gate_and_missing_inputs():
    good_mean_ci = {"stat": 0.01, "lower": -0.01, "upper": 0.03, "prob_gt_threshold": 0.8}
    good_sharpe_ci = {"stat": 0.2, "lower": -0.1, "upper": 0.4, "prob_gt_threshold": 0.8}
    assert development_shrink_lambda(0.2, 0.8, {"stat": -0.001, "prob_gt_threshold": 0.8}, good_sharpe_ci) == 0.0
    assert development_shrink_lambda(0.2, 0.8, good_mean_ci, {"stat": -0.1, "prob_gt_threshold": 0.8}) == 0.0
    assert development_shrink_lambda(None, 0.8, good_mean_ci, good_sharpe_ci) == 0.0
    assert development_shrink_lambda(0.2, None, good_mean_ci, good_sharpe_ci) == 0.0
    assert development_shrink_lambda(0.2, 0.8, {"stat": 0.01}, good_sharpe_ci) == 0.0


def test_development_shrink_lambda_soft_ramp_for_positive_evidence():
    weak_mean_ci = {"stat": 0.001, "lower": -0.01, "upper": 0.02, "prob_gt_threshold": 0.60}
    weak_sharpe_ci = {"stat": 0.01, "lower": -0.2, "upper": 0.3, "prob_gt_threshold": 0.60}
    weak = development_shrink_gate(0.4, 0.20, weak_mean_ci, weak_sharpe_ci)
    assert 0.0 < weak["lambda"] < weak["legacy_pbo_only_lambda"]
    assert weak["evidence"] < 1.0
    assert weak["emits_tilt"] is True

    strong_mean_ci = {"stat": 0.02, "lower": 0.0, "upper": 0.04, "prob_gt_threshold": 0.75}
    strong_sharpe_ci = {"stat": 0.4, "lower": 0.0, "upper": 0.8, "prob_gt_threshold": 0.75}
    assert development_shrink_lambda(0.25, 0.65, strong_mean_ci, strong_sharpe_ci) == 0.75


def test_smoothstep_ramp_boundaries():
    assert smoothstep_ramp(-1.0, 0.0, 2.0) == 0.0
    assert smoothstep_ramp(3.0, 0.0, 2.0) == 1.0
    assert smoothstep_ramp(1.0, 0.0, 2.0) == 0.5
    for value in [-1.0, 0.0, 0.5, 1.0, 2.0, 3.0]:
        out = smoothstep_ramp(value, 0.0, 2.0)
        assert 0.0 <= out <= 1.0


def test_shrink_weights_returns_baseline_when_development_gate_fails():
    baseline = {"fundamental": 0.3, "growth": 0.2, "safety": 0.2, "valuation": 0.2, "trend": 0.1}
    dev_best = {"fundamental": 0.2, "growth": 0.1, "safety": 0.2, "valuation": 0.4, "trend": 0.1}
    shrunk = shrink_product_weights(
        baseline,
        dev_best,
        0.4,
        dsr=0.1,
        mean_ci={"stat": -0.01, "lower": -0.01, "upper": 0.01, "prob_gt_threshold": 0.3},
        sharpe_ci={"stat": -0.2, "lower": -0.2, "upper": 0.2, "prob_gt_threshold": 0.3},
    )
    assert shrunk["lambda"] == 0.0
    assert shrunk["weights"] == baseline


def test_shrink_weights_uses_pbo_as_discount_cap_after_soft_gate():
    baseline = {"fundamental": 0.3, "growth": 0.2, "safety": 0.2, "valuation": 0.2, "trend": 0.1}
    dev_best = {"fundamental": 0.2, "growth": 0.1, "safety": 0.2, "valuation": 0.4, "trend": 0.1}
    shrunk = shrink_product_weights(
        baseline,
        dev_best,
        0.5,
        dsr=0.7,
        mean_ci={"stat": 0.01, "lower": 0.0, "upper": 0.03, "prob_gt_threshold": 0.8},
        sharpe_ci={"stat": 0.1, "lower": 0.0, "upper": 0.4, "prob_gt_threshold": 0.8},
    )
    mid = shrunk["weights"]
    assert shrunk["lambda"] == 0.5
    assert baseline["valuation"] < mid["valuation"] < dev_best["valuation"]
    assert abs(sum(mid.values()) - 1.0) < 1e-12


def test_current_shrunk_default_artifact_equals_contract_baseline():
    if not V2_SHRUNK_DEFAULT_JSON.exists():
        return
    payload = json.loads(V2_SHRUNK_DEFAULT_JSON.read_text(encoding="utf-8"))
    assert payload["confirmatory"] is False
    assert payload["lambda"] == 0.0
    assert payload["weights"] == payload["baseline"]
    gate = payload["shrink_gate"]
    assert gate["gate_type"] == "direction_soft_ramp_v1"
    assert gate["legacy_pbo_only_lambda"] == 0.6
    assert gate["emits_tilt"] is False
    for key in ("evidence", "evidence_components", "ramp_policy", "direction_gate", "failed_reasons"):
        assert key in gate


def test_probe_log_is_independent_from_selection_trial_count():
    for path in (TEST_PROBE_LOG, TEST_TRIAL_LOG):
        if path.exists():
            path.unlink()
    try:
        TEST_TRIAL_LOG.write_text(
            json.dumps({"config_hash": "a"}, ensure_ascii=False) + "\n"
            + json.dumps({"config_hash": "a"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        before = unique_trial_count(TEST_TRIAL_LOG)
        write_probe_log([{"id": "probe", "mean": 0.0}], TEST_PROBE_LOG)
        after = unique_trial_count(TEST_TRIAL_LOG)
        assert before == 1
        assert after == 1
        rows = TEST_PROBE_LOG.read_text(encoding="utf-8").splitlines()
        assert len(rows) == 1
        row = json.loads(rows[0])
        assert row["kind"] == "robustness_probe"
        assert row["selection_trial"] is False
        assert row["confirmatory"] is False
    finally:
        for path in (TEST_PROBE_LOG, TEST_TRIAL_LOG):
            if path.exists():
                path.unlink()


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
