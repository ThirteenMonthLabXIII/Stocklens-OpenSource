from __future__ import annotations

import importlib.util
import os
from typing import Any

import pandas as pd

from research.sample_universe import SampleStock, active_sample
from research.sources.common import (
    ann_date_column,
    compare_adjusted_vs_raw,
    df_columns,
    df_shape,
    gate,
    numeric_nan_rate,
    pb_column,
    pe_column,
    safe_sample_records,
    timed_call,
    today_yyyymmdd,
    throughput_gate,
    unavailable_gates,
    gate_from_failure,
)


SOURCE = "tushare"


def run_probe(sample: list[SampleStock] | None = None, timeout_sec: float = 30.0) -> dict[str, Any]:
    if importlib.util.find_spec("tushare") is None:
        return {
            "source": SOURCE,
            "available": False,
            "gates": unavailable_gates("ENV_UNAVAILABLE", "tushare not installed; see research/requirements-research.txt"),
            "calls": [],
            "raw_columns": {},
        }

    token = os.getenv("TUSHARE_TOKEN") or os.getenv("TS_TOKEN")
    if not token:
        return {
            "source": SOURCE,
            "available": True,
            "gates": unavailable_gates("ENV_UNAVAILABLE", "缺 TUSHARE_TOKEN/TS_TOKEN；Tushare 仅作为增强校验，不阻塞免费路径。"),
            "calls": [],
            "raw_columns": {},
        }

    import tushare as ts

    ts.set_token(token)
    pro = ts.pro_api(token)
    calls: list[dict[str, Any]] = []
    raw_columns: dict[str, Any] = {}
    sample = sample or active_sample(5)

    g1 = _probe_g1_prices(ts, calls, raw_columns, timeout_sec)
    g2 = _probe_g2_financials(pro, calls, raw_columns, timeout_sec)
    g3 = _probe_g3_universe(pro, calls, raw_columns, timeout_sec)
    g4 = _probe_g4_industry(pro, calls, raw_columns, timeout_sec)
    g6 = _probe_g6_valuation(pro, calls, raw_columns, timeout_sec)
    g5 = _probe_g5_throughput(calls)

    return {
        "source": SOURCE,
        "available": True,
        "sample_size": len(sample),
        "gates": {"G1": g1, "G2": g2, "G3": g3, "G4": g4, "G5": g5, "G6": g6},
        "calls": [{k: v for k, v in call.items() if k != "value"} for call in calls],
        "raw_columns": raw_columns,
    }


def _call(calls: list[dict[str, Any]], label: str, fn, timeout_sec: float) -> dict[str, Any]:
    result = timed_call(label, fn, timeout_sec=timeout_sec)
    calls.append(result)
    return result


def _probe_g1_prices(ts, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    raw_call = _call(
        calls,
        "pro_bar raw 600519.SH",
        lambda: ts.pro_bar(ts_code="600519.SH", start_date="20190101", end_date=today_yyyymmdd(), adj=None),
        timeout_sec,
    )
    hfq_call = _call(
        calls,
        "pro_bar hfq 600519.SH",
        lambda: ts.pro_bar(ts_code="600519.SH", start_date="20190101", end_date=today_yyyymmdd(), adj="hfq"),
        timeout_sec,
    )
    if raw_call["ok"] and hfq_call["ok"] and isinstance(raw_call["value"], pd.DataFrame) and isinstance(hfq_call["value"], pd.DataFrame):
        raw = raw_call["value"]
        hfq = hfq_call["value"]
        raw_columns["G1.pro_bar.raw"] = df_columns(raw)
        raw_columns["G1.pro_bar.hfq"] = df_columns(hfq)
        evidence = compare_adjusted_vs_raw(raw, hfq)
        if evidence.get("has_price_diff") and int(evidence.get("aligned_rows", 0)) > 500:
            return gate("GO", "Tushare 可取得日线后复权价，且 hfq 与原始价不同。", evidence)
        return gate("WORKAROUND", "Tushare 返回价格，但复权差异或覆盖度证据不足。", evidence)
    return gate_from_failure("Tushare 价格接口未取得可判定证据。", {"raw_error": raw_call.get("error"), "hfq_error": hfq_call.get("error")})


def _probe_g2_financials(pro, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    call = _call(
        calls,
        "fina_indicator 600519.SH",
        lambda: pro.fina_indicator(ts_code="600519.SH", start_date="20190101", end_date=today_yyyymmdd()),
        timeout_sec,
    )
    if call["ok"] and isinstance(call["value"], pd.DataFrame):
        df = call["value"]
        raw_columns["G2.fina_indicator"] = df_columns(df)
        evidence = {
            "shape": df_shape(df),
            "columns": df_columns(df),
            "ann_date_col": ann_date_column(df),
            "numeric_nan_rate": numeric_nan_rate(df),
            "sample": safe_sample_records(df, 2),
        }
        status = "GO" if evidence["ann_date_col"] and not df.empty else "NO-GO"
        summary = "Tushare fina_indicator 含 ann_date/end_date，可用于点时财务。" if status == "GO" else "Tushare 未证明 fina_indicator 公告日可用。"
        return gate(status, summary, evidence)
    return gate_from_failure("Tushare 财务接口未取得可判定证据。", {"error": call.get("error")})


def _probe_g3_universe(pro, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    call = _call(
        calls,
        "stock_basic all statuses",
        lambda: pro.stock_basic(exchange="", list_status="L,D,P", fields="ts_code,symbol,name,area,industry,list_date,delist_date,list_status"),
        timeout_sec,
    )
    if call["ok"] and isinstance(call["value"], pd.DataFrame):
        df = call["value"]
        raw_columns["G3.stock_basic"] = df_columns(df)
        evidence = {"shape": df_shape(df), "columns": df_columns(df), "sample": safe_sample_records(df, 2)}
        needed = {"list_date", "delist_date", "list_status"}
        status = "GO" if needed.issubset(set(df_columns(df))) and not df.empty else "NO-GO"
        summary = "Tushare stock_basic 含 list_date/delist_date/list_status，可枚举历史在市集合。" if status == "GO" else "Tushare 未证明历史成分字段可用。"
        return gate(status, summary, evidence)
    return gate_from_failure("Tushare 股票基础信息接口未取得可判定证据。", {"error": call.get("error")})


def _probe_g4_industry(pro, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    call = _call(
        calls,
        "stock_basic industry",
        lambda: pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry,list_date"),
        timeout_sec,
    )
    if call["ok"] and isinstance(call["value"], pd.DataFrame):
        df = call["value"]
        raw_columns["G4.stock_basic.industry"] = df_columns(df)
        evidence = {"shape": df_shape(df), "columns": df_columns(df), "sample": safe_sample_records(df, 2)}
        has_industry = "industry" in set(df_columns(df))
        return gate(
            "WORKAROUND" if has_industry and not df.empty else "NO-GO",
            "Tushare stock_basic 可取当前行业字段，但不是点时行业。" if has_industry else "Tushare 未证明行业字段可用。",
            evidence,
            ["stock_basic.industry 通常是当前静态口径；点时行业仍需另证。"] if has_industry else [],
        )
    return gate_from_failure("Tushare 行业字段接口未取得可判定证据。", {"error": call.get("error")})


def _probe_g6_valuation(pro, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    call = _call(
        calls,
        "daily_basic 600519.SH",
        lambda: pro.daily_basic(ts_code="600519.SH", start_date="20190101", end_date=today_yyyymmdd(), fields="ts_code,trade_date,pe,pb,pe_ttm"),
        timeout_sec,
    )
    if call["ok"] and isinstance(call["value"], pd.DataFrame):
        df = call["value"]
        raw_columns["G6.daily_basic"] = df_columns(df)
        evidence = {
            "shape": df_shape(df),
            "columns": df_columns(df),
            "date_col": "trade_date" if "trade_date" in df_columns(df) else None,
            "pe_col": pe_column(df),
            "pb_col": pb_column(df),
            "sample": safe_sample_records(df, 2),
        }
        has_needed = bool(evidence["date_col"] and evidence["pe_col"] and evidence["pb_col"] and df_shape(df)[0] > 100)
        return gate("GO" if has_needed else "WORKAROUND", "Tushare daily_basic 可取历史 PE/PB。" if has_needed else "Tushare daily_basic 返回，但字段/覆盖度证据不足。", evidence)
    error = call.get("error")
    return gate_from_failure("Tushare 历史估值接口未取得可判定证据。", {"error": error})


def _probe_g5_throughput(calls: list[dict[str, Any]]) -> dict[str, Any]:
    return throughput_gate(calls, "Tushare 接口")
