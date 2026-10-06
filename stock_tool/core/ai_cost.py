from __future__ import annotations

from dataclasses import dataclass
import re


USD_TO_CNY = 7.2


@dataclass(frozen=True)
class ModelPricing:
    input_per_1m_usd: float
    output_per_1m_usd: float
    cached_input_per_1m_usd: float | None = None
    source_note: str = ""


PRICING: dict[str, ModelPricing] = {
    "deepseek-chat": ModelPricing(0.14, 0.28, 0.0028, "按 DeepSeek deepseek-chat 兼容 deepseek-v4-flash 非思考模式估算"),
    "deepseek-reasoner": ModelPricing(0.14, 0.28, 0.0028, "按 DeepSeek deepseek-reasoner 兼容 deepseek-v4-flash 思考模式估算"),
    "deepseek-v4-flash": ModelPricing(0.14, 0.28, 0.0028, "DeepSeek V4 Flash"),
    "deepseek-v4-pro": ModelPricing(0.435, 0.87, 0.003625, "DeepSeek V4 Pro"),
    "gpt-4.1-mini": ModelPricing(0.40, 1.60, 0.10, "OpenAI GPT-4.1 mini"),
    "gpt-4.1": ModelPricing(2.00, 8.00, 0.50, "OpenAI GPT-4.1"),
    "gpt-4o-mini": ModelPricing(0.15, 0.60, 0.075, "OpenAI GPT-4o mini"),
    "gpt-5-mini": ModelPricing(0.25, 2.00, 0.025, "OpenAI GPT-5 mini"),
}


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    # Chinese text often maps closer to 1-2 chars/token. Use /2 as a conservative UI estimate.
    return max(1, int(len(text) / 2))


def pricing_for(provider: str, model: str) -> ModelPricing:
    key = (model or "").strip().lower()
    if key in PRICING:
        return PRICING[key]
    if provider.lower() == "openai":
        return PRICING["gpt-4.1-mini"]
    return PRICING["deepseek-v4-flash"]


def _default_model_for(provider: str) -> str:
    return "gpt-4.1-mini" if provider.lower() == "openai" else "deepseek-v4-flash"


def _display_model(provider: str, model: str) -> str:
    return (model or "").strip() or _default_model_for(provider)


def _unknown_model_warning(provider: str, model: str) -> str:
    key = (model or "").strip().lower()
    if not key or key in PRICING:
        return ""
    default_model = _default_model_for(provider)
    return f"未知模型，当前按 {default_model} 默认单价估算，实际费用可能偏差较大。"


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
    pricing = pricing_for(provider, model)
    uncached_input = max(0, input_tokens - cached_input_tokens)
    input_cost = uncached_input / 1_000_000 * pricing.input_per_1m_usd
    cached_cost = 0.0
    if cached_input_tokens and pricing.cached_input_per_1m_usd is not None:
        cached_cost = cached_input_tokens / 1_000_000 * pricing.cached_input_per_1m_usd
    output_cost = max(0, output_tokens) / 1_000_000 * pricing.output_per_1m_usd
    return input_cost + cached_cost + output_cost


def _token_int_or_none(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return max(0, int(text))
    return None


def _cached_token_int(value) -> int:
    parsed = _token_int_or_none(value)
    return parsed if parsed is not None else 0


def _first_present(mapping: dict, *keys: str):
    for key in keys:
        if key in mapping:
            return mapping.get(key)
    return None


def usage_from_payload(payload: dict) -> tuple[int | None, int | None, int]:
    usage = payload.get("usage") or {}
    input_tokens = _first_present(usage, "prompt_tokens", "input_tokens")
    output_tokens = _first_present(usage, "completion_tokens", "output_tokens")
    cached_tokens = 0
    prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if isinstance(prompt_details, dict):
        cached_tokens = _cached_token_int(prompt_details.get("cached_tokens"))
    input_tokens = _token_int_or_none(input_tokens)
    output_tokens = _token_int_or_none(output_tokens)
    if input_tokens is not None:
        cached_tokens = min(cached_tokens, input_tokens)
    return input_tokens, output_tokens, cached_tokens


def format_cost_note(
    provider: str,
    model: str,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    actual_input_tokens: int | None = None,
    actual_output_tokens: int | None = None,
    cached_input_tokens: int = 0,
) -> str:
    pricing = pricing_for(provider, model)
    estimate_usd = cost_usd(provider, model, estimated_input_tokens, estimated_output_tokens)
    warning = _unknown_model_warning(provider, model)
    lines = [
        "AI费用透明提示",
        f"模型：{_display_model(provider, model)}（{pricing.source_note}）",
        f"估算用量：输入约 {estimated_input_tokens} tokens，输出最多约 {estimated_output_tokens} tokens",
        f"估算费用：约 ${estimate_usd:.6f}，约人民币 {estimate_usd * USD_TO_CNY:.4f} 元",
    ]
    if warning:
        lines.append(warning)
    if actual_input_tokens is not None and actual_output_tokens is not None:
        actual_usd = cost_usd(provider, model, actual_input_tokens, actual_output_tokens, cached_input_tokens)
        cache_text = f"，其中缓存输入 {cached_input_tokens} tokens" if cached_input_tokens else ""
        lines.append(f"接口返回实际用量：输入 {actual_input_tokens} tokens，输出 {actual_output_tokens} tokens{cache_text}")
        lines.append(f"按返回用量估算费用：约 ${actual_usd:.6f}，约人民币 {actual_usd * USD_TO_CNY:.4f} 元")
    lines.append("说明：这是按公开单价和 token 粗略换算的透明估算，最终扣费以 API 平台账单为准。")
    return "\n".join(lines)


def pricing_summary(provider: str, model: str) -> str:
    pricing = pricing_for(provider, model)
    warning = _unknown_model_warning(provider, model)
    summary = (
        f"AI费用参考：{_display_model(provider, model)} "
        f"输入 ${pricing.input_per_1m_usd}/百万tokens，输出 ${pricing.output_per_1m_usd}/百万tokens；"
        "每次调用前后会显示估算费用，实际以平台账单为准。"
    )
    if warning:
        summary = f"{summary} {warning}"
    return summary
