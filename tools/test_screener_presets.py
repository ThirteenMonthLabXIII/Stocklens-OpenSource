from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.local_query_parser import parse_local_query
from stock_tool.core.query_schema import apply_local_query_guardrails, query_spec_to_screen_filter
from stock_tool.core.factor_store import FactorSnapshot
from stock_tool.core.screening_contract import assess_snapshot_quality, condition_hash
from stock_tool.core.screener_engine import ScreenFilter, screen_factor_snapshots
from stock_tool.core.screener_presets import builtin_screener_presets


def run_tests() -> None:
    presets = builtin_screener_presets()
    assert len(presets) == 8, f"应内置 8 张筛选预设卡，实际 {len(presets)} 张"

    keys = [preset.key for preset in presets]
    assert len(keys) == len(set(keys)), "预设 key 不应重复"

    for preset in presets:
        spec = parse_local_query(preset.nl_text)
        assert spec is not None, f"{preset.name} 应能被本地解析: {preset.nl_text}"
        spec = apply_local_query_guardrails(spec)
        assert spec.needs_confirmation is False, f"{preset.name} 不应需要二次确认"
        assert spec.conditions, f"{preset.name} 应解析出筛选条件"
        assert spec.resolved_notes, f"{preset.name} 应返回模糊词兜底说明"
        filters = query_spec_to_screen_filter(spec)
        if preset.key == "ma_bullish_breakout":
            assert filters.require_ma_bullish is True, "多头突破卡应启用均线多头过滤"
            assert filters.min_volume_ratio == 1.5, "多头突破卡应启用放量过滤"
            assert filters.min_trend_20d == 0, "多头突破卡应启用趋势向上过滤"
        if preset.key == "box_sideways_quality":
            assert filters.require_box_sideways is True, "箱体横盘优选卡应启用箱体横盘过滤"
            assert filters.min_box_score == 60, "箱体横盘优选卡应要求箱体评分"
            assert filters.max_box_range_pct == 25, "箱体横盘优选卡应限制箱体振幅"
            assert filters.box_window_days == 60, "箱体横盘优选卡应使用近60日窗口"
            assert filters.min_roe == 10, "箱体横盘优选卡应启用 ROE 过滤"
            assert filters.max_debt_ratio == 65, "箱体横盘优选卡应启用负债率过滤"
            assert filters.max_pe == 60, "箱体横盘优选卡应启用 PE 过滤"
            assert filters.min_industry_strength == 60, "箱体横盘优选卡应启用行业强度过滤"
        print(f"[OK] {preset.emoji} {preset.name}: {preset.nl_text}")

    print("[OK] 筛选预设卡片解析覆盖通过")


def test_structured_contract_and_quality_gate() -> None:
    presets = builtin_screener_presets()
    assert {item.template_version for item in presets} == {"v1"}
    assert all(item.template_id.startswith("a_share.") for item in presets)
    assert all(item.hard_conditions for item in presets)

    high_growth = next(item for item in presets if item.key == "high_growth")
    high_growth_fields = {item.field for item in high_growth.hard_conditions}
    assert high_growth_fields == {"revenue_growth", "trend_20d"}, "高成长 v1 不得悄悄加入利润增长或 ROE"
    ma = next(item for item in presets if item.key == "ma_bullish_breakout")
    ma_spec = ma.to_query_spec()
    ma_filters = query_spec_to_screen_filter(ma_spec)
    assert ma_filters.require_ma_bullish is True
    assert not any(item.field == "breakout_high" for item in ma.hard_conditions)

    missing_pe = FactorSnapshot(code="000001", name="缺 PE", roe=20, debt_ratio=30)
    complete = FactorSnapshot(code="000002", name="完整", pe=20, roe=20, debt_ratio=30)
    filters = ScreenFilter(
        min_roe=None,
        max_debt_ratio=None,
        max_pe=40,
        allow_missing_pe=False,
        with_technical=False,
    )
    report = screen_factor_snapshots([missing_pe, complete], filters)
    assert [item.quote.code for item in report.results] == ["000002"]
    quality = assess_snapshot_quality([missing_pe, complete], filters)
    assert quality.status == "unverified"
    assert "pe" in quality.missing_fields
    assert assess_snapshot_quality([missing_pe], filters).status == "blocked"

    money_filter = ScreenFilter(min_roe=None, max_debt_ratio=None, min_main_5d_yi=0, with_technical=False)
    assert assess_snapshot_quality([complete], money_filter).status == "blocked"
    assert assess_snapshot_quality([missing_pe], filters, execution_mode="review_only").status == "review_only"
    assert condition_hash(high_growth.to_query_spec()) == condition_hash(high_growth.to_query_spec())
    print("[OK] 结构化模板、关键字段闸门和复核状态通过")


if __name__ == "__main__":
    run_tests()
    test_structured_contract_and_quality_gate()
