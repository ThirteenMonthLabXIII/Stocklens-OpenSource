"""Demonstrate the real selection policy using synthetic in-memory inputs only."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.selection_policy import evaluate_selection


def synthetic_input() -> dict:
    # Quality/source markers below are simulated inputs to the policy engine.
    # They do not verify market data and never create formal screening receipts.
    return {
        "code": "000001", "name": "SYNTHETIC_SAMPLE",
        "board_id": "main_board", "status": "listed",
        "snapshot_id": "synthetic-demo-only", "as_of": "2026-07-23",
        "available_at": "2026-07-01", "listing_age_trading_days": 180,
        "valid_turnover_days_20d": 20, "valid_price_volume_days": 20,
        "median_turnover_value_20d": 30_000_000,
        "snapshot_quality_status": "verified", "financial_quality_status": "verified",
        "latest_report_age_days": 30, "report_date": "2026-06-30",
        "verified_report_periods": 4, "roe_ttm": 12,
        "net_profit_positive_periods": 4, "operating_cashflow_positive_periods": 4,
        "industry_sector": "synthetic manufacturing",
        "industry_classification_id": "synthetic", "industry_classification_version": "demo-v1",
        "industry_sample_count": 30, "peer_set_digest": "synthetic-peers",
        "debt_ratio": 50, "debt_sector_percentile": 50,
        "adjusted_price_series": True, "adjustment_factor_missing": False,
        "adjustment_factor": 1.0, "corporate_action_fact_id": "synthetic-ca",
        "adjustment_method": "split_adjusted", "valid_price_days_20d": 20,
        "valid_volume_days_20d": 20, "trend_20d": 5, "trend_60d": 2,
        "relative_strength_20d": 3, "technical_fields_quality": "verified",
        "events": [{
            "code": "000001", "event_title": "SYNTHETIC_REPORT_EVENT",
            "event_date": "2026-07-20", "available_at": "2026-07-20",
            "event_source": "synthetic", "source_tier": 1,
            "source_route_verified": True, "deduped": True,
        }],
    }


def demonstrate() -> dict:
    complete = synthetic_input()
    missing = deepcopy(complete)
    missing.pop("roe_ttm")
    st_sample = deepcopy(complete)
    st_sample["is_st"] = True
    future = deepcopy(complete)
    future["events"][0]["available_at"] = "2026-07-25"
    cases = {
        "complete_input": complete, "missing_roe": missing,
        "st_exclusion": st_sample, "future_event": future,
    }
    output = {
        "data_kind": "synthetic_in_memory_only",
        "notice": "模拟数据演示；不是实际行情、正式筛选凭证或投资建议。",
        "cases": {},
    }
    for name, item in cases.items():
        result = evaluate_selection(item, mode="formal", availability_cutoff="2026-07-23")
        output["cases"][name] = {
            "status": result.status,
            "matched_lenses": list(result.matched_lenses),
            "missing_fields": list(result.missing_fields),
            "reason_codes": list(result.reason_codes),
            "lenses": {
                lens.lens_id: {
                    "status": lens.status, "missing_fields": list(lens.missing_fields),
                    "reason_codes": list(lens.reason_codes),
                }
                for lens in result.lens_results
            },
        }
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable demo output")
    args = parser.parse_args()
    result = demonstrate()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["notice"])
        print("不需要服务器、行情接口或 API Key；不读取本地数据库。")
        labels = {
            "complete_input": "完整输入", "missing_roe": "缺失 ROE",
            "st_exclusion": "ST 排除", "future_event": "未来事件",
        }
        for name, case in result["cases"].items():
            lenses = ", ".join(f"{key}={value['status']}" for key, value in case["lenses"].items())
            print(f"{labels[name]}: {case['status']} | {lenses}")
        print("查看阶段0.5/阶段1文档和 tools/test_*.py，了解这些结果的政策边界。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
