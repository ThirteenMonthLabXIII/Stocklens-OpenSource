from __future__ import annotations

import json
import sys
import tempfile
import types
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.ai_analyzer import AIConfig, build_prompt
from stock_tool.core.ai_analyzer import analyze_with_ai
from stock_tool.core.ai_client import AIValidationResult, endpoint_for_config, parse_json_response, validate_ai_key
from stock_tool.core.ai_cost import cost_usd, estimate_tokens, format_cost_note, pricing_for, pricing_summary, usage_from_payload
from stock_tool.core.ai_ranker import rank_candidates
import stock_tool.core.ai_ranker as ai_ranker_module
from stock_tool.core.cache import CacheStore
import stock_tool.core.data_fetcher as data_fetcher_module
from stock_tool.core.data_fetcher import _to_float
from stock_tool.core.data_fetcher import DataFetcher, MarketDataError
from stock_tool.core.data_location import db_path_for, migrate_data_dir, read_configured_data_dir, read_data_dir, write_data_dir
from stock_tool.core.models import (
    EventCatalyst,
    FinancialMetrics,
    MarketSentiment,
    MoneyFlowSnapshot,
    StockQuote,
    fmt_number,
)
from stock_tool.core.secrets import AI_API_KEY_NAME, SecretStore
from stock_tool.core.technical_profile import TechnicalProfile, build_technical_profile
from stock_tool.core.trade_ai import format_trade_ai_report
from stock_tool.core.trade_observer import build_trade_reference, due_date_from_now, infer_main_force_behavior, review_tracking_task


def test_models_and_parsing() -> None:
    assert fmt_number(None) == "-"
    assert fmt_number("bad") == "-"
    assert fmt_number(float("nan")) == "-"
    assert fmt_number(float("inf")) == "-"
    assert fmt_number(12.345, "%", digits=1) == "12.3%"
    assert _to_float("1,234.5%") == 1234.5
    assert _to_float("--") is None
    assert _to_float(None) is None
    assert _to_float("Infinity") is None
    assert _to_float("-inf") is None
    assert _to_float(float("inf")) is None
    print("[OK] 区块1 模型与基础数值解析")


def test_data_location_cache_secret() -> None:
    old_argv = sys.argv[:]
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        sys.argv = [str(base / "main.py")]
        data_dir = base / "data"
        write_data_dir(data_dir)
        assert read_data_dir() == data_dir.resolve()
        assert db_path_for(data_dir).name == "stock_tool.sqlite3"
        missing_data_dir = base / "missing"
        (base / "data_location.txt").write_text(str(missing_data_dir), encoding="utf-8")
        assert read_configured_data_dir() == missing_data_dir.resolve()
        assert read_data_dir() is None
        write_data_dir(data_dir)

        old_dir = base / "old"
        new_dir = base / "new"
        old_dir.mkdir()
        (old_dir / "keep.txt").write_text("ok", encoding="utf-8")
        new_dir.mkdir()
        (new_dir / "keep.txt").write_text("stale", encoding="utf-8")
        migrate_data_dir(old_dir, new_dir)
        assert (new_dir / "keep.txt").read_text(encoding="utf-8") == "ok"

        cache = CacheStore(base / "cache.sqlite3")
        cache.set_json("sample", {"a": 1})
        assert cache.get_json("sample").payload == {"a": 1}
        cache.add_watch("000001", "平安银行")
        assert cache.list_watch()[0]["code"] == "000001"
        cache.add_fetch_log("测试", status="success", message="ok")
        assert cache.fetch_log_summary()["success"] >= 1
        cache.set_json(
            "spot_quotes",
            [
                StockQuote(code=f"{idx:06d}", name=f"样例{idx}", change_pct=1 if idx % 2 else -1).__dict__
                for idx in range(1200)
            ],
        )
        sentiment, sentiment_time = DataFetcher(cache).market_sentiment()
        assert sentiment.up + sentiment.down >= 1000
        assert sentiment_time == cache.get_json("spot_quotes").updated_at
        assert not any(item.key == "market_sentiment" for item in cache.list_cache_metadata()), "行情缓存派生情绪不应伪装成新情绪缓存"
        legacy_db = base / "legacy_cache.sqlite3"
        conn = sqlite3.connect(legacy_db)
        try:
            conn.execute(
                """
                CREATE TABLE tracking_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    entry_price REAL,
                    fail_price REAL,
                    target_price REAL,
                    horizon_days INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    note TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO tracking_tasks(
                    code, name, task_type, status, entry_price, fail_price, target_price,
                    horizon_days, created_at, due_date, note
                )
                VALUES('000002', '旧任务', 'watch', 'open', 10, 9, 12, 5, '2026-01-01T00:00:00', '2099-01-01', '旧库')
                """
            )
            conn.commit()
        finally:
            conn.close()
        legacy_cache = CacheStore(legacy_db)
        legacy_task = legacy_cache.list_tracking_tasks(include_closed=True)[0]
        assert legacy_task.result == "" and legacy_task.last_review == "", "旧 tracking_tasks 表应自动补 result/last_review"
        first_task_id = cache.add_tracking_task("000001", "平安银行", "watch", 10, 9, 12, 5, "2099-01-01", note="a" * 1200)
        duplicate_task_id = cache.add_tracking_task("000001", "平安银行", "watch", 10, 9, 12, 5, "2099-01-01", note="重复")
        holding_task_id = cache.add_tracking_task("000001", "平安银行", "holding", 10, 9, 12, 5, "2099-01-01", note="持仓")
        tasks = cache.list_tracking_tasks(include_closed=True)
        assert first_task_id is not None
        assert duplicate_task_id is None
        assert holding_task_id is not None
        assert len([item for item in tasks if item.code == "000001" and item.task_type == "watch"]) == 1
        assert tasks[-1].note.endswith("...[已截断]") and len(tasks[-1].note) <= 1000

        secrets = SecretStore(cache, use_keyring=False)
        assert secrets.set_secret(AI_API_KEY_NAME, " key ") is False
        assert secrets.get_secret(AI_API_KEY_NAME) == "key"
        secrets.delete_secret(AI_API_KEY_NAME)
        assert secrets.get_secret(AI_API_KEY_NAME) == ""

        class FakeCredentialBackend:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}

            def get(self, name: str) -> str:
                return self.values.get(name, "")

            def set(self, name: str, value: str) -> None:
                self.values[name] = value

            def delete(self, name: str) -> None:
                self.values.pop(name, None)

        secure = SecretStore(cache, use_keyring=False)
        secure.backend = FakeCredentialBackend()
        assert secure.set_secret(AI_API_KEY_NAME, " secure-key ") is True
        assert secure.get_secret(AI_API_KEY_NAME) == "secure-key"
        assert cache.get_setting(AI_API_KEY_NAME, "") == "", "凭据后端成功时不应保留数据库明文"

        cache.set_setting(AI_API_KEY_NAME, " legacy-key ")
        migrated = SecretStore(cache, use_keyring=False)
        migrated.backend = FakeCredentialBackend()
        assert migrated.migrate_legacy_secret(AI_API_KEY_NAME) is True
        assert migrated.get_secret(AI_API_KEY_NAME) == "legacy-key"
        assert cache.get_setting(AI_API_KEY_NAME, "") == "", "旧数据库明文应迁移后删除"

    sys.argv = old_argv
    print("[OK] 区块2 数据目录、缓存与密钥")


def _age_cache_record(cache: CacheStore, key: str, updated_at: datetime) -> None:
    with cache.connect() as conn:
        conn.execute("UPDATE cache SET updated_at = ? WHERE key = ?", (updated_at.isoformat(timespec="seconds"), key))


def test_fetcher_cache_fallback_edges() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = CacheStore(Path(tmp) / "cache.sqlite3")

        direct_calls = {"count": 0}
        original_direct = data_fetcher_module.fetch_direct_quote
        try:
            def fake_direct(_code: str):
                direct_calls["count"] += 1
                return None

            data_fetcher_module.fetch_direct_quote = fake_direct
            fetcher = DataFetcher(cache)
            fetcher.spot_quotes = lambda: ([], None)  # type: ignore[method-assign]
            assert fetcher.quote_by_code("1") is None
            assert direct_calls["count"] == 1, "单股实时失败后不应重复请求同一 direct 源"
            assert cache.list_fetch_logs(1)[0].action == "个股实时"
        finally:
            data_fetcher_module.fetch_direct_quote = original_direct

        cache.set_json(
            "spot_quotes",
            [StockQuote(code=f"{idx:06d}", name=f"样例{idx}", change_pct=1).__dict__ for idx in range(1200)],
        )
        original_sentiment = data_fetcher_module._sentiment_from_eastmoney_http
        try:
            data_fetcher_module._sentiment_from_eastmoney_http = lambda: MarketSentiment(up=1, down=0, flat=0, summary="fresh")  # type: ignore[assignment]
            sentiment, _ = DataFetcher(cache).market_sentiment(force=True)
            assert sentiment.summary == "fresh"
            assert cache.list_fetch_logs(1)[0].source == "eastmoney_http"
        finally:
            data_fetcher_module._sentiment_from_eastmoney_http = original_sentiment

        original_akshare = sys.modules.get("akshare")
        sys.modules["akshare"] = types.SimpleNamespace(stock_news_em=lambda symbol: pd.DataFrame([{"标题": ""}]))
        try:
            events, _ = DataFetcher(cache).event_catalysts("000001", force=True)
            assert events == []
            assert cache.get_json("events:000001") is None, "空事件不应写入6小时缓存"
        finally:
            if original_akshare is None:
                sys.modules.pop("akshare", None)
            else:
                sys.modules["akshare"] = original_akshare

        cache.set_json("financial:000001", FinancialMetrics(roe=10).__dict__)
        _age_cache_record(cache, "financial:000001", datetime.now() - timedelta(days=30))
        original_akshare = sys.modules.get("akshare")
        sys.modules["akshare"] = types.SimpleNamespace(stock_financial_analysis_indicator=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
        try:
            try:
                DataFetcher(cache).financial_metrics("000001", force=True)
            except MarketDataError:
                pass
            else:
                raise AssertionError("超过最大兜底年龄的财务缓存不应继续使用")
            assert "超过兜底上限" in cache.list_fetch_logs(2)[0].message or "超过兜底上限" in cache.list_fetch_logs(2)[1].message
        finally:
            if original_akshare is None:
                sys.modules.pop("akshare", None)
            else:
                sys.modules["akshare"] = original_akshare

        original_history = data_fetcher_module._history_from_tencent_http
        original_sina_history = data_fetcher_module._history_from_sina_http
        original_eastmoney_history = data_fetcher_module._history_from_eastmoney_http
        original_akshare_history = data_fetcher_module._history_from_akshare
        try:
            def fake_history(_code: str, days: int):
                assert days >= data_fetcher_module.HISTORY_CANONICAL_DAYS
                return pd.DataFrame(
                    [{"date": "2026-01-01", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 100}]
                )

            data_fetcher_module._history_from_tencent_http = fake_history  # type: ignore[assignment]
            DataFetcher(cache).history("000001", days=180, force=True)
            keys = {item.key for item in cache.list_cache_metadata()}
            assert "history:000001" in keys
            assert "history:000001:180" not in keys

            def mixed_dirty_history(_code: str, _days: int):
                return pd.DataFrame(
                    [
                        {"date": "bad-date", "open": 1, "high": float("inf"), "low": 1, "close": 2, "volume": 100},
                        {"date": "2026-01-02", "open": 2, "high": 2.5, "low": 1.8, "close": 2.2, "volume": "Infinity"},
                    ]
                )

            data_fetcher_module._history_from_tencent_http = mixed_dirty_history  # type: ignore[assignment]
            history, _ = DataFetcher(cache).history("000002", days=180, force=True)
            assert len(history) == 1 and history.iloc[0]["date"] == "2026-01-02"
            assert pd.isna(history.iloc[0]["volume"]), "无效成交量应置空，不应丢弃有效OHLC"
            cached_history = cache.get_json("history:000002")
            assert cached_history is not None and len(cached_history.payload) == 1

            cache.set_json(
                "history:000003",
                [{"date": "bad-date", "open": 1, "high": float("inf"), "low": 1, "close": 2, "volume": 100}],
            )
            data_fetcher_module._history_from_tencent_http = lambda *_args: (_ for _ in ()).throw(RuntimeError("down"))  # type: ignore[assignment]
            data_fetcher_module._history_from_sina_http = data_fetcher_module._history_from_tencent_http  # type: ignore[assignment]
            data_fetcher_module._history_from_eastmoney_http = data_fetcher_module._history_from_tencent_http  # type: ignore[assignment]
            data_fetcher_module._history_from_akshare = data_fetcher_module._history_from_tencent_http  # type: ignore[assignment]
            try:
                DataFetcher(cache).history("000003", days=180)
            except MarketDataError:
                pass
            else:
                raise AssertionError("全部K线缓存校验失败时不应继续使用旧缓存")
        finally:
            data_fetcher_module._history_from_tencent_http = original_history
            data_fetcher_module._history_from_sina_http = original_sina_history
            data_fetcher_module._history_from_eastmoney_http = original_eastmoney_history
            data_fetcher_module._history_from_akshare = original_akshare_history

    print("[OK] 区块2B 数据抓取缓存边界")


def test_technical_profile() -> None:
    rows = []
    for idx in range(90):
        close = 10 + idx * 0.08
        rows.append(
            {
                "收盘": close,
                "最高": close + 0.25,
                "最低": close - 0.2,
                "开盘": close - 0.04,
                "成交量": 1000 + idx * 10,
            }
        )
    profile = build_technical_profile(pd.DataFrame(rows))
    assert profile.available
    assert profile.close is not None
    assert profile.ma20 is not None
    assert profile.opportunity_label
    assert build_technical_profile(pd.DataFrame()).available is False
    short_profile = build_technical_profile(pd.DataFrame(rows[:10]))
    assert short_profile.available is False
    assert short_profile.status == "low_data"

    dirty_last = pd.DataFrame(rows)
    dirty_last.loc[len(dirty_last) - 1, "收盘"] = float("inf")
    dirty_profile = build_technical_profile(dirty_last)
    assert dirty_profile.available
    dirty_text = json.dumps(dirty_profile.__dict__, ensure_ascii=False)
    assert "inf" not in dirty_text.lower() and "nan" not in dirty_text.lower()
    assert dirty_profile.data_gaps and "异常" in dirty_profile.data_gaps[0]

    dirty_short = pd.DataFrame(rows[:60])
    dirty_short.loc[len(dirty_short) - 1, "收盘"] = None
    dirty_short_profile = build_technical_profile(dirty_short)
    assert dirty_short_profile.available is False
    assert "有效样本仅59条" in dirty_short_profile.summary

    no_volume = pd.DataFrame([{key: value for key, value in row.items() if key != "成交量"} for row in rows])
    no_volume_profile = build_technical_profile(no_volume)
    assert no_volume_profile.available
    assert not no_volume_profile.opportunity_label.startswith(("A", "S"))
    assert no_volume_profile.opportunity_label.startswith(("B", "C"))
    assert any("机会降级" in item for item in no_volume_profile.risk_flags)
    assert any("机会降级" in item for item in no_volume_profile.data_gaps)
    print("[OK] 区块3 技术画像")


def test_trade_reference_and_tracking_review() -> None:
    technical = TechnicalProfile(
        available=True,
        status="triggered",
        trend_stage="uptrend",
        opportunity_label="S 已触发机会",
        entry_trigger=10,
        fail_level=9,
        target_level=13,
        summary="趋势向上",
    )
    flow = MoneyFlowSnapshot(
        code="000001",
        rows=[{"main": 1}, {"main": 2}, {"main": 3}],
        main_3d=6,
        main_5d=6,
        main_10d=3,
        xlarge_5d=1,
        small_5d=-1,
        streak=3,
        summary="主力流入",
    )
    assert "连续流入" in infer_main_force_behavior(flow)
    reference = build_trade_reference(
        StockQuote(code="000001", name="样例", price=10),
        technical,
        flow,
        MarketSentiment(up=2000, down=1000, summary="偏暖"),
        [EventCatalyst(title="事件")],
    )
    assert reference.level == "S"
    assert reference.entry_price == 10
    assert "本软件" not in reference.thesis
    assert due_date_from_now(0)

    class Task:
        fail_price = 9
        target_price = 12
        due_date = "2099-01-01"

    assert review_tracking_task(Task(), StockQuote(code="000001", name="样例", price=8.9))[0] == "closed"
    assert review_tracking_task(Task(), StockQuote(code="000001", name="样例", price=12.1))[0] == "closed"
    assert review_tracking_task(Task(), StockQuote(code="000001", name="样例", price=10))[0] == "open"

    class DirtyTask:
        fail_price = 9
        target_price = 12
        due_date = "bad-date"

    dirty_status, dirty_result, dirty_review = review_tracking_task(DirtyTask(), StockQuote(code="000001", name="样例", price=10))
    assert dirty_status == "open"
    assert dirty_result == "数据异常"
    assert "到期日格式无效" in dirty_review
    print("[OK] 区块4 交易观察与跟踪复盘")


def test_ai_helpers_without_network() -> None:
    assert endpoint_for_config(AIConfig(provider="openai", model="", api_key="x"))[1] == "gpt-4.1-mini"
    assert endpoint_for_config(AIConfig(provider="deepseek", model="", api_key="x"))[1] == "deepseek-v4-flash"
    try:
        endpoint_for_config(AIConfig(provider="bad", model="", api_key="x"))
    except ValueError as exc:
        assert "暂不支持" in str(exc)
    else:
        raise AssertionError("未知AI服务商应被拒绝")
    assert parse_json_response('prefix {"conditions": []} suffix') == {"conditions": []}
    try:
        parse_json_response("[1, 2]")
    except ValueError:
        pass
    else:
        raise AssertionError("AI JSON 数组应被拒绝")

    assert estimate_tokens("abcd") == 2
    assert pricing_for("openai", "").input_per_1m_usd > 0
    assert cost_usd("deepseek", "deepseek-chat", 1000, 1000) > 0
    payload = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 3}}}
    assert usage_from_payload(payload) == (10, 5, 3)
    string_payload = {"usage": {"prompt_tokens": "10", "completion_tokens": "5", "prompt_tokens_details": {"cached_tokens": "3"}}}
    assert usage_from_payload(string_payload) == (10, 5, 3)
    bad_cached_payload = {"usage": {"prompt_tokens": "8", "completion_tokens": "4", "prompt_tokens_details": {"cached_tokens": "bad"}}}
    assert usage_from_payload(bad_cached_payload) == (8, 4, 0)
    clamped_cached_payload = {"usage": {"prompt_tokens": "6", "completion_tokens": "4", "prompt_tokens_details": {"cached_tokens": "99"}}}
    assert usage_from_payload(clamped_cached_payload) == (6, 4, 6)
    invalid_usage_payload = {"usage": {"prompt_tokens": "bad", "completion_tokens": object(), "prompt_tokens_details": {"cached_tokens": "-3"}}}
    assert usage_from_payload(invalid_usage_payload) == (None, None, 0)
    assert "AI费用透明提示" in format_cost_note("deepseek", "deepseek-chat", 10, 20)
    normalized_input, normalized_output, normalized_cached = usage_from_payload(string_payload)
    assert "按返回用量估算费用" in format_cost_note(
        "deepseek",
        "deepseek-chat",
        10,
        20,
        normalized_input,
        normalized_output,
        normalized_cached,
    )
    unknown_openai_summary = pricing_summary("openai", "custom-model")
    unknown_openai_note = format_cost_note("openai", "custom-model", 10, 20)
    assert "未知模型" in unknown_openai_summary and "gpt-4.1-mini" in unknown_openai_summary
    assert "未知模型" in unknown_openai_note and "gpt-4.1-mini" in unknown_openai_note
    unknown_deepseek_summary = pricing_summary("deepseek", "custom-model")
    unknown_deepseek_note = format_cost_note("deepseek", "custom-model", 10, 20)
    assert "未知模型" in unknown_deepseek_summary and "deepseek-v4-flash" in unknown_deepseek_summary
    assert "未知模型" in unknown_deepseek_note and "deepseek-v4-flash" in unknown_deepseek_note
    assert "未知模型" not in pricing_summary("openai", "")
    prompt = build_prompt(StockQuote(code="000001", name="样例"), FinancialMetrics(roe=12))
    assert "000001" in prompt and "ROE" in prompt
    assert AIValidationResult(False, "empty", "未填写", "deepseek", "").status == "empty"

    import stock_tool.core.ai_client as ai_client_module

    class FakeGetResponse:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                import requests

                raise requests.exceptions.HTTPError(response=self)

        def json(self) -> dict:
            return self._payload

    original_get = ai_client_module.requests.get
    try:
        def fake_get(url, *_args, **_kwargs):
            if url.endswith("/models"):
                return FakeGetResponse(200, {"data": [{"id": "deepseek-v4-flash"}]})
            if url.endswith("/user/balance"):
                return FakeGetResponse(200, {"is_available": True, "balance_infos": [{"total_balance": "1.25"}]})
            return FakeGetResponse(404, {})

        ai_client_module.requests.get = fake_get
        validation = validate_ai_key(AIConfig(provider="deepseek", model="", api_key="x"), timeout=1)
        assert validation.ok
        assert validation.model == "deepseek-v4-flash"
        assert validation.balance_available is True

        ai_client_module.requests.get = lambda *_args, **_kwargs: FakeGetResponse(401, {})
        assert validate_ai_key(AIConfig(provider="openai", model="", api_key="bad"), timeout=1).status == "invalid_key"

        def no_balance(url, *_args, **_kwargs):
            if url.endswith("/models"):
                return FakeGetResponse(200, {"data": [{"id": "deepseek-v4-flash"}]})
            return FakeGetResponse(200, {"is_available": False, "balance_infos": [{"total_balance": "0"}]})

        ai_client_module.requests.get = no_balance
        assert validate_ai_key(AIConfig(provider="deepseek", model="", api_key="x"), timeout=1).status == "balance_insufficient"

        ai_client_module.requests.get = lambda *_args, **_kwargs: FakeGetResponse(200, {"data": [{"id": "other-model"}]})
        assert validate_ai_key(AIConfig(provider="openai", model="gpt-missing", api_key="x"), timeout=1).status == "model_unavailable"

        ai_client_module.requests.get = lambda *_args, **_kwargs: FakeGetResponse(200, {"data": []})
        assert validate_ai_key(AIConfig(provider="openai", model="", api_key="x"), timeout=1).status == "model_unavailable"
    finally:
        ai_client_module.requests.get = original_get

    try:
        analyze_with_ai(AIConfig(provider="bad", model="", api_key="x"), StockQuote(code="000001", name="样例"), FinancialMetrics())
    except ValueError as exc:
        assert "暂不支持" in str(exc)
    else:
        raise AssertionError("基本面AI未知服务商应被拒绝")

    report = format_trade_ai_report({"risk_reward": "1.5"})
    assert "风险收益比：1.50" in report
    safe_report = format_trade_ai_report(
        {
            "can_buy": "wait",
            "action": "watch",
            "confidence": float("nan"),
            "risk_reward": float("inf"),
            "entry_price": float("nan"),
            "sell_price": "Infinity",
            "stop_price": "-Infinity",
            "trend_forecast": "NaN",
            "main_force_behavior": "Infinity",
            "evidence": ["NaN", "有效依据"],
        }
    )
    assert "nan" not in safe_report.lower()
    assert "inf" not in safe_report.lower()
    assert "参考买入：-" in safe_report and "风险收益比：-" in safe_report

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "ok",
                                    "ranks": [
                                        {"code": "1", "name": "样例", "ai_score": 120, "level": "Z", "action": "乱写"},
                                        {"code": "", "name": "空代码", "ai_score": 50, "level": "A", "action": "关注"},
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

    original_post = ai_ranker_module.requests.post
    ai_ranker_module.requests.post = lambda *_args, **_kwargs: FakeResponse()
    try:
        candidate = type(
            "Candidate",
            (),
            {
                "quote": StockQuote(code="000001", name="样例", price=10, pe=12, pb=1.2),
                "metrics": FinancialMetrics(roe=15),
                "technical": TechnicalProfile(False, summary="未计算"),
                "local_score": 80,
            },
        )()
        _summary, ranks = rank_candidates(AIConfig(provider="deepseek", model="", api_key="x"), [candidate])
        assert len(ranks) == 1
        assert ranks[0].code == "000001"
        assert ranks[0].ai_score == 100
        assert ranks[0].level == "C"
        assert ranks[0].action == "关注"
    finally:
        ai_ranker_module.requests.post = original_post
    print("[OK] 区块5 AI辅助函数离线校验")


def test_default_entry_and_qt_settings_static() -> None:
    app_entry = (ROOT / "stock_tool" / "ui" / "app_entry.py").read_text(encoding="utf-8")
    qt_workbench = (ROOT / "stock_tool" / "ui" / "qt_workbench.py").read_text(encoding="utf-8")
    assert "stock_tool.ui.qt_workbench" in app_entry, "入口应覆盖 Qt 工作台路径"
    assert "_legacy_frontend_requested" in app_entry, "入口必须拦截已下线的旧前端调用"
    assert "_run_classic" not in app_entry and "_confirm_classic_fallback" not in app_entry
    legacy_module_name = "stock_tool.ui." + "main_window"
    assert legacy_module_name not in app_entry
    assert "Qt 启动失败" in app_entry and "sys.stderr" in app_entry, "Qt 默认入口失败应给出可见提示"
    assert "return 1" in app_entry and "return 2" in app_entry, "启动失败与旧调用必须返回非零状态"
    assert "SecretStore" in qt_workbench and "AI_API_KEY_NAME" in qt_workbench, "Qt 工作台应支持 API Key 保存"
    assert "validate_ai_key" in qt_workbench and "AIValidationResult" in qt_workbench, "Qt 工作台应支持 API Key 自动验证"
    assert "api_key_status_label" in qt_workbench and "保存并验证" in qt_workbench, "Qt 工作台应显示 API Key 验证状态"
    assert "重新验证连接" in qt_workbench, "Qt 工作台设置页应保留二次验证入口"
    assert "实验工作台" not in qt_workbench and "_build_placeholder_page" not in qt_workbench, "P5 正式 Qt 不得保留实验或占位入口"
    assert "ai_cost_label" in qt_workbench and "pricing_summary" in qt_workbench, "Qt 工作台应显示 AI 费用参考"
    assert "currentIndexChanged.connect(self._refresh_ai_cost_label)" in qt_workbench, "Qt 工作台供应商变化应刷新费用参考"
    assert "textChanged.connect(self._refresh_ai_cost_label)" in qt_workbench, "Qt 工作台模型变化应刷新费用参考"
    assert "生成本地观察" in qt_workbench and "不消耗 AI token" in qt_workbench, "Qt 工作台 AI观察应保持本地优先口径"
    assert "AI证据整理" in qt_workbench and "analyze_trade_observation_with_ai" in qt_workbench, "Qt 工作台应提供研究旁路证据整理入口"
    assert "AI证据整理失败，已保留本地观察" in qt_workbench and "失败：{ai_error}" in qt_workbench, "Qt 工作台 AI失败应显式标记且保留本地观察"
    assert "current_observation_note" in qt_workbench and "note=self.current_observation_note or ref.thesis" in qt_workbench, "Qt 工作台保存任务应保留观察证据摘要"
    assert "K线数据获取失败" in qt_workbench and "数据缺口：" in qt_workbench, "Qt 工作台观察应展示数据缺口"
    assert 'ai_text = "已追加" if ai_report else (f"失败：{ai_error}" if ai_error else "未触发")' in qt_workbench, "Qt 工作台保存摘要应明确未触发 AI证据整理"
    assert "观察生成中，请等待完成后再保存" in qt_workbench and "_observation_save_block_message" in qt_workbench, "Qt 工作台保存入口应有生成中 guard"
    assert "_active_background_signals" in qt_workbench and "_active_background_threads" in qt_workbench, "Qt 工作台应持有后台 signal/thread 生命周期"
    assert "failed: Callable[[str], None] | None = None" in qt_workbench, "Qt 工作台后台任务应支持可选失败回调"
    assert "note=ref.thesis" not in qt_workbench, "Qt 工作台不应只保存裸 thesis"
    assert qt_workbench.count('self.current_observation_note = ""') >= 2, "Qt 工作台切换/加载股票时应清空旧观察摘要"
    assert "_stock_load_version" in qt_workbench and "_observer_state_version" in qt_workbench, "Qt 工作台后台任务应有过期结果保护"
    assert "结果已过期，未覆盖当前股票" in qt_workbench, "Qt 工作台过期后台结果不应覆盖当前股票"
    forbidden = ["LicenseManager", "license_key", "verify_license", "激活码", "授权码"]
    for word in forbidden:
        assert word not in qt_workbench, f"Qt 默认工作台不应保留授权残留：{word}"
    print("[OK] 区块6 默认入口与 Qt 设置静态校验")


def main() -> None:
    test_models_and_parsing()
    test_data_location_cache_secret()
    test_fetcher_cache_fallback_edges()
    test_technical_profile()
    test_trade_reference_and_tracking_review()
    test_ai_helpers_without_network()
    test_default_entry_and_qt_settings_static()
    print("[OK] 核心区块离线测试全部通过")


if __name__ == "__main__":
    main()
