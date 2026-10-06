from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from stock_tool.core.screener_engine import ScreenFilter


SUPPORTED_FIELDS = {
    "roe",
    "net_margin",
    "debt_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "gross_margin",
    "revenue_growth",
    "profit_growth",
    "market_cap_yi",
    "trend_20d",
    "volume_ratio",
    "pullback_pct",
    "max_drawdown_30d",
    "max_drawdown_60d",
    "turnover",
    "main_5d_yi",
    "rsi",
    "box_score",
    "box_range_pct",
    "industry_strength",
}
SUPPORTED_OPS = {">=", ">", "<=", "<", "between"}
STOCK_LIST_KEYS = {"stocks", "stock_list", "stock_codes", "codes", "symbols", "results"}
FIELD_LABELS = {
    "roe": "ROE",
    "net_margin": "净利率",
    "debt_ratio": "资产负债率",
    "pe": "PE",
    "pe_ttm": "PE-TTM",
    "pb": "市净率",
    "gross_margin": "毛利率",
    "revenue_growth": "营收增长率",
    "profit_growth": "净利润增长率",
    "market_cap_yi": "总市值",
    "trend_20d": "近20日趋势",
    "volume_ratio": "量比",
    "pullback_pct": "距20日前高回撤",
    "max_drawdown_30d": "近30日最大回撤",
    "max_drawdown_60d": "近60日最大回撤",
    "turnover": "换手率",
    "main_5d_yi": "近5日主力净流入",
    "rsi": "RSI",
    "box_score": "箱体评分",
    "box_range_pct": "箱体振幅",
    "box_position_pct": "箱体位置",
    "industry_strength": "行业强度",
}
FIELD_UNITS = {
    "roe": "%",
    "net_margin": "%",
    "debt_ratio": "%",
    "pe": "倍",
    "pe_ttm": "倍",
    "pb": "倍",
    "gross_margin": "%",
    "revenue_growth": "%",
    "profit_growth": "%",
    "market_cap_yi": "亿元",
    "trend_20d": "%",
    "volume_ratio": "倍",
    "pullback_pct": "%",
    "max_drawdown_30d": "%",
    "max_drawdown_60d": "%",
    "turnover": "%",
    "main_5d_yi": "亿元",
    "rsi": "",
    "box_score": "",
    "box_range_pct": "%",
    "box_position_pct": "%",
    "industry_strength": "",
}
SORTABLE_FIELDS = SUPPORTED_FIELDS | {"price", "local_score", "box_position_pct"}
SORT_FIELD_LABELS = {**FIELD_LABELS, "price": "股价", "local_score": "主评分/专业分"}
EXECUTABLE_RULES = {
    "roe": {">=", ">"},
    "net_margin": {">=", ">"},
    "debt_ratio": {"<=", "<"},
    "pe": {"<=", "<"},
    "pe_ttm": {"<=", "<"},
    "pb": {"<=", "<"},
    "gross_margin": {">=", ">"},
    "revenue_growth": {">=", ">"},
    "profit_growth": {">=", ">"},
    "market_cap_yi": {">=", ">", "<=", "<", "between"},
    "trend_20d": {">=", ">"},
    "volume_ratio": {">=", ">"},
    "pullback_pct": {"<=", "<"},
    "max_drawdown_30d": {"<=", "<"},
    "max_drawdown_60d": {"<=", "<"},
    "turnover": {">=", ">", "<=", "<", "between"},
    "main_5d_yi": {">=", ">"},
    "rsi": {">=", ">", "<=", "<", "between"},
    "box_score": {">=", ">"},
    "box_range_pct": {"<=", "<"},
    "industry_strength": {">=", ">"},
}
AMBIGUOUS_TERMS = {
    "估值低": "把“估值低”理解为 PE <= 30（可调）",
    "低估": "把“低估”理解为 PE <= 30（可调）",
    "便宜": "把“便宜”理解为 PE <= 30（可调）",
    "回撤小": "把“回撤小”理解为 距20日前高回撤 <= 15%（可调）",
    "最大回撤小": "把“最大回撤小”理解为 近30日最大回撤 <= 20%（可调）",
    "30日回撤小": "把“30日回撤小”理解为 近30日最大回撤 <= 15%（可调）",
    "60日回撤小": "把“60日回撤小”理解为 近60日最大回撤 <= 20%（可调）",
    "放量": "把“放量”理解为 量比 >= 1.5（可调）",
    "日增量高": "把“日增量高”理解为 量比 >= 1.5（可调）",
    "量能放大": "把“量能放大”理解为 量比 >= 1.5（可调）",
    "趋势好": "把“趋势好”理解为 近20日趋势 > 0（可调）",
    "走势好": "把“走势好”理解为 近20日趋势 > 0（可调）",
    "强势": "把“强势”理解为 近20日趋势 > 0（可调）",
    "站上均线": "把“站上均线”理解为 近20日趋势 > 0（可调）",
    "换手活跃": "把“换手活跃”理解为 换手率 3%-15%（可调）",
    "主力进场": "把“主力进场”理解为 近5日主力净流入 > 0亿元（资金数据可用时执行）",
    "主力流入": "把“主力流入”理解为 近5日主力净流入 > 0亿元（资金数据可用时执行）",
    "均线多头": "把“均线多头”理解为 MA5 > MA20 > MA60（技术画像可用时执行）",
    "多头排列": "把“多头排列”理解为 MA5 > MA20 > MA60（技术画像可用时执行）",
    "成长好": "把“成长好”理解为 营收增长率 >= 20%（可调）",
    "高成长": "把“高成长”理解为 营收增长率 >= 20%（可调）",
    "稳健": "把“稳健”理解为 ROE >= 10 且 资产负债率 <= 60（可调）",
    "龙头": "把“龙头”理解为 总市值 >= 500亿元（可调）",
    "优质": "把“优质”理解为 ROE >= 15 且 毛利率 >= 30（可调）",
    "箱体横盘": "把“箱体横盘”理解为 横盘蓄势箱体 且 箱体评分 >= 60（研究信号，不是买卖建议）",
    "箱体": "把“箱体”理解为 横盘蓄势箱体 且 箱体评分 >= 60（研究信号，不是买卖建议）",
    "横盘蓄势": "把“横盘蓄势”理解为 箱体评分 >= 60 且仍在箱体内",
    "震荡整理": "把“震荡整理”理解为 箱体评分 >= 60 且仍在箱体内",
    "行业强": "把“行业强”理解为 行业强度 >= 60（行业强弱仅作研究排序信号）",
    "强势行业": "把“强势行业”理解为 行业强度 >= 60（行业强弱仅作研究排序信号）",
    "行业靠前": "把“行业靠前”理解为 行业强度 >= 60（行业强弱仅作研究排序信号）",
}
UNSUPPORTED_FIELD_TERMS = {
    "股息率": "当前版本没有股息率数据源，已忽略该条件",
    "分红率": "当前版本没有分红率数据源，已忽略该条件",
    "MACD": "当前版本暂未接入 MACD 筛选，已忽略该条件",
    "KDJ": "当前版本暂未接入 KDJ 筛选，已忽略该条件",
}

class QueryValidationError(ValueError):
    pass


@dataclass
class Condition:
    field: str
    op: str
    value: float | None = None
    value_to: float | None = None


FUZZY_CONDITION_RULES: dict[str, tuple[Condition, ...]] = {
    "估值低": (Condition("pe", "<=", 30),),
    "低估": (Condition("pe", "<=", 30),),
    "便宜": (Condition("pe", "<=", 30),),
    "回撤小": (Condition("pullback_pct", "<=", 15),),
    "最大回撤小": (Condition("max_drawdown_30d", "<=", 20),),
    "30日回撤小": (Condition("max_drawdown_30d", "<=", 15),),
    "60日回撤小": (Condition("max_drawdown_60d", "<=", 20),),
    "放量": (Condition("volume_ratio", ">=", 1.5),),
    "日增量高": (Condition("volume_ratio", ">=", 1.5),),
    "量能放大": (Condition("volume_ratio", ">=", 1.5),),
    "趋势好": (Condition("trend_20d", ">", 0),),
    "走势好": (Condition("trend_20d", ">", 0),),
    "强势": (Condition("trend_20d", ">", 0),),
    "站上均线": (Condition("trend_20d", ">", 0),),
    "换手活跃": (Condition("turnover", "between", 3, 15),),
    "主力进场": (Condition("main_5d_yi", ">", 0),),
    "主力流入": (Condition("main_5d_yi", ">", 0),),
    "成长好": (Condition("revenue_growth", ">=", 20),),
    "高成长": (Condition("revenue_growth", ">=", 20),),
    "稳健": (Condition("roe", ">=", 10), Condition("debt_ratio", "<=", 60)),
    "龙头": (Condition("market_cap_yi", ">=", 500),),
    "优质": (Condition("roe", ">=", 15), Condition("gross_margin", ">=", 30)),
    "箱体横盘": (Condition("box_score", ">=", 60),),
    "箱体": (Condition("box_score", ">=", 60),),
    "横盘蓄势": (Condition("box_score", ">=", 60),),
    "震荡整理": (Condition("box_score", ">=", 60),),
    "行业强": (Condition("industry_strength", ">=", 60),),
    "强势行业": (Condition("industry_strength", ">=", 60),),
    "行业靠前": (Condition("industry_strength", ">=", 60),),
}
FUZZY_FLAG_RULES: dict[str, str] = {
    "均线多头": "require_ma_bullish",
    "多头排列": "require_ma_bullish",
    "箱体横盘": "require_box_sideways",
    "箱体": "require_box_sideways",
    "横盘蓄势": "require_box_sideways",
    "震荡整理": "require_box_sideways",
}


@dataclass
class SortRule:
    field: str
    direction: str = "desc"


@dataclass
class QuerySpec:
    intent: str = "screen"
    conditions: list[Condition] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: ["ST"])
    sort: list[SortRule] = field(default_factory=list)
    scan_limit: int = 300
    result_limit: int = 80
    allow_missing_pe: bool = True
    with_technical: bool = True
    require_ma_bullish: bool = False
    require_box_sideways: bool = False
    include_industries: list[str] = field(default_factory=list)
    exclude_industries: list[str] = field(default_factory=list)
    needs_confirmation: bool = False
    clarifications: list[str] = field(default_factory=list)
    resolved_notes: list[str] = field(default_factory=list)
    raw_text: str = ""
    missing_data_policy: str = "required_fields_block_formal"
    execution_mode: str = "formal"
    box_window_days: int = 60


def _to_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise QueryValidationError(f"{name} 不能是布尔值")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise QueryValidationError(f"{name} 必须是有限数字")
        return number
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise QueryValidationError(f"{name} 不能为空字符串")
        try:
            number = float(text)
        except ValueError as exc:
            raise QueryValidationError(f"{name} 必须是数字") from exc
        if not math.isfinite(number):
            raise QueryValidationError(f"{name} 必须是有限数字")
        return number
    raise QueryValidationError(f"{name} 必须是数字")


def _to_int(value: Any, default: int, name: str) -> int:
    if value is None or value == "":
        return default
    number = _to_number(value, name)
    if number is None:
        return default
    if int(number) != number:
        raise QueryValidationError(f"{name} 必须是整数")
    return int(number)


def _to_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
    return bool(value)


def _stringify_list_item(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("description", "message", "question", "text", "reason"):
            text = str(item.get(key) or "").strip()
            if text:
                return text
        return json.dumps(item, ensure_ascii=False)
    return str(item).strip()


def _to_str_list(value: Any, name: str, default: list[str] | None = None) -> list[str]:
    if value is None:
        return list(default or [])
    if not isinstance(value, list):
        raise QueryValidationError(f"{name} 必须是列表")
    items: list[str] = []
    for item in value:
        text = _stringify_list_item(item)
        if text:
            items.append(text)
    return items


def _normalize_exclude(value: Any) -> list[str]:
    exclude = _to_str_list(value, "exclude", default=["ST"])
    return exclude or ["ST"]


def _contains_stock_list(payload: dict) -> bool:
    for key in STOCK_LIST_KEYS:
        if key in payload and payload.get(key) is not None:
            return True
    return False


def query_spec_from_dict(payload: dict) -> QuerySpec:
    if not isinstance(payload, dict):
        raise QueryValidationError("策略JSON必须是对象")
    if _contains_stock_list(payload):
        raise QueryValidationError("AI返回了股票列表，当前链路只接受筛选条件，拒绝执行")

    rows = payload.get("conditions", [])
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise QueryValidationError("conditions 必须是列表")
    conditions: list[Condition] = []
    require_ma_bullish = _to_bool(payload.get("require_ma_bullish"), False)
    require_box_sideways = _to_bool(payload.get("require_box_sideways"), False)
    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise QueryValidationError(f"conditions[{idx}] 必须是对象")
        field_name = str(row.get("field", "")).strip()
        if field_name in {"ma_bullish", "require_ma_bullish"}:
            require_ma_bullish = _to_bool(row.get("value"), True)
            continue
        if field_name in {"box_sideways", "require_box_sideways"}:
            require_box_sideways = _to_bool(row.get("value"), True)
            continue
        conditions.append(
            Condition(
                field=field_name,
                op=str(row.get("op", "")).strip(),
                value=_to_number(row.get("value"), f"conditions[{idx}].value"),
                value_to=_to_number(row.get("value_to"), f"conditions[{idx}].value_to"),
            )
        )

    sort_rows = payload.get("sort", [])
    sort: list[SortRule] = []
    if sort_rows is None:
        sort_rows = []
    if not isinstance(sort_rows, list):
        raise QueryValidationError("sort 必须是列表")
    for idx, row in enumerate(sort_rows, start=1):
        if not isinstance(row, dict):
            raise QueryValidationError(f"sort[{idx}] 必须是对象")
        field_name = str(row.get("field", "")).strip()
        if not field_name:
            raise QueryValidationError(f"sort[{idx}].field 不能为空")
        direction = (str(row.get("direction", "desc")).strip() or "desc").lower()
        sort.append(SortRule(field=field_name, direction=direction))

    return QuerySpec(
        intent=str(payload.get("intent") or "screen").strip() or "screen",
        conditions=conditions,
        exclude=_normalize_exclude(payload.get("exclude")),
        sort=sort,
        scan_limit=_to_int(payload.get("scan_limit"), 300, "scan_limit"),
        result_limit=_to_int(payload.get("result_limit"), 80, "result_limit"),
        allow_missing_pe=_to_bool(payload.get("allow_missing_pe"), True),
        with_technical=_to_bool(payload.get("with_technical"), True),
        require_ma_bullish=require_ma_bullish,
        require_box_sideways=require_box_sideways,
        include_industries=_to_str_list(payload.get("include_industries"), "include_industries"),
        exclude_industries=_to_str_list(payload.get("exclude_industries"), "exclude_industries"),
        needs_confirmation=_to_bool(payload.get("needs_confirmation"), False),
        clarifications=_to_str_list(payload.get("clarifications"), "clarifications"),
        resolved_notes=_to_str_list(payload.get("resolved_notes"), "resolved_notes"),
        raw_text=str(payload.get("raw_text") or ""),
        missing_data_policy=str(payload.get("missing_data_policy") or "required_fields_block_formal").strip(),
        execution_mode=str(payload.get("execution_mode") or "formal").strip(),
        box_window_days=_to_int(payload.get("box_window_days"), 60, "box_window_days"),
    )


def query_spec_from_json(text: str) -> QuerySpec:
    def reject_constant(value: str) -> None:
        raise QueryValidationError(f"策略JSON包含非法数字：{value}")

    try:
        payload = json.loads(text, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise QueryValidationError("策略JSON解析失败") from exc
    return query_spec_from_dict(payload)


def query_spec_to_dict(spec: QuerySpec) -> dict:
    return asdict(spec)


def supported_query_schema() -> dict:
    return {
        "fields": [
            {
                "name": field_name,
                "label": FIELD_LABELS[field_name],
                "unit": FIELD_UNITS[field_name],
                "executable_ops": sorted(EXECUTABLE_RULES[field_name]),
            }
            for field_name in sorted(SUPPORTED_FIELDS)
        ],
        "operators": sorted(SUPPORTED_OPS),
        "defaults": {
            "exclude": ["ST"],
            "scan_limit": 300,
            "result_limit": 80,
            "allow_missing_pe": True,
            "with_technical": True,
            "require_ma_bullish": False,
            "require_box_sideways": False,
            "include_industries": [],
            "exclude_industries": [],
        },
    }


def query_spec_summary(spec: QuerySpec) -> str:
    parts: list[str] = []
    for condition in spec.conditions:
        label = FIELD_LABELS.get(condition.field, condition.field)
        unit = FIELD_UNITS.get(condition.field, "")
        if condition.op == "between":
            value = "-" if condition.value is None else f"{condition.value:g}{unit}"
            value_to = "-" if condition.value_to is None else f"{condition.value_to:g}{unit}"
            parts.append(f"{label} 在 {value} 到 {value_to} 之间")
        else:
            value_text = "-" if condition.value is None else f"{condition.value:g}{unit}"
            parts.append(f"{label} {condition.op} {value_text}")
    if spec.sort:
        order_text = "、".join(
            f"{SORT_FIELD_LABELS.get(rule.field, rule.field)}{'升序' if rule.direction == 'asc' else '降序'}"
            for rule in spec.sort
        )
        parts.append("排序：" + order_text)
    if spec.require_ma_bullish:
        parts.append("均线多头：MA5 > MA20 > MA60")
    if spec.require_box_sideways:
        parts.append("箱体横盘：横盘蓄势型箱体")
    if spec.include_industries:
        parts.append("包含行业：" + "、".join(spec.include_industries))
    if spec.exclude_industries:
        parts.append("排除行业：" + "、".join(spec.exclude_industries))
    if spec.exclude:
        parts.append("排除：" + "、".join(spec.exclude))
    parts.append(f"扫描上限 {spec.scan_limit}，结果上限 {spec.result_limit}")
    parts.append("计算技术画像" if spec.with_technical else "不计算技术画像")
    if spec.resolved_notes:
        parts.append("我理解为：" + "；".join(spec.resolved_notes))
    if spec.clarifications:
        parts.append("需澄清：" + "；".join(spec.clarifications))
    if spec.needs_confirmation:
        parts.append("需要用户确认")
    return "；".join(parts)


def _append_condition_once(conditions: list[Condition], candidate: Condition) -> None:
    if candidate not in conditions:
        conditions.append(candidate)


def _append_note_once(notes: list[str], note: str) -> None:
    if note and note not in notes:
        notes.append(note)


def _contains_fuzzy_term(text: str, term: str) -> bool:
    if term != "回撤小":
        if term == "强势":
            return "强势" in text.replace("强势行业", "")
        if term == "箱体":
            narrowed = text
            for explicit_term in ("箱体横盘", "箱体评分", "箱体得分", "箱体振幅", "箱体幅度", "箱体位置", "箱体窗口"):
                narrowed = narrowed.replace(explicit_term, "")
            return term in narrowed
        if term == "行业强":
            narrowed = text.replace("行业强度", "").replace("行业强弱", "")
            return term in narrowed
        return term in text
    narrowed = text
    for explicit_term in ("最大回撤小", "30日回撤小", "60日回撤小"):
        narrowed = narrowed.replace(explicit_term, "")
    return term in narrowed


def resolve_fuzzy_terms(spec: QuerySpec) -> QuerySpec:
    text = spec.raw_text or ""
    if not text:
        return spec
    notes = list(spec.resolved_notes)
    matched_terms: set[str] = set()
    for term, rules in FUZZY_CONDITION_RULES.items():
        if not _contains_fuzzy_term(text, term):
            continue
        matched_terms.add(term)
        for rule in rules:
            _append_condition_once(spec.conditions, rule)
        _append_note_once(notes, AMBIGUOUS_TERMS.get(term, ""))
    for term, flag_name in FUZZY_FLAG_RULES.items():
        if not _contains_fuzzy_term(text, term):
            continue
        matched_terms.add(term)
        if flag_name == "require_ma_bullish":
            spec.require_ma_bullish = True
        if flag_name == "require_box_sideways":
            spec.require_box_sideways = True
        _append_note_once(notes, AMBIGUOUS_TERMS.get(term, ""))
    if matched_terms and spec.clarifications:
        spec.clarifications = [
            item
            for item in spec.clarifications
            if not any(term in item or AMBIGUOUS_TERMS.get(term) == item for term in matched_terms)
        ]
        if not spec.clarifications:
            spec.needs_confirmation = False
    spec.resolved_notes = notes
    return spec


def apply_local_query_guardrails(spec: QuerySpec) -> QuerySpec:
    spec = resolve_fuzzy_terms(spec)
    text = spec.raw_text or ""
    notes = list(spec.resolved_notes)
    text_upper = text.upper()
    for term, message in UNSUPPORTED_FIELD_TERMS.items():
        if term.upper() in text_upper:
            _append_note_once(notes, message)
    spec.resolved_notes = notes
    return spec


def _validate_number(field_name: str, value: float) -> None:
    if not math.isfinite(value):
        raise QueryValidationError(f"{field_name} 必须是有限数字")
    if field_name == "market_cap_yi":
        if value <= 0 or value > 1_000_000:
            raise QueryValidationError(f"{field_name} 数值不合理")
        return
    if field_name in {"pe", "pe_ttm"}:
        if value <= 0 or value > 10_000:
            raise QueryValidationError("pe 数值不合理")
        return
    if field_name == "pb":
        if value <= 0 or value > 10_000:
            raise QueryValidationError("pb 数值不合理")
        return
    if field_name == "rsi":
        if value < 0 or value > 100:
            raise QueryValidationError("rsi 数值必须在 0-100 范围内")
        return
    if field_name in {"box_score", "box_position_pct", "industry_strength"}:
        if value < 0 or value > 100:
            raise QueryValidationError(f"{field_name} 数值必须在 0-100 范围内")
        return
    if field_name == "box_range_pct":
        if value < 0 or value > 200:
            raise QueryValidationError("box_range_pct 数值不合理")
        return
    if value < -1000 or value > 10_000:
        raise QueryValidationError(f"{field_name} 数值不合理")


def validate_query_spec(spec: QuerySpec) -> None:
    if spec.intent != "screen":
        raise QueryValidationError(f"不支持的策略意图：{spec.intent}")
    if spec.needs_confirmation:
        raise QueryValidationError("该策略需要用户确认后才能执行")
    if spec.clarifications:
        raise QueryValidationError("该策略存在需澄清条件，不能直接执行")
    if not 50 <= spec.scan_limit <= 5000:
        raise QueryValidationError("scan_limit 必须在 50-5000 范围内")
    if not 1 <= spec.result_limit <= 500:
        raise QueryValidationError("result_limit 必须在 1-500 范围内")
    if spec.execution_mode not in {"formal", "review_only", "exploratory"}:
        raise QueryValidationError("execution_mode 只能是 formal、review_only 或 exploratory")
    if spec.missing_data_policy not in {"required_fields_block_formal", "allow_non_key_missing"}:
        raise QueryValidationError("不支持的 missing_data_policy")
    if not 20 <= spec.box_window_days <= 180:
        raise QueryValidationError("box_window_days 必须在 20-180 范围内")

    for rule in spec.sort:
        if rule.field not in SORTABLE_FIELDS:
            raise QueryValidationError(f"不支持的排序字段：{rule.field}")
        if rule.direction not in {"asc", "desc"}:
            raise QueryValidationError(f"不支持的排序方向：{rule.direction}")

    for condition in spec.conditions:
        if condition.field not in SUPPORTED_FIELDS:
            raise QueryValidationError(f"不支持的字段：{condition.field}")
        if condition.op not in SUPPORTED_OPS:
            raise QueryValidationError(f"不支持的操作符：{condition.op}")
        if condition.op == "between":
            if condition.value is None or condition.value_to is None:
                raise QueryValidationError(f"{condition.field} between 必须同时提供 value 和 value_to")
            _validate_number(condition.field, condition.value)
            _validate_number(condition.field, condition.value_to)
            if condition.value > condition.value_to:
                raise QueryValidationError(f"{condition.field} between 下限不能大于上限")
        else:
            if condition.value is None:
                raise QueryValidationError(f"{condition.field} {condition.op} 必须提供 value")
            _validate_number(condition.field, condition.value)


def _unsupported_condition(condition: Condition) -> QueryValidationError:
    return QueryValidationError(f"当前版本暂不支持该条件执行：{condition.field} {condition.op}")


def query_spec_to_screen_filter(spec: QuerySpec) -> ScreenFilter:
    validate_query_spec(spec)
    filters = ScreenFilter(
        min_roe=None,
        min_net_margin=None,
        max_debt_ratio=None,
        max_pe=None,
        max_pe_ttm=None,
        max_pb=None,
        min_gross_margin=None,
        min_revenue_growth=None,
        min_profit_growth=None,
        min_market_cap_yi=None,
        max_market_cap_yi=None,
        min_trend_20d=None,
        min_volume_ratio=None,
        max_pullback_pct=None,
        max_max_drawdown_30d=None,
        max_max_drawdown_60d=None,
        min_turnover=None,
        max_turnover=None,
        min_main_5d_yi=None,
        require_ma_bullish=spec.require_ma_bullish,
        min_rsi=None,
        max_rsi=None,
        require_box_sideways=spec.require_box_sideways,
        box_window_days=spec.box_window_days,
        min_box_score=None,
        max_box_range_pct=None,
        include_industries=list(spec.include_industries),
        exclude_industries=list(spec.exclude_industries),
        min_industry_strength=None,
        scan_limit=spec.scan_limit,
        result_limit=spec.result_limit,
        allow_missing_pe=spec.allow_missing_pe,
        with_technical=spec.with_technical,
        exclude=list(spec.exclude),
        sort=[(rule.field, rule.direction) for rule in spec.sort],
    )

    def set_min(attr: str, strict_attr: str, value: float | None, strict: bool) -> None:
        current = getattr(filters, attr)
        current_strict = getattr(filters, strict_attr)
        if current is None or value is not None and value > current:
            setattr(filters, attr, value)
            setattr(filters, strict_attr, strict)
        elif value == current:
            setattr(filters, strict_attr, current_strict or strict)

    def set_max(attr: str, strict_attr: str, value: float | None, strict: bool) -> None:
        current = getattr(filters, attr)
        current_strict = getattr(filters, strict_attr)
        if current is None or value is not None and value < current:
            setattr(filters, attr, value)
            setattr(filters, strict_attr, strict)
        elif value == current:
            setattr(filters, strict_attr, current_strict or strict)

    for condition in spec.conditions:
        field_name = condition.field
        op = condition.op
        value = condition.value

        if op not in EXECUTABLE_RULES.get(field_name, set()):
            raise _unsupported_condition(condition)

        if field_name == "roe" and op in {">=", ">"}:
            set_min("min_roe", "strict_min_roe", value, op == ">")
        elif field_name == "net_margin" and op in {">=", ">"}:
            set_min("min_net_margin", "strict_min_net_margin", value, op == ">")
        elif field_name == "debt_ratio" and op in {"<=", "<"}:
            set_max("max_debt_ratio", "strict_max_debt_ratio", value, op == "<")
        elif field_name == "pe" and op in {"<=", "<"}:
            set_max("max_pe", "strict_max_pe", value, op == "<")
        elif field_name == "pe_ttm" and op in {"<=", "<"}:
            set_max("max_pe_ttm", "strict_max_pe_ttm", value, op == "<")
        elif field_name == "pb" and op in {"<=", "<"}:
            set_max("max_pb", "strict_max_pb", value, op == "<")
        elif field_name == "gross_margin" and op in {">=", ">"}:
            set_min("min_gross_margin", "strict_min_gross_margin", value, op == ">")
        elif field_name == "revenue_growth" and op in {">=", ">"}:
            set_min("min_revenue_growth", "strict_min_revenue_growth", value, op == ">")
        elif field_name == "profit_growth" and op in {">=", ">"}:
            set_min("min_profit_growth", "strict_min_profit_growth", value, op == ">")
        elif field_name == "market_cap_yi" and op in {">=", ">"}:
            set_min("min_market_cap_yi", "strict_min_market_cap_yi", value, op == ">")
        elif field_name == "market_cap_yi" and op in {"<=", "<"}:
            set_max("max_market_cap_yi", "strict_max_market_cap_yi", value, op == "<")
        elif field_name == "market_cap_yi" and op == "between":
            set_min("min_market_cap_yi", "strict_min_market_cap_yi", value, False)
            set_max("max_market_cap_yi", "strict_max_market_cap_yi", condition.value_to, False)
        elif field_name == "trend_20d" and op in {">=", ">"}:
            set_min("min_trend_20d", "strict_min_trend_20d", value, op == ">")
        elif field_name == "volume_ratio" and op in {">=", ">"}:
            set_min("min_volume_ratio", "strict_min_volume_ratio", value, op == ">")
        elif field_name == "pullback_pct" and op in {"<=", "<"}:
            set_max("max_pullback_pct", "strict_max_pullback_pct", value, op == "<")
        elif field_name == "max_drawdown_30d" and op in {"<=", "<"}:
            set_max("max_max_drawdown_30d", "strict_max_max_drawdown_30d", value, op == "<")
        elif field_name == "max_drawdown_60d" and op in {"<=", "<"}:
            set_max("max_max_drawdown_60d", "strict_max_max_drawdown_60d", value, op == "<")
        elif field_name == "turnover" and op in {">=", ">"}:
            set_min("min_turnover", "strict_min_turnover", value, op == ">")
        elif field_name == "turnover" and op in {"<=", "<"}:
            set_max("max_turnover", "strict_max_turnover", value, op == "<")
        elif field_name == "turnover" and op == "between":
            set_min("min_turnover", "strict_min_turnover", value, False)
            set_max("max_turnover", "strict_max_turnover", condition.value_to, False)
        elif field_name == "main_5d_yi" and op in {">=", ">"}:
            set_min("min_main_5d_yi", "strict_min_main_5d_yi", value, op == ">")
        elif field_name == "rsi" and op in {">=", ">"}:
            set_min("min_rsi", "strict_min_rsi", value, op == ">")
        elif field_name == "rsi" and op in {"<=", "<"}:
            set_max("max_rsi", "strict_max_rsi", value, op == "<")
        elif field_name == "rsi" and op == "between":
            set_min("min_rsi", "strict_min_rsi", value, False)
            set_max("max_rsi", "strict_max_rsi", condition.value_to, False)
        elif field_name == "box_score" and op in {">=", ">"}:
            set_min("min_box_score", "strict_min_box_score", value, op == ">")
        elif field_name == "box_range_pct" and op in {"<=", "<"}:
            set_max("max_box_range_pct", "strict_max_box_range_pct", value, op == "<")
        elif field_name == "industry_strength" and op in {">=", ">"}:
            set_min("min_industry_strength", "strict_min_industry_strength", value, op == ">")
        else:
            raise _unsupported_condition(condition)

    return filters
