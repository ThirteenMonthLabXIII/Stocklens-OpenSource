from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import stock_tool.core.data_fetcher as data_fetcher_module
from stock_tool.core.cache import CacheStore
from stock_tool.core.data_fetcher import DataFetcher


# oldest -> newest，单位元。今日为负、近5日合计为正，用于验证窗口退化 bug：
# 若错误地只取当日一行，main_5d 会变成 -14亿；正确多日求和应为 +20亿。
_MAIN_BY_DAY = [3, 3, 3, 3, 3, 3, 3, 10, 8, 9, 7, -14]
_TODAY_YI = -14
_MAIN_3D_YI = 2  # 9 + 7 - 14
_MAIN_5D_YI = 20  # 10 + 8 + 9 + 7 - 14
_MAIN_10D_YI = 35  # 最近10日合计


def _build_klines() -> list[str]:
    lines: list[str] = []
    for idx, main_yi in enumerate(_MAIN_BY_DAY):
        date = f"2026-06-{idx + 10:02d}"
        main = main_yi * 100_000_000
        small = -1 * 100_000_000
        medium = 0
        large = 1 * 100_000_000
        xlarge = 2 * 100_000_000
        f56 = 0
        lines.append(f"{date},{main},{small},{medium},{large},{xlarge},{f56}")
    return lines


class CannedResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class CannedSession:
    def __init__(self, klines: list[str]) -> None:
        self.headers: dict[str, str] = {}
        self.urls: list[str] = []
        self._klines = klines

    def get(self, url: str, params: dict, timeout: int):
        self.urls.append(url)
        return CannedResponse({"data": {"klines": list(self._klines)}})


def _fetcher_with_session(session: CannedSession):
    tmp_db = Path(tempfile.mkdtemp(prefix="mf_window_")) / "cache.db"
    fetcher = DataFetcher(CacheStore(tmp_db))
    old = data_fetcher_module._market_session
    old_em_session = data_fetcher_module._EM_SESSION
    old_last = data_fetcher_module._EM_LAST_REQUEST_AT
    old_min = data_fetcher_module._EM_MIN_INTERVAL
    data_fetcher_module._market_session = lambda trust_env=False: session
    data_fetcher_module._EM_SESSION = None
    data_fetcher_module._EM_LAST_REQUEST_AT = 0.0
    data_fetcher_module._EM_MIN_INTERVAL = 0.0
    return fetcher, old, old_em_session, old_last, old_min


def _restore_session(old, old_em_session, old_last, old_min) -> None:
    data_fetcher_module._market_session = old
    data_fetcher_module._EM_SESSION = old_em_session
    data_fetcher_module._EM_LAST_REQUEST_AT = old_last
    data_fetcher_module._EM_MIN_INTERVAL = old_min


def test_main_windows_sum_multiple_days() -> None:
    session = CannedSession(_build_klines())
    fetcher, old, old_em_session, old_last, old_min = _fetcher_with_session(session)
    try:
        snapshot, _ = fetcher.money_flow("300059", days=20, force=True)
    finally:
        _restore_session(old, old_em_session, old_last, old_min)

    today_yi = snapshot.rows[-1]["main"] / 1e8
    assert round(today_yi, 4) == _TODAY_YI, f"当日主力应为 {_TODAY_YI}亿，实际 {today_yi}"
    assert round(snapshot.main_3d / 1e8, 4) == _MAIN_3D_YI, f"main_3d 应为3日求和 {_MAIN_3D_YI}亿，实际 {snapshot.main_3d / 1e8}"
    assert round(snapshot.main_5d / 1e8, 4) == _MAIN_5D_YI, f"main_5d 应为5日求和 {_MAIN_5D_YI}亿，实际 {snapshot.main_5d / 1e8}"
    assert round(snapshot.main_10d / 1e8, 4) == _MAIN_10D_YI, f"main_10d 应为10日求和 {_MAIN_10D_YI}亿，实际 {snapshot.main_10d / 1e8}"
    # 核心回归点：多日窗口不能退化成当日值，且可与当日符号相反。
    assert abs(snapshot.main_5d - snapshot.rows[-1]["main"]) > 1.0, "main_5d 退化成了当日主力净额（窗口 bug 回归）"
    assert (snapshot.main_5d > 0) != (snapshot.rows[-1]["main"] > 0), "本例近5日应为正、当日为负，符号未区分说明只取了单日"


def test_uses_push2his_history_host() -> None:
    session = CannedSession(_build_klines())
    fetcher, old, old_em_session, old_last, old_min = _fetcher_with_session(session)
    try:
        fetcher.money_flow("300059", days=20, force=True)
    finally:
        _restore_session(old, old_em_session, old_last, old_min)

    assert session.urls, "未发起任何资金流请求"
    assert "push2his" in session.urls[0], (
        f"逐只资金流首选必须走 push2his 历史镜像（实时 push2 只回当日一行），实际 {session.urls}"
    )
    assert session.urls[0].endswith("/api/qt/stock/fflow/daykline/get")


def test_optional_fallback_called_only_after_primary_fails() -> None:
    calls: list[str] = []
    tmp_db = Path(tempfile.mkdtemp(prefix="mf_fallback_")) / "cache.db"
    fetcher = DataFetcher(CacheStore(tmp_db))
    old_primary = data_fetcher_module._money_flow_klines_from_eastmoney_http
    old_fallback = data_fetcher_module._money_flow_fallback_source

    def primary(code: str, lmt: int) -> list[str]:
        calls.append("primary")
        raise data_fetcher_module.MarketDataError("mock eastmoney rate limit", reason="rate_limit")

    def fallback(code: str, lmt: int, primary_reason: str = "unknown") -> list[str]:
        calls.append("fallback")
        return _build_klines()

    try:
        data_fetcher_module._money_flow_klines_from_eastmoney_http = primary
        data_fetcher_module._money_flow_fallback_source = fallback
        snapshot, _ = fetcher.money_flow("300059", days=20, force=True)
    finally:
        data_fetcher_module._money_flow_klines_from_eastmoney_http = old_primary
        data_fetcher_module._money_flow_fallback_source = old_fallback

    assert calls == ["primary", "fallback"], f"兜底源只能在主源失败后调用一次，实际 {calls}"
    assert round(snapshot.main_5d / 1e8, 4) == _MAIN_5D_YI


def test_tencent_partial_fallback_when_history_fails() -> None:
    """东财历史源 + 第三方兜底都失败、且无可用缓存时，应走腾讯当日资金流降级兜底：
    展示当日主力净流入，但 3/5/10 日窗口必须留空（诚实），summary 标注“仅当日值”。"""
    tmp_db = Path(tempfile.mkdtemp(prefix="mf_tencent_")) / "cache.db"
    fetcher = DataFetcher(CacheStore(tmp_db))
    old_primary = data_fetcher_module._money_flow_klines_from_eastmoney_http
    old_fallback = data_fetcher_module._money_flow_fallback_source
    old_tencent = data_fetcher_module._money_flow_today_from_tencent

    def primary(code: str, lmt: int) -> list[str]:
        raise data_fetcher_module.MarketDataError("mock eastmoney rate limit", reason="rate_limit")

    def fallback(*args, **kwargs) -> list[str]:
        raise data_fetcher_module.MarketDataError("mock adata 未安装", reason="empty")

    def tencent(code: str) -> dict:
        return {"date": "2026-06-26", "main": 1.93 * 1e8, "small": -0.5 * 1e8}

    try:
        data_fetcher_module._money_flow_klines_from_eastmoney_http = primary
        data_fetcher_module._money_flow_fallback_source = fallback
        data_fetcher_module._money_flow_today_from_tencent = tencent
        snapshot, _ = fetcher.money_flow("300059", days=20, force=True)
    finally:
        data_fetcher_module._money_flow_klines_from_eastmoney_http = old_primary
        data_fetcher_module._money_flow_fallback_source = old_fallback
        data_fetcher_module._money_flow_today_from_tencent = old_tencent

    assert snapshot.main_5d is None, f"降级快照的 5 日窗口必须留空，实际 {snapshot.main_5d}"
    assert snapshot.main_3d is None and snapshot.main_10d is None, "降级快照不得伪造 3/10 日窗口"
    assert "仅当日值" in snapshot.summary, f"降级 summary 必须标注仅当日值，实际 {snapshot.summary}"
    assert round(snapshot.rows[-1]["main"] / 1e8, 2) == 1.93, "应展示腾讯当日主力净流入"


def test_tencent_parser_extracts_main_net_from_ff_payload() -> None:
    """解析腾讯 ff_* 文本：取主力净流入（索引3，万元）转元，并识别 8 位日期。"""
    captured: dict[str, str] = {}

    class FakeResp:
        text = 'v_ff_sz000858="sz000858~12000~8000~4000~5.1~3000~5000~-2000~-3.0~0~0~0~20260626~某股票~";'

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, url: str, timeout=None):
            captured["url"] = url
            return FakeResp()

    old_session = data_fetcher_module._market_session
    try:
        data_fetcher_module._market_session = lambda trust_env=False: FakeSession()
        row = data_fetcher_module._money_flow_today_from_tencent("000858")
    finally:
        data_fetcher_module._market_session = old_session

    assert "qt.gtimg.cn/q=ff_sz000858" in captured["url"], captured.get("url")
    assert row["main"] == 4000 * 10000.0, f"主力净流入应万元转元，实际 {row['main']}"
    assert row["small"] == -2000 * 10000.0, f"散户净流入应万元转元，实际 {row['small']}"
    assert row["date"] == "2026-06-26", f"应解析出 8 位日期，实际 {row['date']}"


def test_optional_fallback_skips_efinance_on_rate_limit() -> None:
    """efinance 与东财同 IP：主源 IP 级限流时必须跳过 efinance（避免再砸被封 IP），
    非 IP 级失败时才尝试。通过汇总到异常文案里的诊断 note 验证门控，无需安装真实包。"""
    old_min = data_fetcher_module._EM_MIN_INTERVAL
    old_last = data_fetcher_module._EM_LAST_REQUEST_AT
    data_fetcher_module._EM_MIN_INTERVAL = 0.0
    data_fetcher_module._EM_LAST_REQUEST_AT = 0.0
    try:
        try:
            data_fetcher_module._money_flow_fallback_source("000001", 20, primary_reason="rate_limit")
            assert False, "无可用兜底源时应抛 MarketDataError"
        except data_fetcher_module.MarketDataError as exc:
            limited_msg = str(exc)
        try:
            data_fetcher_module._money_flow_fallback_source("000001", 20, primary_reason="network")
            assert False, "无可用兜底源时应抛 MarketDataError"
        except data_fetcher_module.MarketDataError as exc:
            network_msg = str(exc)
    finally:
        data_fetcher_module._EM_MIN_INTERVAL = old_min
        data_fetcher_module._EM_LAST_REQUEST_AT = old_last

    assert "跳过" in limited_msg, f"IP 级限流时应跳过 efinance，实际 {limited_msg}"
    assert "efinance 未安装" not in limited_msg, f"IP 级限流时不应尝试导入 efinance，实际 {limited_msg}"
    assert "efinance 未安装" in network_msg, f"非 IP 级失败时应尝试 efinance，实际 {network_msg}"
    # 诊断信息须可见，不能再静默吞掉（buyer 才能知道为什么没数据）。
    assert "adata" in limited_msg and "adata" in network_msg


def test_adata_real_schema_columns_map() -> None:
    """用 1nchaos/adata 真实日线列名喂列名映射，断言能正确解析出主力净流入。
    注：列名已核对 adata 源码；数值单位（元）依据 adata East 实现与东财同源，
    若未来 adata 改用万元/亿元口径，需在此 fixture 同步核对。"""
    pd = data_fetcher_module.pd
    df = pd.DataFrame(
        [
            {"stock_code": "000001", "trade_date": "2026-06-25", "main_net_inflow": 1.0e8,
             "sm_net_inflow": -0.3e8, "mid_net_inflow": 0.1e8, "lg_net_inflow": 0.4e8, "max_net_inflow": 0.6e8},
            {"stock_code": "000001", "trade_date": "2026-06-26", "main_net_inflow": 2.0e8,
             "sm_net_inflow": -0.5e8, "mid_net_inflow": 0.2e8, "lg_net_inflow": 0.7e8, "max_net_inflow": 1.3e8},
        ]
    )
    klines = data_fetcher_module._money_flow_df_to_klines(df, 20)
    assert len(klines) == 2, f"应解析出 2 行，实际 {klines}"
    last = klines[-1].split(",")
    assert last[0] == "2026-06-26", f"日期列(trade_date)未命中，实际 {last}"
    assert abs(float(last[1]) - 2.0e8) < 1.0, f"主力净流入(main_net_inflow)未命中，实际 {last}"
    assert abs(float(last[5]) - 1.3e8) < 1.0, f"超大单(max_net_inflow)未命中，实际 {last}"


def test_money_flow_all_sources_fail_chain() -> None:
    """东财 + 第三方兜底 + 腾讯当日全部失败、且无可用缓存时：必须抛出而非静默返回，
    且 reason 保留首因（东财 rate_limit），UI 拿到的文案是“不是你的网络问题”这版人性化文案。"""
    tmp_db = Path(tempfile.mkdtemp(prefix="mf_failchain_")) / "cache.db"
    fetcher = DataFetcher(CacheStore(tmp_db))
    old_primary = data_fetcher_module._money_flow_klines_from_eastmoney_http
    old_fallback = data_fetcher_module._money_flow_fallback_source
    old_tencent = data_fetcher_module._money_flow_today_from_tencent

    def primary(code: str, lmt: int) -> list[str]:
        raise data_fetcher_module.MarketDataError("mock eastmoney rate limit", reason="rate_limit")

    def fallback(*args, **kwargs) -> list[str]:
        raise data_fetcher_module.MarketDataError("adata 未安装；efinance 与东财同 IP，主源被限流/封禁时跳过", reason="empty")

    def tencent(code: str) -> dict:
        raise data_fetcher_module.MarketDataError("腾讯资金流接口请求失败", reason="network")

    raised: Exception | None = None
    try:
        data_fetcher_module._money_flow_klines_from_eastmoney_http = primary
        data_fetcher_module._money_flow_fallback_source = fallback
        data_fetcher_module._money_flow_today_from_tencent = tencent
        try:
            fetcher.money_flow("300059", days=20, force=True)
        except Exception as exc:
            raised = exc
    finally:
        data_fetcher_module._money_flow_klines_from_eastmoney_http = old_primary
        data_fetcher_module._money_flow_fallback_source = old_fallback
        data_fetcher_module._money_flow_today_from_tencent = old_tencent

    assert isinstance(raised, data_fetcher_module.MarketDataError), f"全失败时应抛 MarketDataError，实际 {raised!r}"
    assert raised.reason == "rate_limit", f"reason 应保留首因 rate_limit，实际 {raised.reason}"
    assert "不是你的网络问题" in str(raised), f"应展示人性化限流文案，实际 {raised}"


def main() -> None:
    test_main_windows_sum_multiple_days()
    test_uses_push2his_history_host()
    test_optional_fallback_called_only_after_primary_fails()
    test_tencent_partial_fallback_when_history_fails()
    test_tencent_parser_extracts_main_net_from_ff_payload()
    test_optional_fallback_skips_efinance_on_rate_limit()
    test_adata_real_schema_columns_map()
    test_money_flow_all_sources_fail_chain()
    print("[OK] 逐只主力资金多日窗口区块测试通过")


if __name__ == "__main__":
    main()
