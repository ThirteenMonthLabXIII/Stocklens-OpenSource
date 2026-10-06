"""Metric metadata and quality rules for the Phase B research panel."""

from __future__ import annotations

from typing import Any

QUALITY_PASS = "pass"
QUALITY_MISSING = "missing"
QUALITY_SCALE_SUSPECT = "scale_suspect"

CORE_METRICS = (
    "roe",
    "net_margin",
    "gross_margin",
    "profit_growth",
    "debt_ratio",
    "revenue_growth",
)

METRIC_METADATA: dict[str, dict[str, Any]] = {
    "roe": {
        "source_api": "query_profit_data",
        "source_column": "roeAvg",
        "raw_scale": "decimal_ratio",
        "output_scale": "percent",
        "transform": "raw * 100",
        "quality_rule": "null source values are stored as missing",
    },
    "net_margin": {
        "source_api": "query_profit_data",
        "source_column": "npMargin",
        "raw_scale": "decimal_ratio",
        "output_scale": "percent",
        "transform": "raw * 100",
        "quality_rule": "null source values are stored as missing",
    },
    "gross_margin": {
        "source_api": "query_profit_data",
        "source_column": "gpMargin",
        "raw_scale": "decimal_ratio",
        "output_scale": "percent",
        "transform": "raw * 100",
        "quality_rule": "null source values are stored as missing",
    },
    "profit_growth": {
        "source_api": "query_growth_data",
        "source_column": "YOYPNI",
        "raw_scale": "decimal_ratio",
        "output_scale": "percent",
        "transform": "raw * 100",
        "quality_rule": "null source values are stored as missing",
    },
    "debt_ratio": {
        "source_api": "query_balance_data",
        "source_column": "liabilityToAsset",
        "raw_scale": "source_reported_unknown",
        "output_scale": "as_reported",
        "transform": "raw",
        "quality_rule": "adjacent same-year quarterly magnitude jumps over 20x are scale_suspect",
    },
    "revenue_growth": {
        "source_api": "query_profit_data",
        "source_column": "MBRevenue",
        "raw_scale": "currency_amount",
        "output_scale": "percent",
        "transform": "same-quarter YoY: (current / prior_year - 1) * 100",
        "quality_rule": "visible only at current period pubDate; missing base/current revenue is stored with missing_reason",
    },
}


def metric_metadata_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for metric, meta in METRIC_METADATA.items():
        row = {"metric": metric}
        row.update(meta)
        rows.append(row)
    return rows
