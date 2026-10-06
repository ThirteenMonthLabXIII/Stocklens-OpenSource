from __future__ import annotations

import importlib.metadata
import importlib.util
import multiprocessing as mp
import os
import queue
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from stock_tool.core.pit_database import now_iso
from stock_tool.core.pit_protection import ProtectionEvent, ProtectionEventSink, RequestProtector, SourceCircuitOpen


class PITProvider(Protocol):
    name: str
    source_version: str

    def available_status(self) -> tuple[str, str]:
        ...

    def fetch_universe(self, asof_date: str) -> list[dict[str, Any]]:
        ...

    def fetch_prices(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        ...

    def fetch_financials(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        ...

    def fetch_industry(self, asof_date: str) -> list[dict[str, Any]]:
        ...


class ProviderUnavailable(RuntimeError):
    def __init__(self, message: str, error_class: str = "ENV_UNAVAILABLE") -> None:
        super().__init__(message)
        self.error_class = error_class


def classify_provider_error(exc: BaseException | str) -> str:
    text = str(exc).lower()
    if isinstance(exc, SourceCircuitOpen):
        return "SOURCE_COOLDOWN"
    if isinstance(exc, ProviderUnavailable):
        return exc.error_class
    if "source_cooldown" in text or "cooled down" in text:
        return "SOURCE_COOLDOWN"
    if "not installed" in text or "no module named" in text or "token" in text:
        return "ENV_UNAVAILABLE"
    if any(word in text for word in ("timeout", "timed out", "connection", "remote end closed", "proxy", "dns", "socket", "接收数据异常")):
        return "SOURCE_UNREACHABLE"
    return "SOURCE_UNREACHABLE"


def _is_retryable_provider_error(exc: BaseException) -> bool:
    return classify_provider_error(exc) == "SOURCE_UNREACHABLE"


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def _normalize_bs_code(code: str) -> str:
    text = str(code or "").strip()
    if "." in text:
        left, right = text.split(".", 1)
        if left.lower() in {"sh", "sz", "bj"}:
            return f"{left.lower()}.{right[:6]}"
        if right.upper() in {"SH", "SZ", "BJ"}:
            return f"{right.lower()}.{left[-6:]}"
    digits = "".join(ch for ch in text if ch.isdigit())[-6:]
    if not digits:
        return text
    if digits.startswith(("6", "9", "5")):
        return f"sh.{digits}"
    if digits.startswith(("4", "8")):
        return f"bj.{digits}"
    return f"sz.{digits}"


def _plain_code(code: str) -> str:
    return _normalize_bs_code(code).split(".")[-1]


def _exchange(code: str) -> str:
    norm = _normalize_bs_code(code)
    return norm.split(".")[0] if "." in norm else ""


def _is_a_share_stock(code: str, type_value: Any = "") -> bool:
    norm = _normalize_bs_code(code)
    market, _, digits = norm.partition(".")
    if type_value not in ("", None):
        text = str(type_value).strip()
        if text and text not in {"1", "股票", "stock", "Stock"}:
            return False
    if market == "sh":
        return digits.startswith(("600", "601", "603", "605", "688", "689"))
    if market == "sz":
        return digits.startswith(("000", "001", "002", "003", "300", "301"))
    if market == "bj":
        return digits.startswith(("4", "8"))
    return False


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).replace(",", "").replace("%", "").strip()
        if text in {"", "-", "--", "nan", "None"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _decimal_ratio_to_percent(value: Any) -> float | None:
    number = _to_float(value)
    return number * 100.0 if number is not None else None


def _baostock_error_text(value: Any) -> str:
    return str(value or "").strip()


def _is_baostock_not_logged_in(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return "10001001" in text or "未登录" in text or "not login" in text or "not logged" in text


def _baostock_login(bs: Any) -> None:
    login = bs.login()
    if getattr(login, "error_code", "1") != "0":
        raise RuntimeError(_baostock_error_text(getattr(login, "error_msg", "")) or "baostock login failed")


def _drain_baostock_result(rs: Any) -> list[dict[str, Any]]:
    if getattr(rs, "error_code", "1") != "0":
        raise RuntimeError(f"baostock error {getattr(rs, 'error_code', '?')}: {getattr(rs, 'error_msg', '')}")
    rows = []
    fields = [str(field) for field in getattr(rs, "fields", [])]
    while getattr(rs, "error_code", "0") == "0" and rs.next():
        rows.append(dict(zip(fields, rs.get_row_data())))
    return rows


def _run_baostock_command(bs: Any, command: str, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    if command == "login":
        return [{"status": "ok"}]
    if command == "trade_dates":
        rs = bs.query_trade_dates(start_date=kwargs["start_date"], end_date=kwargs["end_date"])
    elif command == "stock_basic":
        code = kwargs.get("code")
        rs = bs.query_stock_basic(code=code) if code else bs.query_stock_basic()
    elif command == "all_stock":
        rs = bs.query_all_stock(day=kwargs["day"])
    elif command == "prices":
        fields = "date,code,open,high,low,close,volume,amount,turn,pctChg,peTTM,pbMRQ"
        rs = bs.query_history_k_data_plus(
            kwargs["code"],
            fields,
            start_date=kwargs["start_date"],
            end_date=kwargs["end_date"],
            frequency="d",
            adjustflag=str(kwargs.get("adjustflag") or "1"),
        )
    elif command == "profit":
        rs = bs.query_profit_data(code=kwargs["code"], year=int(kwargs["year"]), quarter=int(kwargs["quarter"]))
    elif command == "growth":
        rs = bs.query_growth_data(code=kwargs["code"], year=int(kwargs["year"]), quarter=int(kwargs["quarter"]))
    elif command == "balance":
        rs = bs.query_balance_data(code=kwargs["code"], year=int(kwargs["year"]), quarter=int(kwargs["quarter"]))
    elif command == "industry":
        rs = bs.query_stock_industry(date=kwargs["date"])
    else:
        raise ValueError(f"unknown baostock command: {command}")
    return _drain_baostock_result(rs)


def _baostock_worker_loop(requests: mp.Queue, responses: mp.Queue) -> None:
    bs = None
    logged_in = False

    def ensure_login() -> None:
        nonlocal bs, logged_in
        if bs is None:
            import baostock as imported_bs

            bs = imported_bs
        if not logged_in:
            _baostock_login(bs)
            logged_in = True

    def relogin() -> None:
        nonlocal logged_in
        if bs is not None:
            try:
                bs.logout()
            except BaseException:
                pass
        logged_in = False
        ensure_login()

    try:
        while True:
            item = requests.get()
            if item is None:
                break
            request_id, command, kwargs = item
            try:
                ensure_login()
                try:
                    rows = _run_baostock_command(bs, command, kwargs)
                except Exception as exc:
                    if _is_baostock_not_logged_in(exc):
                        relogin()
                        rows = _run_baostock_command(bs, command, kwargs)
                    else:
                        raise
                responses.put((request_id, "ok", rows))
            except BaseException as exc:  # noqa: BLE001 - child must return all failures
                responses.put((request_id, "error", str(exc)))
    finally:
        if bs is not None:
            try:
                bs.logout()
            except BaseException:
                pass


class _BaostockWorkerClient:
    def __init__(self, timeout_sec: float, event_sink: ProtectionEventSink | None = None) -> None:
        self.timeout_sec = float(timeout_sec)
        self._event_sink = event_sink
        self._ctx = mp.get_context("spawn")
        self._requests: mp.Queue | None = None
        self._responses: mp.Queue | None = None
        self._proc: mp.Process | None = None
        self._next_request_id = 0
        self.restart_count = 0

    def set_event_sink(self, sink: ProtectionEventSink | None) -> None:
        self._event_sink = sink

    def request(self, command: str, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        last_error: ProviderUnavailable | None = None
        for worker_attempt in range(2):
            self._ensure_worker()
            request_id = self._next_request_id
            self._next_request_id += 1
            assert self._requests is not None
            assert self._responses is not None
            try:
                self._requests.put((request_id, command, kwargs))
                response_id, kind, payload = self._responses.get(timeout=self.timeout_sec)
                if response_id != request_id:
                    raise ProviderUnavailable(f"baostock {command} returned mismatched worker response", "SOURCE_UNREACHABLE")
                if kind == "ok":
                    return list(payload)
                raise ProviderUnavailable(str(payload), classify_provider_error(str(payload)))
            except queue.Empty:
                message = f"baostock {command} timeout after {self.timeout_sec:.0f}s"
                last_error = ProviderUnavailable(message, "SOURCE_UNREACHABLE")
                self._restart_worker(command, message)
                if worker_attempt == 0:
                    continue
                raise last_error
            except (EOFError, OSError, BrokenPipeError) as exc:
                message = f"baostock {command} worker failed: {exc}"
                last_error = ProviderUnavailable(message, "SOURCE_UNREACHABLE")
                self._restart_worker(command, message)
                if worker_attempt == 0:
                    continue
                raise last_error
        raise last_error or ProviderUnavailable(f"baostock {command} worker failed", "SOURCE_UNREACHABLE")

    def close(self) -> None:
        if self._proc is not None and self._proc.is_alive() and self._requests is not None:
            try:
                self._requests.put(None)
                self._proc.join(3)
            except Exception:
                pass
        self._terminate_worker()

    def _ensure_worker(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            return
        self._requests = self._ctx.Queue()
        self._responses = self._ctx.Queue()
        self._proc = self._ctx.Process(target=_baostock_worker_loop, args=(self._requests, self._responses))
        self._proc.start()

    def _restart_worker(self, command: str, message: str) -> None:
        self.restart_count += 1
        self._emit_worker_restart(command, message)
        self._terminate_worker()

    def _terminate_worker(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(3)
        self._proc = None
        self._requests = None
        self._responses = None

    def _emit_worker_restart(self, command: str, message: str) -> None:
        if self._event_sink is None:
            return
        self._event_sink(
            ProtectionEvent(
                kind="worker_restart",
                source="baostock",
                target=f"baostock.{command}",
                error_class="SOURCE_UNREACHABLE",
                message=message,
            )
        )


class BaostockPITProvider:
    name = "baostock"

    def __init__(self, timeout_sec: float = 60.0, protection_profile: str = "balanced", worker_client: Any | None = None) -> None:
        self.timeout_sec = max(5.0, float(timeout_sec))
        self.source_version = _package_version("baostock")
        self.protector = RequestProtector(self.name, protection_profile)
        self._worker_client = worker_client
        self.latest_trade_day_value = ""
        self.universe_method = ""
        self.universe_rows = 0

    @property
    def protection_profile_name(self) -> str:
        return self.protector.profile_name

    def set_protection_event_sink(self, sink: ProtectionEventSink | None) -> None:
        self.protector.set_event_sink(sink)
        if self._worker_client is not None and hasattr(self._worker_client, "set_event_sink"):
            self._worker_client.set_event_sink(sink)

    @property
    def worker_restarts(self) -> int:
        return int(getattr(self._worker_client, "restart_count", 0) or 0)

    def close(self) -> None:
        if self._worker_client is not None and hasattr(self._worker_client, "close"):
            self._worker_client.close()

    def available_status(self) -> tuple[str, str]:
        if importlib.util.find_spec("baostock") is None:
            return "ENV_UNAVAILABLE", "baostock not installed"
        return "GO", f"baostock {self.source_version}"

    def _query(self, command: str, **kwargs: Any) -> list[dict[str, Any]]:
        def request() -> list[dict[str, Any]]:
            status, message = self.available_status()
            if status != "GO":
                raise ProviderUnavailable(message, status)
            if self._worker_client is None:
                self._worker_client = _BaostockWorkerClient(self.timeout_sec)
            return list(self._worker_client.request(command, kwargs))

        target = f"baostock.{command}"
        return list(
            self.protector.call(
                target,
                request,
                error_classifier=classify_provider_error,
                retryable=_is_retryable_provider_error,
            )
        )

    def fetch_universe(self, asof_date: str) -> list[dict[str, Any]]:
        latest_trade_day, rows = self._query_all_stock_with_trade_day_backoff(asof_date)
        self.universe_method = "baostock.query_all_stock"
        self.universe_rows = len(rows)
        fetched_at = now_iso()
        out = []
        for row in rows:
            code = row.get("code", "")
            if not _is_a_share_stock(code):
                continue
            out.append(
                {
                    "code": _plain_code(code),
                    "name": row.get("code_name", "") or row.get("name", ""),
                    "exchange": _exchange(code),
                    "ipo_date": "",
                    "out_date": "",
                    "status": row.get("tradeStatus", "") or row.get("status", ""),
                    "is_index": 0,
                    "asof_date": latest_trade_day,
                    "source": self.name,
                    "source_version": self.source_version,
                    "fetched_at": fetched_at,
                    "raw": row,
                }
            )
        return out

    def latest_trade_day(self, end_date: str) -> str:
        days = self.recent_trade_days(end_date)
        self.latest_trade_day_value = days[-1]
        return self.latest_trade_day_value

    def recent_trade_days(self, end_date: str) -> list[str]:
        end = _parse_iso_date(end_date)
        start = end - timedelta(days=14)
        rows = self._query("trade_dates", start_date=start.isoformat(), end_date=end.isoformat())
        trade_days = sorted(
            str(row.get("calendar_date", "")).strip()
            for row in rows
            if str(row.get("is_trading_day", "")).strip() == "1" and str(row.get("calendar_date", "")).strip()
        )
        if not trade_days:
            raise ProviderUnavailable(f"baostock query_trade_dates returned no trading day for {start.isoformat()}..{end.isoformat()}", "SOURCE_UNREACHABLE")
        return trade_days

    def _query_all_stock_with_trade_day_backoff(self, end_date: str) -> tuple[str, list[dict[str, Any]]]:
        trade_days = self.recent_trade_days(end_date)
        empty_days: list[str] = []
        for trade_day in reversed(trade_days):
            rows = self._query("all_stock", day=trade_day)
            if rows:
                self.latest_trade_day_value = trade_day
                return trade_day, rows
            empty_days.append(trade_day)
        raise ProviderUnavailable(
            "baostock query_all_stock returned 0 rows for recent trading days: " + ",".join(empty_days),
            "SOURCE_UNREACHABLE",
        )

    def healthcheck(self, start_date: str, end_date: str) -> dict[str, Any]:
        status, message = self.available_status()
        payload: dict[str, Any] = {
            "available_status": status,
            "available_message": message,
            "healthcheck_status": "failed",
            "latest_trade_day": "",
            "universe_method": "",
            "universe_rows": 0,
            "worker_restarts": 0,
        }
        if status != "GO":
            raise ProviderUnavailable(message, status)
        self._query("login")
        latest_trade_day, all_stock = self._query_all_stock_with_trade_day_backoff(end_date)
        self._query("stock_basic", code=_normalize_bs_code("600519"))
        profit_year = max(2019, int(_parse_iso_date(end_date).year) - 1)
        self._query("profit", code=_normalize_bs_code("600519"), year=profit_year, quarter=4)
        self.latest_trade_day_value = latest_trade_day
        self.universe_method = "baostock.query_all_stock"
        self.universe_rows = len(all_stock)
        payload.update(
            {
                "healthcheck_status": "GO",
                "latest_trade_day": latest_trade_day,
                "universe_method": self.universe_method,
                "universe_rows": len(all_stock),
                "worker_restarts": self.worker_restarts,
            }
        )
        return payload

    def fetch_prices(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        norm_code = _normalize_bs_code(code)
        rows = self._query("prices", code=norm_code, start_date=start_date, end_date=end_date, adjustflag="1")
        raw_rows = self._query("prices", code=norm_code, start_date=start_date, end_date=end_date, adjustflag="3")
        raw_by_date = {str(row.get("date", "")): row for row in raw_rows if str(row.get("date", "")).strip()}
        fetched_at = now_iso()
        out = []
        for row in rows:
            trade_date = row.get("date", "")
            if not trade_date:
                continue
            raw_row = raw_by_date.get(str(trade_date))
            out.append(
                {
                    "code": _plain_code(row.get("code", code)),
                    "trade_date": trade_date,
                    "adj": "hfq",
                    "open": _to_float(row.get("open")),
                    "high": _to_float(row.get("high")),
                    "low": _to_float(row.get("low")),
                    "close": _to_float(row.get("close")),
                    "close_raw": _to_float(raw_row.get("close")) if raw_row else None,
                    "volume": _to_float(row.get("volume")),
                    "amount": _to_float(row.get("amount")),
                    "turnover": _to_float(row.get("turn")),
                    "pe_ttm": _to_float(row.get("peTTM")),
                    "pb_mrq": _to_float(row.get("pbMRQ")),
                    "asof_date": trade_date,
                    "source": self.name,
                    "source_version": self.source_version,
                    "fetched_at": fetched_at,
                    "raw": {"hfq": row, "raw": raw_row},
                }
            )
        return out

    def fetch_financials(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        start_year = int(start_date[:4])
        end_year = int(end_date[:4])
        fetched_at = now_iso()
        out = []
        norm_code = _normalize_bs_code(code)
        profit_rows: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for year in range(start_year - 1, end_year + 1):
            for quarter in range(1, 5):
                profit_rows[(year, quarter)] = self._query("profit", code=norm_code, year=year, quarter=quarter)

        def append_metric(metric: str, value: float | None, row: dict[str, Any], report_date: str, pub_date: str, raw: dict[str, Any] | None = None) -> None:
            if value is None:
                return
            out.append(
                {
                    "code": _plain_code(row.get("code", code)),
                    "report_date": report_date,
                    "pub_date": pub_date,
                    "metric": metric,
                    "value": value,
                    "source": self.name,
                    "source_version": self.source_version,
                    "fetched_at": fetched_at,
                    "raw": raw if raw is not None else row,
                }
            )

        for year in range(start_year, end_year + 1):
            for quarter in range(1, 5):
                for row in profit_rows.get((year, quarter), []):
                    pub_date = row.get("pubDate", "") or row.get("statDate", "")
                    report_date = row.get("statDate", "") or f"{year}Q{quarter}"
                    for metric, field in {
                        "roe": "roeAvg",
                        "net_margin": "npMargin",
                        "gross_margin": "gpMargin",
                    }.items():
                        append_metric(metric, _decimal_ratio_to_percent(row.get(field)), row, report_date, pub_date)
                    for metric, field in {
                        "net_profit": "netProfit",
                        "eps_ttm": "epsTTM",
                        "revenue": "MBRevenue",
                    }.items():
                        append_metric(metric, _to_float(row.get(field)), row, report_date, pub_date)
                    current_revenue = _to_float(row.get("MBRevenue"))
                    prior_rows = profit_rows.get((year - 1, quarter), [])
                    prior_revenue = _to_float(prior_rows[0].get("MBRevenue")) if prior_rows else None
                    if current_revenue is not None and prior_revenue is not None and prior_revenue > 0:
                        append_metric(
                            "revenue_growth",
                            (current_revenue / prior_revenue - 1.0) * 100.0,
                            row,
                            report_date,
                            pub_date,
                            raw={"current_profit": row, "prior_profit": prior_rows[0], "calculation": "MBRevenue YoY"},
                        )
                for row in self._query("growth", code=norm_code, year=year, quarter=quarter):
                    pub_date = row.get("pubDate", "") or row.get("statDate", "")
                    report_date = row.get("statDate", "") or f"{year}Q{quarter}"
                    profit_growth = _decimal_ratio_to_percent(row.get("YOYNI"))
                    if profit_growth is None:
                        profit_growth = _decimal_ratio_to_percent(row.get("YOYPNI"))
                    append_metric("profit_growth", profit_growth, row, report_date, pub_date)
                for row in self._query("balance", code=norm_code, year=year, quarter=quarter):
                    pub_date = row.get("pubDate", "") or row.get("statDate", "")
                    report_date = row.get("statDate", "") or f"{year}Q{quarter}"
                    append_metric("debt_ratio", _decimal_ratio_to_percent(row.get("liabilityToAsset")), row, report_date, pub_date)
        return out

    def fetch_industry(self, asof_date: str) -> list[dict[str, Any]]:
        rows = self._query("industry", date=asof_date)
        fetched_at = now_iso()
        out = []
        for row in rows:
            code = row.get("code", "")
            if not _is_a_share_stock(code):
                continue
            out.append(
                {
                    "code": _plain_code(code),
                    # industryClassification 是分类体系名（level），不是行业值；旧代码的 or
                    # 兜底会把 level 标签灌进空行业字段（已退市码大量出现），故移除该兜底。
                    "industry": row.get("industry", ""),
                    "industry_level": row.get("industryClassification", "") or "baostock",
                    "asof_date": asof_date,
                    "source": self.name,
                    "source_version": self.source_version,
                    "fetched_at": fetched_at,
                    "raw": row,
                }
            )
        return out


class TusharePITProvider:
    name = "tushare"

    def __init__(self, token: str | None = None, timeout_sec: float = 60.0, protection_profile: str = "balanced") -> None:
        self.token = (token or os.getenv("TUSHARE_TOKEN") or os.getenv("TS_TOKEN") or "").strip()
        self.timeout_sec = timeout_sec
        self.source_version = _package_version("tushare")
        self.protector = RequestProtector(self.name, protection_profile)
        self.latest_trade_day_value = ""
        self.universe_method = ""
        self.universe_rows = 0

    @property
    def protection_profile_name(self) -> str:
        return self.protector.profile_name

    def set_protection_event_sink(self, sink: ProtectionEventSink | None) -> None:
        self.protector.set_event_sink(sink)

    def available_status(self) -> tuple[str, str]:
        if importlib.util.find_spec("tushare") is None:
            return "ENV_UNAVAILABLE", "tushare not installed"
        if not self.token:
            return "ENV_UNAVAILABLE", "Tushare token not configured; enhanced validation disabled"
        return "GO", f"tushare {self.source_version}"

    def _pro(self):
        status, message = self.available_status()
        if status != "GO":
            raise ProviderUnavailable(message, status)
        import tushare as ts

        ts.set_token(self.token)
        return ts.pro_api(self.token), ts

    def _protected_call(self, target: str, fn):
        return self.protector.call(
            f"tushare.{target}",
            fn,
            error_classifier=classify_provider_error,
            retryable=_is_retryable_provider_error,
        )

    def fetch_universe(self, asof_date: str) -> list[dict[str, Any]]:
        pro, _ts = self._pro()
        df = self._protected_call(
            "stock_basic",
            lambda: pro.stock_basic(exchange="", list_status="L,D,P", fields="ts_code,symbol,name,area,industry,list_date,delist_date,list_status"),
        )
        fetched_at = now_iso()
        rows = []
        for item in df.to_dict(orient="records"):
            ts_code = str(item.get("ts_code", ""))
            rows.append(
                {
                    "code": _plain_code(ts_code),
                    "name": item.get("name", ""),
                    "exchange": _exchange(ts_code),
                    "ipo_date": item.get("list_date", ""),
                    "out_date": item.get("delist_date", "") or "",
                    "status": item.get("list_status", ""),
                    "is_index": 0,
                    "asof_date": asof_date,
                    "source": self.name,
                    "source_version": self.source_version,
                    "fetched_at": fetched_at,
                    "raw": item,
                }
            )
        self.latest_trade_day_value = asof_date
        self.universe_method = "tushare.stock_basic"
        self.universe_rows = len(rows)
        return rows

    def fetch_prices(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        _pro, ts = self._pro()
        ts_code = _to_ts_code(code)
        df = self._protected_call(
            "pro_bar.hfq",
            lambda: ts.pro_bar(ts_code=ts_code, start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adj="hfq"),
        )
        if df is None:
            return []
        raw_df = self._protected_call(
            "pro_bar.raw",
            lambda: ts.pro_bar(ts_code=ts_code, start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adj=None),
        )
        raw_by_date = {}
        if raw_df is not None:
            raw_by_date = {
                _date8(item.get("trade_date", "")): item
                for item in raw_df.to_dict(orient="records")
                if _date8(item.get("trade_date", ""))
            }
        fetched_at = now_iso()
        rows = []
        for item in df.to_dict(orient="records"):
            trade_date = _date8(item.get("trade_date", ""))
            raw_item = raw_by_date.get(trade_date)
            rows.append(
                {
                    "code": _plain_code(ts_code),
                    "trade_date": trade_date,
                    "adj": "hfq",
                    "open": _to_float(item.get("open")),
                    "high": _to_float(item.get("high")),
                    "low": _to_float(item.get("low")),
                    "close": _to_float(item.get("close")),
                    "close_raw": _to_float(raw_item.get("close")) if raw_item else None,
                    "volume": _to_float(item.get("vol")),
                    "amount": _to_float(item.get("amount")),
                    "asof_date": trade_date,
                    "source": self.name,
                    "source_version": self.source_version,
                    "fetched_at": fetched_at,
                    "raw": {"hfq": item, "raw": raw_item},
                }
            )
        return rows

    def fetch_financials(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        pro, _ts = self._pro()
        ts_code = _to_ts_code(code)
        df = self._protected_call(
            "fina_indicator",
            lambda: pro.fina_indicator(ts_code=ts_code, start_date=start_date.replace("-", ""), end_date=end_date.replace("-", "")),
        )
        fetched_at = now_iso()
        rows = []
        metric_fields = {
            "roe": "roe",
            "gross_margin": "grossprofit_margin",
            "net_margin": "netprofit_margin",
            "debt_ratio": "debt_to_assets",
            "revenue_growth": "or_yoy",
            "profit_growth": "netprofit_yoy",
        }
        for item in df.to_dict(orient="records"):
            pub_date = _date8(item.get("ann_date", ""))
            report_date = _date8(item.get("end_date", ""))
            for metric, field in metric_fields.items():
                value = _to_float(item.get(field))
                if value is None:
                    continue
                rows.append(
                    {
                        "code": _plain_code(ts_code),
                        "report_date": report_date,
                        "pub_date": pub_date,
                        "metric": metric,
                        "value": value,
                        "source": self.name,
                        "source_version": self.source_version,
                        "fetched_at": fetched_at,
                        "raw": item,
                    }
                )
        return rows

    def fetch_industry(self, asof_date: str) -> list[dict[str, Any]]:
        pro, _ts = self._pro()
        df = self._protected_call(
            "stock_basic.industry",
            lambda: pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry,list_date"),
        )
        fetched_at = now_iso()
        rows = []
        for item in df.to_dict(orient="records"):
            rows.append(
                {
                    "code": _plain_code(str(item.get("ts_code", ""))),
                    "industry": item.get("industry", "") or "",
                    "industry_level": "tushare_stock_basic_current",
                    "asof_date": asof_date,
                    "source": self.name,
                    "source_version": self.source_version,
                    "fetched_at": fetched_at,
                    "raw": item,
                }
            )
        return rows


def _date8(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return text


def _parse_iso_date(value: str) -> date:
    text = str(value or "").strip()
    if not text or text == "today":
        return date.today()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return date.fromisoformat(text[:10])


def _to_ts_code(code: str) -> str:
    norm = _normalize_bs_code(code)
    market, _, digits = norm.partition(".")
    suffix = "SH" if market == "sh" else "BJ" if market == "bj" else "SZ"
    return f"{digits}.{suffix}"


def fetch_akshare_universe(asof_date: str) -> list[dict[str, Any]]:
    if importlib.util.find_spec("akshare") is None:
        raise ProviderUnavailable("akshare not installed; universe fallback unavailable", "ENV_UNAVAILABLE")
    import akshare as ak

    df = ak.stock_info_a_code_name()
    fetched_at = now_iso()
    rows = []
    for item in df.to_dict(orient="records"):
        code = str(item.get("code", "") or item.get("证券代码", "")).strip()
        if not _is_a_share_stock(code):
            continue
        rows.append(
            {
                "code": _plain_code(code),
                "name": item.get("name", "") or item.get("证券简称", ""),
                "exchange": _exchange(code),
                "ipo_date": "",
                "out_date": "",
                "status": "",
                "is_index": 0,
                "asof_date": asof_date,
                "source": "akshare",
                "source_version": _package_version("akshare"),
                "fetched_at": fetched_at,
                "raw": item,
            }
        )
    if not rows:
        raise ProviderUnavailable("akshare universe fallback returned 0 A-share rows", "SOURCE_UNREACHABLE")
    return rows
