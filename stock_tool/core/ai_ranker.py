"""Legacy AI ranking adapter kept outside the formal Qt candidate flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import lazy_loader as lazy

# 惰性加载 requests：仅在首次发起 AI 排名请求时才导入，避免拖慢启动。
requests = lazy.load("requests")

from stock_tool.core.ai_analyzer import AIConfig
from stock_tool.core.ai_client import _message_content_from_payload, _readable_request_error, parse_json_response
from stock_tool.core.ai_cost import estimate_tokens, format_cost_note, usage_from_payload
from stock_tool.core.models import fmt_number


ALLOWED_LEVELS = {"S", "A", "B", "C", "D"}
ALLOWED_ACTIONS = {"关注", "等待触发", "回避", "持仓观察"}


@dataclass
class AIRank:
    code: str
    name: str
    ai_score: float
    level: str
    action: str
    reason: str
    risk: str


def _candidate_line(item) -> str:
    quote = item.quote
    metrics = item.metrics
    tech = item.technical
    return (
        f"{quote.code} {quote.name} | 主评分/专业分={item.local_score:.1f} | "
        f"价格={fmt_number(quote.price)} PE={fmt_number(quote.pe)} PB={fmt_number(quote.pb)} | "
        f"ROE={fmt_number(metrics.roe, '%')} 毛利={fmt_number(metrics.gross_margin, '%')} "
        f"净利={fmt_number(metrics.net_margin, '%')} 负债={fmt_number(metrics.debt_ratio, '%')} "
        f"营收增={fmt_number(metrics.revenue_growth, '%')} 净利增={fmt_number(metrics.profit_growth, '%')} | "
        f"技术={getattr(tech, 'opportunity_label', '未计算')} "
        f"触发={fmt_number(getattr(tech, 'entry_trigger', None))} "
        f"失效={fmt_number(getattr(tech, 'fail_level', None))}"
    )


def build_rank_prompt(candidates: Iterable, max_items: int = 30) -> str:
    lines = [_candidate_line(item) for item in list(candidates)[:max_items]]
    return f"""
你是一个谨慎的A股候选股排序助手。下面是软件先用本地规则筛出的候选股，请你只基于给定数据进行二次排序。

要求：
1. 不要给确定买卖建议，不承诺收益。
2. 重点区分“值得关注”和“已经适合买入”，候选未触发只能写等待条件。
3. 只输出严格 JSON，不要输出 Markdown。
4. ai_score 为 0-100，越高代表越值得继续研究，不代表必涨。
5. level 只能是 S/A/B/C/D；action 只能是 关注/等待触发/回避/持仓观察。

输出格式：
{{
  "summary": "整体筛选结论，120字以内",
  "ranks": [
    {{"code":"600000","name":"示例","ai_score":82,"level":"A","action":"等待触发","reason":"核心理由","risk":"主要风险"}}
  ]
}}

候选股：
{chr(10).join(lines)}
""".strip()


def _endpoint(config: AIConfig) -> tuple[str, str]:
    provider = config.provider.lower()
    if provider == "openai":
        return "https://api.openai.com/v1/chat/completions", config.model or "gpt-4.1-mini"
    if provider == "deepseek":
        return "https://api.deepseek.com/v1/chat/completions", config.model or "deepseek-v4-flash"
    raise ValueError(f"暂不支持的AI服务商：{config.provider}")


def _bounded_score(value) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(number, 100.0))


def _clean_level(value) -> str:
    level = str(value or "").strip().upper()[:1]
    return level if level in ALLOWED_LEVELS else "C"


def _clean_action(value) -> str:
    action = str(value or "").strip()
    return action if action in ALLOWED_ACTIONS else "关注"


def rank_candidates(config: AIConfig, candidates: list, max_items: int = 30) -> tuple[str, list[AIRank]]:
    if not config.api_key.strip():
        raise ValueError("请先在设置页填写 API Key")
    if not candidates:
        raise ValueError("请先完成基础筛选，得到候选股后再进行AI排序")

    url, model = _endpoint(config)
    prompt = build_rank_prompt(candidates, max_items=max_items)
    max_tokens = 1800
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
    payload = parse_json_response(content)

    rank_rows = payload.get("ranks", [])
    if not isinstance(rank_rows, list):
        raise ValueError("AI排序返回的 ranks 必须是列表")

    ranks = []
    for row in rank_rows:
        if not isinstance(row, dict):
            continue
        raw_code = str(row.get("code", "")).strip()
        if not raw_code:
            continue
        ranks.append(
            AIRank(
                code=raw_code.zfill(6),
                name=str(row.get("name", "")),
                ai_score=_bounded_score(row.get("ai_score")),
                level=_clean_level(row.get("level")),
                action=_clean_action(row.get("action")),
                reason=str(row.get("reason", "")),
                risk=str(row.get("risk", "")),
            )
        )
    if not ranks:
        raise ValueError("AI排序未返回可用候选结果")
    ranks.sort(key=lambda x: x.ai_score, reverse=True)
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
    summary = str(payload.get("summary") or "AI排序完成")
    return f"{summary}\n{cost_note}", ranks
