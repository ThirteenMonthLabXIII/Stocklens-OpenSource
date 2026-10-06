from __future__ import annotations

import os
import io
import json
import math
import random
import time
import queue
import threading
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import lazy_loader as lazy

# 惰性加载：pandas/requests 在窗口绘制前是启动耗时大头（实测 pandas≈781ms、
# requests≈348ms）。用 lazy.load 后，二者仅在首次访问其属性（真正拉取行情/财务时）
# 才导入，使源码启动与 exe 运行期均明显加快。所有 pd./requests. 调用点无需改动。
pd = lazy.load("pandas")
requests = lazy.load("requests")


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class QuoteSnapshot:
    """一条公开行情快照及其可复核的来源、行情时间与抓取时间。"""

    quote: StockQuote
    source: str
    quote_at: datetime | None
    fetched_at: datetime

from stock_tool.core.cache import CacheStore
from stock_tool.core.models import EventCatalyst, FinancialMetrics, MarketSentiment, MoneyFlowSnapshot, StockQuote, fmt_number


QUOTE_TTL = timedelta(minutes=5)
FINANCIAL_TTL = timedelta(days=1)
HISTORY_TTL = timedelta(hours=12)
MONEY_FLOW_TTL = timedelta(minutes=15)
SENTIMENT_TTL = timedelta(minutes=10)
EVENT_TTL = timedelta(hours=6)
QUOTE_FALLBACK_MAX_AGE = timedelta(days=1)
FINANCIAL_FALLBACK_MAX_AGE = timedelta(days=7)
HISTORY_FALLBACK_MAX_AGE = timedelta(days=15)
MONEY_FLOW_FALLBACK_MAX_AGE = timedelta(days=1)
SENTIMENT_FALLBACK_MAX_AGE = timedelta(days=1)
EVENT_FALLBACK_MAX_AGE = timedelta(days=3)
HISTORY_CANONICAL_DAYS = 365
SPOT_MIN_ROWS = 1000
SPOT_AKSHARE_TIMEOUT = 28
TUSHARE_MONEY_FLOW_FALLBACK_SETTING = "tushare_money_flow_fallback_enabled"
TUSHARE_MONEY_FLOW_MIN_ROWS = 1000
TUSHARE_API_URL = "https://api.tushare.pro"
TICKFLOW_API_KEY_SETTING = "tickflow_api_key"
EASTMONEY_A_MARKET_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
_MARKET_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


class MarketDataError(RuntimeError):
    def __init__(self, message: str, *, reason: str = "unknown") -> None:
        super().__init__(message)
        self.reason = reason


def _cache_age_text(updated_at: datetime) -> str:
    age = datetime.now() - updated_at
    if age.days >= 1:
        return f"{age.days}天"
    hours = age.seconds // 3600
    if hours >= 1:
        return f"{hours}小时"
    minutes = max(1, age.seconds // 60)
    return f"{minutes}分钟"


def _stale_cache_message(record, max_age: timedelta) -> str:
    return f"旧缓存已超过兜底上限（{_cache_age_text(record.updated_at)} > {max_age}），拒绝使用"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        text = str(value).replace("%", "").replace(",", "").strip()
        if text in {"", "-", "--", "nan", "None"}:
            return None
        numeric = float(text)
        return numeric if math.isfinite(numeric) else None
    except (TypeError, ValueError):
        return None


def _first_existing(row: pd.Series, names: list[str]) -> Any:
    for name in names:
        if name in row.index:
            value = row[name]
            if value is not None and not pd.isna(value):
                return value
    return None


def _latest_row(df: pd.DataFrame) -> pd.Series:
    """Pick the latest financial row across AKShare variants."""
    if df.empty:
        raise ValueError("未获取到财务指标")
    for col in ["日期", "报告期", "公告日期"]:
        if col in df.columns:
            tmp = df.copy()
            tmp["_sort_date"] = pd.to_datetime(tmp[col], errors="coerce")
            tmp = tmp.sort_values("_sort_date")
            if tmp["_sort_date"].notna().any():
                return tmp.iloc[-1]
    return df.iloc[0]


@contextmanager
def _without_proxy_env():
    """Market data sites often fail through a system proxy; bypass it locally."""
    keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"]
    old = {key: os.environ.get(key) for key in keys}
    try:
        for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
            os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _market_session(trust_env: bool = False) -> requests.Session:
    session = requests.Session()
    session.trust_env = trust_env
    session.headers.update(_MARKET_HEADERS)
    return session


_EM_MIN_INTERVAL = 1.0
_EM_MAX_JITTER = 0.4
# 单次东财调用的总请求预算上限。镜像轮询本身就是“重试”，限流风暴下不能把
# 同一批被风控的 IP 反复打几十次（既拖慢前台，又会加深封禁），故给总数封顶。
_EM_MAX_REQUESTS = 6
# 同一逻辑查询的分页爬取（全市场资金流/行情/情绪逐页）使用的小间隔。分页不是
# “突发的不同请求”，按 1s 防突发间隔会白白拖慢整轮筛选；实际网络延迟通常已 > 此值。
_EM_PAGE_INTERVAL = 0.15
_EM_LOCK = threading.Lock()
_EM_LAST_REQUEST_AT = 0.0
_EM_SESSION: Any | None = None
_EM_HOST_GROUPS = {
    "realtime": [
        "push2.eastmoney.com",
        "push2delay.eastmoney.com",
        "82.push2.eastmoney.com",
    ],
    "his": [
        "push2his.eastmoney.com",
        "1.push2his.eastmoney.com",
        "7.push2his.eastmoney.com",
        "17.push2his.eastmoney.com",
        "21.push2his.eastmoney.com",
        "82.push2his.eastmoney.com",
        "push2delay.eastmoney.com",
    ],
}


def _eastmoney_session(trust_env: bool = False):
    global _EM_SESSION
    if trust_env:
        session = _market_session(trust_env=True)
    else:
        if _EM_SESSION is None:
            _EM_SESSION = _market_session(trust_env=False)
        session = _EM_SESSION
    session.headers.update({"Referer": "https://quote.eastmoney.com/"})
    return session


def _eastmoney_throttle(min_interval: float | None = None) -> None:
    """串行节流闸。默认按 _EM_MIN_INTERVAL 防突发；同一逻辑查询的分页爬取可传入
    很小的 min_interval（分页不是“突发的不同请求”），避免整轮筛选被白白拖慢。"""
    global _EM_LAST_REQUEST_AT
    interval = _EM_MIN_INTERVAL if min_interval is None else max(0.0, float(min_interval))
    with _EM_LOCK:
        now = time.monotonic()
        if _EM_LAST_REQUEST_AT:
            wait = interval - (now - _EM_LAST_REQUEST_AT)
            if wait > 0:
                time.sleep(wait + random.uniform(0, min(_EM_MAX_JITTER, interval)))
                now = time.monotonic()
        _EM_LAST_REQUEST_AT = now


def _http_status_from_exception(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    try:
        return int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return None


def _classify_market_exception(exc: Exception) -> str:
    if isinstance(exc, MarketDataError):
        return exc.reason
    status_code = _http_status_from_exception(exc)
    text = str(exc)
    lowered = text.lower()
    if status_code in {401, 403} or "403" in text or "forbidden" in lowered:
        return "forbidden"
    if status_code == 429 or "429" in text or "too many requests" in lowered:
        return "rate_limit"
    if status_code and 500 <= status_code < 600:
        return "rate_limit"
    if any(marker in text for marker in ["RemoteDisconnected", "ServerDisconnected", "ConnectionResetError"]):
        return "rate_limit"
    if any(marker in lowered for marker in ["connection aborted", "remote end closed", "connection reset", "forcibly closed"]):
        return "rate_limit"
    if "empty" in lowered or "空数据" in text or "返回空" in text:
        return "empty"
    if "nameresolutionerror" in text or "dns" in lowered or "getaddrinfo failed" in lowered:
        return "network"
    if "network is unreachable" in lowered or "failed to establish a new connection" in lowered:
        return "network"
    if "proxyerror" in text or "proxy" in lowered:
        return "proxy"
    if isinstance(exc, requests.Timeout) or "timed out" in lowered or "timeout" in lowered:
        return "rate_limit"
    if "connection" in text or "max retries exceeded" in lowered:
        return "network"
    return "unknown"


def _eastmoney_hosts(host_group: str, hosts: list[str] | None = None) -> list[str]:
    if hosts:
        return list(dict.fromkeys(hosts))
    return list(_EM_HOST_GROUPS.get(host_group, _EM_HOST_GROUPS["his"]))


def _eastmoney_backoff(attempt: int) -> None:
    waits = [0.5, 1.5, 3.0]
    time.sleep(waits[min(attempt, len(waits) - 1)] + random.uniform(0, _EM_MAX_JITTER))


def _eastmoney_get(
    path: str,
    params: dict[str, Any],
    host_group: str = "his",
    *,
    retries: int = 3,
    hosts: list[str] | None = None,
    allow_proxy_fallback: bool = False,
    trust_env: bool = False,
    timeout: int | tuple[float, float] = 8,
    max_requests: int = _EM_MAX_REQUESTS,
    min_interval: float | None = None,
) -> dict[str, Any]:
    endpoint = "/" + path.lstrip("/")
    host_list = _eastmoney_hosts(host_group, hosts)
    attempts = max(1, int(retries or 1))
    budget = max(1, int(max_requests or 1))
    last_exc: Exception | None = None
    last_reason = "unknown"
    sent = 0
    session = _eastmoney_session(trust_env=trust_env)

    for attempt in range(attempts):
        for host in host_list:
            if sent >= budget:
                break
            sent += 1
            try:
                _eastmoney_throttle(min_interval)
                resp = session.get(f"https://{host}{endpoint}", params=params, timeout=timeout)
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict):
                    raise MarketDataError(f"{host} 返回非JSON对象", reason="empty")
                if payload.get("rc") not in (None, 0):
                    raise MarketDataError(f"{host} 返回异常：{payload.get('rt') or payload.get('rc')}", reason="empty")
                if not payload.get("data"):
                    raise MarketDataError(f"{host} 返回空数据", reason="empty")
                return payload
            except Exception as exc:
                last_exc = exc
                last_reason = _classify_market_exception(exc)
                if last_reason == "forbidden":
                    raise MarketDataError(f"{host} 东方财富接口拒绝访问：{exc}", reason=last_reason)
                continue
        if sent >= budget:
            break
        if attempt < attempts - 1:
            _eastmoney_backoff(attempt)

    # 国内东财接口默认直连；仅在确实疑似系统代理拦截时，才升级走代理重试一次
    # （VPN/Clash 下把国内流量送进海外节点通常更糟，不能作为常规回退）。
    if allow_proxy_fallback and not trust_env and last_reason == "proxy":
        try:
            return _eastmoney_get(
                path, params, host_group,
                retries=1, hosts=hosts, allow_proxy_fallback=False,
                trust_env=True, timeout=timeout,
                max_requests=min(budget, len(host_list)),
                min_interval=min_interval,
            )
        except Exception as exc:
            last_exc = exc
            last_reason = _classify_market_exception(exc)

    hosts_text = "、".join(host_list[:3]) + ("等" if len(host_list) > 3 else "")
    raise MarketDataError(f"东方财富{hosts_text}节点均失败：{last_exc}", reason=last_reason)


def _market_prefix(code: str, style: str = "tencent") -> str:
    code = code.strip().zfill(6)
    if code.startswith(("6", "9", "5")):
        return ("sh" if style in {"tencent", "sina"} else "SH") + code
    if code.startswith(("4", "8")):
        return ("bj" if style in {"tencent", "sina"} else "BJ") + code
    return ("sz" if style in {"tencent", "sina"} else "SZ") + code


def _tickflow_symbol(code: str) -> str:
    clean = str(code or "").strip().zfill(6)
    if clean.startswith(("6", "9", "5")):
        return f"{clean}.SH"
    if clean.startswith(("4", "8")):
        return f"{clean}.BJ"
    return f"{clean}.SZ"


def _tickflow_code(symbol: str) -> str:
    text = str(symbol or "").strip().upper().split(".", 1)[0]
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else ""


def _tickflow_client(api_key: str) -> Any:
    from tickflow import TickFlow

    return TickFlow(api_key=api_key.strip())


def _tickflow_quote_to_stock_quote(item: Mapping[str, Any]) -> StockQuote | None:
    code = _tickflow_code(item.get("symbol"))
    if not code:
        return None
    ext = item.get("ext") or {}
    name = str(ext.get("name") or item.get("name") or code).strip()
    prev_close = _to_float(item.get("prev_close"))
    price = _to_float(item.get("last_price"))
    change_pct = _to_float(ext.get("change_pct"))
    if change_pct is None and price is not None and prev_close:
        change_pct = (price - prev_close) / prev_close * 100
    return StockQuote(
        code=code,
        name=name,
        price=price,
        change_pct=change_pct,
        turnover=_to_float(ext.get("turnover_rate")),
    )


def _spot_quotes_from_tickflow(api_key: str) -> list[StockQuote]:
    client = _tickflow_client(api_key)
    try:
        rows = client.quotes.get_by_universes(["CN_Equity_A"])
        symbols = [str(item.get("symbol") or "").strip() for item in rows if item.get("symbol")]
        names: dict[str, str] = {}
        for offset in range(0, len(symbols), 1000):
            instruments = client.instruments.batch(symbols[offset:offset + 1000])
            names.update(
                {
                    str(item.get("symbol") or "").strip(): str(item.get("name") or "").strip()
                    for item in instruments
                    if item.get("symbol") and item.get("name")
                }
            )
        quotes = []
        for item in rows:
            quote = _tickflow_quote_to_stock_quote(item)
            if quote is not None:
                quote.name = names.get(str(item.get("symbol") or "").strip(), quote.name)
                quotes.append(quote)
        return _validate_spot_quotes(quotes, "TickFlow")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _tickflow_history_frame(item: Mapping[str, Any], days: int) -> pd.DataFrame:
    timestamps = list(item.get("timestamp") or [])
    rows = []
    for index, timestamp in enumerate(timestamps):
        try:
            date_text = datetime.fromtimestamp(float(timestamp) / 1000, SHANGHAI_TZ).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            continue
        rows.append(
            {
                "日期": date_text,
                "开盘": _to_float((item.get("open") or [])[index]),
                "收盘": _to_float((item.get("close") or [])[index]),
                "最高": _to_float((item.get("high") or [])[index]),
                "最低": _to_float((item.get("low") or [])[index]),
                "成交量": _to_float((item.get("volume") or [])[index]),
                "成交额": _to_float((item.get("amount") or [])[index]),
            }
        )
    return _history_frame(rows, days)


def _history_batch_from_tickflow(api_key: str, codes: list[str], days: int) -> dict[str, pd.DataFrame]:
    symbols = [_tickflow_symbol(code) for code in codes]
    clients: list[Any] = [_tickflow_client(api_key)]
    try:
        try:
            result = clients[0].klines.batch(
                symbols,
                period="1d",
                count=max(int(days or 1), HISTORY_CANONICAL_DAYS),
                adjust="forward",
                max_workers=5,
                batch_size=100,
            )
            if not result:
                raise RuntimeError("TickFlow 付费K线返回空结果")
        except Exception as paid_exc:
            from tickflow import TickFlow

            with redirect_stdout(io.StringIO()):
                free_client = TickFlow.free()
            clients.append(free_client)
            free_data: dict[str, Any] = {}
            free_failures: list[str] = []
            for offset in range(0, len(symbols), 20):
                chunk = symbols[offset:offset + 20]
                try:
                    chunk_data = free_client.klines.batch(
                        chunk,
                        period="1d",
                        count=max(int(days or 1), HISTORY_CANONICAL_DAYS),
                        adjust="forward",
                        max_workers=1,
                        batch_size=20,
                    )
                    free_data.update(chunk_data)
                except Exception as free_exc:
                    free_failures.append(f"{','.join(chunk)}: {free_exc}")
            if not free_data:
                raise MarketDataError(
                    f"TickFlow 付费K线无权限：{paid_exc}；免费K线兜底全部失败：{'；'.join(free_failures[-3:])}",
                    reason="permission",
                )
            result = free_data
        return {
            code: _tickflow_history_frame(result[symbol], days)
            for code, symbol in zip(codes, symbols)
            if symbol in result
        }
    finally:
        for client in clients:
            close = getattr(client, "close", None)
            if callable(close):
                close()


def _financial_metrics_from_tickflow(item: Mapping[str, Any]) -> FinancialMetrics:
    return FinancialMetrics(
        roe=_to_float(item.get("roe") if item.get("roe") is not None else item.get("roe_diluted")),
        gross_margin=_to_float(item.get("gross_margin")),
        net_margin=_to_float(item.get("net_margin")),
        debt_ratio=_to_float(item.get("debt_to_asset_ratio")),
        revenue_growth=_to_float(item.get("revenue_yoy")),
        profit_growth=_to_float(item.get("net_income_yoy")),
        operating_cashflow_per_share=_to_float(item.get("ocfps")),
        report_date=str(item.get("period_end") or item.get("announce_date") or ""),
    )


def _financial_metrics_batch_from_tickflow(api_key: str, codes: list[str]) -> dict[str, FinancialMetrics]:
    client = _tickflow_client(api_key)
    try:
        symbols = [_tickflow_symbol(code) for code in codes]
        result = client.financials.metrics(
            symbols,
            latest=True,
            max_workers=5,
            batch_size=100,
        )
        metrics: dict[str, FinancialMetrics] = {}
        for code, symbol in zip(codes, symbols):
            records = result.get(symbol) or []
            if records:
                metrics[code] = _financial_metrics_from_tickflow(records[0])
        return metrics
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()




def _eastmoney_secid(code: str) -> str:
    code = code.strip().zfill(6)
    market_id = "1" if code.startswith(("6", "9", "5")) else "0"
    return f"{market_id}.{code}"


def _fmt_yi(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value / 100000000:+.2f}亿"


def _history_frame(rows: list[dict[str, Any]], days: int) -> pd.DataFrame:
    df = _validate_history_frame(pd.DataFrame(rows)).tail(max(days, 60))
    if df.empty:
        raise MarketDataError("K线接口解析后为空")
    return df


def _history_col(df: pd.DataFrame, names: list[str]) -> str | None:
    lowered = {str(col).lower(): col for col in df.columns}
    for name in names:
        if name in df.columns:
            return name
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _validate_history_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        raise MarketDataError("K线接口解析后为空")
    date_col = _history_col(df, ["日期", "date", "day", "交易日期"])
    open_col = _history_col(df, ["开盘", "open"])
    high_col = _history_col(df, ["最高", "high"])
    low_col = _history_col(df, ["最低", "low"])
    close_col = _history_col(df, ["收盘", "close"])
    volume_col = _history_col(df, ["成交量", "volume", "vol"])
    missing = [
        label
        for label, col in [("日期", date_col), ("开盘", open_col), ("最高", high_col), ("最低", low_col), ("收盘", close_col)]
        if not col
    ]
    if missing:
        raise MarketDataError(f"历史行情缺少字段：{','.join(missing)}")

    clean = df.copy()
    dates = pd.to_datetime(clean[date_col], errors="coerce", format="mixed")
    open_s = clean[open_col].map(_to_float)
    high_s = clean[high_col].map(_to_float)
    low_s = clean[low_col].map(_to_float)
    close_s = clean[close_col].map(_to_float)
    mask = (
        dates.notna()
        & open_s.notna()
        & high_s.notna()
        & low_s.notna()
        & close_s.notna()
        & (open_s > 0)
        & (high_s > 0)
        & (low_s > 0)
        & (close_s > 0)
        & (high_s >= pd.concat([open_s, close_s], axis=1).max(axis=1))
        & (low_s <= pd.concat([open_s, close_s], axis=1).min(axis=1))
        & (high_s >= low_s)
    )
    clean = clean.loc[mask].copy()
    if clean.empty:
        raise MarketDataError("K线接口校验后无有效OHLC记录")

    clean[date_col] = dates.loc[mask].dt.strftime("%Y-%m-%d")
    clean[open_col] = open_s.loc[mask].astype(float)
    clean[high_col] = high_s.loc[mask].astype(float)
    clean[low_col] = low_s.loc[mask].astype(float)
    clean[close_col] = close_s.loc[mask].astype(float)
    if volume_col:
        clean[volume_col] = clean[volume_col].map(lambda value: numeric if (numeric := _to_float(value)) is not None and numeric >= 0 else None)
    clean = clean.sort_values(date_col).drop_duplicates(subset=[date_col], keep="last").reset_index(drop=True)
    return clean


def _validate_spot_quotes(quotes: list[StockQuote], source: str) -> list[StockQuote]:
    clean = [
        quote for quote in quotes
        if quote.code
        and quote.code.isdigit()
        and len(quote.code) == 6
        and quote.name
        and quote.name.lower() not in {"nan", "none"}
    ]
    if len(clean) < SPOT_MIN_ROWS:
        raise MarketDataError(f"{source} 全市场行情样本不足，仅获取 {len(clean)} 条")
    return clean


def _spot_quotes_from_akshare(bypass_proxy: bool = True) -> list[StockQuote]:
    import akshare as ak

    if bypass_proxy:
        with _without_proxy_env():
            df = _call_with_timeout(lambda: ak.stock_zh_a_spot_em(), timeout=SPOT_AKSHARE_TIMEOUT)
    else:
        df = _call_with_timeout(lambda: ak.stock_zh_a_spot_em(), timeout=SPOT_AKSHARE_TIMEOUT)
    if df.empty:
        raise MarketDataError("akshare全市场行情返回空数据")
    quotes = []
    for _, row in df.iterrows():
        quotes.append(
            StockQuote(
                code=str(row.get("代码", "")).zfill(6),
                name=str(row.get("名称", "")),
                price=_to_float(row.get("最新价")),
                change_pct=_to_float(row.get("涨跌幅")),
                turnover=_to_float(row.get("换手率")),
                market_cap=_to_float(row.get("总市值")),
                pe=_to_float(row.get("市盈率-动态")),
                pb=_to_float(row.get("市净率")),
            )
        )
    return _validate_spot_quotes(quotes, "akshare")


def _spot_quotes_from_eastmoney_http(host: str = "push2.eastmoney.com", trust_env: bool = False) -> list[StockQuote]:
    page_size = 100
    rows: list[dict[str, Any]] = []
    last_exc: Exception | None = None
    hosts = [host] if host and host != "push2.eastmoney.com" else None
    for page in range(1, 50):
        try:
            payload = _eastmoney_get(
                "/api/qt/clist/get",
                {
                    "pn": str(page),
                    "pz": str(page_size),
                    "po": "1",
                    "np": "1",
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f20",
                    "fs": EASTMONEY_A_MARKET_FS,
                    "fields": "f12,f14,f2,f3,f8,f20,f9,f23",
                },
                host_group="realtime",
                hosts=hosts,
                allow_proxy_fallback=not trust_env,
                trust_env=trust_env,
                retries=2,
                timeout=5,
                min_interval=_EM_PAGE_INTERVAL,
            )
            data = payload.get("data") or {}
            page_rows = data.get("diff") or []
            if not page_rows:
                break
            rows.extend(page_rows)
            total = int(data.get("total") or 0)
            if len(page_rows) < page_size or (total and len(rows) >= total):
                break
        except Exception as exc:
            last_exc = exc
            break
    if not rows:
        raise MarketDataError(f"{host} 全市场行情返回空数据：{last_exc}", reason=_classify_market_exception(last_exc) if last_exc else "empty")
    quotes = [
        StockQuote(
            code=str(row.get("f12", "")).zfill(6),
            name=str(row.get("f14", "")),
            price=_to_float(row.get("f2")),
            change_pct=_to_float(row.get("f3")),
            turnover=_to_float(row.get("f8")),
            market_cap=_to_float(row.get("f20")),
            pe=_to_float(row.get("f9")),
            pb=_to_float(row.get("f23")),
        )
        for row in rows
    ]
    return _validate_spot_quotes(quotes, f"东方财富HTTP({host})")


def _market_money_flow_from_eastmoney_http(host: str = "push2.eastmoney.com", trust_env: bool = False) -> dict[str, float]:
    page_size = 200
    rows: list[dict[str, Any]] = []
    last_exc: Exception | None = None
    hosts = [host] if host and host != "push2.eastmoney.com" else None
    for page in range(1, 90):
        try:
            payload = _eastmoney_get(
                "/api/qt/clist/get",
                {
                    "pn": str(page),
                    "pz": str(page_size),
                    "po": "1",
                    "np": "1",
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f164",
                    "fs": EASTMONEY_A_MARKET_FS,
                    "fields": "f12,f14,f164",
                },
                host_group="realtime",
                hosts=hosts,
                allow_proxy_fallback=not trust_env,
                trust_env=trust_env,
                retries=2,
                timeout=8,
                min_interval=_EM_PAGE_INTERVAL,
            )
            data = payload.get("data") or {}
            page_rows = data.get("diff") or []
            if not page_rows:
                break
            rows.extend(page_rows)
            total = int(data.get("total") or 0)
            if len(page_rows) < page_size or (total and len(rows) >= total):
                break
        except Exception as exc:
            last_exc = exc
            break
    if not rows:
        raise MarketDataError(f"{host} 全市场资金流返回空数据：{last_exc}", reason=_classify_market_exception(last_exc) if last_exc else "empty")

    flow_map: dict[str, float] = {}
    for row in rows:
        code = str(row.get("f12", "")).strip().zfill(6)
        value = _to_float(row.get("f164"))
        if len(code) == 6 and value is not None:
            flow_map[code] = value
    if len(flow_map) < SPOT_MIN_ROWS:
        raise MarketDataError(f"{host} 全市场资金流有效数据过少：{len(flow_map)}", reason="empty")
    return flow_map


def _redact_secret(text: str, secret: str) -> str:
    clean = str(text or "")
    if secret:
        clean = clean.replace(secret, "<TUSHARE_TOKEN>")
    return clean


def _tushare_error_reason(payload: dict[str, Any]) -> str:
    code = str(payload.get("code", ""))
    message = str(payload.get("msg") or payload.get("message") or "")
    lowered = message.lower()
    if code in {"2002", "2017"} or any(marker in message for marker in ["权限", "积分", "无权限", "积分不足"]) or "permission" in lowered:
        return "permission"
    if code in {"2001", "2003"} or "token" in lowered:
        return "permission"
    return "unknown"


def _tushare_http_post(
    api_name: str,
    token: str,
    params: dict[str, Any],
    fields: str,
    *,
    session: Any | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    http = session or requests.Session()
    resp = http.post(
        TUSHARE_API_URL,
        json={"api_name": api_name, "token": token, "params": params, "fields": fields},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise MarketDataError(f"Tushare {api_name} 返回非JSON对象", reason="empty")
    if payload.get("code") not in (0, "0", None):
        reason = _tushare_error_reason(payload)
        message = payload.get("msg") or payload.get("message") or payload.get("code")
        raise MarketDataError(f"Tushare {api_name} 未通过：{_redact_secret(str(message), token)}", reason=reason)
    return payload


def _tushare_records(payload: dict[str, Any], api_name: str, token: str) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    fields = data.get("fields") or []
    items = data.get("items") or []
    if not isinstance(fields, list) or not isinstance(items, list):
        raise MarketDataError(f"Tushare {api_name} 返回结构异常", reason="empty")
    return [dict(zip([str(field) for field in fields], item)) for item in items if isinstance(item, (list, tuple))]


def _recent_tushare_trade_dates(token: str, session: Any | None = None) -> list[str]:
    today = datetime.now().date()
    start = (today - timedelta(days=21)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    payload = _tushare_http_post(
        "trade_cal",
        token,
        {"exchange": "", "start_date": start, "end_date": end},
        "cal_date,is_open",
        session=session,
    )
    records = _tushare_records(payload, "trade_cal", token)
    trade_dates = [
        str(row.get("cal_date"))
        for row in records
        if str(row.get("is_open")) in {"1", "1.0", "True", "true"} and str(row.get("cal_date", "")).isdigit()
    ]
    trade_dates = sorted(set(trade_dates), reverse=True)
    if len(trade_dates) < 5:
        raise MarketDataError("Tushare trade_cal 未返回足够的最近5个开市交易日", reason="empty")
    return trade_dates[:5]


def _plain_stock_code_from_ts_code(ts_code: Any) -> str:
    code = str(ts_code or "").strip().upper().split(".", 1)[0]
    return code.zfill(6) if code.isdigit() and len(code) <= 6 else ""


def _market_money_flow_from_tushare_moneyflow_dc(token: str, session: Any | None = None) -> dict[str, float]:
    clean_token = (token or "").strip()
    if not clean_token:
        raise MarketDataError("Tushare token 未配置", reason="permission")

    http_session = session or requests.Session()
    trade_dates = _recent_tushare_trade_dates(clean_token, session=http_session)
    aggregated: dict[str, float] = {}
    for trade_date in trade_dates:
        payload = _tushare_http_post(
            "moneyflow_dc",
            clean_token,
            {"trade_date": trade_date},
            "ts_code,trade_date,net_amount",
            session=http_session,
        )
        data = payload.get("data") or {}
        fields = {str(field) for field in (data.get("fields") or [])}
        missing = {"ts_code", "net_amount"} - fields
        if missing:
            raise MarketDataError(f"Tushare moneyflow_dc 缺少字段：{','.join(sorted(missing))}", reason="empty")
        records = _tushare_records(payload, "moneyflow_dc", clean_token)
        if len(records) < TUSHARE_MONEY_FLOW_MIN_ROWS:
            raise MarketDataError(f"Tushare moneyflow_dc {trade_date} 返回行数过少：{len(records)}", reason="empty")
        valid_count = 0
        for row in records:
            code = _plain_stock_code_from_ts_code(row.get("ts_code"))
            net_wanyuan = _to_float(row.get("net_amount"))
            if code and net_wanyuan is not None:
                aggregated[code] = aggregated.get(code, 0.0) + net_wanyuan * 10000.0
                valid_count += 1
        if valid_count < TUSHARE_MONEY_FLOW_MIN_ROWS:
            raise MarketDataError(f"Tushare moneyflow_dc {trade_date} 有效资金流行数过少：{valid_count}", reason="empty")

    if len(aggregated) < TUSHARE_MONEY_FLOW_MIN_ROWS:
        raise MarketDataError(f"Tushare moneyflow_dc 5日聚合有效股票过少：{len(aggregated)}", reason="empty")
    return aggregated


def _spot_quotes_from_sina_http(trust_env: bool = False) -> list[StockQuote]:
    session = _market_session(trust_env=trust_env)
    session.headers.update({"Referer": "https://finance.sina.com.cn/"})
    page_size = 80
    rows: list[dict[str, Any]] = []
    last_exc: Exception | None = None
    for page in range(1, 90):
        try:
            resp = session.get(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
                params={
                    "page": str(page),
                    "num": str(page_size),
                    "sort": "symbol",
                    "asc": "1",
                    "node": "hs_a",
                    "symbol": "",
                    "_s_r_a": "page",
                },
                timeout=6,
            )
            resp.raise_for_status()
            page_rows = resp.json()
            if not page_rows:
                break
            rows.extend(page_rows)
            if len(page_rows) < page_size:
                break
        except Exception as exc:
            last_exc = exc
            break
    if not rows:
        raise MarketDataError(f"新浪全市场行情返回空数据：{last_exc}")

    quotes = []
    for row in rows:
        market_cap_wan = _to_float(row.get("mktcap"))
        quotes.append(
            StockQuote(
                code=str(row.get("code", "")).zfill(6),
                name=str(row.get("name", "")),
                price=_to_float(row.get("trade")),
                change_pct=_to_float(row.get("changepercent")),
                turnover=_to_float(row.get("turnoverratio")),
                market_cap=market_cap_wan * 10000 if market_cap_wan is not None else None,
                pe=_to_float(row.get("per")),
                pb=_to_float(row.get("pb")),
            )
        )
    return _validate_spot_quotes(quotes, "新浪HTTP")


def _quote_from_tencent(code: str) -> StockQuote | None:
    symbol = _market_prefix(code, "tencent")
    session = _market_session()
    resp = session.get("https://qt.gtimg.cn/q=" + symbol, timeout=8)
    resp.raise_for_status()
    text = resp.content.decode("gbk", "replace")
    payload = text.split("=", 1)[-1].strip().strip('";')
    fields = payload.split("~")
    if len(fields) < 39 or not fields[1].strip():
        return None
    price = _to_float(fields[3])
    pre_close = _to_float(fields[4])
    pct = _to_float(fields[32])
    if pct is None and price is not None and pre_close:
        pct = (price - pre_close) / pre_close * 100
    return StockQuote(
        code=code.strip().zfill(6),
        name=fields[1].strip(),
        price=price,
        change_pct=pct,
        turnover=_to_float(fields[38]) if len(fields) > 38 else None,
        pe=_to_float(fields[39]) if len(fields) > 39 else None,
    )


def _quote_from_sina(code: str) -> StockQuote | None:
    symbol = _market_prefix(code, "sina")
    session = _market_session()
    session.headers.update({"Referer": "https://finance.sina.com.cn/"})
    resp = session.get("https://hq.sinajs.cn/list=" + symbol, timeout=8)
    resp.raise_for_status()
    text = resp.content.decode("gbk", "replace")
    payload = text.split("=", 1)[-1].strip().strip('";')
    fields = payload.split(",")
    if len(fields) < 10 or not fields[0].strip():
        return None
    price = _to_float(fields[3])
    pre_close = _to_float(fields[2])
    pct = ((price - pre_close) / pre_close * 100) if price is not None and pre_close else None
    return StockQuote(
        code=code.strip().zfill(6),
        name=fields[0].strip(),
        price=price,
        change_pct=pct,
    )


def _parse_public_quote_time(*parts: str) -> datetime | None:
    """公开源只有本地交易所时间，统一转为 Asia/Shanghai 的带时区时间。"""
    text = "".join(str(part or "").strip() for part in parts)
    for pattern in ("%Y%m%d%H%M%S", "%Y-%m-%d%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=SHANGHAI_TZ)
        except ValueError:
            continue
    return None


def _parse_tencent_quote_batch(text: str, requested_codes: set[str], fetched_at: datetime) -> dict[str, QuoteSnapshot]:
    """解析腾讯 q= 批量响应；时间字段缺失仍返回快照，由预警层拒绝触发。"""
    parsed: dict[str, QuoteSnapshot] = {}
    for line in text.replace("\r", "").split(";"):
        if "=\"" not in line:
            continue
        key, payload = line.split("=", 1)
        symbol = key.strip().split("_")[-1].lower()
        code = "".join(ch for ch in symbol if ch.isdigit())[-6:].zfill(6)
        if code not in requested_codes:
            continue
        fields = payload.strip().strip('"').split("~")
        if len(fields) < 5 or not fields[1].strip():
            continue
        price = _to_float(fields[3])
        pre_close = _to_float(fields[4])
        pct = _to_float(fields[32]) if len(fields) > 32 else None
        if pct is None and price is not None and pre_close:
            pct = (price - pre_close) / pre_close * 100
        parsed[code] = QuoteSnapshot(
            quote=StockQuote(
                code=code,
                name=fields[1].strip(),
                price=price,
                change_pct=pct,
                turnover=_to_float(fields[38]) if len(fields) > 38 else None,
                pe=_to_float(fields[39]) if len(fields) > 39 else None,
            ),
            source="tencent",
            quote_at=_parse_public_quote_time(fields[30]) if len(fields) > 30 else None,
            fetched_at=fetched_at,
        )
    return parsed


def _parse_sina_quote_batch(text: str, requested_codes: set[str], fetched_at: datetime) -> dict[str, QuoteSnapshot]:
    """解析新浪 list= 批量响应，补齐腾讯缺失代码。"""
    parsed: dict[str, QuoteSnapshot] = {}
    for line in text.replace("\r", "").split(";"):
        if "=\"" not in line:
            continue
        key, payload = line.split("=", 1)
        symbol = key.strip().split("_")[-1].lower()
        code = "".join(ch for ch in symbol if ch.isdigit())[-6:].zfill(6)
        if code not in requested_codes:
            continue
        fields = payload.strip().strip('"').split(",")
        if len(fields) < 4 or not fields[0].strip():
            continue
        price = _to_float(fields[3])
        pre_close = _to_float(fields[2]) if len(fields) > 2 else None
        pct = ((price - pre_close) / pre_close * 100) if price is not None and pre_close else None
        parsed[code] = QuoteSnapshot(
            quote=StockQuote(code=code, name=fields[0].strip(), price=price, change_pct=pct),
            source="sina",
            quote_at=_parse_public_quote_time(fields[30], fields[31]) if len(fields) > 31 else None,
            fetched_at=fetched_at,
        )
    return parsed


def _public_quote_get(session: Any, url: str) -> str:
    """单批最多一次重试；调用方负责跨批抖动。"""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = session.get(url, timeout=5)
            response.raise_for_status()
            return response.content.decode("gbk", "replace")
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.08)
    raise last_error or RuntimeError("public quote request failed")


def fetch_direct_quote(code: str) -> StockQuote | None:
    for fn in (_quote_from_tencent, _quote_from_sina):
        try:
            quote = fn(code)
            if quote and quote.name:
                return quote
        except Exception:
            continue
    return None


def _friendly_market_error(exc: Exception) -> MarketDataError:
    if isinstance(exc, MarketDataError) and exc.reason in {"rate_limit", "empty"}:
        return MarketDataError("行情接口暂时被限流（不是你的网络问题）：软件已降低请求频率，请稍后重试；若有可用旧缓存会自动使用。", reason=exc.reason)
    if isinstance(exc, MarketDataError) and exc.reason == "forbidden":
        return MarketDataError("行情接口拒绝访问：可能是公开接口临时风控或安全软件拦截，请稍后重试；若有可用旧缓存会自动使用。", reason=exc.reason)
    if isinstance(exc, MarketDataError) and exc.reason == "network":
        return MarketDataError("当前网络无法访问公开行情接口，请检查网络、DNS 或防火墙后重试。", reason=exc.reason)
    classified = _classify_market_exception(exc)
    if classified in {"rate_limit", "empty"}:
        return MarketDataError("行情接口暂时被限流（不是你的网络问题）：软件已降低请求频率，请稍后重试；若有可用旧缓存会自动使用。", reason=classified)
    if classified == "network":
        return MarketDataError("当前网络无法访问公开行情接口，请检查网络、DNS 或防火墙后重试。", reason=classified)
    text = str(exc)
    if isinstance(exc, requests.Timeout) or "timed out" in text.lower() or "timeout" in text.lower():
        return MarketDataError("行情源请求超时：可能是网络较慢、接口限流或防火墙拦截，请稍后重试或切换网络。", reason="rate_limit")
    if "ProxyError" in text or "proxy" in text.lower():
        return MarketDataError("行情源连接失败：系统代理拦截了东方财富接口。软件已尝试绕过代理，请检查网络/代理设置后重试。", reason="proxy")
    if "403" in text or "Forbidden" in text:
        return MarketDataError("行情源拒绝访问：可能被接口限流、防火墙或安全软件拦截，请稍后重试。", reason="forbidden")
    if "Max retries exceeded" in text or "Connection" in text or "NameResolutionError" in text:
        reason = _classify_market_exception(exc)
        if reason == "rate_limit":
            return MarketDataError("行情接口暂时被限流（不是你的网络问题）：软件已降低请求频率，请稍后重试；若有可用旧缓存会自动使用。", reason=reason)
        return MarketDataError("行情源连接失败：当前网络无法访问公开行情接口，请稍后重试或切换网络。", reason=reason)
    return MarketDataError(f"行情源获取失败：{text[:180]}", reason=_classify_market_exception(exc))


def _short_error(exc: Exception) -> str:
    return str(_friendly_market_error(exc)).replace("\n", " ")[:240]


def _cache_fallback_message(action: str, updated_at: datetime, exc: Exception) -> str:
    age = _cache_age_text(updated_at)
    reason = getattr(_friendly_market_error(exc), "reason", "unknown")
    if reason in {"rate_limit", "empty", "forbidden"}:
        return f"{action}接口暂时被限流（不是你的网络问题），已使用{age}前的数据，稍后会自动重试。"
    if reason == "network":
        return f"当前网络无法访问公开行情接口，已使用{age}前的{action}缓存；请检查网络后重试。"
    return f"{action}实时源失败，已使用{age}前的旧缓存：{_short_error(exc)}"


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _call_with_timeout(fn, timeout: float):
    q: queue.Queue = queue.Queue(maxsize=1)

    def runner() -> None:
        try:
            q.put((True, fn()))
        except Exception as exc:
            q.put((False, exc))

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    try:
        ok, value = q.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"请求超过 {timeout:.0f} 秒仍无响应")
    if ok:
        return value
    raise value


def _history_from_eastmoney_http(code: str, days: int) -> pd.DataFrame:
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")
    payload = _eastmoney_get(
        "/api/qt/stock/kline/get",
        {
            "secid": _eastmoney_secid(code),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "1",
            "beg": start_date,
            "end": end_date,
            "lmt": str(max(days, 60) + 10),
        },
        host_group="his",
        retries=3,
        timeout=5,
    )
    klines = (payload.get("data") or {}).get("klines") or []
    if not klines:
        raise MarketDataError("东方财富K线节点返回空数据", reason="empty")
    rows = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 8:
            continue
        rows.append(
            {
                "日期": parts[0],
                "开盘": _to_float(parts[1]),
                "收盘": _to_float(parts[2]),
                "最高": _to_float(parts[3]),
                "最低": _to_float(parts[4]),
                "成交量": _to_float(parts[5]),
                "成交额": _to_float(parts[6]),
                "振幅": _to_float(parts[7]) if len(parts) > 7 else None,
                "涨跌幅": _to_float(parts[8]) if len(parts) > 8 else None,
                "涨跌额": _to_float(parts[9]) if len(parts) > 9 else None,
                "换手率": _to_float(parts[10]) if len(parts) > 10 else None,
            }
        )
    return _history_frame(rows, days)


def _money_flow_klines_from_eastmoney_http(code: str, lmt: int) -> list[str]:
    """逐只主力资金日线。daykline 是历史日线端点，避免实时 kline 只回当日。"""
    last_exc: Exception | None = None
    # daykline 是首选历史端点，给足镜像预算；旧 kline 仅作次级兜底，预算压低避免重复砸限流 IP。
    endpoints = [
        ("/api/qt/stock/fflow/daykline/get", max(120, int(lmt or 1)), _EM_MAX_REQUESTS),
        ("/api/qt/stock/fflow/kline/get", max(60, int(lmt or 1)), 2),
    ]
    for path, limit, budget in endpoints:
        try:
            payload = _eastmoney_get(
                path,
                {
                    "lmt": str(limit),
                    "klt": "101",
                    "secid": _eastmoney_secid(code),
                    "fields1": "f1,f2,f3,f7",
                    "fields2": "f51,f52,f53,f54,f55,f56",
                },
                host_group="his",
                retries=2,
                timeout=(3.05, 8),
                max_requests=budget,
            )
            klines = ((payload.get("data") or {}).get("klines") or [])
            if klines:
                return klines
        except Exception as exc:
            last_exc = exc
            # IP/网络级失败时，次级 kline 端点走同一被限流 IP 必然同样结果，
            # 不再空耗一轮，直接交给第三方兜底。
            if _classify_market_exception(exc) in {"rate_limit", "network", "forbidden"}:
                break
            continue
    raise MarketDataError(f"东方财富多个主力资金节点均失败：{last_exc}", reason=_classify_market_exception(last_exc) if last_exc else "empty")


def _call_optional_money_flow_api(module: Any, code: str):
    candidates = [
        ("stock", "market", "get_money_flow"),
        ("stock", "market", "get_capital_flow"),
        ("stock", "finance", "get_money_flow"),
        ("stock", "finance", "get_capital_flow"),
    ]
    for path in candidates:
        target = module
        for name in path:
            target = getattr(target, name, None)
            if target is None:
                break
        if callable(target):
            for kwargs in ({"code": code, "source": "tencent"}, {"code": code, "source": "baidu"}, {"code": code}, {"stock_code": code}):
                try:
                    return target(**kwargs)
                except TypeError:
                    continue
    return None


def _money_flow_df_to_klines(df: Any, lmt: int) -> list[str]:
    if df is None or getattr(df, "empty", True):
        return []
    # 列名别名兼容多家第三方 schema。adata 真实日线列名为英文蛇形
    # （main_net_inflow / sm_/mid_/lg_/max_net_inflow，已核对 1nchaos/adata 源码），
    # 务必覆盖，否则真实 adata 返回会因列名未命中而被当成空数据丢弃。
    aliases = {
        "date": ["日期", "date", "trade_date", "交易日期"],
        "main": ["主力净流入", "主力净流入净额", "主力净额", "main", "main_net_inflow"],
        "small": ["小单净流入", "小单净额", "small", "sm_net_inflow"],
        "medium": ["中单净流入", "中单净额", "medium", "mid_net_inflow"],
        "large": ["大单净流入", "大单净额", "large", "lg_net_inflow"],
        "xlarge": ["超大单净流入", "超大单净额", "xlarge", "max_net_inflow"],
    }
    columns = {str(col).strip().lower(): col for col in df.columns}

    def pick(names: list[str]):
        for name in names:
            if name in df.columns:
                return name
            lowered = name.lower()
            if lowered in columns:
                return columns[lowered]
        return None

    picked = {key: pick(names) for key, names in aliases.items()}
    if not picked["date"] or not picked["main"]:
        return []
    rows = []
    clean = df.copy()
    clean["_sort_date"] = pd.to_datetime(clean[picked["date"]], errors="coerce", format="mixed")
    clean = clean.sort_values("_sort_date").tail(max(1, int(lmt or 1)))
    for _, row in clean.iterrows():
        date_value = row.get(picked["date"])
        date_text = str(date_value)[:10]
        values = []
        for key in ["main", "small", "medium", "large", "xlarge"]:
            col = picked.get(key)
            values.append(_to_float(row.get(col)) if col else 0.0)
        rows.append(",".join([date_text] + [str(value or 0.0) for value in values]))
    return rows


def _money_flow_fallback_source(code: str, lmt: int, primary_reason: str = "unknown") -> list[str]:
    """第三方资金流兜底（可选依赖）。失败时不再静默吞掉，而是把每个源失败的原因
    汇总进异常文案，便于“数据缺口”面板告诉买家究竟为什么没数据。

    诚实说明：adata 的默认 get_capital_flow 与 efinance 的 get_history_bill 底层都走东财
    （push2his 同一接口/同一 IP）。当主源因 IP 级限流/封禁失败时，efinance 必然同样失败，
    直接跳过以免再砸被封 IP；adata 因其 README 宣称的多源/动态代理机制仍保留一次尝试。
    真正与东财不同 IP 的独立源是腾讯当日资金流（见 _money_flow_today_from_tencent）。"""
    _eastmoney_throttle()
    notes: list[str] = []

    try:
        import adata  # type: ignore
    except ImportError:
        notes.append("adata 未安装")
    except Exception as exc:
        notes.append(f"adata 导入失败：{_short_error(exc)}")
    else:
        try:
            df = _call_optional_money_flow_api(adata, code)
            klines = _money_flow_df_to_klines(df, lmt)
            if klines:
                return klines
            notes.append("adata 未返回可识别的资金流列")
        except Exception as exc:
            notes.append(f"adata 调用失败：{_short_error(exc)}")

    if primary_reason in {"rate_limit", "forbidden"}:
        notes.append("efinance 与东财同 IP，主源被限流/封禁时跳过")
    else:
        try:
            import efinance as ef  # type: ignore
        except ImportError:
            notes.append("efinance 未安装")
        except Exception as exc:
            notes.append(f"efinance 导入失败：{_short_error(exc)}")
        else:
            try:
                stock_api = getattr(ef, "stock", None)
                func = getattr(stock_api, "get_history_bill", None)
                if not callable(func):
                    notes.append("efinance 无 get_history_bill 接口")
                else:
                    try:
                        df = func(code)
                    except TypeError:
                        df = func(stock_codes=code)
                    klines = _money_flow_df_to_klines(df, lmt)
                    if klines:
                        return klines
                    notes.append("efinance 未返回可识别的资金流列")
            except Exception as exc:
                notes.append(f"efinance 调用失败：{_short_error(exc)}")

    raise MarketDataError("；".join(notes) or "未命中可用的第三方资金流兜底源", reason="empty")


def _money_flow_today_from_tencent(code: str) -> dict[str, Any]:
    """腾讯实时资金流（qt.gtimg.cn/q=ff_*）当日快照，作为东财历史源限流时的降级兜底。

    与东财历史源不同主机、不同 IP，是真正的独立换源；但它只回当日主力/散户净流入
    （单位万元），没有日线历史，无法还原 3/5/10 日窗口。调用方据此构造“仅当日”降级快照，
    切勿把这一行喂给多日求和（会重蹈窗口退化 bug）。返回单位为元。"""
    symbol = _market_prefix(code, "tencent")
    session = _market_session()
    session.headers.update({"Referer": "https://gu.qq.com/"})
    try:
        resp = session.get(f"https://qt.gtimg.cn/q=ff_{symbol}", timeout=(3.05, 6))
        resp.raise_for_status()
    except Exception as exc:
        raise MarketDataError(f"腾讯资金流接口请求失败：{exc}", reason=_classify_market_exception(exc)) from exc
    quoted = (resp.text or "").split('"')
    parts = (quoted[1] if len(quoted) >= 2 else "").split("~")
    if len(parts) < 4 or not parts[0]:
        raise MarketDataError("腾讯资金流接口返回空数据", reason="empty")
    main_wan = _to_float(parts[3])
    if main_wan is None:
        raise MarketDataError("腾讯资金流接口未含主力净流入字段", reason="empty")
    small_wan = _to_float(parts[7]) if len(parts) > 7 else None
    date_text = datetime.now().strftime("%Y-%m-%d")
    for token in parts:
        token = token.strip()
        if len(token) == 8 and token.isdigit():
            date_text = f"{token[:4]}-{token[4:6]}-{token[6:]}"
            break
    return {
        "date": date_text,
        "main": main_wan * 10000.0,
        "small": (small_wan * 10000.0) if small_wan is not None else 0.0,
    }


def _history_from_tencent_http(code: str, days: int) -> pd.DataFrame:
    symbol = _market_prefix(code, "tencent")
    session = _market_session()
    session.headers.update({"Referer": "https://gu.qq.com/"})
    resp = session.get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        params={"param": f"{symbol},day,,,{max(days, 60)},qfq"},
        timeout=7,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = ((payload.get("data") or {}).get(symbol) or {})
    klines = data.get("qfqday") or data.get("day") or []
    if not klines:
        raise MarketDataError("腾讯K线接口返回空数据")
    rows = []
    for item in klines:
        if len(item) < 6:
            continue
        rows.append(
            {
                "日期": item[0],
                "开盘": _to_float(item[1]),
                "收盘": _to_float(item[2]),
                "最高": _to_float(item[3]),
                "最低": _to_float(item[4]),
                "成交量": _to_float(item[5]),
                "成交额": _to_float(item[6]) if len(item) > 6 else None,
            }
        )
    return _history_frame(rows, days)


def _history_from_sina_http(code: str, days: int) -> pd.DataFrame:
    symbol = _market_prefix(code, "sina")
    session = _market_session()
    session.headers.update({"Referer": "https://finance.sina.com.cn/"})
    resp = session.get(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        params={"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(max(days, 60))},
        timeout=7,
    )
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError:
        payload = json.loads(resp.text)
    if not payload:
        raise MarketDataError("新浪K线接口返回空数据")
    rows = []
    for item in payload:
        rows.append(
            {
                "日期": str(item.get("day", ""))[:10],
                "开盘": _to_float(item.get("open")),
                "收盘": _to_float(item.get("close")),
                "最高": _to_float(item.get("high")),
                "最低": _to_float(item.get("low")),
                "成交量": _to_float(item.get("volume")),
            }
        )
    return _history_frame(rows, days)


def _history_from_akshare(code: str, days: int) -> pd.DataFrame:
    import akshare as ak

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    with _without_proxy_env():
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=end_date)
    if df.empty:
        raise MarketDataError("akshare返回空历史行情")
    return df


def _sentiment_from_eastmoney_http() -> MarketSentiment:
    last_exc: Exception | None = None
    rows = []
    try:
        collected = []
        for page in range(1, 70):
            payload = _eastmoney_get(
                "/api/qt/clist/get",
                {
                    "pn": str(page),
                    "pz": "100",
                    "po": "1",
                    "np": "1",
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f3",
                    "fs": EASTMONEY_A_MARKET_FS,
                    "fields": "f12,f14,f3",
                },
                host_group="realtime",
                retries=2,
                timeout=4,
                min_interval=_EM_PAGE_INTERVAL,
            )
            page_rows = ((payload.get("data") or {}).get("diff") or [])
            if not page_rows:
                break
            collected.extend(page_rows)
            if len(page_rows) < 100:
                break
        rows = collected
    except Exception as exc:
        last_exc = exc
    if not rows:
        raise MarketDataError(f"市场情绪HTTP接口返回空数据：{last_exc}", reason=_classify_market_exception(last_exc) if last_exc else "empty")
    if len(rows) < 1000:
        raise MarketDataError(f"市场情绪样本不足，仅获取 {len(rows)} 条", reason="empty")
    changes = [_to_float(row.get("f3")) for row in rows]
    changes = [value for value in changes if value is not None]
    up = sum(1 for value in changes if value > 0)
    down = sum(1 for value in changes if value < 0)
    flat = max(0, len(changes) - up - down)
    limit_up = sum(1 for value in changes if value >= 9.8)
    limit_down = sum(1 for value in changes if value <= -9.8)
    avg = sum(changes) / len(changes) if changes else None
    if up >= down * 1.5 and limit_up > limit_down:
        tone = "偏暖"
    elif down >= up * 1.5 or limit_down > limit_up:
        tone = "偏弱"
    else:
        tone = "震荡"
    summary = f"{tone}：上涨{up}家，下跌{down}家，涨停{limit_up}家，跌停{limit_down}家，平均涨跌幅{fmt_number(avg, '%')}"
    return MarketSentiment(up=up, down=down, flat=flat, limit_up=limit_up, limit_down=limit_down, avg_change_pct=avg, summary=summary)


def _sentiment_from_quotes(quotes: list[StockQuote]) -> MarketSentiment:
    changes = [q.change_pct for q in quotes if q.change_pct is not None]
    if len(changes) < 1000:
        raise MarketDataError(f"行情样本不足，仅 {len(changes)} 条")
    up = sum(1 for value in changes if value > 0)
    down = sum(1 for value in changes if value < 0)
    flat = max(0, len(changes) - up - down)
    limit_up = sum(1 for value in changes if value >= 9.8)
    limit_down = sum(1 for value in changes if value <= -9.8)
    avg = sum(changes) / len(changes) if changes else None
    if up >= down * 1.5 and limit_up > limit_down:
        tone = "偏暖"
    elif down >= up * 1.5 or limit_down > limit_up:
        tone = "偏弱"
    else:
        tone = "震荡"
    summary = f"{tone}：上涨{up}家，下跌{down}家，涨停{limit_up}家，跌停{limit_down}家，平均涨跌幅{fmt_number(avg, '%')}"
    return MarketSentiment(up=up, down=down, flat=flat, limit_up=limit_up, limit_down=limit_down, avg_change_pct=avg, summary=summary)


class DataFetcher:
    def __init__(
        self,
        cache: CacheStore | None = None,
        tushare_token_provider: Callable[[], str] | None = None,
        tickflow_api_key_provider: Callable[[], str] | None = None,
    ) -> None:
        self.cache = cache or CacheStore()
        self.tushare_token_provider = tushare_token_provider
        self.tickflow_api_key_provider = tickflow_api_key_provider
        self.last_market_money_flow_source = ""
        self.last_market_money_flow_note = ""

    def _tickflow_api_key_text(self) -> str:
        if self.tickflow_api_key_provider is not None:
            try:
                key = self.tickflow_api_key_provider()
                if key:
                    return str(key).strip()
            except Exception:
                return ""
        return (os.environ.get("TICKFLOW_API_KEY") or self.cache.get_setting(TICKFLOW_API_KEY_SETTING, "")).strip()

    def has_tickflow(self) -> bool:
        return bool(self._tickflow_api_key_text())

    def batch_financial_metrics(self, codes: list[str]) -> dict[str, FinancialMetrics]:
        api_key = self._tickflow_api_key_text()
        if not api_key:
            return {}
        return _financial_metrics_batch_from_tickflow(api_key, codes)

    def batch_history(self, codes: list[str], days: int = HISTORY_CANONICAL_DAYS) -> dict[str, pd.DataFrame]:
        api_key = self._tickflow_api_key_text()
        if not api_key:
            return {}
        return _history_batch_from_tickflow(api_key, codes, days)

    def _tushare_money_flow_enabled(self) -> bool:
        return self.cache.get_setting(TUSHARE_MONEY_FLOW_FALLBACK_SETTING, "0") == "1"

    def _tushare_token_text(self) -> str:
        if self.tushare_token_provider is not None:
            try:
                token = self.tushare_token_provider()
                if token:
                    return str(token).strip()
            except Exception:
                return ""
        return (os.environ.get("TUSHARE_TOKEN") or os.environ.get("TS_TOKEN") or self.cache.get_setting("tushare_token", "")).strip()

    def spot_quotes(self, force: bool = False) -> tuple[list[StockQuote], datetime | None]:
        start = time.perf_counter()
        cache_key = "spot_quotes"
        if not force:
            cached = self.cache.get_json(cache_key, QUOTE_TTL)
            if cached:
                self.cache.add_fetch_log("全市场行情", source="cache", status="cache", message="命中5分钟行情缓存", duration_ms=_elapsed_ms(start))
                return [StockQuote(**item) for item in cached.payload], cached.updated_at

        failures: list[str] = []
        tickflow_key = self._tickflow_api_key_text()
        sources = [
            *([("tickflow", lambda: _spot_quotes_from_tickflow(tickflow_key))] if tickflow_key else []),
            ("sina_http", lambda: _spot_quotes_from_sina_http(trust_env=False)),
            ("sina_http_proxy", lambda: _spot_quotes_from_sina_http(trust_env=True)),
            ("eastmoney_gateway", lambda: _spot_quotes_from_eastmoney_http()),
            ("akshare_direct", lambda: _spot_quotes_from_akshare(bypass_proxy=True)),
            ("akshare_proxy", lambda: _spot_quotes_from_akshare(bypass_proxy=False)),
        ]
        for source_name, fetch_fn in sources:
            source_start = time.perf_counter()
            try:
                quotes = fetch_fn()
                self.cache.set_json(cache_key, [quote.__dict__ for quote in quotes])
                prefix = "获取" if not failures else f"前序{len(failures)}个源失败后获取"
                self.cache.add_fetch_log(
                    "全市场行情",
                    source=source_name,
                    status="success",
                    message=f"{prefix} {len(quotes)} 条行情",
                    duration_ms=_elapsed_ms(start),
                )
                return quotes, datetime.now().astimezone()
            except Exception as exc:
                failures.append(f"{source_name}: {_short_error(exc)}")
                self.cache.add_fetch_log(
                    "全市场行情",
                    source=source_name,
                    status="failed",
                    message=_short_error(exc),
                    duration_ms=_elapsed_ms(source_start),
                )
                continue

        cached = self.cache.get_json(cache_key)
        if cached and datetime.now() - cached.updated_at <= QUOTE_FALLBACK_MAX_AGE:
            message = f"全市场实时源失败，使用旧行情缓存：{' | '.join(failures[-3:])}"
            self.cache.add_fetch_log("全市场行情", source="cache", status="cache", message=message, duration_ms=_elapsed_ms(start))
            return [StockQuote(**item) for item in cached.payload], cached.updated_at
        if cached:
            self.cache.add_fetch_log(
                "全市场行情",
                source="cache",
                status="failed",
                message=_stale_cache_message(cached, QUOTE_FALLBACK_MAX_AGE),
                duration_ms=_elapsed_ms(start),
            )

        message = (
            "全市场行情获取失败：新浪HTTP、东方财富HTTP、备用节点和akshare均不可用，且本机没有可用旧缓存。"
            "可能原因包括公开接口限流、网络防火墙/安全软件拦截、系统代理配置异常或行情源临时变更。"
            f"最近失败：{' | '.join(failures[-3:])}"
        )
        self.cache.add_fetch_log(
            "全市场行情",
            source="sina/eastmoney/akshare",
            status="failed",
            message=message,
            duration_ms=_elapsed_ms(start),
        )
        raise MarketDataError(message)

    def market_money_flow(self, force: bool = False) -> dict[str, float]:
        start = time.perf_counter()
        cache_key = "market_money_flow"
        self.last_market_money_flow_source = ""
        self.last_market_money_flow_note = ""
        if not force:
            cached = self.cache.get_json(cache_key, MONEY_FLOW_TTL)
            if cached:
                self.cache.add_fetch_log("全市场资金流", source="cache", status="cache", message="命中15分钟资金流缓存", duration_ms=_elapsed_ms(start))
                self.last_market_money_flow_source = "cache"
                self.last_market_money_flow_note = "主力进场使用15分钟内资金流缓存。"
                return {str(code).zfill(6): float(value) for code, value in dict(cached.payload).items()}

        failures: list[str] = []
        sources = [
            ("eastmoney_gateway", lambda: _market_money_flow_from_eastmoney_http()),
        ]
        for source_name, fetch_fn in sources:
            source_start = time.perf_counter()
            try:
                flow_map = fetch_fn()
                self.cache.set_json(cache_key, flow_map)
                prefix = "获取" if not failures else f"前序{len(failures)}个源失败后获取"
                self.cache.add_fetch_log(
                    "全市场资金流",
                    source=source_name,
                    status="success",
                    message=f"{prefix} {len(flow_map)} 条5日主力资金",
                    duration_ms=_elapsed_ms(start),
                )
                self.last_market_money_flow_source = source_name
                self.last_market_money_flow_note = "主力进场使用东方财富批量资金流。"
                return flow_map
            except Exception as exc:
                failures.append(f"{source_name}: {_short_error(exc)}")
                self.cache.add_fetch_log(
                    "全市场资金流",
                    source=source_name,
                    status="failed",
                    message=_short_error(exc),
                    duration_ms=_elapsed_ms(source_start),
                )
                continue

        cached = self.cache.get_json(cache_key)
        if cached and datetime.now() - cached.updated_at <= MONEY_FLOW_FALLBACK_MAX_AGE:
            message = _cache_fallback_message("全市场资金流", cached.updated_at, MarketDataError(" | ".join(failures[-3:]), reason="rate_limit" if any("限流" in item for item in failures) else "unknown"))
            self.cache.add_fetch_log("全市场资金流", source="cache", status="cache", message=message, duration_ms=_elapsed_ms(start))
            self.last_market_money_flow_source = "cache_stale"
            self.last_market_money_flow_note = "东方财富资金流不可用，已使用1天内旧缓存；未消耗 Tushare 权限/积分。"
            return {str(code).zfill(6): float(value) for code, value in dict(cached.payload).items()}
        if cached:
            self.cache.add_fetch_log(
                "全市场资金流",
                source="cache",
                status="failed",
                message=_stale_cache_message(cached, MONEY_FLOW_FALLBACK_MAX_AGE),
                duration_ms=_elapsed_ms(start),
            )

        tushare_enabled = self._tushare_money_flow_enabled()
        tushare_token = self._tushare_token_text()
        if tushare_enabled and tushare_token:
            source_start = time.perf_counter()
            try:
                flow_map = _market_money_flow_from_tushare_moneyflow_dc(tushare_token)
                self.cache.set_json(cache_key, flow_map)
                self.cache.add_fetch_log(
                    "全市场资金流",
                    source="tushare_moneyflow_dc",
                    status="success",
                    message=(
                        f"东方财富不可用且无可用缓存，使用 Tushare moneyflow_dc 增强兜底 {len(flow_map)} 条；"
                        "可能消耗 Tushare 权限/积分"
                    ),
                    duration_ms=_elapsed_ms(start),
                )
                self.last_market_money_flow_source = "tushare_moneyflow_dc"
                self.last_market_money_flow_note = "东方财富资金流不可用，已使用 Tushare moneyflow_dc 增强兜底。"
                return flow_map
            except Exception as exc:
                failures.append(f"tushare_moneyflow_dc: {_short_error(exc)}")
                self.cache.add_fetch_log(
                    "全市场资金流",
                    source="tushare_moneyflow_dc",
                    status="failed",
                    message=f"Tushare 兜底失败（权限/积分/token/网络或字段问题）：{_short_error(exc)}",
                    duration_ms=_elapsed_ms(source_start),
                )
        elif tushare_enabled:
            failures.append("tushare_moneyflow_dc: Tushare token 未配置")
            self.cache.add_fetch_log(
                "全市场资金流",
                source="tushare_moneyflow_dc",
                status="skipped",
                message="Tushare 兜底开关已开启，但 token 未配置；不阻断筛选",
                duration_ms=_elapsed_ms(start),
            )
        else:
            self.cache.add_fetch_log(
                "全市场资金流",
                source="tushare_moneyflow_dc",
                status="skipped",
                message="Tushare 资金流兜底未启用；保持默认免费链路",
                duration_ms=_elapsed_ms(start),
            )

        message = (
            "全市场资金流获取失败：东方财富HTTP、备用节点和代理回退均不可用，且本机没有可用旧缓存。"
            "可能原因包括公开接口限流、网络防火墙/安全软件拦截、系统代理配置异常、行情源临时变更，"
            "或 Tushare 未启用/无 token/无权限/积分不足。"
            f"最近失败：{' | '.join(failures[-3:])}"
        )
        self.last_market_money_flow_source = "unavailable"
        self.last_market_money_flow_note = "资金流不可用：东财限流；Tushare 未启用/无权限/无 token，已忽略主力条件。"
        self.cache.add_fetch_log(
            "全市场资金流",
            source="eastmoney/tushare",
            status="failed",
            message=message,
            duration_ms=_elapsed_ms(start),
        )
        raise _friendly_market_error(MarketDataError(message))

    def search(self, keyword: str, limit: int = 30) -> list[StockQuote]:
        keyword = keyword.strip().lower()
        if not keyword:
            return []
        if keyword.isdigit() and len(keyword) == 6:
            quote = self.quote_by_code(keyword)
            return [quote] if quote else []
        try:
            # A new name search should try the public source first. The normal
            # stale-cache fallback remains available if all sources fail.
            quotes, _ = self.spot_quotes(force=True)
        except MarketDataError:
            return []
        matches = [quote for quote in quotes if keyword in quote.code.lower() or keyword in quote.name.lower()]
        return matches[:limit]

    def quotes_by_codes(self, codes: list[str] | set[str] | tuple[str, ...]) -> dict[str, QuoteSnapshot]:
        """批量取得预警专用免费行情，不回退全市场缓存或逐条 HTTP 请求。"""
        unique_codes = list(dict.fromkeys(
            str(code or "").strip().zfill(6)
            for code in codes
            if str(code or "").strip().isdigit() and len(str(code or "").strip()) <= 6
        ))
        snapshots: dict[str, QuoteSnapshot] = {}
        if not unique_codes:
            return snapshots
        started = time.perf_counter()
        session = _market_session()
        for offset in range(0, len(unique_codes), 20):
            if offset:
                time.sleep(random.uniform(0.04, 0.12))
            batch = unique_codes[offset:offset + 20]
            requested = set(batch)
            fetched_at = datetime.now(SHANGHAI_TZ)
            symbols = ",".join(_market_prefix(code, "tencent") for code in batch)
            try:
                text = _public_quote_get(session, "https://qt.gtimg.cn/q=" + symbols)
                snapshots.update(_parse_tencent_quote_batch(text, requested, fetched_at))
            except Exception as exc:
                self.cache.add_fetch_log(
                    "预警批量行情", ",".join(batch), "tencent_http", "failed",
                    f"腾讯批量行情失败：{_short_error(exc)}", _elapsed_ms(started),
                )
            missing = requested.difference(snapshots)
            if missing:
                try:
                    session.headers.update({"Referer": "https://finance.sina.com.cn/"})
                    symbols = ",".join(_market_prefix(code, "sina") for code in sorted(missing))
                    text = _public_quote_get(session, "https://hq.sinajs.cn/list=" + symbols)
                    snapshots.update(_parse_sina_quote_batch(text, missing, datetime.now(SHANGHAI_TZ)))
                except Exception as exc:
                    self.cache.add_fetch_log(
                        "预警批量行情", ",".join(sorted(missing)), "sina_http", "failed",
                        f"新浪补源失败：{_short_error(exc)}", _elapsed_ms(started),
                    )
        source_summary = "+".join(sorted({item.source for item in snapshots.values()})) or "public_quote"
        self.cache.add_fetch_log(
            "预警批量行情", ",".join(unique_codes), source_summary, "success" if snapshots else "failed",
            f"可用 {len(snapshots)}/{len(unique_codes)} 条（腾讯主源，新浪补齐）", _elapsed_ms(started),
        )
        return snapshots

    def quote_by_code(self, code: str) -> StockQuote | None:
        start = time.perf_counter()
        code = code.strip().zfill(6)
        direct = fetch_direct_quote(code)
        if direct:
            self.cache.add_fetch_log("个股实时", target=code, source="tencent_or_sina", status="success", message="命中腾讯/新浪单股实时源", duration_ms=_elapsed_ms(start))
            return direct
        try:
            quotes, _ = self.spot_quotes()
            for quote in quotes:
                if quote.code == code:
                    self.cache.add_fetch_log("个股实时", target=code, source="spot_quotes", status="success", message="从全市场行情匹配", duration_ms=_elapsed_ms(start))
                    return quote
            self.cache.add_fetch_log("个股实时", target=code, source="spot_quotes", status="failed", message="全市场行情中未找到该代码", duration_ms=_elapsed_ms(start))
        except Exception as exc:
            self.cache.add_fetch_log("个股实时", target=code, source="spot_quotes", status="failed", message=f"单股实时源失败，全市场回退也失败：{_short_error(exc)}", duration_ms=_elapsed_ms(start))
        return None

    def financial_metrics(self, code: str, force: bool = False) -> tuple[FinancialMetrics, datetime | None]:
        start = time.perf_counter()
        code = code.strip().zfill(6)
        cache_key = f"financial:{code}"
        if not force:
            cached = self.cache.get_json(cache_key, FINANCIAL_TTL)
            if cached:
                self.cache.add_fetch_log("财务指标", target=code, source="cache", status="cache", message="命中1天财务缓存", duration_ms=_elapsed_ms(start))
                return FinancialMetrics(**cached.payload), cached.updated_at

        try:
            import akshare as ak

            start_year = str(max(2000, datetime.now().year - 6))
            with _without_proxy_env():
                df = ak.stock_financial_analysis_indicator(symbol=code, start_year=start_year)
            row = _latest_row(df)
            metrics = FinancialMetrics(
                roe=_to_float(_first_existing(row, ["净资产收益率(%)", "净资产收益率", "摊薄净资产收益率(%)", "加权净资产收益率(%)"])),
                gross_margin=_to_float(_first_existing(row, ["销售毛利率(%)", "毛利率(%)", "销售毛利率", "主营业务利润率(%)"])),
                net_margin=_to_float(_first_existing(row, ["销售净利率(%)", "净利率(%)", "销售净利率", "净利润率(%)"])),
                debt_ratio=_to_float(_first_existing(row, ["资产负债率(%)", "资产负债率"])),
                revenue_growth=_to_float(_first_existing(row, ["主营业务收入增长率(%)", "营业收入同比增长率(%)", "营业收入增长率(%)", "主营业务收入增长率"])),
                profit_growth=_to_float(_first_existing(row, ["净利润增长率(%)", "归属净利润同比增长率(%)", "净利润同比增长率(%)"])),
                operating_cashflow_per_share=_to_float(_first_existing(row, ["每股经营性现金流(元)", "每股经营现金流量净额(元)"])),
                report_date=str(_first_existing(row, ["日期", "报告期", "公告日期"]) or ""),
            )
            self.cache.set_json(cache_key, metrics.__dict__)
            self.cache.add_fetch_log("财务指标", target=code, source="akshare", status="success", message=f"报告期 {metrics.report_date or '-'}", duration_ms=_elapsed_ms(start))
            return metrics, datetime.now().astimezone()
        except Exception as exc:
            cached = self.cache.get_json(cache_key)
            if cached and datetime.now() - cached.updated_at <= FINANCIAL_FALLBACK_MAX_AGE:
                self.cache.add_fetch_log(
                    "财务指标",
                    target=code,
                    source="cache",
                    status="cache",
                    message=f"主源失败，使用旧缓存：{_short_error(exc)}",
                    duration_ms=_elapsed_ms(start),
                )
                return FinancialMetrics(**cached.payload), cached.updated_at
            if cached:
                self.cache.add_fetch_log(
                    "财务指标",
                    target=code,
                    source="cache",
                    status="failed",
                    message=_stale_cache_message(cached, FINANCIAL_FALLBACK_MAX_AGE),
                    duration_ms=_elapsed_ms(start),
                )
            self.cache.add_fetch_log("财务指标", target=code, source="akshare", status="failed", message=_short_error(exc), duration_ms=_elapsed_ms(start))
            raise _friendly_market_error(exc)

    def history(self, code: str, days: int = 365, force: bool = False) -> tuple[pd.DataFrame, datetime | None]:
        start = time.perf_counter()
        code = code.strip().zfill(6)
        fetch_days = max(int(days or 1), HISTORY_CANONICAL_DAYS)
        cache_key = f"history:{code}"
        if not force:
            cached = self.cache.get_json(cache_key, HISTORY_TTL)
            if cached:
                try:
                    cached_df = _validate_history_frame(pd.DataFrame(cached.payload))
                    self.cache.add_fetch_log("K线历史", target=code, source="cache", status="cache", message="命中12小时K线缓存", duration_ms=_elapsed_ms(start))
                    return cached_df, cached.updated_at
                except Exception as exc:
                    self.cache.add_fetch_log("K线历史", target=code, source="cache", status="failed", message=f"K线缓存校验失败：{_short_error(exc)}", duration_ms=_elapsed_ms(start))

        last_exc: Exception | None = None
        failures: list[str] = []
        sources = [
            ("tencent_http", _history_from_tencent_http),
            ("sina_http", _history_from_sina_http),
            ("eastmoney_http", _history_from_eastmoney_http),
            ("akshare", _history_from_akshare),
        ]
        for source_name, fetch_fn in sources:
            try:
                df = _validate_history_frame(fetch_fn(code, fetch_days))
                self.cache.set_json(cache_key, df.to_dict(orient="records"))
                prefix = "获取" if not failures else f"前序{len(failures)}个源失败后获取"
                self.cache.add_fetch_log(
                    "K线历史",
                    target=code,
                    source=source_name,
                    status="success",
                    message=f"{prefix} {len(df)} 条K线",
                    duration_ms=_elapsed_ms(start),
                )
                return df, datetime.now().astimezone()
            except Exception as exc:
                last_exc = exc
                failures.append(f"{source_name}: {_short_error(exc)}")
                continue

        cached = self.cache.get_json(cache_key)
        if cached and datetime.now() - cached.updated_at <= HISTORY_FALLBACK_MAX_AGE:
            try:
                cached_df = _validate_history_frame(pd.DataFrame(cached.payload))
                self.cache.add_fetch_log(
                    "K线历史",
                    target=code,
                    source="cache",
                    status="cache",
                    message=f"全部数据源失败，使用旧缓存：{' | '.join(failures[-2:])}",
                    duration_ms=_elapsed_ms(start),
                )
                return cached_df, cached.updated_at
            except Exception as exc:
                self.cache.add_fetch_log(
                    "K线历史",
                    target=code,
                    source="cache",
                    status="failed",
                    message=f"旧K线缓存校验失败：{_short_error(exc)}",
                    duration_ms=_elapsed_ms(start),
                )
        if cached and datetime.now() - cached.updated_at > HISTORY_FALLBACK_MAX_AGE:
            self.cache.add_fetch_log(
                "K线历史",
                target=code,
                source="cache",
                status="failed",
                message=_stale_cache_message(cached, HISTORY_FALLBACK_MAX_AGE),
                duration_ms=_elapsed_ms(start),
            )
        if last_exc:
            self.cache.add_fetch_log(
                "K线历史",
                target=code,
                source="tencent/sina/eastmoney/akshare",
                status="failed",
                message=" | ".join(failures[-3:]),
                duration_ms=_elapsed_ms(start),
            )
            raise _friendly_market_error(last_exc)
        raise MarketDataError("K线历史获取失败：未知错误")

    def money_flow(self, code: str, days: int = 20, force: bool = False) -> tuple[MoneyFlowSnapshot, datetime | None]:
        start = time.perf_counter()
        code = code.strip().zfill(6)
        cache_key = f"money_flow:{code}:{days}"
        if not force:
            cached = self.cache.get_json(cache_key, MONEY_FLOW_TTL)
            if cached:
                self.cache.add_fetch_log("主力资金", target=code, source="cache", status="cache", message="命中15分钟资金流缓存", duration_ms=_elapsed_ms(start))
                return MoneyFlowSnapshot(**cached.payload), cached.updated_at

        try:
            try:
                klines = _money_flow_klines_from_eastmoney_http(code, days)
                money_source = "eastmoney_gateway"
            except Exception as primary_exc:
                primary_reason = _classify_market_exception(primary_exc)
                try:
                    klines = _money_flow_fallback_source(code, days, primary_reason=primary_reason)
                    money_source = "optional_fallback"
                except Exception as fallback_exc:
                    raise MarketDataError(
                        f"{_short_error(primary_exc)}；第三方兜底不可用：{fallback_exc}",
                        reason=primary_reason,
                    ) from fallback_exc
            rows = []
            for line in klines:
                parts = str(line).split(",")
                if len(parts) < 6:
                    continue
                rows.append(
                    {
                        "date": parts[0].strip(),
                        "main": _to_float(parts[1]) or 0.0,
                        "small": _to_float(parts[2]) or 0.0,
                        "medium": _to_float(parts[3]) or 0.0,
                        "large": _to_float(parts[4]) or 0.0,
                        "xlarge": _to_float(parts[5]) or 0.0,
                    }
                )
            if not rows:
                raise MarketDataError("主力资金接口返回空数据")

            def _sum(key: str, n: int) -> float:
                return float(sum((row.get(key) or 0.0) for row in rows[-n:]))

            streak = 0
            if rows[-1]["main"] != 0:
                sign = rows[-1]["main"] > 0
                for row in reversed(rows):
                    value = row["main"]
                    if value == 0 or (value > 0) != sign:
                        break
                    streak += 1
                if not sign:
                    streak = -streak

            main_3d = _sum("main", min(3, len(rows)))
            main_5d = _sum("main", min(5, len(rows)))
            main_10d = _sum("main", min(10, len(rows)))
            xlarge_5d = _sum("xlarge", min(5, len(rows)))
            small_5d = _sum("small", min(5, len(rows)))
            summary = (
                f"近3日主力{_fmt_yi(main_3d)}，近5日主力{_fmt_yi(main_5d)}，"
                f"超大单{_fmt_yi(xlarge_5d)}，散户{_fmt_yi(small_5d)}，连续性{streak}日"
            )
            snapshot = MoneyFlowSnapshot(
                code=code,
                rows=rows,
                main_3d=main_3d,
                main_5d=main_5d,
                main_10d=main_10d,
                xlarge_5d=xlarge_5d,
                small_5d=small_5d,
                streak=streak,
                summary=summary,
            )
            self.cache.set_json(cache_key, snapshot.__dict__)
            self.cache.add_fetch_log("主力资金", target=code, source=money_source, status="success", message=summary, duration_ms=_elapsed_ms(start))
            return snapshot, datetime.now()
        except Exception as exc:
            cached = self.cache.get_json(cache_key)
            if cached and datetime.now() - cached.updated_at <= MONEY_FLOW_FALLBACK_MAX_AGE:
                self.cache.add_fetch_log("主力资金", target=code, source="cache", status="cache", message=_cache_fallback_message("资金流", cached.updated_at, exc), duration_ms=_elapsed_ms(start))
                return MoneyFlowSnapshot(**cached.payload), cached.updated_at
            # 无可用历史缓存时，用腾讯当日资金流做最后兜底（独立源、独立 IP）。
            # 只有当日值、无历史窗口：诚实地把窗口字段留空，summary 标注“仅当日”，
            # 不缓存（让后续调用优先重试东财历史源，待解封后自动恢复完整窗口）。
            try:
                today = _money_flow_today_from_tencent(code)
            except Exception:
                today = None
            if today is not None:
                snapshot = MoneyFlowSnapshot(
                    code=code,
                    rows=[{
                        "date": today.get("date", ""),
                        "main": today.get("main") or 0.0,
                        "small": today.get("small") or 0.0,
                        "medium": 0.0,
                        "large": 0.0,
                        "xlarge": 0.0,
                    }],
                    main_3d=None,
                    main_5d=None,
                    main_10d=None,
                    xlarge_5d=None,
                    small_5d=None,
                    streak=0,
                    summary=f"当日主力净流入{_fmt_yi(today.get('main'))}（东财历史源限流，仅当日值，3/5/10日窗口暂不可用）",
                )
                self.cache.add_fetch_log("主力资金", target=code, source="tencent_today_partial", status="success", message=snapshot.summary, duration_ms=_elapsed_ms(start))
                return snapshot, datetime.now()
            if cached:
                self.cache.add_fetch_log(
                    "主力资金",
                    target=code,
                    source="cache",
                    status="failed",
                    message=_stale_cache_message(cached, MONEY_FLOW_FALLBACK_MAX_AGE),
                    duration_ms=_elapsed_ms(start),
                )
            self.cache.add_fetch_log("主力资金", target=code, source="eastmoney", status="failed", message=_short_error(exc), duration_ms=_elapsed_ms(start))
            raise _friendly_market_error(exc)

    def market_sentiment(self, force: bool = False) -> tuple[MarketSentiment, datetime | None]:
        start = time.perf_counter()
        cache_key = "market_sentiment"
        def valid_cached_sentiment(record) -> MarketSentiment | None:
            if not record:
                return None
            item = MarketSentiment(**record.payload)
            if item.up + item.down + item.flat < 1000:
                return None
            return item

        if not force:
            cached = self.cache.get_json(cache_key, SENTIMENT_TTL)
            cached_item = valid_cached_sentiment(cached)
            if cached_item:
                self.cache.add_fetch_log("市场情绪", source="cache", status="cache", message="命中10分钟市场情绪缓存", duration_ms=_elapsed_ms(start))
                return cached_item, cached.updated_at
        spot_cached = self.cache.get_json("spot_quotes", QUOTE_TTL) if not force else None
        if spot_cached:
            try:
                quotes = [StockQuote(**item) for item in spot_cached.payload]
                sentiment = _sentiment_from_quotes(quotes)
                self.cache.add_fetch_log("市场情绪", source="spot_cache", status="success", message=sentiment.summary, duration_ms=_elapsed_ms(start))
                return sentiment, spot_cached.updated_at
            except Exception:
                pass
        try:
            sentiment = _sentiment_from_eastmoney_http()
            self.cache.set_json(cache_key, sentiment.__dict__)
            self.cache.add_fetch_log("市场情绪", source="eastmoney_http", status="success", message=sentiment.summary, duration_ms=_elapsed_ms(start))
            return sentiment, datetime.now()
        except Exception as exc:
            cached = self.cache.get_json(cache_key)
            cached_item = valid_cached_sentiment(cached)
            if cached_item and datetime.now() - cached.updated_at <= SENTIMENT_FALLBACK_MAX_AGE:
                self.cache.add_fetch_log("市场情绪", source="cache", status="cache", message=f"市场情绪源失败，使用旧缓存：{_short_error(exc)}", duration_ms=_elapsed_ms(start))
                return cached_item, cached.updated_at
            if cached_item:
                self.cache.add_fetch_log(
                    "市场情绪",
                    source="cache",
                    status="failed",
                    message=_stale_cache_message(cached, SENTIMENT_FALLBACK_MAX_AGE),
                    duration_ms=_elapsed_ms(start),
                )
            sentiment = MarketSentiment(summary=f"市场情绪暂不可用：{_short_error(exc)}")
            self.cache.add_fetch_log("市场情绪", source="eastmoney_http", status="failed", message=sentiment.summary, duration_ms=_elapsed_ms(start))
            return sentiment, None

    def event_catalysts(self, code: str, limit: int = 6, force: bool = False) -> tuple[list[EventCatalyst], datetime | None]:
        start = time.perf_counter()
        code = code.strip().zfill(6)
        cache_key = f"events:{code}"
        if not force:
            cached = self.cache.get_json(cache_key, EVENT_TTL)
            if cached:
                self.cache.add_fetch_log("事件催化", target=code, source="cache", status="cache", message="命中6小时事件缓存", duration_ms=_elapsed_ms(start))
                return [EventCatalyst(**item) for item in cached.payload], cached.updated_at
        try:
            import akshare as ak

            def fetch_news():
                with _without_proxy_env():
                    return ak.stock_news_em(symbol=code)

            df = _call_with_timeout(fetch_news, timeout=8)
            events: list[EventCatalyst] = []
            for _, row in df.head(limit).iterrows():
                title = str(row.get("新闻标题") or row.get("标题") or row.get("title") or "").strip()
                if not title:
                    continue
                events.append(
                    EventCatalyst(
                        title=title[:120],
                        source=str(row.get("文章来源") or row.get("来源") or "东方财富"),
                        date=str(row.get("发布时间") or row.get("日期") or "")[:19],
                        url=str(row.get("新闻链接") or row.get("链接") or ""),
                    )
                )
            if events:
                self.cache.set_json(cache_key, [event.__dict__ for event in events])
                self.cache.add_fetch_log("事件催化", target=code, source="akshare/eastmoney", status="success", message=f"获取 {len(events)} 条事件", duration_ms=_elapsed_ms(start))
            else:
                self.cache.add_fetch_log("事件催化", target=code, source="akshare/eastmoney", status="info", message="接口返回空事件，未写入6小时缓存", duration_ms=_elapsed_ms(start))
            return events, datetime.now()
        except Exception as exc:
            cached = self.cache.get_json(cache_key)
            if cached and cached.payload and datetime.now() - cached.updated_at <= EVENT_FALLBACK_MAX_AGE:
                self.cache.add_fetch_log("事件催化", target=code, source="cache", status="cache", message=f"事件源失败，使用旧缓存：{_short_error(exc)}", duration_ms=_elapsed_ms(start))
                return [EventCatalyst(**item) for item in cached.payload], cached.updated_at
            if cached:
                self.cache.add_fetch_log(
                    "事件催化",
                    target=code,
                    source="cache",
                    status="failed",
                    message=_stale_cache_message(cached, EVENT_FALLBACK_MAX_AGE) if cached.payload else "旧事件缓存为空，拒绝作为兜底",
                    duration_ms=_elapsed_ms(start),
                )
            self.cache.add_fetch_log("事件催化", target=code, source="akshare/eastmoney", status="failed", message=_short_error(exc), duration_ms=_elapsed_ms(start))
            return [], None
