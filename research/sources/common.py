from __future__ import annotations

import json
import math
import os
import queue
import re
import statistics
import threading
import time
from contextlib import contextmanager
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import pandas as pd


STATUSES = ("GO", "WORKAROUND", "NO-GO", "UNTESTED", "ENV_UNAVAILABLE", "SOURCE_UNREACHABLE")
NON_DATA_FAILURE_STATUSES = {"ENV_UNAVAILABLE", "SOURCE_UNREACHABLE", "UNTESTED"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_yyyymmdd() -> str:
    return date.today().strftime("%Y%m%d")


def today_iso() -> str:
    return date.today().isoformat()


@contextmanager
def no_proxy_env():
    """Bypass system proxies for market-data probes, mirroring production."""
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


def timed_call(label: str, fn: Callable[[], Any], timeout_sec: float = 30.0) -> dict[str, Any]:
    started = time.perf_counter()
    out: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            out.put(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 - probes must record all source failures.
            out.put(("error", exc))

    thread = threading.Thread(target=target, name=f"probe:{label}", daemon=True)
    thread.start()
    thread.join(timeout_sec)
    duration = time.perf_counter() - started
    if thread.is_alive():
        classified = classify_error(f"timeout after {timeout_sec:.1f}s")
        return {
            "ok": False,
            "label": label,
            "duration_sec": round(duration, 3),
            "error": f"timeout after {timeout_sec:.1f}s",
            "error_class": classified["error_class"],
            "status": classified["status"],
        }

    status, payload = out.get()
    if status == "error":
        classified = classify_error(payload)
        return {
            "ok": False,
            "label": label,
            "duration_sec": round(duration, 3),
            "error": short_error(payload),
            "error_class": classified["error_class"],
            "status": classified["status"],
        }
    return {
        "ok": True,
        "label": label,
        "duration_sec": round(duration, 3),
        "value": payload,
    }


def short_error(exc: BaseException | str) -> str:
    text = str(exc)
    text = " ".join(text.split())
    return text[:500]


def classify_error(exc: BaseException | str | None) -> dict[str, str]:
    """Classify probe failures without turning environment/network issues into data NO-GO."""
    text = short_error(exc or "")
    low = text.lower()
    env_needles = [
        "not installed",
        "no module named",
        "缺 tushare_token",
        "缺 ts_token",
        "missing token",
        "token missing",
        "未配置 token",
        "未测：缺",
        "function missing",
    ]
    if any(item in low for item in env_needles):
        return {"status": "ENV_UNAVAILABLE", "error_class": "ENV_UNAVAILABLE", "reason": text}

    source_needles = [
        "timeout",
        "timed out",
        "remote end closed",
        "connection",
        "connectionreset",
        "connection aborted",
        "connection refused",
        "max retries exceeded",
        "proxy",
        "dns",
        "nameresolution",
        "name resolution",
        "temporary failure",
        "network",
        "socket",
        "ssl",
        "eof",
        "receive",
        "接收数据异常",
        "utf-8",
        "login failed",
        "login",
        "断连",
        "超时",
        "连接",
    ]
    if not text or any(item in low for item in source_needles):
        return {"status": "SOURCE_UNREACHABLE", "error_class": "SOURCE_UNREACHABLE", "reason": text}
    return {"status": "SOURCE_UNREACHABLE", "error_class": "SOURCE_UNREACHABLE", "reason": text}


def summarize_failure_status(errors: list[Any]) -> str:
    classes = [classify_error(error)["status"] for error in errors if error]
    if not classes:
        return "SOURCE_UNREACHABLE"
    if all(status == "ENV_UNAVAILABLE" for status in classes):
        return "ENV_UNAVAILABLE"
    if any(status == "SOURCE_UNREACHABLE" for status in classes):
        return "SOURCE_UNREACHABLE"
    return classes[0]


def gate_from_failure(summary: str, evidence: dict[str, Any] | None = None, risks: list[str] | None = None) -> dict[str, Any]:
    evidence = evidence or {}
    errors = []
    for value in evidence.values():
        if isinstance(value, list):
            errors.extend(value)
        elif value:
            errors.append(value)
    status = summarize_failure_status(errors)
    evidence["error_class"] = status
    return gate(status, summary, evidence, risks)


def gate(status: str, summary: str, evidence: dict[str, Any] | None = None, risks: list[str] | None = None) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"bad status: {status}")
    return {
        "status": status,
        "summary": summary,
        "evidence": evidence or {},
        "risks": risks or [],
    }


def unavailable_gates(status: str, reason: str) -> dict[str, Any]:
    return {key: gate(status, reason, {"error_class": status}) for key in ("G1", "G2", "G3", "G4", "G5", "G6")}


def df_columns(df: Any) -> list[str]:
    if isinstance(df, pd.DataFrame):
        return [str(col) for col in df.columns]
    return []


def df_shape(df: Any) -> list[int]:
    if isinstance(df, pd.DataFrame):
        return [int(df.shape[0]), int(df.shape[1])]
    return [0, 0]


def safe_sample_records(df: Any, n: int = 2) -> list[dict[str, Any]]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    records = df.head(n).to_dict(orient="records")
    return json_safe(records)


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [json_safe(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if pd.isna(value) if not isinstance(value, (list, tuple, dict)) else False:
        return None
    return value


def first_existing_column(columns: list[str], candidates: list[str]) -> str | None:
    normalized = {str(col).lower(): str(col) for col in columns}
    for candidate in candidates:
        found = normalized.get(candidate.lower())
        if found:
            return found
    for col in columns:
        low = str(col).lower()
        if any(candidate.lower() in low for candidate in candidates):
            return str(col)
    return None


def close_column(df: pd.DataFrame) -> str | None:
    return first_existing_column(df_columns(df), ["close", "收盘", "收盘价"])


def date_column(df: pd.DataFrame) -> str | None:
    return first_existing_column(df_columns(df), ["date", "日期", "交易日期"])


def ann_date_column(df: pd.DataFrame) -> str | None:
    return first_existing_column(df_columns(df), ["ann_date", "公告日期", "披露日期", "pubDate", "publish_date"])


def pe_column(df: pd.DataFrame) -> str | None:
    return first_existing_column(df_columns(df), ["peTTM", "pe_ttm", "pe", "市盈率", "市盈率ttm", "市盈率-动态"])


def pb_column(df: pd.DataFrame) -> str | None:
    return first_existing_column(df_columns(df), ["pbMRQ", "pb", "市净率", "市净率mrq"])


def list_date_column(df: pd.DataFrame) -> str | None:
    return first_existing_column(df_columns(df), ["list_date", "上市日期", "ipoDate", "上市时间"])


def delist_date_column(df: pd.DataFrame) -> str | None:
    return first_existing_column(df_columns(df), ["delist_date", "退市日期", "outDate", "终止上市日期"])


def numeric_nan_rate(df: pd.DataFrame, columns: list[str] | None = None) -> float | None:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    use_cols = columns or [str(col) for col in df.columns]
    values = []
    for col in use_cols:
        if col in df.columns:
            series = pd.to_numeric(df[col], errors="coerce")
            values.append(float(series.isna().mean()))
    if not values:
        return None
    return round(float(statistics.mean(values)), 4)


def compare_adjusted_vs_raw(raw: pd.DataFrame, adjusted: pd.DataFrame) -> dict[str, Any]:
    raw_date = date_column(raw)
    adj_date = date_column(adjusted)
    raw_close = close_column(raw)
    adj_close = close_column(adjusted)
    evidence: dict[str, Any] = {
        "raw_shape": df_shape(raw),
        "adjusted_shape": df_shape(adjusted),
        "raw_date_col": raw_date,
        "adjusted_date_col": adj_date,
        "raw_close_col": raw_close,
        "adjusted_close_col": adj_close,
    }
    if not all([raw_date, adj_date, raw_close, adj_close]):
        evidence["has_price_diff"] = False
        evidence["reason"] = "missing date/close columns"
        return evidence

    left = raw[[raw_date, raw_close]].rename(columns={raw_date: "date", raw_close: "raw_close"}).copy()
    right = adjusted[[adj_date, adj_close]].rename(columns={adj_date: "date", adj_close: "adj_close"}).copy()
    left["date"] = pd.to_datetime(left["date"], errors="coerce")
    right["date"] = pd.to_datetime(right["date"], errors="coerce")
    left["raw_close"] = pd.to_numeric(left["raw_close"], errors="coerce")
    right["adj_close"] = pd.to_numeric(right["adj_close"], errors="coerce")
    merged = left.merge(right, on="date", how="inner").dropna()
    if merged.empty:
        evidence["has_price_diff"] = False
        evidence["reason"] = "no aligned non-null rows"
        return evidence
    diff = (merged["raw_close"] - merged["adj_close"]).abs()
    evidence.update(
        {
            "aligned_rows": int(len(merged)),
            "first_date": merged["date"].min().date().isoformat(),
            "last_date": merged["date"].max().date().isoformat(),
            "raw_nan_rate": numeric_nan_rate(raw, [raw_close]),
            "adjusted_nan_rate": numeric_nan_rate(adjusted, [adj_close]),
            "max_abs_close_diff": round(float(diff.max()), 6),
            "has_price_diff": bool((diff > 1e-6).any()),
        }
    )
    return evidence


def median_duration_sec(calls: list[dict[str, Any]]) -> float | None:
    durations = [float(item["duration_sec"]) for item in calls if "duration_sec" in item]
    if not durations:
        return None
    return round(float(statistics.median(durations)), 3)


def all_failures_are_timeouts(errors: list[Any]) -> bool:
    clean = [str(error) for error in errors if error]
    return bool(clean) and all("timeout after" in error for error in clean)


def throughput_sla_status(hours: float) -> str:
    if hours <= 8:
        return "GO"
    if hours <= 48:
        return "WORKAROUND"
    return "NO-GO"


def throughput_gate(calls: list[dict[str, Any]], label: str = "接口") -> dict[str, Any]:
    median = median_duration_sec(calls)
    ok_calls = sum(1 for call in calls if call.get("ok"))
    failed_calls = sum(1 for call in calls if not call.get("ok"))
    if median is None:
        return gate("UNTESTED", "无接口延迟样本。", {"ok_calls": ok_calls, "failed_calls": failed_calls})
    if ok_calls == 0:
        errors = [call.get("error") for call in calls if not call.get("ok")]
        status = summarize_failure_status(errors)
        return gate(
            status,
            f"无成功接口调用；失败调用中位耗时 {median}s，不能估算真实吞吐。",
            {"median_failed_call_sec": median, "ok_calls": ok_calls, "failed_calls": failed_calls, "error_class": status},
        )
    per_5000_est = round(median * 5000 / 3600, 2)
    status = throughput_sla_status(per_5000_est)
    summary = f"{label}中位延迟 {median}s；按逐股串行粗估 5000 股约 {per_5000_est} 小时。"
    risks = ["存在失败调用，需断点续跑和失败清单重试。"] if failed_calls else []
    if status == "WORKAROUND":
        risks.append("全量串行估算超过8小时，必须依赖缓存、增量和断点续跑。")
    return gate(
        status,
        summary,
        {"median_call_sec": median, "ok_calls": ok_calls, "failed_calls": failed_calls, "serial_5000_stock_hours": per_5000_est},
        risks,
    )


def normalize_stock_code(value: Any) -> str:
    text = str(value or "").strip()
    if "." in text:
        left, right = text.split(".", 1)
        if left.lower() in {"sh", "sz", "bj"}:
            return f"{left.lower()}.{right[:6]}"
        if right.upper() in {"SH", "SZ", "BJ"}:
            prefix = right.lower()
            return f"{prefix}.{left[-6:]}"
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        code = digits[-6:]
        if code.startswith(("6", "9", "5")):
            return f"sh.{code}"
        if code.startswith(("4", "8")):
            return f"bj.{code}"
        return f"sz.{code}"
    return text


def is_a_share_stock_code(value: Any) -> bool:
    code = normalize_stock_code(value)
    match = re.fullmatch(r"(sh|sz|bj)\.(\d{6})", code)
    if not match:
        return False
    market, digits = match.groups()
    if market == "sh":
        return digits.startswith(("600", "601", "603", "605", "688", "689"))
    if market == "sz":
        return digits.startswith(("000", "001", "002", "003", "300", "301"))
    if market == "bj":
        return digits.startswith(("4", "8"))
    return False


def filter_a_share_stock_rows(df: pd.DataFrame, code_col: str = "code", type_col: str | None = None) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty or code_col not in df.columns:
        return pd.DataFrame(columns=list(df.columns) if isinstance(df, pd.DataFrame) else [])
    clean = df.copy()
    mask = clean[code_col].map(is_a_share_stock_code)
    if type_col and type_col in clean.columns:
        type_text = clean[type_col].astype(str).str.strip()
        mask &= type_text.isin({"", "1", "股票", "stock", "Stock"})
    return clean.loc[mask].copy()


def active_universe_count(universe_df: pd.DataFrame, asof_date: str, code_col: str = "code") -> int:
    if not isinstance(universe_df, pd.DataFrame) or universe_df.empty:
        return 0
    df = filter_a_share_stock_rows(universe_df, code_col=code_col, type_col="type" if "type" in universe_df.columns else None)
    if df.empty:
        return 0
    asof = pd.to_datetime(asof_date, errors="coerce")
    if pd.isna(asof):
        return int(len(df))
    if "ipoDate" in df.columns:
        ipo = pd.to_datetime(df["ipoDate"], errors="coerce")
        df = df.loc[ipo.isna() | (ipo <= asof)]
    if "outDate" in df.columns:
        out = pd.to_datetime(df["outDate"].replace("", pd.NA), errors="coerce")
        df = df.loc[out.isna() | (out > asof)]
    if "status" in df.columns:
        status = df["status"].astype(str).str.strip()
        df = df.loc[status.isin({"", "1", "上市", "L", "normal"})]
    return int(len(df))
