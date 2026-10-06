from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.ai_analyzer import AIConfig
from stock_tool.core.ai_client import parse_json_response
from stock_tool.core.cache import CacheStore
from stock_tool.core.data_fetcher import DataFetcher
from stock_tool.core.local_query_parser import parse_local_query
from stock_tool.core.nl_query import parse_natural_language_query
import stock_tool.core.nl_query as nl_query_module
from stock_tool.core.models import FinancialMetrics, MoneyFlowSnapshot, StockQuote
from stock_tool.core.query_schema import (
    apply_local_query_guardrails,
    QueryValidationError,
    query_spec_from_dict,
    query_spec_from_json,
    query_spec_to_dict,
    query_spec_to_screen_filter,
    query_spec_summary,
    resolve_fuzzy_terms,
    validate_query_spec,
)
from stock_tool.core.screener_engine import ScreenFilter, passes_filter, passes_technical_filter, screen_market
from stock_tool.core.technical_profile import TechnicalProfile


class FakeFetcher:
    def __init__(self) -> None:
        self.quotes = [
            StockQuote(code="000001", name="样例银行", price=10, market_cap=50_000_000_000, pe=8),
            StockQuote(code="000002", name="样例科技", price=20, market_cap=80_000_000_000, pe=55),
            StockQuote(code="000003", name="ST样例", price=5, market_cap=120_000_000_000, pe=20),
        ]
        self.metrics = {
            "000001": FinancialMetrics(roe=18, net_margin=12, debt_ratio=55, revenue_growth=22, profit_growth=25),
            "000002": FinancialMetrics(roe=10, net_margin=8, debt_ratio=40, revenue_growth=30, profit_growth=35),
            "000003": FinancialMetrics(roe=30, net_margin=20, debt_ratio=20, revenue_growth=50, profit_growth=50),
        }

    def spot_quotes(self):
        return self.quotes, None

    def financial_metrics(self, code: str):
        return self.metrics[code], None


class FakeMoneyFlowFetcher(FakeFetcher):
    def __init__(self, fail_money_flow: bool = False) -> None:
        super().__init__()
        self.fail_money_flow = fail_money_flow

    def market_money_flow(self, force: bool = False):
        if self.fail_money_flow:
            raise RuntimeError("mock money flow unavailable")
        return {
            "000001": 200_000_000,
            "000002": -100_000_000,
        }

    def money_flow(self, code: str, days: int = 20, force: bool = False):
        if self.fail_money_flow:
            raise RuntimeError("mock money flow unavailable")
        value = 200_000_000 if code == "000001" else -100_000_000
        return MoneyFlowSnapshot(code=code, rows=[{"main": value}], main_5d=value, summary="mock"), None


def _expect_ok(name: str, payload: dict) -> None:
    spec = query_spec_from_dict(payload)
    validate_query_spec(spec)
    filters = query_spec_to_screen_filter(spec)
    print(f"     摘要: {query_spec_summary(spec)}")
    print(f"[OK] {name}: {filters}")


def _expect_error(name: str, payload: dict) -> None:
    try:
        spec = query_spec_from_dict(payload)
        validate_query_spec(spec)
        query_spec_to_screen_filter(spec)
    except QueryValidationError as exc:
        print(f"[OK] {name}: 拒绝执行 -> {exc}")
        return
    raise AssertionError(f"{name} 应该失败，但通过了")


def run_local_tests() -> None:
    base = {
        "intent": "screen",
        "exclude": ["ST"],
        "scan_limit": 300,
        "result_limit": 80,
        "allow_missing_pe": True,
        "with_technical": True,
    }
    _expect_ok("roe >= 15", {**base, "conditions": [{"field": "roe", "op": ">=", "value": "15"}]})
    _expect_ok("debt_ratio <= 60", {**base, "conditions": [{"field": "debt_ratio", "op": "<=", "value": 60}]})
    _expect_ok("pe <= 40", {**base, "conditions": [{"field": "pe", "op": "<=", "value": 40}]})
    _expect_ok("pb <= 2", {**base, "conditions": [{"field": "pb", "op": "<=", "value": 2}]})
    _expect_ok("gross_margin >= 30", {**base, "conditions": [{"field": "gross_margin", "op": ">=", "value": 30}]})
    _expect_ok("trend_20d > 0", {**base, "conditions": [{"field": "trend_20d", "op": ">", "value": 0}]})
    _expect_ok("rsi between 30 and 70", {**base, "conditions": [{"field": "rsi", "op": "between", "value": 30, "value_to": 70}]})
    _expect_ok("box_score >= 60", {**base, "conditions": [{"field": "box_score", "op": ">=", "value": 60}]})
    _expect_ok("box_range_pct <= 25", {**base, "conditions": [{"field": "box_range_pct", "op": "<=", "value": 25}]})
    _expect_ok("industry_strength >= 60", {**base, "conditions": [{"field": "industry_strength", "op": ">=", "value": 60}]})
    _expect_ok(
        "market_cap_yi between 100 and 1000",
        {**base, "conditions": [{"field": "market_cap_yi", "op": "between", "value": "100", "value_to": "1000"}]},
    )
    _expect_error("未知字段", {**base, "conditions": [{"field": "dividend_yield", "op": ">=", "value": 2}]})
    _expect_error("非法操作符", {**base, "conditions": [{"field": "roe", "op": "约等于", "value": 15}]})
    _expect_error("PB等于2", {**base, "conditions": [{"field": "pb", "op": "=", "value": 2}]})
    _expect_error("非法数字", {**base, "conditions": [{"field": "roe", "op": ">=", "value": ""}]})
    _expect_error("非有限数字", {**base, "conditions": [{"field": "roe", "op": ">=", "value": float("nan")}]})
    _expect_error("AI返回股票列表", {**base, "conditions": [], "stocks": [{"code": "600000", "name": "示例"}]})
    _expect_error("AI返回代码列表", {**base, "conditions": [], "codes": ["600000"]})
    _expect_error("AI返回空结果字段", {**base, "conditions": [], "results": []})
    _expect_error("非screen意图", {**base, "intent": "recommend", "conditions": [{"field": "roe", "op": ">=", "value": 15}]})
    _expect_error(
        "between上下限反转",
        {**base, "conditions": [{"field": "market_cap_yi", "op": "between", "value": 1000, "value_to": 100}]},
    )
    _expect_error("scan_limit过大", {**base, "conditions": [], "scan_limit": 5001})
    _expect_error("result_limit过小", {**base, "conditions": [], "result_limit": 0})
    _expect_error(
        "不支持的between组合",
        {**base, "conditions": [{"field": "debt_ratio", "op": "between", "value": 30, "value_to": 60}]},
    )
    parsed = parse_json_response('prefix {"conditions": []} suffix')
    assert parsed == {"conditions": []}, "AI JSON兜底提取失败"
    for bad_json in ['{"conditions":[{"field":"roe","op":">=","value":NaN}]}', '{"conditions":[{"field":"roe","op":">=","value":Infinity}]}']:
        try:
            parse_json_response(bad_json)
        except ValueError:
            pass
        else:
            raise AssertionError("AI JSON 不应接受 NaN/Infinity")
        try:
            query_spec_from_json(bad_json)
        except QueryValidationError:
            pass
        else:
            raise AssertionError("策略 JSON 不应接受 NaN/Infinity")

    for malformed in [
        {**base, "conditions": [{"field": "roe", "op": ">=", "value": 15}], "clarifications": {"question": "请确认"}},
        {**base, "conditions": [], "exclude": "退市"},
        {**base, "conditions": [], "sort": {"field": "roe"}},
    ]:
        try:
            query_spec_from_dict(malformed)
        except QueryValidationError:
            pass
        else:
            raise AssertionError("策略 JSON 关键列表字段类型错误时应拒绝")

    exclude_spec = query_spec_from_dict({**base, "conditions": [], "exclude": []})
    assert exclude_spec.exclude == ["ST"], "exclude 为空时应默认排除ST"

    clarification_spec = query_spec_from_dict(
        {
            **base,
            "conditions": [],
            "raw_text": "找估值低、趋势好的股票",
            "clarifications": ["估值低，请明确PE阈值", {"description": "趋势好，请明确技术条件"}],
        }
    )
    apply_local_query_guardrails(clarification_spec)
    assert clarification_spec.needs_confirmation is False
    assert not clarification_spec.clarifications
    assert any("估值低" in item for item in clarification_spec.resolved_notes)
    assert any("趋势好" in item for item in clarification_spec.resolved_notes)
    print("[OK] exclude默认值与旧澄清吸收通过")

    _expect_error(
        "模糊条件要求确认",
        {**base, "conditions": [], "needs_confirmation": True, "clarifications": ["请确认估值低和趋势好的具体阈值"]},
    )
    vague_spec = query_spec_from_dict({**base, "conditions": [{"field": "pe", "op": "<=", "value": 50}], "raw_text": "找估值低、趋势好的股票"})
    apply_local_query_guardrails(vague_spec)
    validate_query_spec(vague_spec)
    assert vague_spec.needs_confirmation is False
    assert any(condition.field == "trend_20d" for condition in vague_spec.conditions)
    assert vague_spec.resolved_notes, "模糊词兜底应回写 resolved_notes"
    print("[OK] 本地模糊词兜底: 已转换为可执行条件")

    unsupported_spec = query_spec_from_dict(
        {**base, "conditions": [{"field": "roe", "op": ">=", "value": 15}], "raw_text": "找ROE高于15，股息率高于3的股票"}
    )
    apply_local_query_guardrails(unsupported_spec)
    assert unsupported_spec.needs_confirmation is False
    assert any("股息率" in item for item in unsupported_spec.resolved_notes)
    validate_query_spec(unsupported_spec)
    print("[OK] 本地不支持字段兜底: 软忽略并保留可执行条件")

    fuzzy_slice = query_spec_from_dict({**base, "conditions": [], "raw_text": "找回撤小、放量、低估、主力进场的股票"})
    resolve_fuzzy_terms(fuzzy_slice)
    got_conditions = {(item.field, item.op, item.value, item.value_to) for item in fuzzy_slice.conditions}
    assert ("pullback_pct", "<=", 15, None) in got_conditions
    assert ("volume_ratio", ">=", 1.5, None) in got_conditions
    assert ("pe", "<=", 30, None) in got_conditions
    assert ("main_5d_yi", ">", 0, None) in got_conditions
    assert fuzzy_slice.resolved_notes
    fuzzy_filters = query_spec_to_screen_filter(fuzzy_slice)
    assert fuzzy_filters.max_pullback_pct == 15
    assert fuzzy_filters.min_volume_ratio == 1.5
    assert fuzzy_filters.max_pe == 30
    assert fuzzy_filters.min_main_5d_yi == 0
    print("[OK] 第一可用切片模糊词解析与 ScreenFilter 映射通过")

    box_industry_slice = query_spec_from_dict(
        {**base, "conditions": [], "raw_text": "找箱体横盘、基本面稳健、行业强的股票"}
    )
    resolve_fuzzy_terms(box_industry_slice)
    box_industry_filters = query_spec_to_screen_filter(box_industry_slice)
    assert box_industry_filters.require_box_sideways is True
    assert box_industry_filters.min_box_score == 60
    assert box_industry_filters.min_industry_strength == 60
    assert box_industry_filters.min_roe == 10
    assert box_industry_filters.max_debt_ratio == 60
    assert any("箱体横盘" in item for item in box_industry_slice.resolved_notes)
    assert any("行业强" in item or "强势行业" in item for item in box_industry_slice.resolved_notes)
    print("[OK] 箱体横盘 + 基本面稳健 + 行业强 本地兜底通过")

    max_drawdown_slice = query_spec_from_dict({**base, "conditions": [], "raw_text": "找最大回撤小、放量的股票"})
    resolve_fuzzy_terms(max_drawdown_slice)
    got_conditions = {(item.field, item.op, item.value, item.value_to) for item in max_drawdown_slice.conditions}
    assert ("max_drawdown_30d", "<=", 20, None) in got_conditions
    assert ("volume_ratio", ">=", 1.5, None) in got_conditions
    assert not any(item.field == "pullback_pct" for item in max_drawdown_slice.conditions), "最大回撤小不应触发旧回撤小兜底"
    max_drawdown_filters = query_spec_to_screen_filter(max_drawdown_slice)
    assert max_drawdown_filters.max_max_drawdown_30d == 20
    assert max_drawdown_filters.max_pullback_pct is None

    pullback_slice = query_spec_from_dict({**base, "conditions": [], "raw_text": "找回撤小的股票"})
    resolve_fuzzy_terms(pullback_slice)
    got_conditions = {(item.field, item.op, item.value, item.value_to) for item in pullback_slice.conditions}
    assert ("pullback_pct", "<=", 15, None) in got_conditions
    assert not any(item.field.startswith("max_drawdown") for item in pullback_slice.conditions)
    print("[OK] 最大回撤兜底与旧回撤小并存通过")

    ma_rsi_spec = parse_local_query("RSI 在 30 到 70、均线多头")
    assert ma_rsi_spec is not None, "RSI区间+均线多头应走本地解析"
    ma_rsi_filters = query_spec_to_screen_filter(ma_rsi_spec)
    assert ma_rsi_filters.min_rsi == 30
    assert ma_rsi_filters.max_rsi == 70
    assert ma_rsi_filters.require_ma_bullish is True
    ma_rsi_summary = query_spec_summary(ma_rsi_spec)
    assert "RSI 在 30 到 70" in ma_rsi_summary
    assert "均线多头" in ma_rsi_summary
    print("[OK] RSI区间与均线多头本地解析映射通过")

    turnover_symbol_spec = parse_local_query("换手率>15%")
    assert turnover_symbol_spec is not None, "换手率符号比较应走本地解析"
    turnover_symbol_filters = query_spec_to_screen_filter(turnover_symbol_spec)
    assert turnover_symbol_filters.min_turnover == 15
    assert turnover_symbol_filters.strict_min_turnover is True
    print("[OK] 换手率符号比较本地解析通过")

    tech_filters = ScreenFilter(
        min_volume_ratio=1.5,
        max_pullback_pct=15,
        min_turnover=3,
        max_turnover=15,
        min_trend_20d=0,
        strict_min_trend_20d=True,
    )
    good_technical = TechnicalProfile(True, close=12, ma20=10, volume_ratio=1.8, pullback_pct=8)
    bad_technical = TechnicalProfile(True, close=9, ma20=10, volume_ratio=1.2, pullback_pct=18)
    assert passes_technical_filter(good_technical, StockQuote(code="1", name="通过", turnover=6), tech_filters)
    assert not passes_technical_filter(bad_technical, StockQuote(code="2", name="拒绝", turnover=6), tech_filters)
    assert not passes_technical_filter(good_technical, StockQuote(code="3", name="低换手", turnover=1), tech_filters)
    print("[OK] 量价技术过滤通过/拒绝边界通过")

    min_rsi_filters = query_spec_to_screen_filter(query_spec_from_dict({**base, "conditions": [{"field": "rsi", "op": ">=", "value": 30}]}))
    assert passes_technical_filter(TechnicalProfile(True, rsi14=30), StockQuote(code="4", name="RSI边界"), min_rsi_filters)
    assert not passes_technical_filter(TechnicalProfile(True, rsi14=29.9), StockQuote(code="5", name="RSI偏低"), min_rsi_filters)
    strict_max_rsi_filters = query_spec_to_screen_filter(query_spec_from_dict({**base, "conditions": [{"field": "rsi", "op": "<", "value": 70}]}))
    assert strict_max_rsi_filters.strict_max_rsi is True
    assert not passes_technical_filter(TechnicalProfile(True, rsi14=70), StockQuote(code="6", name="RSI边界"), strict_max_rsi_filters)
    assert passes_technical_filter(TechnicalProfile(True, rsi14=69.9), StockQuote(code="7", name="RSI未过热"), strict_max_rsi_filters)
    print("[OK] RSI筛选严格边界通过")

    money_filters = ScreenFilter(
        min_roe=None,
        max_debt_ratio=None,
        max_pe=None,
        min_main_5d_yi=0,
        strict_min_main_5d_yi=True,
        with_technical=False,
    )
    money_report = screen_market(FakeMoneyFlowFetcher(), money_filters)
    assert [item.quote.code for item in money_report.results] == ["000001"], "资金流过滤应只保留近5日主力净流入为正的候选"
    assert money_report.results[0].sort_values["main_5d_yi"] == 2
    money_down_report = screen_market(FakeMoneyFlowFetcher(fail_money_flow=True), money_filters)
    assert len(money_down_report.results) == 2, "资金接口不可用时应忽略主力条件，不中断筛选"
    assert any("资金数据暂不可用" in item for item in money_down_report.notes)
    assert any("无需配置 Tushare token" in item for item in money_down_report.notes)
    print("[OK] 资金流过滤与降级 notes 通过")

    spec = query_spec_from_dict(
        {
            **base,
            "conditions": [
                {"field": "roe", "op": ">=", "value": 15},
                {"field": "debt_ratio", "op": "<=", "value": 60},
                {"field": "pe", "op": "<=", "value": 40},
                {"field": "market_cap_yi", "op": "between", "value": 100, "value_to": 1000},
            ],
            "with_technical": False,
        }
    )
    filters = query_spec_to_screen_filter(spec)
    report = screen_market(FakeFetcher(), filters)
    assert report.scanned == 2, f"应该排除ST并扫描2只，实际 {report.scanned}"
    assert len(report.results) == 1, f"应该命中1只，实际 {len(report.results)}"
    assert report.results[0].quote.code == "000001", "完整链路命中股票不符合预期"
    print("[OK] QuerySpec -> ScreenFilter -> screen_market 假数据链路通过")

    strict_filters = query_spec_to_screen_filter(
        query_spec_from_dict({**base, "conditions": [{"field": "pe", "op": "<", "value": 100}]})
    )
    assert strict_filters.strict_max_pe is True
    assert not passes_filter(StockQuote(code="000004", name="边界PE", pe=100), FinancialMetrics(), strict_filters)
    assert passes_filter(StockQuote(code="000005", name="低PE", pe=99.9), FinancialMetrics(), strict_filters)

    strict_filters = query_spec_to_screen_filter(
        query_spec_from_dict({**base, "conditions": [{"field": "roe", "op": ">", "value": 15}]})
    )
    assert strict_filters.strict_min_roe is True
    assert not passes_filter(StockQuote(code="000006", name="边界ROE"), FinancialMetrics(roe=15), strict_filters)
    assert passes_filter(StockQuote(code="000007", name="高ROE"), FinancialMetrics(roe=15.1), strict_filters)
    print("[OK] 严格比较边界通过")

    zero_threshold_filters = query_spec_to_screen_filter(
        query_spec_from_dict({**base, "conditions": [{"field": "revenue_growth", "op": ">=", "value": 0}]})
    )
    assert not passes_filter(
        StockQuote(code="000008", name="负增长"),
        FinancialMetrics(revenue_growth=-1),
        zero_threshold_filters,
    ), "0 阈值不应被当作未设置条件"
    assert passes_filter(
        StockQuote(code="000009", name="零增长"),
        FinancialMetrics(revenue_growth=0),
        zero_threshold_filters,
    )
    print("[OK] 0阈值筛选边界通过")

    duplicate_filters = query_spec_to_screen_filter(
        query_spec_from_dict(
            {
                **base,
                "conditions": [
                    {"field": "roe", "op": ">=", "value": 10},
                    {"field": "roe", "op": ">", "value": 15},
                    {"field": "pe", "op": "<=", "value": 50},
                    {"field": "pe", "op": "<", "value": 40},
                ],
            }
        )
    )
    assert duplicate_filters.min_roe == 15
    assert duplicate_filters.strict_min_roe is True
    assert duplicate_filters.max_pe == 40
    assert duplicate_filters.strict_max_pe is True
    print("[OK] 同字段重复条件取更严格阈值通过")

    sorted_spec = query_spec_from_dict(
        {**base, "conditions": [], "sort": [{"field": "price", "direction": "asc"}], "with_technical": False}
    )
    sorted_report = screen_market(FakeFetcher(), query_spec_to_screen_filter(sorted_spec))
    assert [item.quote.code for item in sorted_report.results] == ["000001", "000002"], "sort price asc 应影响执行顺序"

    custom_exclude_spec = query_spec_from_dict({**base, "conditions": [], "exclude": ["科技"], "with_technical": False})
    custom_exclude_report = screen_market(FakeFetcher(), query_spec_to_screen_filter(custom_exclude_spec))
    assert [item.quote.code for item in custom_exclude_report.results] == ["000001"], "自定义 exclude 应参与执行过滤"
    print("[OK] sort 与自定义 exclude 执行通过")

    local_samples = [
        ("找ROE高于15，负债率低于60，PE低于40的股票", True),
        ("找市值100亿到1000亿，PE低于50的股票", True),
        ("找净利率大于10，营收增长大于20的股票", True),
        ("找PB低于2，ROE高于15的股票", True),
        ("找市净率不高于3，销售毛利率不低于25的股票", True),
        ("找毛利率高于30，负债率低于60的股票", True),
        ("找近20日趋势大于0，PE低于40的股票", True),
        ("找站上20日线，PE低于40的股票", True),
        ("找收盘价高于20日均线，PE低于40的股票", True),
        ("找净资产收益率大于等于15，资产负债率小于等于60的股票", True),
        ("找资产负债率不超过60，市盈率不高于40的股票", True),
        ("找净利润增长超过30，营收增长至少20的股票", True),
        ("找估值低、趋势好的股票", True),
        ("找PE等于20的股票", False),
        ("找PB等于2的股票", False),
        ("找ROE高于15或者PE低于20的股票", False),
        ("找ROE高于15，换手率高于5的股票", True),
        ("找换手率高于5的股票", True),
        ("找MACD金叉，RSI低位的股票", True),
        ("找回撤小、放量、低估、主力进场的股票", True),
        ("找最大回撤小、放量的股票", True),
        ("找最大回撤<=20的股票", True),
        ("找30日回撤低于15%的股票", True),
        ("找60日回撤小的股票", True),
        ("找回撤低于10%，量比高于2，换手3-8%的股票", True),
        ("找RSI在30到70，均线多头的股票", True),
        ("找箱体横盘、基本面稳健、行业强的股票", True),
        ("找震荡整理，行业靠前的股票", True),
        ("找箱体评分不低于70，行业强度不低于65的股票", True),
    ]
    for text, executable in local_samples:
        spec = parse_local_query(text)
        assert spec is not None, f"本地解析应返回 QuerySpec: {text}"
        print(f"     本地摘要: {query_spec_summary(spec)}")
        try:
            query_spec_to_screen_filter(spec)
        except QueryValidationError as exc:
            if executable:
                raise AssertionError(f"本地解析结果应可执行: {text} -> {exc}") from exc
            print(f"[OK] 本地解析拒绝执行: {text} -> {exc}")
        else:
            if not executable:
                raise AssertionError(f"本地解析结果应拒绝执行: {text}")
            print(f"[OK] 本地解析可执行: {text}")

    print("[OK] 本地自然语言解析覆盖通过")

    explicit_max_drawdown = parse_local_query("找最大回撤<=20，30日回撤低于15%，60日回撤低于20%的股票")
    assert explicit_max_drawdown is not None
    explicit_filters = query_spec_to_screen_filter(explicit_max_drawdown)
    assert explicit_filters.max_max_drawdown_30d == 15
    assert explicit_filters.max_max_drawdown_60d == 20
    assert explicit_filters.max_pullback_pct is None
    print("[OK] 最大回撤显式本地解析映射通过")

    local_box_industry = parse_local_query("找箱体评分不低于70，行业强度不低于65，包含行业银行、证券，排除行业地产")
    assert local_box_industry is not None
    local_box_filters = query_spec_to_screen_filter(local_box_industry)
    assert local_box_filters.min_box_score == 70
    assert local_box_filters.min_industry_strength == 65
    assert local_box_filters.require_box_sideways is False, "显式箱体评分阈值不应误触发箱体横盘默认条件"
    assert local_box_filters.include_industries == ["银行", "证券"]
    assert local_box_filters.exclude_industries == ["地产"]
    print("[OK] 箱体/行业显式阈值与行业名单本地解析通过")

    explicit_low_thresholds = parse_local_query("找箱体评分不低于50，行业强度不低于30的股票")
    assert explicit_low_thresholds is not None
    explicit_low_filters = query_spec_to_screen_filter(explicit_low_thresholds)
    assert explicit_low_filters.min_box_score == 50
    assert explicit_low_filters.min_industry_strength == 30
    assert explicit_low_filters.require_box_sideways is False

    strong_industry = parse_local_query("找强势行业的股票")
    assert strong_industry is not None
    strong_industry_filters = query_spec_to_screen_filter(strong_industry)
    assert strong_industry_filters.min_industry_strength == 60
    assert strong_industry_filters.min_trend_20d is None, "强势行业不应误触发个股趋势强势"
    print("[OK] 显式箱体/行业阈值不被模糊词覆盖")

    local_sort_cases = [
        ("找ROE高于15，并按ROE从高到低排序的股票", [("roe", "desc")]),
        ("找PE低于40，按市值从低到高排列", [("market_cap_yi", "asc")]),
        ("按综合评分排序选股", [("local_score", "desc")]),
        ("找净利率大于10的股票，市值最大的优先", [("market_cap_yi", "desc")]),
    ]
    for text, expected in local_sort_cases:
        spec = parse_local_query(text)
        assert spec is not None, f"本地排序解析应返回 QuerySpec: {text}"
        got = [(rule.field, rule.direction) for rule in spec.sort]
        assert got == expected, f"本地排序解析不符: {text} -> {got}，期望 {expected}"
        query_spec_to_screen_filter(spec)
        print(f"     本地摘要: {query_spec_summary(spec)}")
        print(f"[OK] 本地排序解析可执行: {text} -> {got}")

    for none_text in ["帮我看看今天的行情", "你好", "推荐几只股票看看"]:
        assert parse_local_query(none_text) is None, f"无明确筛选意图应回退AI: {none_text}"
    print("[OK] 本地排序解析与AI回退路径通过")


def _mock_ai_sequence(contents: list[str]):
    calls = {"count": 0}

    def fake_request_ai_text(_config, _prompt, max_tokens=1000, timeout=60):
        idx = calls["count"]
        calls["count"] += 1
        if idx >= len(contents):
            raise AssertionError("AI mock 调用次数超过预期")
        return contents[idx], f"mock cost {idx + 1}"

    return calls, fake_request_ai_text


def run_ai_mock_tests() -> None:
    original = nl_query_module.request_ai_text
    config = AIConfig(provider="deepseek", model="", api_key="test-key")
    base_json = {
        "intent": "screen",
        "conditions": [{"field": "roe", "op": ">=", "value": 15}],
        "exclude": ["ST"],
        "scan_limit": 300,
        "result_limit": 80,
        "allow_missing_pe": True,
        "with_technical": True,
        "needs_confirmation": False,
        "clarifications": [],
        "raw_text": "找ROE高于15的股票",
    }

    try:
        calls, fake = _mock_ai_sequence(["不是JSON", json.dumps(base_json, ensure_ascii=False)])
        nl_query_module.request_ai_text = fake
        spec, cost_note = parse_natural_language_query(config, "找ROE高于15的股票")
        assert spec.conditions[0].field == "roe"
        assert calls["count"] == 2
        assert "自动修复重试一次" in cost_note

        unknown = {**base_json, "conditions": [{"field": "unknown_factor", "op": ">=", "value": 1}]}
        fixed = {**base_json, "conditions": [{"field": "pb", "op": "<=", "value": 2}]}
        calls, fake = _mock_ai_sequence([json.dumps(unknown, ensure_ascii=False), json.dumps(fixed, ensure_ascii=False)])
        nl_query_module.request_ai_text = fake
        spec, _cost_note = parse_natural_language_query(config, "找PB低于2的股票")
        assert spec.conditions[0].field == "pb"
        assert calls["count"] == 2

        stock_list = {**base_json, "conditions": [], "stocks": [{"code": "600000", "name": "示例"}]}
        calls, fake = _mock_ai_sequence([json.dumps(stock_list, ensure_ascii=False), json.dumps(stock_list, ensure_ascii=False)])
        nl_query_module.request_ai_text = fake
        try:
            parse_natural_language_query(config, "推荐几只股票")
        except QueryValidationError as exc:
            assert "AI返回了股票列表" in str(exc)
        else:
            raise AssertionError("AI 返回股票列表应最终拒绝")
        assert calls["count"] == 2

        guessed = {**base_json, "conditions": [{"field": "pe", "op": "<=", "value": 30}], "raw_text": ""}
        calls, fake = _mock_ai_sequence([json.dumps(guessed, ensure_ascii=False)])
        nl_query_module.request_ai_text = fake
        spec, _cost_note = parse_natural_language_query(config, "找估值低的股票")
        assert spec.needs_confirmation is False
        assert any("估值低" in item for item in spec.resolved_notes)
        assert calls["count"] == 1

        ai_omitted_unsupported = {**base_json, "conditions": [{"field": "roe", "op": ">=", "value": 15}], "raw_text": "找ROE高于15的股票"}
        calls, fake = _mock_ai_sequence([json.dumps(ai_omitted_unsupported, ensure_ascii=False)])
        nl_query_module.request_ai_text = fake
        spec, _cost_note = parse_natural_language_query(config, "找ROE高于15，股息率高于3的股票")
        assert spec.needs_confirmation is False
        assert any("股息率" in item for item in spec.resolved_notes)
        assert calls["count"] == 1

        for message in [
            "AI 请求过快或额度限制，请稍后重试",
            "AI 请求超时，请稍后重试",
            "AI API Key 无效或权限不足，请检查设置页中的 API Key",
        ]:
            def failing_request(_config, _prompt, max_tokens=1000, timeout=60, message=message):
                raise ValueError(message)

            nl_query_module.request_ai_text = failing_request
            try:
                parse_natural_language_query(config, "找ROE高于15的股票")
            except ValueError as exc:
                assert message in str(exc)
            else:
                raise AssertionError("AI API 错误应给出可读提示")
    finally:
        nl_query_module.request_ai_text = original

    print("[OK] AI mock 加固测试通过")


def run_ai_tests(provider: str, model: str, api_key: str) -> None:
    samples = [
        "找ROE高于15，负债率低于60，PE低于40的股票",
        "找净利率大于10，营收增长大于20的股票",
        "找市值100亿到1000亿，PE低于50的股票",
        "找估值低、趋势好的股票",
    ]
    config = AIConfig(provider=provider, model=model, api_key=api_key)
    for text in samples:
        print(f"\n[AI] {text}")
        spec, cost_note = parse_natural_language_query(config, text)
        print(json.dumps(query_spec_to_dict(spec), ensure_ascii=False, indent=2))
        print("摘要:", query_spec_summary(spec))
        try:
            validate_query_spec(spec)
            filters = query_spec_to_screen_filter(spec)
            print(f"可执行 ScreenFilter: {filters}")
        except QueryValidationError as exc:
            print(f"拒绝直接执行: {exc}")
        print(cost_note)


def run_chain(provider: str, model: str, api_key: str, text: str, with_technical: bool = False) -> None:
    config = AIConfig(provider=provider, model=model, api_key=api_key)
    spec, cost_note = parse_natural_language_query(config, text)
    spec.scan_limit = 50
    spec.result_limit = 10
    spec.with_technical = with_technical
    filters = query_spec_to_screen_filter(spec)
    report = screen_market(DataFetcher(CacheStore()), filters)
    print(json.dumps(query_spec_to_dict(spec), ensure_ascii=False, indent=2))
    print("摘要:", query_spec_summary(spec))
    print(filters)
    print(f"扫描 {report.scanned} 只，命中 {len(report.results)} 只，失败 {report.failed} 只")
    if report.errors:
        print("失败样例:")
        for error in report.errors:
            print(f"- {error}")
    for item in report.results:
        print(f"{item.quote.code} {item.quote.name} 本地分={item.local_score} 理由={'；'.join(item.reasons)}")
    print(cost_note)


def main() -> None:
    parser = argparse.ArgumentParser(description="自然语言策略解析第一阶段测试")
    parser.add_argument("--ai", action="store_true", help="调用AI做手动解析测试")
    parser.add_argument("--chain", action="store_true", help="调用AI并跑通 screen_market 小样本链路")
    parser.add_argument("--provider", default="deepseek", choices=["deepseek", "openai"])
    parser.add_argument("--model", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--text", default="找ROE高于15，负债率低于60，PE低于40的股票")
    parser.add_argument("--with-technical", action="store_true", help="链路测试中计算技术画像，速度会明显变慢")
    args = parser.parse_args()

    run_local_tests()
    run_ai_mock_tests()
    if args.ai:
        if not args.api_key:
            raise SystemExit("调用AI测试需要传入 --api-key")
        run_ai_tests(args.provider, args.model, args.api_key)
    if args.chain:
        if not args.api_key:
            raise SystemExit("调用AI测试需要传入 --api-key")
        run_chain(args.provider, args.model, args.api_key, args.text, with_technical=args.with_technical)


if __name__ == "__main__":
    main()
