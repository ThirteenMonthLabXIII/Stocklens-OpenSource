from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.factor_store import FactorSnapshot
import stock_tool.core.data_fetcher as data_fetcher_module
from stock_tool.core.cache import CacheStore
from stock_tool.core.data_fetcher import DataFetcher, MarketDataError
from stock_tool.core.models import FinancialMetrics, MoneyFlowSnapshot, StockQuote
from stock_tool.core.screener_engine import ScreenFilter, screen_factor_snapshots, screen_market


class FakeMoneyFlowFetcher:
    def __init__(self, count: int = 20, fail: bool = False) -> None:
        self.fail = fail
        self.money_flow_calls = 0
        self.market_money_flow_calls = 0
        self.quotes = [
            StockQuote(
                code=f"{idx:06d}",
                name=f"样例{idx}",
                price=10 + idx,
                market_cap=(count - idx + 1) * 100_000_000,
                pe=20,
                turnover=6,
            )
            for idx in range(1, count + 1)
        ]

    def spot_quotes(self):
        return self.quotes, None

    def financial_metrics(self, code: str):
        return FinancialMetrics(roe=18, net_margin=12, debt_ratio=45, revenue_growth=20, profit_growth=18), None

    def market_money_flow(self, force: bool = False):
        self.market_money_flow_calls += 1
        if self.fail:
            raise RuntimeError("mock eastmoney blocked")
        return {
            quote.code: (200_000_000 if int(quote.code) % 2 == 0 else -100_000_000)
            for quote in self.quotes
        }

    def money_flow(self, code: str, days: int = 20, force: bool = False):
        self.money_flow_calls += 1
        code_number = int(code)
        main_5d = 200_000_000 if code_number % 2 == 0 else -100_000_000
        return MoneyFlowSnapshot(code=code, rows=[{"main": main_5d}], main_5d=main_5d, summary="mock"), None


def _money_filter() -> ScreenFilter:
    return ScreenFilter(
        min_roe=None,
        max_debt_ratio=None,
        max_pe=None,
        min_main_5d_yi=0,
        strict_min_main_5d_yi=True,
        with_technical=False,
        result_limit=100,
    )


def test_market_money_flow_called_once() -> None:
    fetcher = FakeMoneyFlowFetcher(count=20, fail=False)
    progress_messages: list[str] = []
    report = screen_market(fetcher, _money_filter(), progress=lambda done, total, message: progress_messages.append(message))
    assert fetcher.market_money_flow_calls == 1, f"全市场资金流应只批量调用1次，实际 {fetcher.market_money_flow_calls}"
    assert fetcher.money_flow_calls == 0, f"批量筛选不应逐只调用 money_flow，实际 {fetcher.money_flow_calls}"
    assert report.results
    assert progress_messages[:2] == ["正在获取全市场资金流...", "资金流就绪，开始筛选"]


def test_money_flow_filter_when_available() -> None:
    fetcher = FakeMoneyFlowFetcher(count=6, fail=False)
    report = screen_market(fetcher, _money_filter())
    assert fetcher.market_money_flow_calls == 1
    assert fetcher.money_flow_calls == 0
    got = [item.quote.code for item in report.results]
    assert got == ["000002", "000004", "000006"], f"资金流正常时应保留近5日主力净流入为正的候选，实际 {got}"
    assert all((item.sort_values.get("main_5d_yi") or 0) > 0 for item in report.results)
    assert any("当前已获取" in note and "无需 Tushare token" in note for note in report.notes)


def test_market_money_flow_fail_fast() -> None:
    fetcher = FakeMoneyFlowFetcher(count=20, fail=True)
    report = screen_market(fetcher, _money_filter())
    assert fetcher.market_money_flow_calls == 1, f"资金源失败后应只批量调用1次，实际 {fetcher.market_money_flow_calls}"
    assert fetcher.money_flow_calls == 0, f"批量资金源失败后不应退回逐只调用，实际 {fetcher.money_flow_calls}"
    assert len(report.results) == 20, "资金源不可用时应忽略主力条件，保留财务/技术通过的候选"
    assert report.failed == 0
    assert any("资金数据暂不可用" in note for note in report.notes)
    assert any("公开接口限流" in note and "无需配置 Tushare token" in note for note in report.notes)
    assert all("mock eastmoney blocked" not in note for note in report.notes), "买家可见提示不应暴露内部异常原文"


def test_money_flow_code_zfill_matching() -> None:
    fetcher = FakeMoneyFlowFetcher(count=2, fail=False)
    fetcher.quotes[0].code = "1"
    fetcher.quotes[1].code = "2"

    def market_money_flow(force: bool = False):
        fetcher.market_money_flow_calls += 1
        return {"000001": 200_000_000, "000002": -100_000_000}

    fetcher.market_money_flow = market_money_flow
    report = screen_market(fetcher, _money_filter())
    got = [item.quote.code for item in report.results]
    assert got == ["1"], f"候选代码未补零时也应正确命中资金流并过滤，实际 {got}"
    assert report.results[0].sort_values["main_5d_yi"] == 2


def test_eastmoney_a_market_fs_keeps_star_and_bj_board() -> None:
    fs = data_fetcher_module.EASTMONEY_A_MARKET_FS
    assert "m:1+t:23" in fs, f"东财全A fs 必须包含科创板片段 m:1+t:23，实际 {fs}"
    assert "m:0+t:81+s:2048" in fs, f"东财全A fs 必须包含北交所片段 m:0+t:81+s:2048，实际 {fs}"


def test_market_money_flow_http_uses_page_size_200_and_merges_pages() -> None:
    class CannedResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class CannedSession:
        def __init__(self) -> None:
            self.headers = {}
            self.calls: list[dict] = []

        def get(self, url: str, params: dict, timeout: int):
            self.calls.append(dict(params))
            page = int(params["pn"])
            if page == 1:
                diff = [{"f12": f"{idx:06d}", "f14": f"样例{idx}", "f164": float(idx)} for idx in range(1, 201)]
            else:
                diff = [{"f12": "000201", "f14": "样例201", "f164": 201.0}]
            return CannedResponse({"data": {"total": 201, "diff": diff}})

    session = CannedSession()
    old_market_session = data_fetcher_module._market_session
    old_min_rows = data_fetcher_module.SPOT_MIN_ROWS
    try:
        data_fetcher_module._market_session = lambda trust_env=False: session
        data_fetcher_module.SPOT_MIN_ROWS = 1
        flow_map = data_fetcher_module._market_money_flow_from_eastmoney_http()
    finally:
        data_fetcher_module._market_session = old_market_session
        data_fetcher_module.SPOT_MIN_ROWS = old_min_rows

    assert [call["pz"] for call in session.calls] == ["200", "200"]
    assert all(call["fs"] == data_fetcher_module.EASTMONEY_A_MARKET_FS for call in session.calls), (
        f"批量资金流必须使用统一东财全A fs 常量，实际 {[call.get('fs') for call in session.calls]}"
    )
    assert len(flow_map) == 201
    assert flow_map["000001"] == 1.0
    assert flow_map["000201"] == 201.0


def _temp_cache() -> CacheStore:
    return CacheStore(Path(tempfile.mkdtemp(prefix="screen_mf_")) / "cache.db")


def test_tushare_fallback_disabled_without_token_or_switch() -> None:
    cache = _temp_cache()
    fetcher = DataFetcher(cache, tushare_token_provider=lambda: "fixture-token")
    old_primary = data_fetcher_module._market_money_flow_from_eastmoney_http
    old_tushare = data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc
    calls: list[str] = []

    def primary() -> dict[str, float]:
        raise MarketDataError("mock eastmoney blocked", reason="rate_limit")

    def forbidden_tushare(_token: str) -> dict[str, float]:
        calls.append("tushare")
        raise AssertionError("开关关闭时不应调用 Tushare")

    try:
        data_fetcher_module._market_money_flow_from_eastmoney_http = primary
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc = forbidden_tushare
        try:
            fetcher.market_money_flow(force=True)
            raise AssertionError("东财失败且兜底关闭时应降级为异常，由筛选层忽略主力条件")
        except MarketDataError:
            pass
    finally:
        data_fetcher_module._market_money_flow_from_eastmoney_http = old_primary
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc = old_tushare

    assert calls == [], f"开关关闭时不应消耗 Tushare，实际 {calls}"

    cache.set_setting(data_fetcher_module.TUSHARE_MONEY_FLOW_FALLBACK_SETTING, "1")
    fetcher_no_token = DataFetcher(cache, tushare_token_provider=lambda: "")
    try:
        data_fetcher_module._market_money_flow_from_eastmoney_http = primary
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc = forbidden_tushare
        try:
            fetcher_no_token.market_money_flow(force=True)
            raise AssertionError("无 token 时应降级为异常，由筛选层忽略主力条件")
        except MarketDataError:
            pass
    finally:
        data_fetcher_module._market_money_flow_from_eastmoney_http = old_primary
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc = old_tushare
    assert calls == [], f"无 token 时不应调用 Tushare，实际 {calls}"


def test_tushare_fallback_called_after_eastmoney_failure() -> None:
    cache = _temp_cache()
    cache.set_setting(data_fetcher_module.TUSHARE_MONEY_FLOW_FALLBACK_SETTING, "1")
    fetcher = DataFetcher(cache, tushare_token_provider=lambda: "fixture-token")
    old_primary = data_fetcher_module._market_money_flow_from_eastmoney_http
    old_tushare = data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc
    calls: list[str] = []

    def primary() -> dict[str, float]:
        calls.append("eastmoney")
        raise MarketDataError("mock eastmoney blocked", reason="rate_limit")

    def tushare(token: str) -> dict[str, float]:
        calls.append(f"tushare:{token}")
        return {"000001": 120_000_000.0}

    try:
        data_fetcher_module._market_money_flow_from_eastmoney_http = primary
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc = tushare
        flow_map = fetcher.market_money_flow(force=True)
    finally:
        data_fetcher_module._market_money_flow_from_eastmoney_http = old_primary
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc = old_tushare

    assert calls == ["eastmoney", "tushare:fixture-token"], f"应先东财、后 Tushare，实际 {calls}"
    assert flow_map == {"000001": 120_000_000.0}
    assert fetcher.last_market_money_flow_source == "tushare_moneyflow_dc"
    assert "Tushare moneyflow_dc" in fetcher.last_market_money_flow_note
    assert cache.get_json("market_money_flow") is not None, "Tushare 成功后应写入同一个资金流缓存"


def test_money_flow_cache_prevents_tushare_consumption() -> None:
    cache = _temp_cache()
    cache.set_setting(data_fetcher_module.TUSHARE_MONEY_FLOW_FALLBACK_SETTING, "1")
    cache.set_json("market_money_flow", {"000001": 1.0})
    fetcher = DataFetcher(cache, tushare_token_provider=lambda: "fixture-token")
    old_tushare = data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc

    def forbidden_tushare(_token: str) -> dict[str, float]:
        raise AssertionError("15分钟内新缓存命中时不应调用 Tushare")

    try:
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc = forbidden_tushare
        flow_map = fetcher.market_money_flow()
    finally:
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc = old_tushare

    assert flow_map == {"000001": 1.0}
    assert fetcher.last_market_money_flow_source == "cache"


def test_one_day_old_cache_takes_priority_over_tushare() -> None:
    cache = _temp_cache()
    cache.set_setting(data_fetcher_module.TUSHARE_MONEY_FLOW_FALLBACK_SETTING, "1")
    cache.set_json("market_money_flow", {"000001": 2.0})
    one_hour_ago = (datetime.now() - timedelta(hours=1)).isoformat()
    with cache.connect() as conn:
        conn.execute("UPDATE cache SET updated_at = ? WHERE key = ?", (one_hour_ago, "market_money_flow"))
    fetcher = DataFetcher(cache, tushare_token_provider=lambda: "fixture-token")
    old_primary = data_fetcher_module._market_money_flow_from_eastmoney_http
    old_tushare = data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc

    def primary() -> dict[str, float]:
        raise MarketDataError("mock eastmoney blocked", reason="rate_limit")

    def forbidden_tushare(_token: str) -> dict[str, float]:
        raise AssertionError("1天内旧缓存可用时不应调用 Tushare")

    try:
        data_fetcher_module._market_money_flow_from_eastmoney_http = primary
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc = forbidden_tushare
        flow_map = fetcher.market_money_flow()
    finally:
        data_fetcher_module._market_money_flow_from_eastmoney_http = old_primary
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc = old_tushare

    assert flow_map == {"000001": 2.0}
    assert fetcher.last_market_money_flow_source == "cache_stale"
    assert "未消耗 Tushare" in fetcher.last_market_money_flow_note


def test_tushare_moneyflow_dc_http_aggregates_5d_and_converts_units() -> None:
    class CannedPostResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class CannedPostSession:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.payloads = [
                {
                    "code": 0,
                    "data": {
                        "fields": ["cal_date", "is_open"],
                        "items": [["20260622", 1], ["20260623", 1], ["20260624", 1], ["20260625", 1], ["20260626", 1]],
                    },
                },
                *[
                    {
                        "code": 0,
                        "data": {
                            "fields": ["ts_code", "trade_date", "net_amount"],
                            "items": [
                                ["000001.SZ", trade_date, amount],
                                ["688001.SH", trade_date, "2.5"],
                                ["830001.BJ", trade_date, "-1"],
                            ],
                        },
                    }
                    for trade_date, amount in [
                        ("20260626", 1),
                        ("20260625", 2),
                        ("20260624", 3),
                        ("20260623", 4),
                        ("20260622", 5),
                    ]
                ],
            ]

        def post(self, url: str, json: dict, timeout: int):
            self.calls.append(dict(json))
            return CannedPostResponse(self.payloads.pop(0))

    session = CannedPostSession()
    old_min_rows = data_fetcher_module.TUSHARE_MONEY_FLOW_MIN_ROWS
    try:
        data_fetcher_module.TUSHARE_MONEY_FLOW_MIN_ROWS = 1
        flow_map = data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc("fixture-token", session=session)
    finally:
        data_fetcher_module.TUSHARE_MONEY_FLOW_MIN_ROWS = old_min_rows

    assert [call["api_name"] for call in session.calls] == ["trade_cal", "moneyflow_dc", "moneyflow_dc", "moneyflow_dc", "moneyflow_dc", "moneyflow_dc"]
    assert all(call["fields"] == "ts_code,trade_date,net_amount" for call in session.calls[1:])
    assert flow_map["000001"] == 15 * 10000.0, f"net_amount 万元必须乘以10000转元，实际 {flow_map['000001']}"
    assert flow_map["688001"] == 12.5 * 10000.0
    assert flow_map["830001"] == -5 * 10000.0


def test_tushare_moneyflow_dc_missing_fields_and_permission_fail_cleanly() -> None:
    class CannedPostResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class MissingFieldSession:
        def __init__(self) -> None:
            self.payloads = [
                {"code": 0, "data": {"fields": ["cal_date", "is_open"], "items": [["20260626", 1], ["20260625", 1], ["20260624", 1], ["20260623", 1], ["20260622", 1]]}},
                {"code": 0, "data": {"fields": ["ts_code", "trade_date"], "items": [["000001.SZ", "20260626"]]}},
            ]

        def post(self, url: str, json: dict, timeout: int):
            return CannedPostResponse(self.payloads.pop(0))

    old_min_rows = data_fetcher_module.TUSHARE_MONEY_FLOW_MIN_ROWS
    try:
        data_fetcher_module.TUSHARE_MONEY_FLOW_MIN_ROWS = 1
        try:
            data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc("fixture-token", session=MissingFieldSession())
            raise AssertionError("缺字段时应失败降级")
        except MarketDataError as exc:
            assert "缺少字段" in str(exc)
    finally:
        data_fetcher_module.TUSHARE_MONEY_FLOW_MIN_ROWS = old_min_rows

    class PermissionSession:
        def post(self, url: str, json: dict, timeout: int):
            return CannedPostResponse({"code": 2002, "msg": f"权限不足 token={json['token']}"})

    try:
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc("secret-token", session=PermissionSession())
        raise AssertionError("权限不足时应失败降级")
    except MarketDataError as exc:
        assert exc.reason == "permission"
        assert "secret-token" not in str(exc), "错误提示不得暴露 token"


def test_tushare_failure_does_not_pollute_money_flow_cache() -> None:
    cache = _temp_cache()
    cache.set_setting(data_fetcher_module.TUSHARE_MONEY_FLOW_FALLBACK_SETTING, "1")
    fetcher = DataFetcher(cache, tushare_token_provider=lambda: "fixture-token")
    old_primary = data_fetcher_module._market_money_flow_from_eastmoney_http
    old_tushare = data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc

    def primary() -> dict[str, float]:
        raise MarketDataError("mock eastmoney blocked", reason="rate_limit")

    def failing_tushare(_token: str) -> dict[str, float]:
        raise MarketDataError("Tushare moneyflow_dc 缺少字段：net_amount", reason="empty")

    try:
        data_fetcher_module._market_money_flow_from_eastmoney_http = primary
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc = failing_tushare
        try:
            fetcher.market_money_flow(force=True)
            raise AssertionError("Tushare 失败时应继续降级为异常")
        except MarketDataError:
            pass
    finally:
        data_fetcher_module._market_money_flow_from_eastmoney_http = old_primary
        data_fetcher_module._market_money_flow_from_tushare_moneyflow_dc = old_tushare

    assert cache.get_json("market_money_flow") is None, "Tushare 失败不能污染资金流缓存"
    assert "已忽略主力条件" in fetcher.last_market_money_flow_note


def test_factor_snapshots_ignore_money_flow_condition() -> None:
    snapshots = [
        FactorSnapshot(code=f"{idx:06d}", name=f"快照{idx}", price=10, pe=20, roe=18, debt_ratio=45)
        for idx in range(1, 6)
    ]
    report = screen_factor_snapshots(snapshots, _money_filter())
    assert len(report.results) == 5, "因子快照不含资金流时应忽略主力条件，而不是清零结果"
    assert any("因子快照不含资金流" in note for note in report.notes)
    assert any("无需 Tushare token" in note for note in report.notes)


def main() -> None:
    test_market_money_flow_called_once()
    test_money_flow_filter_when_available()
    test_market_money_flow_fail_fast()
    test_money_flow_code_zfill_matching()
    test_eastmoney_a_market_fs_keeps_star_and_bj_board()
    test_market_money_flow_http_uses_page_size_200_and_merges_pages()
    test_tushare_fallback_disabled_without_token_or_switch()
    test_tushare_fallback_called_after_eastmoney_failure()
    test_money_flow_cache_prevents_tushare_consumption()
    test_one_day_old_cache_takes_priority_over_tushare()
    test_tushare_moneyflow_dc_http_aggregates_5d_and_converts_units()
    test_tushare_moneyflow_dc_missing_fields_and_permission_fail_cleanly()
    test_tushare_failure_does_not_pollute_money_flow_cache()
    test_factor_snapshots_ignore_money_flow_condition()
    print("[OK] 筛选资金流批量化专项测试通过")


if __name__ == "__main__":
    main()
