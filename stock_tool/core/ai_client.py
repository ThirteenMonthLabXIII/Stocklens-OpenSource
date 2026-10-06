from __future__ import annotations

import json
import re
from dataclasses import dataclass

import lazy_loader as lazy

# 惰性加载 requests：仅在首次校验 Key / 发起请求时才导入，避免拖慢启动。
requests = lazy.load("requests")

from stock_tool.core.ai_analyzer import AIConfig
from stock_tool.core.ai_cost import estimate_tokens, format_cost_note, usage_from_payload


@dataclass(frozen=True)
class AIValidationResult:
    ok: bool
    status: str
    message: str
    provider: str
    model: str
    model_available: bool = False
    balance_available: bool | None = None
    http_status: int | None = None


def endpoint_for_config(config: AIConfig) -> tuple[str, str]:
    provider = config.provider.lower()
    if provider == "openai":
        return "https://api.openai.com/v1/chat/completions", config.model or "gpt-4.1-mini"
    if provider == "deepseek":
        return "https://api.deepseek.com/v1/chat/completions", config.model or "deepseek-v4-flash"
    raise ValueError(f"暂不支持的AI服务商：{config.provider}")


def parse_json_response(content: str) -> dict:
    text = (content or "").strip()
    def reject_constant(value: str) -> None:
        raise ValueError(f"AI返回JSON包含非法数字：{value}")

    try:
        payload = json.loads(text, parse_constant=reject_constant)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise ValueError("AI返回内容不是JSON，无法解析")
        payload = json.loads(match.group(), parse_constant=reject_constant)
    if not isinstance(payload, dict):
        raise ValueError("AI返回JSON必须是对象")
    return payload


def _readable_request_error(exc: Exception, provider: str = "") -> ValueError:
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


def _validation_from_error(exc: Exception, provider: str, model: str) -> AIValidationResult:
    http_status = None
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        http_status = exc.response.status_code
        if http_status in {401, 403}:
            return AIValidationResult(False, "invalid_key", "API Key 无效或权限不足", provider, model, http_status=http_status)
        if http_status == 402:
            return AIValidationResult(False, "balance_insufficient", "余额不足", provider, model, http_status=http_status)
        if http_status == 422:
            return AIValidationResult(False, "model_unavailable", "模型可能不可用", provider, model, http_status=http_status)
        if http_status == 429:
            return AIValidationResult(False, "rate_limited", "请求过快或额度限制", provider, model, http_status=http_status)
        if http_status >= 500:
            return AIValidationResult(False, "service_unavailable", f"服务暂时不可用（HTTP {http_status}）", provider, model, http_status=http_status)
        return AIValidationResult(False, "request_failed", f"验证失败（HTTP {http_status}）", provider, model, http_status=http_status)
    if isinstance(exc, requests.exceptions.Timeout):
        return AIValidationResult(False, "timeout", "请求超时", provider, model)
    if isinstance(exc, requests.exceptions.RequestException):
        return AIValidationResult(False, "network_error", f"网络异常：{exc}", provider, model)
    return AIValidationResult(False, "request_failed", f"验证失败：{exc}", provider, model)


def _models_from_payload(payload: dict) -> set[str]:
    data = payload.get("data")
    if not isinstance(data, list):
        return set()
    models: set[str] = set()
    for item in data:
        if isinstance(item, dict):
            value = str(item.get("id") or "").strip()
            if value:
                models.add(value)
    return models


def _model_validation_result(provider: str, model: str, models: set[str]) -> AIValidationResult | None:
    if not models:
        return AIValidationResult(False, "model_unavailable", "模型列表为空或格式异常，模型可能不可用", provider, model)
    if model not in models:
        return AIValidationResult(False, "model_unavailable", "模型可能不可用", provider, model, model_available=False)
    return None


def _deepseek_balance_available(payload: dict) -> bool:
    if payload.get("is_available") is False:
        return False
    infos = payload.get("balance_infos")
    if not isinstance(infos, list):
        return True
    totals = []
    for item in infos:
        if not isinstance(item, dict):
            continue
        raw = item.get("total_balance")
        try:
            totals.append(float(raw))
        except (TypeError, ValueError):
            continue
    return True if not totals else any(value > 0 for value in totals)


def _get_json(url: str, api_key: str, timeout: int) -> dict:
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def validate_ai_key(config: AIConfig, timeout: int = 20) -> AIValidationResult:
    provider = config.provider.lower().strip()
    try:
        _chat_url, model = endpoint_for_config(config)
    except ValueError as exc:
        return AIValidationResult(False, "unsupported_provider", str(exc), provider or config.provider, config.model)
    api_key = config.api_key.strip()
    if not api_key:
        return AIValidationResult(False, "empty", "未填写", provider, model)

    try:
        if provider == "openai":
            models = _models_from_payload(_get_json("https://api.openai.com/v1/models", api_key, timeout))
            model_error = _model_validation_result(provider, model, models)
            if model_error:
                return model_error
            return AIValidationResult(True, "ok", "验证通过", provider, model, model_available=True)

        if provider == "deepseek":
            models = _models_from_payload(_get_json("https://api.deepseek.com/models", api_key, timeout))
            model_error = _model_validation_result(provider, model, models)
            if model_error:
                return model_error
            balance_payload = _get_json("https://api.deepseek.com/user/balance", api_key, timeout)
            balance_available = _deepseek_balance_available(balance_payload)
            if not balance_available:
                return AIValidationResult(
                    False,
                    "balance_insufficient",
                    "余额不足",
                    provider,
                    model,
                    model_available=True,
                    balance_available=False,
                )
            return AIValidationResult(
                True,
                "ok",
                "验证通过",
                provider,
                model,
                model_available=True,
                balance_available=True,
            )
    except Exception as exc:
        return _validation_from_error(exc, provider, model)

    return AIValidationResult(False, "unsupported_provider", f"暂不支持的AI服务商：{config.provider}", provider, model)


def _message_content_from_payload(payload: dict) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("AI 返回结构异常，请稍后重试或检查模型设置") from exc
    if not isinstance(content, str):
        raise ValueError("AI 返回内容格式异常，请稍后重试或检查模型设置")
    return content.strip()


def request_ai_text(
    config: AIConfig,
    prompt: str,
    max_tokens: int = 1000,
    timeout: int = 60,
) -> tuple[str, str]:
    if not config.api_key.strip():
        raise ValueError("请先在设置页填写 API Key")

    url, model = endpoint_for_config(config)
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        response_payload = response.json()
    except Exception as exc:
        raise _readable_request_error(exc, config.provider.lower()) from exc

    content = _message_content_from_payload(response_payload)
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
    return content, cost_note


def request_ai_json(
    config: AIConfig,
    prompt: str,
    max_tokens: int = 1000,
    timeout: int = 60,
) -> tuple[dict, str]:
    content, cost_note = request_ai_text(config, prompt, max_tokens=max_tokens, timeout=timeout)
    payload = parse_json_response(content)
    return payload, cost_note


def request_ai_json_with_raw(
    config: AIConfig,
    prompt: str,
    max_tokens: int = 1000,
    timeout: int = 60,
) -> tuple[dict, str, str]:
    """Return parsed JSON together with the exact model response text.

    The normal helper intentionally keeps its historical two-value API.  The
    evidence-package path uses this variant so a malformed or truncated model
    response can still be persisted and replayed from the audit record.
    """

    content, cost_note = request_ai_text(config, prompt, max_tokens=max_tokens, timeout=timeout)
    try:
        payload = parse_json_response(content)
    except Exception as exc:
        error = ValueError(str(exc))
        error.raw_response = content  # type: ignore[attr-defined]
        raise error from exc
    return payload, cost_note, content
