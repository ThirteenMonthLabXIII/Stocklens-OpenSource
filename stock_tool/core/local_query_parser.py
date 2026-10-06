from __future__ import annotations

import re

from stock_tool.core.query_schema import (
    AMBIGUOUS_TERMS,
    Condition,
    QuerySpec,
    SortRule,
    UNSUPPORTED_FIELD_TERMS,
    apply_local_query_guardrails,
    validate_query_spec,
)


NUMBER_PATTERN = r"(-?\d+(?:\.\d+)?)"
FIELD_ALIASES = {
    "roe": [r"ROE", r"净资产收益率"],
    "net_margin": [r"净利率", r"销售净利率"],
    "debt_ratio": [r"资产负债率", r"负债率"],
    "pe_ttm": [r"PE\s*-?\s*TTM", r"滚动市盈率"],
    "pe": [r"PE(?!\s*-?\s*TTM)", r"市盈率", r"动态市盈率"],
    "pb": [r"PB", r"市净率"],
    "gross_margin": [r"毛利率", r"销售毛利率"],
    "revenue_growth": [r"营收增长率", r"营收增长", r"营业收入增长率", r"收入增长"],
    "profit_growth": [r"净利润增长率", r"净利润增长", r"利润增长"],
    "trend_20d": [r"近20日趋势", r"20日趋势"],
    "volume_ratio": [r"量比", r"放量"],
    "max_drawdown_30d": [r"近?30日最大回撤", r"30日最大回撤", r"最大回撤", r"30日回撤"],
    "max_drawdown_60d": [r"近?60日最大回撤", r"60日最大回撤", r"60日回撤"],
    "pullback_pct": [r"距20日前高回撤", r"20日前高回撤", r"回撤幅度", r"(?<!最大)(?<!30日)(?<!60日)回撤"],
    "turnover": [r"换手率", r"换手"],
    "main_5d_yi": [r"近5日主力净流入", r"5日主力净流入", r"主力净流入", r"主力流入"],
    "rsi": [r"RSI", r"相对强弱"],
    "box_score": [r"箱体评分", r"箱体得分"],
    "box_range_pct": [r"箱体振幅", r"箱体幅度"],
    "industry_strength": [r"行业强度", r"行业评分", r"行业得分"],
}
UNSUPPORTED_LOGIC_HINTS = ("或者", "或")

# 仅识别高置信度排序短语；模糊或多字段优先级表达继续交给 AI。
SORT_FIELD_ALIASES = {
    **FIELD_ALIASES,
    "market_cap_yi": [r"总市值", r"市值"],
    "price": [r"股价", r"现价", r"价格"],
    "local_score": [r"综合评分", r"综合得分", r"综合分", r"评分"],
}
SORT_ASC_HINTS = (r"从低到高", r"从小到大", r"由低到高", r"由小到大", r"升序", r"最低", r"最小")
SORT_DESC_HINTS = (r"从高到低", r"从大到小", r"由高到低", r"由大到小", r"降序", r"最高", r"最大")


def _normalize_text(text: str) -> str:
    return (
        text.strip()
        .replace("％", "%")
        .replace("＞", ">")
        .replace("＜", "<")
        .replace("＝", "=")
        .replace("，", ",")
        .replace("；", ",")
        .replace("、", ",")
    )


def _number(value: str) -> float:
    return float(value)


def _field_pattern(aliases: list[str]) -> str:
    return r"(?:%s)" % "|".join(aliases)


def _add_condition(conditions: list[Condition], field: str, op: str, value: float, value_to: float | None = None) -> None:
    candidate = Condition(field=field, op=op, value=value, value_to=value_to)
    if candidate not in conditions:
        conditions.append(candidate)


def _parse_market_cap(text: str, conditions: list[Condition]) -> None:
    pattern = re.compile(
        rf"(?:总?市值|市值)\s*(?:在|介于)?\s*{NUMBER_PATTERN}\s*亿?\s*(?:到|至|-|~|—)\s*{NUMBER_PATTERN}\s*亿?(?:之间)?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        value = _number(match.group(1))
        value_to = _number(match.group(2))
        _add_condition(conditions, "market_cap_yi", "between", value, value_to)


def _parse_turnover_range(text: str, conditions: list[Condition]) -> None:
    pattern = re.compile(
        rf"(?:换手率|换手)\s*(?:在|介于)?\s*{NUMBER_PATTERN}\s*%?\s*(?:到|至|-|~|—)\s*{NUMBER_PATTERN}\s*%?(?:之间)?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        value = _number(match.group(1))
        value_to = _number(match.group(2))
        _add_condition(conditions, "turnover", "between", value, value_to)


def _parse_rsi_range(text: str, conditions: list[Condition]) -> None:
    pattern = re.compile(
        rf"(?:RSI|相对强弱)\s*(?:在|介于)?\s*{NUMBER_PATTERN}\s*(?:到|至|-|~|—)\s*{NUMBER_PATTERN}\s*(?:之间)?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        value = _number(match.group(1))
        value_to = _number(match.group(2))
        _add_condition(conditions, "rsi", "between", value, value_to)


def _parse_field_conditions(text: str, conditions: list[Condition]) -> None:
    gt_ops = {
        ">=": r"(?:>=|=>|大于等于|高于等于|不低于|不少于|至少)",
        ">": r"(?:>|高于|大于|超过|超)",
    }
    lt_ops = {
        "<=": r"(?:<=|=<|小于等于|低于等于|不超过|不高于|不大于|至多|最高)",
        "<": r"(?:<|低于|小于|少于)",
    }
    eq_ops = {"=": r"(?:==|=|等于|为)"}
    all_ops = {**gt_ops, **lt_ops, **eq_ops}

    for field, aliases in FIELD_ALIASES.items():
        field_re = _field_pattern(aliases)
        for op, op_re in all_ops.items():
            pattern = re.compile(rf"{field_re}\s*{op_re}\s*{NUMBER_PATTERN}\s*%?", re.IGNORECASE)
            for match in pattern.finditer(text):
                _add_condition(conditions, field, op, _number(match.group(1)))


def _parse_trend_phrases(text: str, conditions: list[Condition]) -> None:
    phrases = [
        r"站上\s*20\s*日线",
        r"收盘价?\s*(?:高于|大于|站上|突破)\s*20\s*日均线",
        r"收盘价?\s*(?:高于|大于|站上|突破)\s*MA20",
    ]
    for phrase in phrases:
        if re.search(phrase, text, flags=re.IGNORECASE):
            _add_condition(conditions, "trend_20d", ">", 0)


def _clean_industry_token(value: str) -> str:
    token = value.strip()
    for suffix in ("行业", "板块", "股票", "个股"):
        if token.endswith(suffix) and len(token) > len(suffix):
            token = token[: -len(suffix)]
    return token.strip()


def _parse_industry_list_segment(segment: str) -> list[str]:
    stop_words = ("的股票", "的个股", "股票", "个股")
    for word in stop_words:
        segment = segment.replace(word, "")
    items = []
    for raw in re.split(r"[,/\s]+", segment):
        token = _clean_industry_token(raw)
        if token and token not in items:
            items.append(token)
    return items


def _extract_industry_list(text: str, markers: tuple[str, ...], stop_markers: tuple[str, ...]) -> list[str]:
    for marker in markers:
        start = text.find(marker)
        if start < 0:
            continue
        value_start = start + len(marker)
        value_end = len(text)
        for stop in stop_markers:
            stop_at = text.find(stop, value_start)
            if stop_at >= 0:
                value_end = min(value_end, stop_at)
        return _parse_industry_list_segment(text[value_start:value_end])
    return []


def _parse_industry_lists(text: str) -> tuple[list[str], list[str]]:
    include = _extract_industry_list(
        text,
        ("包含行业", "只看行业", "行业包含", "行业为", "行业是"),
        ("排除行业", "剔除行业", "不含行业", "排除板块", "剔除板块"),
    )
    exclude = _extract_industry_list(
        text,
        ("排除行业", "剔除行业", "不含行业", "排除板块", "剔除板块"),
        ("包含行业", "只看行业", "行业包含", "行业为", "行业是"),
    )
    return include, exclude


def _add_sort(sort_rules: list[SortRule], field: str, direction: str) -> None:
    if any(rule.field == field for rule in sort_rules):
        return
    sort_rules.append(SortRule(field=field, direction=direction))


def _parse_sort_phrases(text: str, sort_rules: list[SortRule]) -> None:
    asc_re = "(?:%s)" % "|".join(SORT_ASC_HINTS)
    desc_re = "(?:%s)" % "|".join(SORT_DESC_HINTS)
    for field, aliases in SORT_FIELD_ALIASES.items():
        field_re = _field_pattern(aliases)
        if re.search(rf"{field_re}\s*(?:的)?\s*{asc_re}", text, flags=re.IGNORECASE):
            _add_sort(sort_rules, field, "asc")
        elif re.search(rf"{field_re}\s*(?:的)?\s*{desc_re}", text, flags=re.IGNORECASE):
            _add_sort(sort_rules, field, "desc")
        elif re.search(rf"(?:按|以|根据)\s*{field_re}[^，,。]{{0,3}}(?:排序|排列)", text, flags=re.IGNORECASE):
            _add_sort(sort_rules, field, "desc")


def parse_local_query(text: str) -> QuerySpec | None:
    """Parse high-confidence Chinese screening expressions without calling AI."""
    normalized = _normalize_text(text)
    if not normalized:
        return None

    conditions: list[Condition] = []
    _parse_market_cap(normalized, conditions)
    _parse_turnover_range(normalized, conditions)
    _parse_rsi_range(normalized, conditions)
    _parse_field_conditions(normalized, conditions)
    _parse_trend_phrases(normalized, conditions)
    include_industries, exclude_industries = _parse_industry_lists(normalized)

    sort_rules: list[SortRule] = []
    _parse_sort_phrases(normalized, sort_rules)

    has_ambiguous_term = any(term in normalized for term in AMBIGUOUS_TERMS)
    normalized_upper = normalized.upper()
    has_unsupported_field = any(term.upper() in normalized_upper for term in UNSUPPORTED_FIELD_TERMS)
    has_unsupported_logic = any(term in normalized for term in UNSUPPORTED_LOGIC_HINTS)
    if (
        not conditions
        and not sort_rules
        and not include_industries
        and not exclude_industries
        and not has_ambiguous_term
        and not has_unsupported_field
    ):
        return None

    spec = QuerySpec(
        conditions=conditions,
        sort=sort_rules,
        exclude=["ST"],
        scan_limit=300,
        result_limit=80,
        allow_missing_pe=True,
        with_technical=True,
        include_industries=include_industries,
        exclude_industries=exclude_industries,
        raw_text=text.strip(),
    )
    if has_unsupported_logic and conditions:
        spec.clarifications.append("当前版本不支持“或/或者”条件，请改写为可同时满足的明确筛选条件")
        spec.needs_confirmation = True
    if any(condition.op == "=" for condition in conditions):
        spec.clarifications.append("当前版本不支持等于条件，请改写为大于/小于或区间筛选")
        spec.needs_confirmation = True
    spec = apply_local_query_guardrails(spec)
    if not spec.needs_confirmation and not spec.clarifications:
        validate_query_spec(spec)
    return spec
