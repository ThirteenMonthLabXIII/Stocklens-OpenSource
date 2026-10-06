from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stock_tool.core.query_schema import QuerySpec, query_spec_from_dict


@dataclass(frozen=True)
class TemplateCondition:
    field: str
    op: str
    value: float
    value_to: float | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"field": self.field, "op": self.op, "value": self.value}
        if self.value_to is not None:
            payload["value_to"] = self.value_to
        return payload


@dataclass(frozen=True)
class ScreenerPreset:
    key: str
    emoji: str
    name: str
    description: str
    nl_text: str
    template_id: str
    template_version: str
    research_hypothesis: str
    hard_conditions: tuple[TemplateCondition, ...]
    risk_conditions: tuple[str, ...]
    ranking_factors: tuple[str, ...]
    required_fields: tuple[str, ...]
    missing_data_policy: str
    execution_modes: tuple[str, ...]
    require_ma_bullish: bool = False
    require_box_sideways: bool = False
    box_window_days: int = 60
    universe_id: str = "a_share_listed_pit"
    signal_asof_policy: str = "snapshot_updated_at"
    observation_horizons: tuple[int, ...] = (3, 5, 10, 20)
    benchmark_id: str = "csi300"
    disable_rule: str = "数据契约不完整或成本后样本外表现恶化时停用"

    def to_query_spec(self) -> QuerySpec:
        """Build executable conditions from the immutable template contract."""

        return query_spec_from_dict(
            {
                "conditions": [condition.as_dict() for condition in self.hard_conditions],
                "sort": [{"field": field, "direction": "desc"} for field in self.ranking_factors],
                "allow_missing_pe": "pe" not in self.required_fields,
                "with_technical": True,
                "require_ma_bullish": self.require_ma_bullish,
                "require_box_sideways": self.require_box_sideways,
                "box_window_days": self.box_window_days,
                "raw_text": self.nl_text,
            }
        )

    def contract_text(self, *, data_source: str = "待运行", snapshot_asof: str = "待获取", status: str = "待运行") -> str:
        conditions = "、".join(
            f"{item.field} {item.op} {item.value:g}" + (f"~{item.value_to:g}" if item.value_to is not None else "")
            for item in self.hard_conditions
        ) or "无"
        return (
            f"模板：{self.name} · {self.template_version}\n"
            f"研究假设：{self.research_hypothesis}\n"
            f"硬条件：{conditions}\n"
            f"风险条件：{'、'.join(self.risk_conditions) or '无'}\n"
            f"排序因子：{'、'.join(self.ranking_factors) or '固定基线'}\n"
            f"必需字段：{'、'.join(self.required_fields) or '无'}\n"
            f"缺失策略：{self.missing_data_policy}\n"
            f"数据来源：{data_source}；快照日期：{snapshot_asof}\n"
            f"执行模式：正式筛选 / 历史复核；当前状态：{status}"
        )


def _preset(
    *,
    key: str,
    emoji: str,
    name: str,
    description: str,
    nl_text: str,
    research_hypothesis: str,
    hard_conditions: tuple[TemplateCondition, ...],
    risk_conditions: tuple[str, ...] = ("排除 ST / *ST",),
    ranking_factors: tuple[str, ...] = (),
    required_fields: tuple[str, ...] = (),
    require_ma_bullish: bool = False,
    require_box_sideways: bool = False,
    box_window_days: int = 60,
) -> ScreenerPreset:
    return ScreenerPreset(
        key=key,
        emoji=emoji,
        name=name,
        description=description,
        nl_text=nl_text,
        template_id=f"a_share.{key}",
        template_version="v1",
        research_hypothesis=research_hypothesis,
        hard_conditions=hard_conditions,
        risk_conditions=risk_conditions,
        ranking_factors=ranking_factors,
        required_fields=required_fields,
        missing_data_policy="关键字段缺失时阻断正式筛选；非关键字段标记 partial",
        execution_modes=("formal", "review_only"),
        require_ma_bullish=require_ma_bullish,
        require_box_sideways=require_box_sideways,
        box_window_days=box_window_days,
    )


BUILTIN_SCREENER_PRESETS = [
    _preset(
        key="main_inflow_dip",
        emoji="🔥",
        name="主力低吸",
        description="低估值+放量+主力净流入为正",
        nl_text="低估、放量、主力进场",
        research_hypothesis="低估值、量能改善且近5日主力资金净流入的股票进入观察池。",
        hard_conditions=(
            TemplateCondition("pe", "<=", 60),
            TemplateCondition("volume_ratio", ">=", 1.5),
            TemplateCondition("main_5d_yi", ">=", 0),
        ),
        ranking_factors=("main_5d_yi", "volume_ratio"),
        required_fields=("pe", "main_5d_yi"),
    ),
    _preset(
        key="strong_pullback",
        emoji="📈",
        name="强势小回撤",
        description="趋势向上且回撤可控",
        nl_text="趋势好、回撤小、放量",
        research_hypothesis="趋势保持向上、回撤受控且量能没有明显萎缩。",
        hard_conditions=(
            TemplateCondition("trend_20d", ">=", 0),
            TemplateCondition("pullback_pct", "<=", 15),
            TemplateCondition("volume_ratio", ">=", 1.5),
        ),
        ranking_factors=("trend_20d", "volume_ratio"),
    ),
    _preset(
        key="undervalued_value",
        emoji="💎",
        name="低估价值",
        description="低估值+财务稳健",
        nl_text="估值低、稳健",
        research_hypothesis="估值处于可研究区间，同时盈利能力和负债率满足稳健约束。",
        hard_conditions=(
            TemplateCondition("pe", "<=", 30),
            TemplateCondition("roe", ">=", 10),
            TemplateCondition("debt_ratio", "<=", 60),
        ),
        ranking_factors=("pe", "roe"),
        required_fields=("pe", "roe", "debt_ratio"),
    ),
    _preset(
        key="high_growth",
        emoji="🚀",
        name="高成长",
        description="营收高增+趋势向上",
        nl_text="高成长、趋势好",
        research_hypothesis="营收增长和近期趋势共同支持成长主题研究，不隐含利润增长或 ROE 条件。",
        hard_conditions=(
            TemplateCondition("revenue_growth", ">=", 20),
            TemplateCondition("trend_20d", ">=", 0),
        ),
        ranking_factors=("revenue_growth", "trend_20d"),
        required_fields=("revenue_growth",),
    ),
    _preset(
        key="bluechip_stable",
        emoji="🏦",
        name="龙头稳健",
        description="大市值+稳健",
        nl_text="龙头、稳健",
        research_hypothesis="大市值股票具备基本盈利和负债约束，作为稳健观察池。",
        hard_conditions=(
            TemplateCondition("market_cap_yi", ">=", 500),
            TemplateCondition("roe", ">=", 10),
            TemplateCondition("debt_ratio", "<=", 60),
        ),
        ranking_factors=("market_cap_yi", "roe"),
        required_fields=("market_cap_yi", "roe", "debt_ratio"),
    ),
    _preset(
        key="volume_price_rise",
        emoji="💧",
        name="量价齐升",
        description="放量+换手活跃+趋势好",
        nl_text="放量、换手活跃、趋势好",
        research_hypothesis="量能、换手和趋势同时改善，形成可观察的量价共振样本。",
        hard_conditions=(
            TemplateCondition("volume_ratio", ">=", 1.5),
            TemplateCondition("turnover", "between", 3, 15),
            TemplateCondition("trend_20d", ">=", 0),
        ),
        ranking_factors=("trend_20d", "volume_ratio", "turnover"),
    ),
    _preset(
        key="ma_bullish_breakout",
        emoji="🟧",
        name="多头突破",
        description="均线多头+放量+趋势向上",
        nl_text="均线多头、放量、趋势好",
        research_hypothesis="均线多头、量能放大且趋势向上的样本进入观察池；v1 不包含近期创新高条件。",
        hard_conditions=(
            TemplateCondition("volume_ratio", ">=", 1.5),
            TemplateCondition("trend_20d", ">=", 0),
        ),
        ranking_factors=("trend_20d", "volume_ratio"),
        require_ma_bullish=True,
    ),
    _preset(
        key="box_sideways_quality",
        emoji="▣",
        name="箱体横盘优选",
        description="箱体评分≥60+财务稳健+行业强",
        nl_text="近60日箱体横盘、箱体评分>=60、箱体振幅<=25、ROE>=10、负债率<=65、PE<=60、行业强度>=60",
        research_hypothesis="近60日箱体稳定、财务稳健且行业强度较高的样本进入观察池。",
        hard_conditions=(
            TemplateCondition("box_score", ">=", 60),
            TemplateCondition("box_range_pct", "<=", 25),
            TemplateCondition("roe", ">=", 10),
            TemplateCondition("debt_ratio", "<=", 65),
            TemplateCondition("pe", "<=", 60),
            TemplateCondition("industry_strength", ">=", 60),
        ),
        ranking_factors=("industry_strength", "box_score", "roe"),
        required_fields=("box_score", "box_range_pct", "roe", "debt_ratio", "pe", "industry_strength"),
        require_box_sideways=True,
        box_window_days=60,
    ),
]


def builtin_screener_presets() -> list[ScreenerPreset]:
    return list(BUILTIN_SCREENER_PRESETS)
