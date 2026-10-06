from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.ai_analyzer import AIConfig, analyze_with_ai
from stock_tool.core.ai_client import parse_json_response, request_ai_text, validate_ai_key
from stock_tool.core.ai_ranker import rank_candidates
from stock_tool.core.models import FinancialMetrics, MarketSentiment, MoneyFlowSnapshot, StockQuote, TradeReference
from stock_tool.core.nl_query import parse_natural_language_query
from stock_tool.core.query_schema import QueryValidationError
from stock_tool.core.technical_profile import TechnicalProfile
from stock_tool.core.trade_ai import analyze_trade_observation_with_ai
import stock_tool.core.ai_client as ai_client_module
import stock_tool.core.ai_analyzer as ai_analyzer_module
import stock_tool.core.ai_ranker as ai_ranker_module
import stock_tool.core.nl_query as nl_query_module
import stock_tool.core.trade_ai as trade_ai_module


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {
            "choices": [{"message": {"content": json.dumps({"conditions": []})}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self) -> dict:
        return self._payload


def _expect_request_error(post_fn, expected: str) -> None:
    original_post = ai_client_module.requests.post
    ai_client_module.requests.post = post_fn
    try:
        try:
            request_ai_text(AIConfig(provider="deepseek", model="", api_key="x"), "{}", timeout=1)
        except ValueError as exc:
            if expected not in str(exc):
                raise AssertionError(f"错误提示不符合预期：expected={expected!r} actual={exc}") from exc
        else:
            raise AssertionError("AI 请求异常应被转为 ValueError")
    finally:
        ai_client_module.requests.post = original_post


def test_request_error_messages() -> None:
    _expect_request_error(lambda *_args, **_kwargs: FakeResponse(401), "API Key 无效")
    _expect_request_error(lambda *_args, **_kwargs: FakeResponse(403), "API Key 无效")
    _expect_request_error(lambda *_args, **_kwargs: FakeResponse(402), "余额不足")
    _expect_request_error(lambda *_args, **_kwargs: FakeResponse(422), "模型可能不可用")
    _expect_request_error(lambda *_args, **_kwargs: FakeResponse(429), "请求过快")
    _expect_request_error(lambda *_args, **_kwargs: FakeResponse(500), "服务暂时不可用")

    def timeout_post(*_args, **_kwargs):
        raise requests.exceptions.Timeout("timeout")

    _expect_request_error(timeout_post, "请求超时")

    def network_post(*_args, **_kwargs):
        raise requests.exceptions.ConnectionError("network down")

    _expect_request_error(network_post, "网络请求失败")

    _expect_request_error(lambda *_args, **_kwargs: FakeResponse(200, {"unexpected": []}), "返回结构异常")
    print("[OK] AI HTTP/网络异常提示通过")


def test_validation_failure_modes() -> None:
    original_get = ai_client_module.requests.get
    try:
        def openai_models(*_args, **_kwargs):
            return FakeResponse(200, {"data": [{"id": "gpt-4.1-mini"}]})

        ai_client_module.requests.get = openai_models
        assert validate_ai_key(AIConfig(provider="openai", model="", api_key="x"), timeout=1).status == "ok"

        ai_client_module.requests.get = lambda *_args, **_kwargs: FakeResponse(401, {})
        assert validate_ai_key(AIConfig(provider="openai", model="", api_key="bad"), timeout=1).status == "invalid_key"

        ai_client_module.requests.get = lambda *_args, **_kwargs: FakeResponse(403, {})
        assert validate_ai_key(AIConfig(provider="openai", model="", api_key="bad"), timeout=1).status == "invalid_key"

        ai_client_module.requests.get = lambda *_args, **_kwargs: FakeResponse(429, {})
        assert validate_ai_key(AIConfig(provider="openai", model="", api_key="x"), timeout=1).status == "rate_limited"

        ai_client_module.requests.get = lambda *_args, **_kwargs: FakeResponse(500, {})
        assert validate_ai_key(AIConfig(provider="openai", model="", api_key="x"), timeout=1).status == "service_unavailable"

        def timeout_get(*_args, **_kwargs):
            raise requests.exceptions.Timeout("timeout")

        ai_client_module.requests.get = timeout_get
        assert validate_ai_key(AIConfig(provider="openai", model="", api_key="x"), timeout=1).status == "timeout"

        def network_get(*_args, **_kwargs):
            raise requests.exceptions.ConnectionError("network down")

        ai_client_module.requests.get = network_get
        assert validate_ai_key(AIConfig(provider="openai", model="", api_key="x"), timeout=1).status == "network_error"

        def deepseek_ok(url, *_args, **_kwargs):
            if url.endswith("/models"):
                return FakeResponse(200, {"data": [{"id": "deepseek-v4-flash"}]})
            return FakeResponse(200, {"is_available": True, "balance_infos": [{"total_balance": "0.5"}]})

        ai_client_module.requests.get = deepseek_ok
        assert validate_ai_key(AIConfig(provider="deepseek", model="", api_key="x"), timeout=1).status == "ok"

        def deepseek_no_balance(url, *_args, **_kwargs):
            if url.endswith("/models"):
                return FakeResponse(200, {"data": [{"id": "deepseek-v4-flash"}]})
            return FakeResponse(200, {"is_available": False, "balance_infos": [{"total_balance": "0"}]})

        ai_client_module.requests.get = deepseek_no_balance
        assert validate_ai_key(AIConfig(provider="deepseek", model="", api_key="x"), timeout=1).status == "balance_insufficient"

        ai_client_module.requests.get = lambda *_args, **_kwargs: FakeResponse(402, {})
        assert validate_ai_key(AIConfig(provider="deepseek", model="", api_key="x"), timeout=1).status == "balance_insufficient"

        ai_client_module.requests.get = lambda *_args, **_kwargs: FakeResponse(422, {})
        assert validate_ai_key(AIConfig(provider="deepseek", model="", api_key="x"), timeout=1).status == "model_unavailable"

        ai_client_module.requests.get = lambda *_args, **_kwargs: FakeResponse(200, {"data": []})
        assert validate_ai_key(AIConfig(provider="openai", model="", api_key="x"), timeout=1).status == "model_unavailable"

        ai_client_module.requests.get = lambda *_args, **_kwargs: FakeResponse(200, {"unexpected": []})
        assert validate_ai_key(AIConfig(provider="deepseek", model="", api_key="x"), timeout=1).status == "model_unavailable"
    finally:
        ai_client_module.requests.get = original_get
    print("[OK] API Key 自动验证异常模式通过")


def test_json_failure_modes() -> None:
    for content in ["不是JSON", "[1, 2, 3]", ""]:
        try:
            parse_json_response(content)
        except ValueError:
            pass
        else:
            raise AssertionError(f"非法 JSON 应被拒绝：{content!r}")
    print("[OK] AI JSON异常拒绝通过")


def test_nl_query_failure_modes() -> None:
    original_request = nl_query_module.request_ai_text
    config = AIConfig(provider="deepseek", model="", api_key="x")
    try:
        calls = {"count": 0}

        def bad_then_bad(_config, _prompt, max_tokens=1000, timeout=60):
            calls["count"] += 1
            return "不是JSON", "mock cost"

        nl_query_module.request_ai_text = bad_then_bad
        try:
            parse_natural_language_query(config, "推荐几只股票")
        except QueryValidationError as exc:
            if "AI 解析失败" not in str(exc):
                raise AssertionError(f"坏 JSON 错误提示不符合预期：{exc}") from exc
        else:
            raise AssertionError("连续坏 JSON 应被拒绝")
        if calls["count"] != 2:
            raise AssertionError(f"坏 JSON 应触发一次修复重试，actual={calls['count']}")

        stock_list = {
            "intent": "screen",
            "conditions": [],
            "exclude": ["ST"],
            "stocks": [{"code": "600000", "name": "示例"}],
        }

        def stock_list_output(_config, _prompt, max_tokens=1000, timeout=60):
            return json.dumps(stock_list, ensure_ascii=False), "mock cost"

        nl_query_module.request_ai_text = stock_list_output
        try:
            parse_natural_language_query(config, "推荐几只股票")
        except QueryValidationError as exc:
            if "股票列表" not in str(exc):
                raise AssertionError(f"股票列表错误提示不符合预期：{exc}") from exc
        else:
            raise AssertionError("AI 返回股票列表应被拒绝")
    finally:
        nl_query_module.request_ai_text = original_request
    print("[OK] 自然语言AI异常模式通过")


def _fake_candidate():
    return type(
        "Candidate",
        (),
        {
            "quote": StockQuote(code="000001", name="样例", price=10, pe=12, pb=1.2),
            "metrics": FinancialMetrics(roe=15, gross_margin=35, net_margin=12, debt_ratio=45),
            "technical": TechnicalProfile(False, summary="未计算"),
            "local_score": 80,
        },
    )()


def _rank_response(content: str) -> FakeResponse:
    return FakeResponse(
        200,
        {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def _trade_ai_payload(**overrides) -> dict:
    payload = {
        "can_buy": "wait",
        "action": "watch",
        "entry_price": 10.5,
        "sell_price": 12.5,
        "stop_price": 9.2,
        "trend_forecast": "等待触发",
        "main_force_behavior": "unknown 待确认",
        "confidence": 5,
        "opportunity_level": "A",
        "risk_reward": 2.0,
        "buy_condition": "站上触发价",
        "sell_condition": "达到目标价",
        "stop_condition": "跌破失效价",
        "reasoning_summary": "候选形态需等待确认",
        "evidence": ["候选"],
        "counter_evidence": ["未触发"],
        "data_gaps": [],
        "disclaimer": "仅为数据观察参考，不构成投资建议",
    }
    payload.update(overrides)
    return payload


def test_ai_rank_failure_modes() -> None:
    original_post = ai_ranker_module.requests.post
    config = AIConfig(provider="deepseek", model="", api_key="x")
    try:
        ai_ranker_module.requests.post = lambda *_args, **_kwargs: FakeResponse(200, {"unexpected": []})
        try:
            rank_candidates(config, [_fake_candidate()])
        except ValueError as exc:
            if "返回结构异常" not in str(exc):
                raise AssertionError(f"AI排序结构异常提示不符合预期：{exc}") from exc
        else:
            raise AssertionError("AI排序结构异常应被拒绝")

        ai_ranker_module.requests.post = lambda *_args, **_kwargs: _rank_response('{"summary":"bad","ranks":{}}')
        try:
            rank_candidates(config, [_fake_candidate()])
        except ValueError as exc:
            if "ranks 必须是列表" not in str(exc):
                raise AssertionError(f"AI排序 ranks 类型错误提示不符合预期：{exc}") from exc
        else:
            raise AssertionError("AI排序 ranks 非列表应被拒绝")

        ai_ranker_module.requests.post = lambda *_args, **_kwargs: _rank_response('{"summary":"bad","ranks":[{"code":""}]}')
        try:
            rank_candidates(config, [_fake_candidate()])
        except ValueError as exc:
            if "未返回可用候选结果" not in str(exc):
                raise AssertionError(f"AI排序空结果错误提示不符合预期：{exc}") from exc
        else:
            raise AssertionError("AI排序无有效候选应被拒绝")
    finally:
        ai_ranker_module.requests.post = original_post
    print("[OK] AI排序异常模式通过")


def test_trade_ai_failure_modes() -> None:
    original_post = trade_ai_module.requests.post
    config = AIConfig(provider="deepseek", model="", api_key="x")
    quote = StockQuote(code="000001", name="样例", price=10, pe=12, pb=1.2)
    technical = TechnicalProfile(True, status="candidate", opportunity_label="A", summary="候选", entry_trigger=10.5, fail_level=9.2)
    flow = MoneyFlowSnapshot(code="000001", rows=[], summary="资金缺失")
    sentiment = MarketSentiment(summary="中性")
    reference = TradeReference(
        action="watch",
        level="A",
        entry_price=10.5,
        fail_price=9.2,
        target_price=12.5,
        horizon_days=20,
        position_hint="观察",
        thesis="等待触发",
        risks=["未触发"],
    )
    dirty_prompt = trade_ai_module.build_trade_decision_prompt(
        quote,
        TechnicalProfile(
            True,
            status="candidate",
            opportunity_label="B 量能缺口观察",
            summary="收盘=inf，MA20=nan",
            entry_trigger=float("inf"),
            fail_level=9.2,
            target_level=float("inf"),
            data_gaps=["缺少成交量，机会降级"],
        ),
        flow,
        sentiment,
        [],
        TradeReference(
            action="watch",
            level="B",
            entry_price=float("inf"),
            fail_price=9.2,
            target_price=float("inf"),
            horizon_days=20,
            position_hint="观察",
            thesis="风险收益比 nan",
            risks=["目标 Infinity"],
        ),
    )
    if "Infinity" in dirty_prompt or "NaN" in dirty_prompt or "entry_trigger=inf" in dirty_prompt or "target_level=inf" in dirty_prompt:
        raise AssertionError("交易AI prompt 不应包含裸 NaN/Infinity 技术价位")
    if "data_gaps=缺少成交量，机会降级" not in dirty_prompt:
        raise AssertionError("交易AI prompt 应透传技术画像数据缺口")
    try:
        trade_ai_module.requests.post = lambda *_args, **_kwargs: FakeResponse(200, {"unexpected": []})
        try:
            analyze_trade_observation_with_ai(config, quote, technical, flow, sentiment, [], reference)
        except ValueError as exc:
            if "返回结构异常" not in str(exc):
                raise AssertionError(f"交易AI结构异常提示不符合预期：{exc}") from exc
        else:
            raise AssertionError("交易AI结构异常应被拒绝")

        trade_ai_module.requests.post = lambda *_args, **_kwargs: _rank_response("[1, 2, 3]")
        try:
            analyze_trade_observation_with_ai(config, quote, technical, flow, sentiment, [], reference)
        except ValueError as exc:
            if "JSON必须是对象" not in str(exc):
                raise AssertionError(f"交易AI数组错误提示不符合预期：{exc}") from exc
        else:
            raise AssertionError("交易AI返回数组应被拒绝")

        raw_invalid_number = """
        {
          "can_buy": "wait",
          "action": "watch",
          "entry_price": NaN,
          "sell_price": Infinity,
          "stop_price": 9.2,
          "trend_forecast": "等待触发",
          "main_force_behavior": "unknown 待确认",
          "confidence": 5,
          "opportunity_level": "A",
          "risk_reward": Infinity,
          "buy_condition": "站上触发价",
          "sell_condition": "达到目标价",
          "stop_condition": "跌破失效价",
          "reasoning_summary": "候选形态需等待确认",
          "evidence": ["候选"],
          "counter_evidence": ["未触发"],
          "data_gaps": [],
          "disclaimer": "仅为数据观察参考，不构成投资建议"
        }
        """
        trade_ai_module.requests.post = lambda *_args, **_kwargs: _rank_response(raw_invalid_number)
        try:
            analyze_trade_observation_with_ai(config, quote, technical, flow, sentiment, [], reference)
        except ValueError as exc:
            if "非法数字" not in str(exc):
                raise AssertionError(f"交易AI NaN/Infinity 应被明确拒绝：{exc}") from exc
        else:
            raise AssertionError("交易AI原始 JSON NaN/Infinity 应被拒绝")

        trade_ai_module.requests.post = lambda *_args, **_kwargs: _rank_response(json.dumps(_trade_ai_payload(can_buy="yes", action="buy"), ensure_ascii=False))
        try:
            analyze_trade_observation_with_ai(config, quote, technical, flow, sentiment, [], reference)
        except ValueError as exc:
            if "candidate 形态不能输出 yes" not in str(exc):
                raise AssertionError(f"交易AI硬规则错误提示不符合预期：{exc}") from exc
        else:
            raise AssertionError("candidate 形态下 AI yes 应被拒绝")

        trade_ai_module.requests.post = lambda *_args, **_kwargs: _rank_response(
            json.dumps(_trade_ai_payload(entry_price=99, sell_price=120, stop_price=1), ensure_ascii=False)
        )
        report = analyze_trade_observation_with_ai(config, quote, technical, flow, sentiment, [], reference)
        if "参考买入：10.50元" not in report or "参考卖出：12.50元" not in report or "风控止损：9.20元" not in report:
            raise AssertionError("交易AI价格冲突应回落到本地风控锚点")
        if "本地风控冲突：" not in report or "已回落" not in report:
            raise AssertionError("交易AI价格冲突应显示本地冲突提示")

        trade_ai_module.requests.post = lambda *_args, **_kwargs: _rank_response(
            json.dumps(
                _trade_ai_payload(
                    entry_price="NaN",
                    sell_price="Infinity",
                    stop_price="-Infinity",
                    confidence="NaN",
                    risk_reward="Infinity",
                    trend_forecast="NaN",
                    main_force_behavior="Infinity",
                    evidence=["NaN", "候选"],
                ),
                ensure_ascii=False,
            )
        )
        safe_report = analyze_trade_observation_with_ai(config, quote, technical, flow, sentiment, [], reference)
        lower_report = safe_report.lower()
        if "nan" in lower_report or "inf" in lower_report:
            raise AssertionError(f"交易AI报告不应泄漏 NaN/Infinity：{safe_report}")
        if "参考买入：10.50元" not in safe_report or "参考卖出：12.50元" not in safe_report or "风控止损：9.20元" not in safe_report:
            raise AssertionError("交易AI字符串异常价位应回落到本地风控锚点")
        if "风险收益比：-" not in safe_report or "置信度：-/10" not in safe_report:
            raise AssertionError("交易AI异常置信度/风险收益比应防御格式化")

        trade_ai_module.requests.post = lambda *_args, **_kwargs: _rank_response(
            json.dumps(_trade_ai_payload(opportunity_level="Z", confidence=999, risk_reward=999), ensure_ascii=False)
        )
        sanitized_report = analyze_trade_observation_with_ai(config, quote, technical, flow, sentiment, [], reference)
        if "机会等级：A" not in sanitized_report or "机会等级：Z" in sanitized_report:
            raise AssertionError("非法机会等级应回落到本地观察等级")
        if "置信度：-/10" not in sanitized_report or "999/10" in sanitized_report:
            raise AssertionError("非法置信度不得原样展示")
        if "风险收益比：-" not in sanitized_report or "风险收益比：999" in sanitized_report:
            raise AssertionError("极端风险收益比不得原样展示")
        if "本地风控冲突：" not in sanitized_report or "机会等级" not in sanitized_report or "风险收益比" not in sanitized_report:
            raise AssertionError("非法AI展示字段应记录本地冲突或降级说明")
    finally:
        trade_ai_module.requests.post = original_post
    print("[OK] 交易AI异常模式通过")


def test_basic_ai_failure_modes() -> None:
    original_post = ai_analyzer_module.requests.post
    config = AIConfig(provider="deepseek", model="", api_key="x")
    try:
        ai_analyzer_module.requests.post = lambda *_args, **_kwargs: FakeResponse(200, {"unexpected": []})
        try:
            analyze_with_ai(config, StockQuote(code="000001", name="样例"), FinancialMetrics())
        except ValueError as exc:
            if "返回结构异常" not in str(exc):
                raise AssertionError(f"个股AI结构异常提示不符合预期：{exc}") from exc
        else:
            raise AssertionError("个股AI结构异常应被拒绝")
    finally:
        ai_analyzer_module.requests.post = original_post
    print("[OK] 个股AI异常模式通过")


def main() -> int:
    test_request_error_messages()
    test_validation_failure_modes()
    test_json_failure_modes()
    test_nl_query_failure_modes()
    test_ai_rank_failure_modes()
    test_trade_ai_failure_modes()
    test_basic_ai_failure_modes()
    print("[OK] AI异常外部服务验收通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
