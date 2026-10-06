"""Regression tests for fact-level, not batch-level, quality status."""

from __future__ import annotations

from datetime import datetime, timezone
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.factor_store import FactorSnapshot, build_factor_snapshot_manifest


def test_partial_fact_cannot_be_verified() -> None:
    snapshot = FactorSnapshot(
        code="000001",
        name="样例",
        price=10,
        pe=12,
        roe=None,
        debt_ratio=30,
        data_date="2026-07-23",
        quote_updated_at="2026-07-24T09:00:00+00:00",
        updated_at="2026-07-24T09:00:00+00:00",
    )
    snapshot.missing_fields = "roe"
    manifest, facts = build_factor_snapshot_manifest(
        [snapshot],
        quote_updated_at=datetime(2026, 7, 24, 9, tzinfo=timezone.utc),
        total=1,
        failed=0,
        limit=1,
    )
    assert manifest.quality_status == "partial"
    summary = manifest.quality_summary_json
    assert '"fact_total"' in summary
    assert '"partial_fact_count"' in summary
    assert any(fact.field_name == "roe" and fact.quality_status == "partial" for fact in facts)


def test_formal_manifest_rejects_timestamp_fallback() -> None:
    snapshot = FactorSnapshot(
        code="000002",
        name="缺真实来源时间",
        price=10,
        roe=20,
        data_date="2026-07-23",
        updated_at="2026-07-24T09:00:00+00:00",
    )
    try:
        build_factor_snapshot_manifest(
            [snapshot],
            quote_updated_at=None,
            total=1,
            failed=0,
            limit=1,
            formal_mode=True,
        )
    except ValueError as exc:
        assert "available_at" in str(exc)
    else:
        raise AssertionError("formal manifest 不得用 updated_at/data_date 回退 available_at")


if __name__ == "__main__":
    test_partial_fact_cannot_be_verified()
    test_formal_manifest_rejects_timestamp_fallback()
    print("[OK] fact quality aggregation tests passed")
