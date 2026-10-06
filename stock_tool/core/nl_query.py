from __future__ import annotations

from stock_tool.core.ai_analyzer import AIConfig
from stock_tool.core.ai_client import parse_json_response, request_ai_text
from stock_tool.core.query_schema import (
    QuerySpec,
    QueryValidationError,
    apply_local_query_guardrails,
    query_spec_from_dict,
    query_spec_to_screen_filter,
    supported_query_schema,
)


def build_query_parse_prompt(user_text: str) -> str:
    schema = supported_query_schema()
    fields_text = "\n".join(
        f"- {item['name']}：{item['label']}，单位 {item['unit']}，可执行操作 {', '.join(item['executable_ops'])}"
        for item in schema["fields"]
    )
    operators_text = ", ".join(schema["operators"])
    return f"""
你是A股选股软件的“自然语言筛选条件解析器”。你的任务只是把用户中文条件翻译成可校验JSON，不做选股，不返回股票列表。

硬规则：
1. 只能输出严格JSON对象，不要输出Markdown、解释文字或股票列表。
2. 不能返回 stocks、stock_list、codes、symbols、results 等股票列表字段。
3. 只能使用字段白名单和可执行操作：
{fields_text}
4. 只能使用操作符：{operators_text}。
5. “估值低/低估/便宜”可按 PE<=30，“趋势好/走势好/强势”可按 trend_20d>0，“回撤小”可按 pullback_pct<=15，“最大回撤小”可按 max_drawdown_30d<=20，“30日回撤小”可按 max_drawdown_30d<=15，“60日回撤小”可按 max_drawdown_60d<=20，“放量”可按 volume_ratio>=1.5，“主力进场/主力流入”可按 main_5d_yi>0，“均线多头/多头排列”请设置 require_ma_bullish=true，“箱体横盘/横盘蓄势/震荡整理”请设置 require_box_sideways=true 且 box_score>=60，“行业强/强势行业/行业靠前”可按 industry_strength>=60，“稳健/优质/龙头/高成长”可按本地默认口径兜底；请把兜底说明写入 resolved_notes。
6. 只有完全无法映射到字段白名单的真实未知意图，才写入 clarifications 并设置 needs_confirmation=true。
7. 默认 exclude 包含 "ST"。
8. 默认 scan_limit=300，result_limit=80，with_technical=true，allow_missing_pe=true。
9. 数值单位：百分比字段直接使用百分数数值，例如 ROE高于15% 写 15；market_cap_yi 和 main_5d_yi 单位为亿元。

输出JSON格式：
{{
  "intent": "screen",
  "conditions": [
    {{"field": "roe", "op": ">=", "value": 15, "value_to": null}}
  ],
  "exclude": ["ST"],
  "sort": [],
  "scan_limit": 300,
  "result_limit": 80,
  "allow_missing_pe": true,
  "with_technical": true,
  "require_ma_bullish": false,
  "require_box_sideways": false,
  "include_industries": [],
  "exclude_industries": [],
  "needs_confirmation": false,
  "clarifications": [],
  "resolved_notes": [],
  "raw_text": "用户原文"
}}

用户原文：
{user_text.strip()}
""".strip()


def build_query_repair_prompt(user_text: str, error_reason: str, previous_output: str) -> str:
    schema = supported_query_schema()
    fields_text = "\n".join(
        f"- {item['name']}：{item['label']}，单位 {item['unit']}，可执行操作 {', '.join(item['executable_ops'])}"
        for item in schema["fields"]
    )
    return f"""
你仍然只是A股选股软件的“自然语言筛选条件解析器”。上一次输出没有通过本地校验，请只修复为严格JSON对象。

硬规则：
1. 只输出JSON对象，不要Markdown或解释。
2. 不返回股票列表，也不要包含 stocks、stock_list、codes、symbols、results。
3. 只能使用下面字段和可执行操作：
{fields_text}
4. 可按本地默认口径兜底的模糊词不要写入 clarifications；请输出可执行条件，并在 resolved_notes 说明口径。
5. “均线多头/多头排列”请设置 require_ma_bullish=true；“箱体横盘/横盘蓄势/震荡整理”请设置 require_box_sideways=true 且 box_score>=60；“行业强/强势行业/行业靠前”可按 industry_strength>=60。
6. 默认 exclude=["ST"]，scan_limit=300，result_limit=80，with_technical=true，allow_missing_pe=true，require_ma_bullish=false，require_box_sideways=false，include_industries=[]，exclude_industries=[]。
6. 不要使用股票明细数据；你只需要翻译用户原文里的筛选条件。

用户原文：
{user_text.strip()}

上次错误原因：
{error_reason}

上次AI输出：
{previous_output.strip()}
""".strip()


def _parse_and_guard(user_text: str, content: str) -> QuerySpec:
    try:
        payload = parse_json_response(content)
    except ValueError as exc:
        raise QueryValidationError(str(exc)) from exc
    spec = query_spec_from_dict(payload)
    spec.raw_text = user_text.strip()
    spec = apply_local_query_guardrails(spec)
    if not spec.needs_confirmation and not spec.clarifications:
        query_spec_to_screen_filter(spec)
    return spec


def _compact_previous_output(content: str) -> str:
    text = content.strip()
    if not text:
        return "(空输出)"
    if len(text) > 2000:
        return text[:2000] + "\n...(已截断)"
    return text


def parse_natural_language_query(config: AIConfig, user_text: str) -> tuple[QuerySpec, str]:
    prompt = build_query_parse_prompt(user_text)
    content, cost_note = request_ai_text(config, prompt, max_tokens=1000, timeout=60)
    try:
        return _parse_and_guard(user_text, content), cost_note
    except QueryValidationError as first_error:
        repair_prompt = build_query_repair_prompt(user_text, str(first_error), _compact_previous_output(content))
        repair_content, repair_cost_note = request_ai_text(config, repair_prompt, max_tokens=1000, timeout=60)
        try:
            spec = _parse_and_guard(user_text, repair_content)
        except QueryValidationError as second_error:
            raise QueryValidationError(f"AI 解析失败：{second_error}") from second_error
        cost_lines = [cost_note, "AI 首次返回格式异常，已自动修复重试一次。", repair_cost_note]
        return spec, "\n".join(line for line in cost_lines if line)


def parse_mvp_remaining_query(config: AIConfig, user_text: str, already_parsed_summary: str) -> tuple[QuerySpec, str]:
    schema = supported_query_schema()
    fields_text = "\n".join(
        f"- {item['name']}：{item['label']}，可执行操作 {', '.join(item['executable_ops'])}"
        for item in schema["fields"]
    )
    prompt = f"""
你是MVP闭集选股解析器的补充解析步骤。模板、股票池和下列条件已经由确定性词典解析；只提取尚未覆盖的明确数值条件。

硬规则：
1. 只输出QuerySpec JSON，不返回股票、模板、权重或解释。
2. 不重复已解析条件；没有额外条件时 conditions=[]。
3. 只能使用以下字段及其操作：
{fields_text}
4. 可按本地默认口径兜底的模糊词不要写入 clarifications；请输出可执行条件，并在 resolved_notes 说明口径。
5. 不支持的期间、股息率、MACD/KDJ 和情绪条件不得省略，必须写入 clarifications。换手率、量比、回撤、近5日主力净流入、RSI、箱体评分、箱体振幅、行业强度已支持；均线多头请设置 require_ma_bullish=true，箱体横盘请设置 require_box_sideways=true。

已经解析：{already_parsed_summary}
用户完整原文：{user_text.strip()}
""".strip()
    content, cost_note = request_ai_text(config, prompt, max_tokens=1000, timeout=60)
    return _parse_and_guard(user_text, content), cost_note
