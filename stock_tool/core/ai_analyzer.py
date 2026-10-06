from __future__ import annotations

from dataclasses import dataclass

import lazy_loader as lazy

# 惰性加载 requests：仅在首次发起 AI 请求时才导入，避免拖慢启动。
requests = lazy.load("requests")

from stock_tool.core.ai_cost import estimate_tokens, format_cost_note, usage_from_payload
from stock_tool.core.models import FinancialMetrics, StockQuote, fmt_number


@dataclass
class AIConfig:
    provider: str
    api_key: str
    model: str


def build_prompt(quote: StockQuote, metrics: FinancialMetrics) -> str:
    return f"""
你是一位严谨的A股财务数据分析助手。请仅基于下列公开财务和行情数据，做一段中文基本面解读。
要求：
1. 不得承诺收益，不得给出直接买卖建议。
2. 必须包含核心亮点、主要风险、估值观察和一句话总结。
3. 字数控制在300字左右，语言专业但易懂。

公司：{quote.name}（{quote.code}）
行情与估值：
- 当前价：{fmt_number(quote.price, "元")}
- 涨跌幅：{fmt_number(quote.change_pct, "%")}
- 市盈率PE：{fmt_number(quote.pe)}
- 市净率PB：{fmt_number(quote.pb)}
- 总市值：{fmt_number((quote.market_cap or 0) / 100000000, "亿元") if quote.market_cap else "-"}

最新财务指标：
- 报告期：{metrics.report_date or "-"}
- ROE：{fmt_number(metrics.roe, "%")}
- 毛利率：{fmt_number(metrics.gross_margin, "%")}
- 净利率：{fmt_number(metrics.net_margin, "%")}
- 资产负债率：{fmt_number(metrics.debt_ratio, "%")}
- 营收增长率：{fmt_number(metrics.revenue_growth, "%")}
- 净利润增长率：{fmt_number(metrics.profit_growth, "%")}
- 每股经营现金流：{fmt_number(metrics.operating_cashflow_per_share, "元")}
""".strip()


def _readable_ai_error(exc: Exception) -> ValueError:
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        status = response.status_code if response is not None else None
        if status in {401, 403}:
            return ValueError("AI API Key 无效或权限不足，请检查设置页中的 API Key")
        if status == 402:
            return ValueError("AI 账户余额不足，请到服务商控制台充值后重试")
        if status == 422:
            return ValueError("AI 模型可能不可用或请求参数不被支持，请检查设置页中的模型名")
        if status == 429:
            return ValueError("AI 请求过快或额度限制，请稍后重试")
        if status and status >= 500:
            return ValueError(f"AI 服务暂时不可用（HTTP {status}），请稍后重试")
        if status:
            return ValueError(f"AI 请求失败（HTTP {status}），请检查模型、额度或网络")
    if isinstance(exc, requests.exceptions.Timeout):
        return ValueError("AI 请求超时，请稍后重试")
    if isinstance(exc, requests.exceptions.RequestException):
        return ValueError(f"AI 网络请求失败：{exc}")
    return ValueError(f"AI 请求失败：{exc}")


def _message_content_from_payload(payload: dict) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("AI 返回结构异常，请稍后重试或检查模型设置") from exc
    if not isinstance(content, str):
        raise ValueError("AI 返回内容格式异常，请稍后重试或检查模型设置")
    return content.strip()


def analyze_with_ai(config: AIConfig, quote: StockQuote, metrics: FinancialMetrics) -> str:
    if not config.api_key.strip():
        raise ValueError("请先在设置页填写 API Key")

    provider = config.provider.lower()
    if provider == "openai":
        url = "https://api.openai.com/v1/chat/completions"
        model = config.model or "gpt-4.1-mini"
    elif provider == "deepseek":
        url = "https://api.deepseek.com/v1/chat/completions"
        model = config.model or "deepseek-v4-flash"
    else:
        raise ValueError(f"暂不支持的AI服务商：{config.provider}")

    prompt = build_prompt(quote, metrics)
    max_tokens = 700
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": max_tokens,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise _readable_ai_error(exc) from exc
    content = _message_content_from_payload(payload)
    input_tokens, output_tokens, cached_tokens = usage_from_payload(payload)
    cost_note = format_cost_note(
        provider,
        model,
        estimate_tokens(prompt),
        max_tokens,
        input_tokens,
        output_tokens,
        cached_tokens,
    )
    return f"{content}\n\n{cost_note}"
