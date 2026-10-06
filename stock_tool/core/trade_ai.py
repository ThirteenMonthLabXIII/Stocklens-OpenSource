"""Legacy trade-observation AI adapter.

This module is retained for historical callers and old database/report
compatibility only.  Its payload is not a formal ``AgentResult`` and must not
enter the new Qt candidate flow.  New code should use the research-only
evidence contracts instead.
"""

from __future__ import annotations

import math
import re

import lazy_loader as lazy

# 惰性加载 requests：仅在首次发起交易复盘 AI 请求时才导入，避免拖慢启动。
requests = lazy.load("requests")

from stock_tool.core.ai_analyzer import AIConfig
from stock_tool.core.ai_client import _message_content_from_payload, _readable_request_error, parse_json_response
from stock_tool.core.ai_cost import estimate_tokens, format_cost_note, usage_from_payload
from stock_tool.core.models import EventCatalyst, MarketSentiment, MoneyFlowSnapshot, StockQuote, TradeReference, fmt_number
from stock_tool.core.technical_profile import TechnicalProfile


def _endpoint(config: AIConfig) -> tuple[str, str]:
    provider = config.provider.lower()
    if provider == "openai":
        return "https://api.openai.com/v1/chat/completions", config.model or "gpt-4.1-mini"
    if provider == "deepseek":
        return "https://api.deepseek.com/v1/chat/completions", config.model or "deepseek-v4-flash"
    raise ValueError(f"暂不支持的AI服务商：{config.provider}")


def _events_text(events: list[EventCatalyst] | None) -> str:
    if not events:
        return "暂无明确事件催化"
    lines = []
    for event in events[:5]:
        prefix = f"{event.date} " if event.date else ""
        source = f"（{event.source}）" if event.source else ""
        lines.append(f"- {prefix}{event.title}{source}")
    return "\n".join(lines)


def _sanitize_freetext(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    return re.sub(r"(?i)(?<![a-z])[-+]?(?:nan|inf|infinity)(?![a-z])", "-", text)


def build_trade_decision_prompt(
    quote: StockQuote,
    technical: TechnicalProfile,
    money_flow: MoneyFlowSnapshot | None,
    sentiment: MarketSentiment | None,
    events: list[EventCatalyst] | None,
    reference: TradeReference,
) -> str:
    risk_text = _sanitize_freetext("；".join(reference.risks[:6])) if reference.risks else "暂无明确风险标签"
    event_text = _events_text(events)
    money_text = _sanitize_freetext(money_flow.summary) if money_flow else "资金流缺失"
    sentiment_text = _sanitize_freetext(sentiment.summary) if sentiment else "市场情绪缺失"
    rows_count = len(money_flow.rows) if money_flow and money_flow.rows else 0
    technical_gaps = "；".join(getattr(technical, "data_gaps", []) or []) or "无"

    return f"""
你是A股交易参考助手，任务是把软件已抓取的行情、技术面、资金流、市场情绪和事件催化整理成可执行观察参考。

硬规则：
1. 输出只能基于给定数据，不得编造换手率、资金连续性、新闻或价位。
2. 候选形态不等于买点。若技术状态为 candidate，只能输出 wait，不能输出 yes。
3. 只有技术状态为 triggered，且风险收益比、资金和市场情绪没有明显反证时，can_buy 才可为 yes。
4. 若技术状态为 avoid/failed 或技术数据不可用，can_buy 必须为 no 或 wait。
5. 主力行为只能根据 main_3d/main_5d/main_10d/xlarge_5d/small_5d/streak 和样本天数判断；样本不足时必须写 unknown/待确认。
6. 事件催化只能作为验证因素，不能单独推出可买。
7. 参考买入价、卖出价、止损价优先沿用本地交易参考；缺失时填 null，不要自行编价格。
8. 用“交易参考/观察条件”措辞，不承诺收益，不写保证上涨。

请只输出严格 JSON，不要输出 Markdown：
{{
  "can_buy": "yes|wait|no|hold_only",
  "action": "buy|watch|hold|avoid",
  "entry_price": 数字或 null,
  "sell_price": 数字或 null,
  "stop_price": 数字或 null,
  "trend_forecast": "对未来{reference.horizon_days}天趋势的简短判断",
  "main_force_behavior": "accumulating|distributing|washing|neutral|unknown 加中文解释",
  "confidence": 1-10,
  "opportunity_level": "S|A|B|C|D",
  "risk_reward": 数字或 null,
  "buy_condition": "允许买入或继续观察的条件",
  "sell_condition": "卖出/止盈/减仓条件",
  "stop_condition": "止损/失效条件",
  "reasoning_summary": "180字以内总结",
  "evidence": ["依据1", "依据2", "依据3"],
  "counter_evidence": ["反证1", "反证2"],
  "data_gaps": ["缺失或不可靠数据"],
  "disclaimer": "仅为数据观察参考，不构成投资建议"
}}

【股票】
{quote.name}（{quote.code}）
当前价：{fmt_number(quote.price, "元")}
涨跌幅：{fmt_number(quote.change_pct, "%")}
PE：{fmt_number(quote.pe)}
PB：{fmt_number(quote.pb)}
总市值：{fmt_number((quote.market_cap or 0) / 100000000, "亿元") if quote.market_cap else "-"}

【技术画像】
available={technical.available}
status={technical.status}
setup={technical.setup_name}
trend_stage={technical.trend_stage}
label={technical.opportunity_label}
summary={_sanitize_freetext(technical.summary)}
entry_trigger={_value_or_dash(technical.entry_trigger)}
fail_level={_value_or_dash(technical.fail_level)}
target_level={_value_or_dash(technical.target_level)}
risk_flags={technical.risk_flags}
data_gaps={technical_gaps}

【资金流】
样本天数：{rows_count}
summary={money_text}
main_3d={money_flow.main_3d if money_flow else None}
main_5d={money_flow.main_5d if money_flow else None}
main_10d={money_flow.main_10d if money_flow else None}
xlarge_5d={money_flow.xlarge_5d if money_flow else None}
small_5d={money_flow.small_5d if money_flow else None}
streak={money_flow.streak if money_flow else None}

【市场情绪】
{sentiment_text}

【事件催化】
{event_text}

【本地交易参考，若与模型判断冲突，以此为风控锚点】
等级：{reference.level}
动作：{reference.action}
参考买入/触发价：{_value_or_dash(reference.entry_price)}
参考卖出/目标价：{_value_or_dash(reference.target_price)}
风控止损/失效价：{_value_or_dash(reference.fail_price)}
观察周期：{reference.horizon_days}天
仓位参考：{reference.position_hint}
主要风险：{risk_text}
本地摘要：
{_sanitize_freetext(reference.thesis)}
""".strip()


def _parse_json(content: str) -> dict:
    return parse_json_response(content)


def _is_invalid_text_number(text: str) -> bool:
    return text.strip().lower() in {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}


def _value_or_dash(value) -> str:
    if isinstance(value, bool) or value is None:
        return "-"
    if isinstance(value, (int, float)):
        return str(value) if math.isfinite(float(value)) else "-"
    text = str(value).strip()
    if not text or _is_invalid_text_number(text):
        return "-"
    return text


def _list_text(value) -> str:
    if isinstance(value, list):
        items = [_value_or_dash(item) for item in value]
        items = [item for item in items if item != "-"]
        return "；".join(items) if items else "-"
    return _value_or_dash(value)


def _float_or_none(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _confidence_text(value) -> str:
    numeric = _float_or_none(value)
    if numeric is None or numeric < 1 or numeric > 10:
        return "-"
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.1f}"


def _normalize_opportunity_level(value) -> str | None:
    text = str(value or "").strip().upper()
    return text if text in {"S", "A", "B", "C", "D"} else None


def _risk_reward_or_none(value) -> float | None:
    numeric = _float_or_none(value)
    if numeric is None or numeric <= 0 or numeric > 100:
        return None
    return numeric


def _price_matches(value: float | None, anchor: float | None) -> bool:
    if value is None and anchor is None:
        return True
    if value is None or anchor is None:
        return False
    tolerance = max(0.01, abs(float(anchor)) * 0.01)
    return abs(float(value) - float(anchor)) <= tolerance


def validate_trade_ai_payload(payload: dict, technical: TechnicalProfile, reference: TradeReference) -> dict:
    can_buy = str(payload.get("can_buy", "")).strip().lower()
    action = str(payload.get("action", "")).strip().lower()
    if can_buy not in {"yes", "wait", "no", "hold_only"} or action not in {"buy", "watch", "hold", "avoid"}:
        raise ValueError("AI返回结构异常：can_buy/action 不符合约定枚举")

    status = technical.status if technical else ""
    if status == "candidate" and can_buy == "yes":
        raise ValueError("AI返回违反交易规则：candidate 形态不能输出 yes")
    if (not technical or not technical.available or status in {"avoid", "failed"}) and can_buy not in {"no", "wait"}:
        raise ValueError("AI返回违反交易规则：技术不可用、avoid 或 failed 时只能输出 no/wait")
    if can_buy == "yes" and status != "triggered":
        raise ValueError("AI返回违反交易规则：只有 triggered 状态才允许输出 yes")

    checked = dict(payload)
    conflicts: list[str] = []
    level = _normalize_opportunity_level(checked.get("opportunity_level"))
    if level is None:
        fallback_level = _normalize_opportunity_level(reference.level)
        checked["opportunity_level"] = fallback_level
        conflicts.append("机会等级不符合本地枚举，已回落到本地观察等级" if fallback_level else "机会等级不符合本地枚举，已降级为 -")
    else:
        checked["opportunity_level"] = level

    confidence = _float_or_none(checked.get("confidence"))
    if confidence is None or confidence < 1 or confidence > 10:
        checked["confidence"] = None
        conflicts.append("置信度不在 1-10 范围内，已降级为 -")
    else:
        checked["confidence"] = confidence

    risk_reward = _risk_reward_or_none(checked.get("risk_reward"))
    if risk_reward is None:
        checked["risk_reward"] = None
        conflicts.append("风险收益比不是合理正数，已降级为 -")
    else:
        checked["risk_reward"] = risk_reward

    anchors = {
        "entry_price": ("参考买入", reference.entry_price),
        "sell_price": ("参考卖出", reference.target_price),
        "stop_price": ("风控止损", reference.fail_price),
    }
    for field, (label, anchor) in anchors.items():
        value = _float_or_none(checked.get(field))
        if not _price_matches(value, anchor):
            if anchor is None:
                conflicts.append(f"{label}缺少本地锚点，已忽略模型价位")
                checked[field] = None
            else:
                conflicts.append(f"{label}与本地锚点不一致，已回落为 {fmt_number(anchor, '元')}")
                checked[field] = anchor
        elif value is not None:
            checked[field] = value
    if conflicts:
        checked["_local_conflicts"] = conflicts
    return checked


def format_trade_ai_report(payload: dict) -> str:
    evidence = _list_text(payload.get("evidence"))
    counter = _list_text(payload.get("counter_evidence"))
    gaps = _list_text(payload.get("data_gaps"))
    conflicts = _list_text(payload.get("_local_conflicts"))
    confidence = _confidence_text(payload.get("confidence"))
    rr = _risk_reward_or_none(payload.get("risk_reward"))
    rr_text = f"{rr:.2f}" if rr is not None else "-"
    return f"""
AI增强参考（模型生成）
能否买入：{_value_or_dash(payload.get("can_buy"))} / {_value_or_dash(payload.get("action"))}
机会等级：{_value_or_dash(payload.get("opportunity_level"))}；置信度：{confidence}/10；风险收益比：{rr_text}
参考买入：{fmt_number(payload.get("entry_price"), "元")}
参考卖出：{fmt_number(payload.get("sell_price"), "元")}
风控止损：{fmt_number(payload.get("stop_price"), "元")}
趋势预测：{_value_or_dash(payload.get("trend_forecast"))}
主力行为：{_value_or_dash(payload.get("main_force_behavior"))}
买入条件：{_value_or_dash(payload.get("buy_condition"))}
卖出条件：{_value_or_dash(payload.get("sell_condition"))}
止损条件：{_value_or_dash(payload.get("stop_condition"))}
核心摘要：{_value_or_dash(payload.get("reasoning_summary"))}
核心依据：{evidence}
反向证据：{counter}
数据缺口：{gaps}
本地风控冲突：{conflicts}
说明：{_value_or_dash(payload.get("disclaimer")) if payload.get("disclaimer") is not None else "仅为数据观察参考，不构成投资建议"}
""".strip()


def analyze_trade_observation_with_ai(
    config: AIConfig,
    quote: StockQuote,
    technical: TechnicalProfile,
    money_flow: MoneyFlowSnapshot | None,
    sentiment: MarketSentiment | None,
    events: list[EventCatalyst] | None,
    reference: TradeReference,
) -> str:
    if not config.api_key.strip():
        raise ValueError("请先在设置页填写 API Key")

    url, model = _endpoint(config)
    prompt = build_trade_decision_prompt(quote, technical, money_flow, sentiment, events, reference)
    max_tokens = 1600
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        response.raise_for_status()
        response_payload = response.json()
    except Exception as exc:
        raise _readable_request_error(exc, config.provider.lower()) from exc
    content = _message_content_from_payload(response_payload)
    payload = _parse_json(content)
    payload = validate_trade_ai_payload(payload, technical, reference)
    input_tokens, output_tokens, cached_tokens = usage_from_payload(response_payload)
    cost_note = format_cost_note(
        config.provider.lower(),
        model,
        estimate_tokens(prompt),
        max_tokens,
        input_tokens,
        output_tokens,
        cached_tokens,
    )
    return f"{format_trade_ai_report(payload)}\n\n{cost_note}"
