from __future__ import annotations

import importlib.util
import time
from typing import Any

import pandas as pd

from research.sample_universe import SampleStock, active_sample
from research.sources.common import (
    ann_date_column,
    compare_adjusted_vs_raw,
    df_columns,
    df_shape,
    filter_a_share_stock_rows,
    gate,
    numeric_nan_rate,
    pb_column,
    pe_column,
    safe_sample_records,
    short_error,
    today_iso,
    throughput_gate,
    unavailable_gates,
    active_universe_count,
    gate_from_failure,
    classify_error,
)


SOURCE = "baostock"


def run_probe(sample: list[SampleStock] | None = None, timeout_sec: float = 30.0) -> dict[str, Any]:
    if importlib.util.find_spec("baostock") is None:
        return {
            "source": SOURCE,
            "available": False,
            "gates": unavailable_gates("ENV_UNAVAILABLE", "baostock not installed; see research/requirements-research.txt"),
            "calls": [],
            "raw_columns": {},
        }

    import baostock as bs

    calls: list[dict[str, Any]] = []
    raw_columns: dict[str, Any] = {}
    login = _call(calls, "bs.login", lambda: bs.login(), timeout_sec)
    if not login["ok"] or getattr(login.get("value"), "error_code", "1") != "0":
        error_text = login.get("error") or getattr(login.get("value"), "error_msg", "baostock login failed")
        status = classify_error(error_text)["status"]
        return {
            "source": SOURCE,
            "available": True,
            "login": {"ok": False, "error": error_text, "error_class": status},
            "gates": unavailable_gates(status, "baostock login failed"),
            "calls": [{k: v for k, v in call.items() if k != "value"} for call in calls],
            "raw_columns": raw_columns,
        }

    try:
        sample = sample or active_sample(5)
        g1 = _probe_g1_prices(bs, calls, raw_columns, timeout_sec)
        g2 = _probe_g2_financials(bs, calls, raw_columns, timeout_sec)
        g3 = _probe_g3_universe(bs, calls, raw_columns, timeout_sec)
        g4 = _probe_g4_industry(bs, calls, raw_columns, timeout_sec)
        g6 = _probe_g6_valuation(calls, raw_columns, raw_columns.get("G1.query_history_k_data_plus.hfq"))
        g5 = _probe_g5_throughput(calls)
    finally:
        _call(calls, "bs.logout", lambda: bs.logout(), 5.0)

    return {
        "source": SOURCE,
        "available": True,
        "sample_size": len(sample),
        "gates": {"G1": g1, "G2": g2, "G3": g3, "G4": g4, "G5": g5, "G6": g6},
        "calls": [{k: v for k, v in call.items() if k != "value"} for call in calls],
        "raw_columns": raw_columns,
    }


def _call(calls: list[dict[str, Any]], label: str, fn, timeout_sec: float = 0.0) -> dict[str, Any]:
    # baostock keeps one global, non-thread-safe socket; drive it sequentially in
    # the current thread (NOT via timed_call's daemon threads, which corrupt the
    # shared stream -> utf-8 decode / 接收数据异常 on large responses).
    started = time.perf_counter()
    try:
        value = fn()
        result = {"ok": True, "label": label, "duration_sec": round(time.perf_counter() - started, 3), "value": value}
    except BaseException as exc:  # noqa: BLE001 - record all source failures
        classified = classify_error(exc)
        result = {
            "ok": False,
            "label": label,
            "duration_sec": round(time.perf_counter() - started, 3),
            "error": short_error(exc),
            "error_class": classified["error_class"],
            "status": classified["status"],
        }
    calls.append(result)
    return result


# _drain / _query_to_df were extracted verbatim into research.bs_session for
# Phase B ETL reuse; re-exported here so this probe's behavior is unchanged.
from research.bs_session import drain as _drain, query_to_df as _query_to_df  # noqa: E402


def _probe_g1_prices(bs, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    code = "sh.600519"
    fields = "date,code,open,high,low,close,volume,peTTM,pbMRQ"
    raw_call = _call(
        calls,
        "query_history_k_data_plus raw sh.600519",
        lambda: _query_to_df(lambda: bs.query_history_k_data_plus(code, fields, start_date="2019-01-01", end_date=today_iso(), frequency="d", adjustflag="3")),
        timeout_sec,
    )
    hfq_call = _call(
        calls,
        "query_history_k_data_plus hfq sh.600519",
        lambda: _query_to_df(lambda: bs.query_history_k_data_plus(code, fields, start_date="2019-01-01", end_date=today_iso(), frequency="d", adjustflag="1")),
        timeout_sec,
    )
    if raw_call["ok"] and hfq_call["ok"]:
        raw = raw_call["value"]
        hfq = hfq_call["value"]
        raw_columns["G1.query_history_k_data_plus.raw"] = df_columns(raw)
        raw_columns["G1.query_history_k_data_plus.hfq"] = df_columns(hfq)
        evidence = compare_adjusted_vs_raw(raw, hfq)
        if evidence.get("has_price_diff") and int(evidence.get("aligned_rows", 0)) > 500:
            return gate("GO", "Baostock 可取得日线后复权价，且 hfq 与原始价不同。", evidence)
        return gate("WORKAROUND", "Baostock 返回价格，但复权差异或覆盖度证据不足。", evidence)
    return gate_from_failure("Baostock 价格接口未取得可判定证据。", {"raw_error": raw_call.get("error"), "hfq_error": hfq_call.get("error")})


def _probe_g6_valuation(calls: list[dict[str, Any]], raw_columns: dict[str, Any], hfq_columns: list[str] | None) -> dict[str, Any]:
    hfq_call = next((call for call in calls if call["label"] == "query_history_k_data_plus hfq sh.600519"), None)
    df = hfq_call.get("value") if hfq_call and hfq_call.get("ok") else None
    if isinstance(df, pd.DataFrame):
        evidence = {
            "shape": df_shape(df),
            "columns": hfq_columns or df_columns(df),
            "date_col": "date" if "date" in df_columns(df) else None,
            "pe_col": pe_column(df),
            "pb_col": pb_column(df),
            "sample": safe_sample_records(df, 2),
        }
        has_needed = bool(evidence["date_col"] and evidence["pe_col"] and evidence["pb_col"] and df_shape(df)[0] > 100)
        return gate("GO" if has_needed else "WORKAROUND", "Baostock K 线接口含历史 peTTM/pbMRQ。" if has_needed else "Baostock K 线返回，但历史估值字段/覆盖度证据不足。", evidence)
    error = hfq_call.get("error") if hfq_call else "G1 hfq call missing"
    return gate_from_failure("Baostock 历史估值随 G1 hfq 调用未取得可判定证据。", {"error": error})


def _probe_g2_financials(bs, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    call = _call(calls, "query_profit_data sh.600519 2024Q1", lambda: _query_to_df(lambda: bs.query_profit_data(code="sh.600519", year=2024, quarter=1)), timeout_sec)
    if call["ok"] and isinstance(call["value"], pd.DataFrame):
        df = call["value"]
        raw_columns["G2.query_profit_data"] = df_columns(df)
        evidence = {
            "shape": df_shape(df),
            "columns": df_columns(df),
            "ann_date_col": ann_date_column(df),
            "numeric_nan_rate": numeric_nan_rate(df),
            "sample": safe_sample_records(df, 2),
        }
        status = "GO" if evidence["ann_date_col"] and not df.empty else "NO-GO"
        summary = "Baostock 财务数据含公告日字段，可用于点时可见性验证。" if status == "GO" else "Baostock 财务数据未证明公告日可用。"
        return gate(status, summary, evidence)
    return gate_from_failure("Baostock 财务接口未取得可判定证据。", {"error": call.get("error")})


def _probe_g3_universe(bs, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    basic_call = _call(calls, "query_stock_basic", lambda: _query_to_df(lambda: bs.query_stock_basic()), timeout_sec)
    all_call = _call(calls, "query_all_stock 2024-01-02", lambda: _query_to_df(lambda: bs.query_all_stock(day="2024-01-02")), timeout_sec)
    evidence: dict[str, Any] = {}
    if basic_call["ok"] and isinstance(basic_call["value"], pd.DataFrame):
        df = basic_call["value"]
        raw_columns["G3.query_stock_basic"] = df_columns(df)
        filtered = filter_a_share_stock_rows(df, "code", "type" if "type" in df.columns else None)
        evidence["stock_basic"] = {
            "shape": df_shape(df),
            "filtered_a_share_shape": df_shape(filtered),
            "excluded_non_stock_or_index": max(0, df_shape(df)[0] - df_shape(filtered)[0]),
            "columns": df_columns(df),
            "sample": safe_sample_records(filtered if not filtered.empty else df, 2),
        }
    else:
        evidence["stock_basic_error"] = basic_call.get("error")
    if all_call["ok"] and isinstance(all_call["value"], pd.DataFrame):
        df = all_call["value"]
        raw_columns["G3.query_all_stock"] = df_columns(df)
        evidence["all_stock"] = {"shape": df_shape(df), "columns": df_columns(df), "sample": safe_sample_records(df, 2)}
    else:
        evidence["all_stock_error"] = all_call.get("error")

    columns = set(evidence.get("stock_basic", {}).get("columns", []))
    needed = {"ipoDate", "outDate", "status"}
    filtered_count = int(evidence.get("stock_basic", {}).get("filtered_a_share_shape", [0])[0])
    if needed.issubset(columns) and filtered_count > 0:
        return gate("GO", "Baostock stock_basic 过滤指数后仍含 A 股个股，并有 ipoDate/outDate/status，可枚举历史在市集合。", evidence)
    if evidence.get("all_stock", {}).get("shape", [0])[0]:
        return gate("WORKAROUND", "Baostock 可按日期取股票列表，但退市/状态字段证据不足。", evidence)
    if evidence.get("stock_basic_error") and evidence.get("all_stock_error"):
        return gate_from_failure("Baostock 历史成分接口未取得可判定证据。", evidence)
    return gate("NO-GO", "Baostock 接口返回成功，但未证明可重建过滤指数后的历史在市名单。", evidence)


def _probe_g4_industry(bs, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    dates = ["2024-01-02", today_iso()]
    coverages: list[dict[str, Any]] = []
    errors: list[str] = []
    for day in dates:
        industry_call = _call(calls, f"query_stock_industry {day}", lambda day=day: _query_to_df(lambda: bs.query_stock_industry(date=day)), timeout_sec)
        all_call = _call(calls, f"query_all_stock industry coverage {day}", lambda day=day: _query_to_df(lambda: bs.query_all_stock(day=day)), timeout_sec)
        if industry_call["ok"] and isinstance(industry_call["value"], pd.DataFrame):
            industry = industry_call["value"]
            raw_columns[f"G4.query_stock_industry.{day}"] = df_columns(industry)
        else:
            errors.append(str(industry_call.get("error") or "industry failed"))
            continue
        if all_call["ok"] and isinstance(all_call["value"], pd.DataFrame):
            universe = all_call["value"]
            raw_columns[f"G4.query_all_stock.{day}"] = df_columns(universe)
        else:
            errors.append(str(all_call.get("error") or "universe failed"))
            continue

        code_col = "code" if "code" in universe.columns else df_columns(universe)[0] if df_columns(universe) else "code"
        universe_count = active_universe_count(universe, day, code_col=code_col)
        industry_code_col = "code" if "code" in industry.columns else df_columns(industry)[0] if df_columns(industry) else "code"
        industry_col = "industry" if "industry" in industry.columns else "industryClassification" if "industryClassification" in industry.columns else None
        filtered_industry = filter_a_share_stock_rows(industry, industry_code_col)
        non_empty = int(filtered_industry[industry_col].astype(str).str.strip().ne("").sum()) if industry_col and not filtered_industry.empty else 0
        coverage = round(non_empty / universe_count, 4) if universe_count else 0.0
        coverages.append(
            {
                "date": day,
                "universe_count": universe_count,
                "industry_rows": int(len(filtered_industry)),
                "non_empty_industry": non_empty,
                "coverage": coverage,
                "columns": df_columns(industry),
                "sample": safe_sample_records(filtered_industry, 2),
            }
        )

    evidence = {"dates": coverages, "errors": errors}
    if not coverages:
        return gate_from_failure("Baostock 行业接口未取得可判定证据。", evidence)
    min_coverage = min(item["coverage"] for item in coverages)
    if len(coverages) >= 2 and min_coverage >= 0.95:
        return gate("GO", "Baostock 行业覆盖率在至少两个历史日期均达到 95%。", evidence)
    return gate(
        "WORKAROUND",
        "Baostock 行业可取，但覆盖率不足 95% 或历史日期验证不足，不能作为无条件 GO。",
        evidence,
        ["全量建库需输出行业缺失清单并补源或降级为当前行业口径。"],
    )


def _probe_g5_throughput(calls: list[dict[str, Any]]) -> dict[str, Any]:
    return throughput_gate([call for call in calls if call["label"] != "bs.logout"], "Baostock 接口")
