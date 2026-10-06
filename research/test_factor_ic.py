from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research._testkit import run_tests
from research.factor_ic import assign_size_bucket, grouped_rank_ic, rank_ic, summarize_ic


def test_rank_ic_perfect_positive_and_negative_direction():
    factor = pd.Series([1, 2, 3, 4, 5])
    returns = pd.Series([1, 2, 3, 4, 5])
    assert abs(rank_ic(factor, returns, "high") - 1.0) < 1e-12
    assert abs(rank_ic(factor, returns, "low") + 1.0) < 1e-12


def test_rank_ic_random_like_series_is_near_zero():
    factor = pd.Series([1, 2, 3, 4, 5, 6])
    returns = pd.Series([3, 6, 1, 5, 2, 4])
    assert abs(rank_ic(factor, returns, "high")) < 0.2


def test_rank_ic_ranks_after_pairwise_missing_drop():
    factor = pd.Series([1, 2, None, 4])
    returns = pd.Series([1, 2, 100, 4])
    assert abs(rank_ic(factor, returns, "high") - 1.0) < 1e-12


def test_grouped_rank_ic_removes_size_only_effect():
    frame = pd.DataFrame(
        {
            "factor": [10, 9, 8, 1, 2, 3],
            "forward": [0.09, 0.10, 0.08, -0.08, -0.10, -0.09],
            "size_bucket": ["small", "small", "small", "large", "large", "large"],
        }
    )
    raw = rank_ic(frame["factor"], frame["forward"], "high")
    neutral = grouped_rank_ic(frame, "factor", "forward", "size_bucket", "high")
    assert raw > 0.7
    assert abs(neutral) < 0.1


def test_assign_size_bucket_is_monotonic_and_handles_bad_values():
    buckets = assign_size_bucket(pd.Series([100, 200, None, -1, 300, 400, 500]), buckets=3)
    assert pd.isna(buckets.iloc[2])
    assert pd.isna(buckets.iloc[3])
    assert int(buckets.iloc[0]) <= int(buckets.iloc[1]) <= int(buckets.iloc[4]) <= int(buckets.iloc[5]) <= int(buckets.iloc[6])


def test_summarize_ic_reports_ir_and_t_stat():
    rows = pd.DataFrame(
        [
            {"factor": "a", "horizon_days": 20, "ic_type": "raw", "ic": 0.1},
            {"factor": "a", "horizon_days": 20, "ic_type": "raw", "ic": 0.2},
            {"factor": "a", "horizon_days": 20, "ic_type": "raw", "ic": 0.3},
        ]
    )
    summary = summarize_ic(rows)[0]
    assert summary["periods"] == 3
    assert summary["mean_ic"] == 0.20000000000000004
    assert summary["ir"] is not None
    assert summary["t_stat"] is not None


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
