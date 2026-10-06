from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research._testkit import raises, run_tests
from research.metrics_robustness import cscv_train_test_indices, pbo_cscv, period_sharpe_ratio, stationary_bootstrap_ci


def test_cscv_train_test_indices_embargo_purges_test_boundaries():
    splits = cscv_train_test_indices(12, n_splits=4, embargo=1)
    assert len(splits) == 6
    assert (splits[0][0], splits[0][1]) == ([7, 8, 9, 10, 11], [0, 1, 2, 3, 4, 5])
    assert (splits[-1][0], splits[-1][1]) == ([0, 1, 2, 3, 4], [6, 7, 8, 9, 10, 11])


def test_pbo_embargo_purges_train_rows_without_changing_default_api():
    returns = pd.DataFrame(
        {
            "stable": [0.02, 0.01, 0.02, 0.01, 0.02, 0.01, 0.02, 0.01],
            "fragile": [0.03, -0.02, 0.03, -0.02, 0.03, -0.02, 0.03, -0.02],
        }
    )
    baseline = pbo_cscv(returns, n_splits=4)
    purged = pbo_cscv(returns, n_splits=4, embargo=1)
    assert baseline["embargo"] == 0
    assert purged["embargo"] == 1
    assert purged["purged_train_rows"] > 0
    assert purged["splits"] <= baseline["splits"]


def test_pbo_rejects_negative_embargo():
    with raises(ValueError):
        pbo_cscv(pd.DataFrame({"a": [1, 2], "b": [2, 1]}), n_splits=2, embargo=-1)


def test_pbo_keeps_legacy_no_embargo_behavior():
    returns = pd.DataFrame(
        {
            "stable": [0.02, 0.01, 0.02, 0.01, 0.02, 0.01, 0.02, 0.01],
            "fragile": [0.03, -0.02, 0.03, -0.02, 0.03, -0.02, 0.03, -0.02],
        }
    )
    baseline = pbo_cscv(returns, n_splits=4, embargo=0)
    assert baseline["embargo"] == 0
    assert baseline["purged_train_rows"] == 0
    assert baseline["splits"] == 6


def test_stationary_bootstrap_ci_contains_known_mean_and_is_reproducible():
    returns = pd.Series([0.01, -0.01] * 20)
    first = stationary_bootstrap_ci(returns, lambda s: float(s.mean()), n_boot=300, expected_block=3, seed=7)
    second = stationary_bootstrap_ci(returns, lambda s: float(s.mean()), n_boot=300, expected_block=3, seed=7)
    assert first == second
    assert first["lower"] <= 0.0 <= first["upper"]
    assert first["n_obs"] == 40
    assert first["n_boot"] == 300
    assert first["prob_threshold"] == 0.0
    assert first["prob_n"] == 300
    assert 0.0 <= first["prob_gt_threshold"] <= 1.0


def test_stationary_bootstrap_ci_handles_sharpe_statistic():
    returns = pd.Series([0.02, -0.01, 0.01, 0.00, 0.03, -0.02] * 5)
    ci = stationary_bootstrap_ci(returns, period_sharpe_ratio, n_boot=200, expected_block=4, seed=11)
    assert ci["stat"] is not None
    assert ci["lower"] is not None
    assert ci["upper"] is not None
    assert ci["lower"] <= ci["upper"]


def test_stationary_bootstrap_probability_uses_bootstrap_samples():
    positive = pd.Series([0.01, 0.02, 0.015, 0.012] * 10)
    ci = stationary_bootstrap_ci(positive, lambda s: float(s.mean()), n_boot=200, expected_block=3, seed=19)
    assert ci["prob_gt_threshold"] > 0.5
    assert ci["prob_n"] == 200


def test_stationary_bootstrap_probability_is_near_half_for_symmetric_series():
    symmetric = pd.Series([0.01, -0.01] * 40)
    ci = stationary_bootstrap_ci(symmetric, lambda s: float(s.mean()), n_boot=500, expected_block=3, seed=23)
    assert 0.35 <= ci["prob_gt_threshold"] <= 0.65


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
