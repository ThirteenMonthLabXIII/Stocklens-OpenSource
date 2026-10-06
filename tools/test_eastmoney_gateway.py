from __future__ import annotations

import http.client
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import stock_tool.core.data_fetcher as data_fetcher_module
from stock_tool.core.data_fetcher import MarketDataError, _eastmoney_get, _friendly_market_error


class CannedResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            exc = RuntimeError(f"{self.status_code} error")
            exc.response = self
            raise exc

    def json(self) -> dict:
        return self._payload


class CannedSession:
    def __init__(self, payloads: list[dict] | None = None, *, status_code: int = 200, exc: Exception | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.payloads = list(payloads or [{"data": {"ok": True}}])
        self.status_code = status_code
        self.exc = exc

    def get(self, url: str, params: dict, timeout: int):
        self.calls.append((url, dict(params)))
        if self.exc:
            raise self.exc
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return CannedResponse(payload, status_code=self.status_code)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _patch_gateway(session: CannedSession, *, min_interval: float = 0.0):
    old_market_session = data_fetcher_module._market_session
    old_em_session = data_fetcher_module._EM_SESSION
    old_last = data_fetcher_module._EM_LAST_REQUEST_AT
    old_min = data_fetcher_module._EM_MIN_INTERVAL
    old_jitter = data_fetcher_module._EM_MAX_JITTER
    data_fetcher_module._market_session = lambda trust_env=False: session
    data_fetcher_module._EM_SESSION = None
    data_fetcher_module._EM_LAST_REQUEST_AT = 0.0
    data_fetcher_module._EM_MIN_INTERVAL = min_interval
    data_fetcher_module._EM_MAX_JITTER = 0.0

    def restore() -> None:
        data_fetcher_module._market_session = old_market_session
        data_fetcher_module._EM_SESSION = old_em_session
        data_fetcher_module._EM_LAST_REQUEST_AT = old_last
        data_fetcher_module._EM_MIN_INTERVAL = old_min
        data_fetcher_module._EM_MAX_JITTER = old_jitter

    return restore


def test_gateway_serial_min_interval() -> None:
    session = CannedSession()
    clock = FakeClock()
    restore = _patch_gateway(session, min_interval=1.0)
    old_monotonic = data_fetcher_module.time.monotonic
    old_sleep = data_fetcher_module.time.sleep
    old_uniform = data_fetcher_module.random.uniform
    try:
        data_fetcher_module.time.monotonic = clock.monotonic
        data_fetcher_module.time.sleep = clock.sleep
        data_fetcher_module.random.uniform = lambda a, b: 0.0
        _eastmoney_get("/api/test", {}, host_group="realtime", hosts=["push2.eastmoney.com"])
        _eastmoney_get("/api/test", {}, host_group="realtime", hosts=["push2.eastmoney.com"])
    finally:
        data_fetcher_module.time.monotonic = old_monotonic
        data_fetcher_module.time.sleep = old_sleep
        data_fetcher_module.random.uniform = old_uniform
        restore()

    assert len(session.calls) == 2
    assert clock.sleeps == [1.0], f"第二次请求应等待1秒，实际 {clock.sleeps}"


def test_gateway_rotates_realtime_mirrors() -> None:
    session = CannedSession(payloads=[{}, {}, {"data": {"ok": True}}])
    restore = _patch_gateway(session)
    try:
        payload = _eastmoney_get("/api/test", {}, host_group="realtime", retries=1)
    finally:
        restore()

    assert payload["data"]["ok"] is True
    urls = [url for url, _params in session.calls]
    assert "https://push2.eastmoney.com/api/test" in urls
    assert "https://push2delay.eastmoney.com/api/test" in urls
    assert "https://82.push2.eastmoney.com/api/test" in urls


def test_gateway_does_not_retry_forbidden() -> None:
    session = CannedSession(status_code=403)
    restore = _patch_gateway(session)
    try:
        try:
            _eastmoney_get("/api/test", {}, host_group="realtime", retries=3)
            raise AssertionError("403 应立即失败")
        except MarketDataError as exc:
            assert exc.reason == "forbidden"
    finally:
        restore()

    assert len(session.calls) == 1, f"4xx 逻辑拒绝不应重试，实际调用 {len(session.calls)} 次"


def test_gateway_page_burst_uses_small_interval() -> None:
    # 同一逻辑查询的分页爬取传入小 min_interval：第二页应只等 0.15s 而非全局 1s，
    # 让整轮筛选不被防突发节流白白拖慢。
    session = CannedSession()
    clock = FakeClock()
    restore = _patch_gateway(session, min_interval=1.0)
    old_monotonic = data_fetcher_module.time.monotonic
    old_sleep = data_fetcher_module.time.sleep
    old_uniform = data_fetcher_module.random.uniform
    try:
        data_fetcher_module.time.monotonic = clock.monotonic
        data_fetcher_module.time.sleep = clock.sleep
        data_fetcher_module.random.uniform = lambda a, b: 0.0
        _eastmoney_get("/api/test", {}, host_group="realtime", hosts=["push2.eastmoney.com"], min_interval=0.15)
        _eastmoney_get("/api/test", {}, host_group="realtime", hosts=["push2.eastmoney.com"], min_interval=0.15)
    finally:
        data_fetcher_module.time.monotonic = old_monotonic
        data_fetcher_module.time.sleep = old_sleep
        data_fetcher_module.random.uniform = old_uniform
        restore()

    assert clock.sleeps == [0.15], f"分页突发应只等 0.15s，实际 {clock.sleeps}"


def test_gateway_caps_total_requests_on_failure() -> None:
    # 全主机持续返回空数据（模拟限流风暴）。修复前为 trust_env(2)×retries(3)×hosts(7)=42 次，
    # 既拖慢前台又反复砸被风控的同一 IP；修复后总请求数必须被 _EM_MAX_REQUESTS 封顶。
    session = CannedSession(payloads=[{"data": None}])
    restore = _patch_gateway(session)
    try:
        try:
            _eastmoney_get("/api/test", {}, host_group="his", retries=3)
            raise AssertionError("全失败应抛出 MarketDataError")
        except MarketDataError as exc:
            assert exc.reason in {"empty", "rate_limit", "network"}, f"非预期 reason：{exc.reason}"
    finally:
        restore()

    cap = data_fetcher_module._EM_MAX_REQUESTS
    assert len(session.calls) == cap, f"失败路径总请求应被封顶到 {cap}，实际 {len(session.calls)}（疑似重试×主机×代理爆炸）"


def test_friendly_error_distinguishes_rate_limit_and_network() -> None:
    limited = _friendly_market_error(MarketDataError("RemoteDisconnected", reason="rate_limit"))
    network = _friendly_market_error(MarketDataError("NameResolutionError", reason="network"))
    assert "不是你的网络问题" in str(limited)
    assert "检查网络" in str(network)

    remote = _friendly_market_error(http.client.RemoteDisconnected("Remote end closed connection without response"))
    assert "不是你的网络问题" in str(remote)


def main() -> None:
    test_gateway_serial_min_interval()
    test_gateway_rotates_realtime_mirrors()
    test_gateway_does_not_retry_forbidden()
    test_gateway_page_burst_uses_small_interval()
    test_gateway_caps_total_requests_on_failure()
    test_friendly_error_distinguishes_rate_limit_and_network()
    print("[OK] 东方财富网关抗限流区块测试通过")


if __name__ == "__main__":
    main()
