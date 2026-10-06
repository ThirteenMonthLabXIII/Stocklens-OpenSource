from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research._testkit import run_tests
from research.etl_fundamentals import _apply_debt_ratio_quality, analyze_debt_ratio_scale, apply_debt_ratio_scale
from research.metric_metadata import QUALITY_PASS, QUALITY_SCALE_SUSPECT


def test_debt_ratio_scale_jump_is_marked_not_passed():
    rows = [
        {
            "code": "sh.600001",
            "stat_date": "2024-03-31",
            "pub_date": "2024-04-30",
            "metric": "debt_ratio",
            "value": 0.5,
            "source_api": "fixture",
            "metric_quality": QUALITY_PASS,
            "missing_reason": None,
            "fetched_at": "t",
        },
        {
            "code": "sh.600001",
            "stat_date": "2024-06-30",
            "pub_date": "2024-08-30",
            "metric": "debt_ratio",
            "value": 60.0,
            "source_api": "fixture",
            "metric_quality": QUALITY_PASS,
            "missing_reason": None,
            "fetched_at": "t",
        },
    ]
    _apply_debt_ratio_quality(rows)
    assert rows[0]["metric_quality"] == QUALITY_SCALE_SUSPECT
    assert rows[1]["metric_quality"] == QUALITY_SCALE_SUSPECT
    assert rows[0]["missing_reason"] == "adjacent_quarter_scale_jump_gt_20x"


def test_debt_ratio_stable_adjacent_quarters_stays_pass():
    rows = [
        {
            "code": "sh.600001",
            "stat_date": "2024-03-31",
            "pub_date": "2024-04-30",
            "metric": "debt_ratio",
            "value": 50.0,
            "source_api": "fixture",
            "metric_quality": QUALITY_PASS,
            "missing_reason": None,
            "fetched_at": "t",
        },
        {
            "code": "sh.600001",
            "stat_date": "2024-06-30",
            "pub_date": "2024-08-30",
            "metric": "debt_ratio",
            "value": 55.0,
            "source_api": "fixture",
            "metric_quality": QUALITY_PASS,
            "missing_reason": None,
            "fetched_at": "t",
        },
    ]
    _apply_debt_ratio_quality(rows)
    assert {row["metric_quality"] for row in rows} == {QUALITY_PASS}


def test_debt_ratio_decimal_distribution_is_scaled_to_percent():
    rows = [
        {"code": f"sh.60000{i}", "stat_date": "2024-03-31", "pub_date": "2024-04-30", "metric": "debt_ratio", "value": 0.4 + i / 100, "source_api": "fixture", "metric_quality": QUALITY_PASS, "missing_reason": None, "fetched_at": "t"}
        for i in range(8)
    ]
    rows.append({"code": "sh.600099", "stat_date": "2024-03-31", "pub_date": "2024-04-30", "metric": "debt_ratio", "value": 55.0, "source_api": "fixture", "metric_quality": QUALITY_PASS, "missing_reason": None, "fetched_at": "t"})
    decision = analyze_debt_ratio_scale(rows)
    assert decision["decision"] == "decimal_ratio_to_percent"
    apply_debt_ratio_scale(rows, decision)
    assert round(float(rows[0]["value"]), 6) == 40.0


def test_debt_ratio_mixed_distribution_blocks_silent_fix():
    rows = [
        {"code": f"sh.6000{i:02d}", "stat_date": "2024-03-31", "pub_date": "2024-04-30", "metric": "debt_ratio", "value": value, "source_api": "fixture", "metric_quality": QUALITY_PASS, "missing_reason": None, "fetched_at": "t"}
        for i, value in enumerate([0.4, 0.5, 0.6, 20.0, 30.0, 40.0, 250.0, 260.0])
    ]
    decision = analyze_debt_ratio_scale(rows)
    assert decision["decision"] == "blocked_mixed_scale"
    before = [row["value"] for row in rows]
    apply_debt_ratio_scale(rows, decision)
    assert [row["value"] for row in rows] == before


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
