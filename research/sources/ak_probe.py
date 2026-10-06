from __future__ import annotations

import importlib.util
from typing import Any

import pandas as pd

from research.sample_universe import SampleStock, active_sample, delisted_sample
from research.sources.common import (
    ann_date_column,
    compare_adjusted_vs_raw,
    date_column,
    df_columns,
    df_shape,
    gate,
    list_date_column,
    no_proxy_env,
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


SOURCE = "akshare"


def run_probe(sample: list[SampleStock] | None = None, timeout_sec: float = 30.0) -> dict[str, Any]:
    if importlib.util.find_spec("akshare") is None:
        return {
            "source": SOURCE,
            "available": False,
            "gates": unavailable_gates("ENV_UNAVAILABLE", "akshare not installed"),
            "calls": [],
            "raw_columns": {},
        }

    import akshare as ak

    calls: list[dict[str, Any]] = []
    raw_columns: dict[str, Any] = {}
    sample = sample or active_sample(6) + delisted_sample(3)

    g1 = _probe_g1_prices(ak, calls, raw_columns, timeout_sec)
    g2 = _probe_g2_financials(ak, calls, raw_columns, timeout_sec)
    g3 = _probe_g3_universe(ak, calls, raw_columns, timeout_sec)
    g4 = _probe_g4_industry(ak, calls, raw_columns, timeout_sec)
    g6 = _probe_g6_valuation(ak, calls, raw_columns, timeout_sec)
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


def _ak(fn):
    def wrapped():
        with no_proxy_env():
            return fn()

    return wrapped


def _probe_g1_prices(ak, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    code = "600519"
    short_raw = _call(
        calls,
        f"stock_zh_a_hist short raw {code}",
        _ak(lambda: ak.stock_zh_a_hist(symbol=code, period="daily", start_date="20240101", end_date="20240131", adjust="")),
        timeout_sec,
    )
    short_hfq = _call(
        calls,
        f"stock_zh_a_hist short hfq {code}",
        _ak(lambda: ak.stock_zh_a_hist(symbol=code, period="daily", start_date="20240101", end_date="20240131", adjust="hfq")),
        timeout_sec,
    )
    if not (short_raw["ok"] and short_hfq["ok"]):
        errors = [call.get("error") for call in calls if call["label"].startswith("stock_zh_a_hist") and not call["ok"]]
        return gate_from_failure("AkShare 短窗价格连通预检未取得可判定证据。", {"errors": errors[:6]})

    long_raw = _call(
        calls,
        f"stock_zh_a_hist long raw {code}",
        _ak(lambda: ak.stock_zh_a_hist(symbol=code, period="daily", start_date="20190101", end_date=today_yyyymmdd(), adjust="")),
        timeout_sec,
    )
    long_hfq = _call(
        calls,
        f"stock_zh_a_hist long hfq {code}",
        _ak(lambda: ak.stock_zh_a_hist(symbol=code, period="daily", start_date="20190101", end_date=today_yyyymmdd(), adjust="hfq")),
        timeout_sec,
    )
    if not (long_raw["ok"] and long_hfq["ok"]):
        return gate(
            "WORKAROUND",
            "AkShare 短窗价格可连通，但 2019 至今长窗未完成；Phase B 需分年/分段拉取。",
            {"long_raw_error": long_raw.get("error"), "long_hfq_error": long_hfq.get("error")},
        )

    raw = long_raw["value"]
    hfq = long_hfq["value"]
    raw_columns[f"G1.raw.{code}"] = df_columns(raw)
    raw_columns[f"G1.hfq.{code}"] = df_columns(hfq)
    if not (isinstance(raw, pd.DataFrame) and isinstance(hfq, pd.DataFrame) and not raw.empty and not hfq.empty):
        return gate("NO-GO", "AkShare 长窗价格返回空或非 DataFrame。", {"raw_shape": df_shape(raw), "hfq_shape": df_shape(hfq)})

    evidence = compare_adjusted_vs_raw(raw, hfq)
    evidence["code"] = code

    has_diff = bool(evidence.get("has_price_diff"))
    min_rows = int(evidence.get("aligned_rows", 0))
    high_nan = (evidence.get("adjusted_nan_rate") or 0) > 0.02
    status = "GO" if has_diff and min_rows > 500 and not high_nan else "WORKAROUND"
    summary = "可取得 2019 至今日线后复权价，且至少一个分红样本 hfq 与原始价不同。" if status == "GO" else "可取得价格，但复权差异/覆盖度/缺失率证据不足。"
    return gate(status, summary, evidence)


def _probe_g2_financials(ak, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    yjbb_results = []
    for period in ("20240331", "20231231"):
        call = _call(calls, f"stock_yjbb_em {period}", _ak(lambda period=period: ak.stock_yjbb_em(date=period)), timeout_sec)
        if call["ok"] and isinstance(call["value"], pd.DataFrame):
            df = call["value"]
            raw_columns[f"G2.stock_yjbb_em.{period}"] = df_columns(df)
            ann_col = ann_date_column(df)
            yjbb_results.append(
                {
                    "period": period,
                    "shape": df_shape(df),
                    "ann_date_col": ann_col,
                    "sample": safe_sample_records(df, 1),
                }
            )

    indicator_call = _call(
        calls,
        "stock_financial_analysis_indicator 600519",
        _ak(lambda: ak.stock_financial_analysis_indicator(symbol="600519", start_year="2019")),
        timeout_sec,
    )
    indicator_evidence: dict[str, Any] = {}
    if indicator_call["ok"] and isinstance(indicator_call["value"], pd.DataFrame):
        df = indicator_call["value"]
        raw_columns["G2.stock_financial_analysis_indicator.600519"] = df_columns(df)
        indicator_evidence = {
            "shape": df_shape(df),
            "ann_date_col": ann_date_column(df),
            "numeric_nan_rate": numeric_nan_rate(df),
            "sample": safe_sample_records(df, 1),
        }

    has_ann = any(item.get("ann_date_col") for item in yjbb_results) or bool(indicator_evidence.get("ann_date_col"))
    has_indicator_rows = bool(indicator_evidence.get("shape", [0])[0])
    if has_ann and has_indicator_rows:
        return gate(
            "WORKAROUND",
            "AkShare 可见公告日和财务指标，但公告日与 9 指标不是同一稳定点时接口，需后续映射验证。",
            {"yjbb": yjbb_results, "indicator": indicator_evidence},
            ["公告日与财务指标口径可能需按报告期合并，不能直接视为点时面板。"],
        )
    if has_ann:
        return gate("WORKAROUND", "AkShare 可见公告日，但未同时证明 9 指标历史面板可用。", {"yjbb": yjbb_results, "indicator": indicator_evidence})
    errors = [call.get("error") for call in calls if call["label"].startswith(("stock_yjbb_em", "stock_financial")) and not call["ok"]]
    if errors and not (yjbb_results or indicator_evidence):
        return gate_from_failure("AkShare 财务接口未取得可判定证据。", {"yjbb": yjbb_results, "indicator": indicator_evidence, "errors": errors})
    return gate("NO-GO", "AkShare 未证明可取得带公告日的历史财务点时数据。", {"yjbb": yjbb_results, "indicator": indicator_evidence, "errors": errors})


def _probe_g3_universe(ak, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for name in ("stock_info_sh_delist", "stock_info_sz_delist"):
        fn = getattr(ak, name, None)
        if not fn:
            evidence[name] = {"error": "function missing"}
            continue
        call = _call(calls, name, _ak(fn), timeout_sec)
        if call["ok"] and isinstance(call["value"], pd.DataFrame):
            df = call["value"]
            raw_columns[f"G3.{name}"] = df_columns(df)
            evidence[name] = {"shape": df_shape(df), "columns": df_columns(df), "sample": safe_sample_records(df, 1)}
        else:
            evidence[name] = {"error": call.get("error")}

    info_checks = []
    for code in ("600519", "000979"):
        call = _call(calls, f"stock_individual_info_em {code}", _ak(lambda code=code: ak.stock_individual_info_em(symbol=code)), timeout_sec)
        if call["ok"] and isinstance(call["value"], pd.DataFrame):
            df = call["value"]
            raw_columns[f"G3.stock_individual_info_em.{code}"] = df_columns(df)
            info_checks.append({"code": code, "shape": df_shape(df), "columns": df_columns(df), "sample": safe_sample_records(df, 5)})
        else:
            info_checks.append({"code": code, "error": call.get("error")})
    evidence["individual_info"] = info_checks

    has_delist = any(isinstance(v, dict) and v.get("shape", [0])[0] for k, v in evidence.items() if "delist" in k)
    has_list_info = any("上市" in str(item.get("sample", "")) or list_date_column(pd.DataFrame(item.get("sample", []))) for item in info_checks)
    if has_delist and has_list_info:
        return gate(
            "WORKAROUND",
            "AkShare 有退市表与个股上市信息证据，但未证明可直接枚举任意历史日在市全集。",
            evidence,
            ["若只用当日现货名单仍会有幸存者偏差；Phase B 需另建 universe 表验证。"],
        )
    errors = []
    for value in evidence.values():
        if isinstance(value, dict) and value.get("error"):
            errors.append(value.get("error"))
        if isinstance(value, list):
            errors.extend(item.get("error") for item in value if isinstance(item, dict) and item.get("error"))
    if errors and not any(
        isinstance(v, dict) and v.get("shape", [0])[0]
        for v in evidence.values()
        if isinstance(v, dict)
    ):
        return gate_from_failure("AkShare 成分/退市接口未取得可判定证据。", evidence)
    return gate("NO-GO", "AkShare 未证明可重建含退市股的历史在市名单。", evidence)


def _probe_g4_industry(ak, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    call = _call(calls, "stock_board_industry_name_em", _ak(lambda: ak.stock_board_industry_name_em()), timeout_sec)
    evidence: dict[str, Any] = {}
    if call["ok"] and isinstance(call["value"], pd.DataFrame) and not call["value"].empty:
        df = call["value"]
        raw_columns["G4.stock_board_industry_name_em"] = df_columns(df)
        evidence["boards"] = {"shape": df_shape(df), "columns": df_columns(df), "sample": safe_sample_records(df, 3)}
        symbol_col = df_columns(df)[0]
        symbol = str(df.iloc[0][symbol_col])
        cons_call = _call(calls, f"stock_board_industry_cons_em {symbol}", _ak(lambda: ak.stock_board_industry_cons_em(symbol=symbol)), timeout_sec)
        if cons_call["ok"] and isinstance(cons_call["value"], pd.DataFrame):
            cons = cons_call["value"]
            raw_columns[f"G4.stock_board_industry_cons_em.{symbol}"] = df_columns(cons)
            evidence["constituents"] = {"symbol": symbol, "shape": df_shape(cons), "columns": df_columns(cons), "sample": safe_sample_records(cons, 2)}
        has_asof = any("date" in col.lower() or "日期" in col for col in df_columns(df))
        return gate(
            "GO" if has_asof else "WORKAROUND",
            "AkShare 可取得行业分类；是否点时取决于日期字段。" if has_asof else "AkShare 可取得当前行业分类，但未证明点时行业。",
            evidence,
            [] if has_asof else ["行业若无日期，只能作为当前口径或后续 workaround，不能复现点时行业内分位。"],
        )
    error = call.get("error")
    return gate_from_failure("AkShare 行业接口未取得可判定证据。", {"error": error})


def _probe_g6_valuation(ak, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    if not hasattr(ak, "stock_a_indicator_lg"):
        return _probe_g6_valuation_baidu(ak, calls, raw_columns, timeout_sec)

    call = _call(calls, "stock_a_indicator_lg 600519", _ak(lambda: ak.stock_a_indicator_lg(symbol="600519")), timeout_sec)
    if call["ok"] and isinstance(call["value"], pd.DataFrame):
        df = call["value"]
        raw_columns["G6.stock_a_indicator_lg.600519"] = df_columns(df)
        evidence = {
            "shape": df_shape(df),
            "columns": df_columns(df),
            "date_col": "trade_date" if "trade_date" in df_columns(df) else ("日期" if "日期" in df_columns(df) else None),
            "pe_col": pe_column(df),
            "pb_col": pb_column(df),
            "sample": safe_sample_records(df, 2),
        }
        has_needed = bool(evidence["date_col"] and evidence["pe_col"] and evidence["pb_col"] and df_shape(df)[0] > 100)
        return gate("GO" if has_needed else "WORKAROUND", "AkShare 可取历史 PE/PB 估值。" if has_needed else "AkShare 历史估值接口返回，但字段/覆盖度证据不足。", evidence)
    error = call.get("error")
    return gate_from_failure("AkShare 历史估值接口未取得可判定证据。", {"error": error})


def _probe_g6_valuation_baidu(ak, calls: list[dict[str, Any]], raw_columns: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    pe_call = _call(
        calls,
        "stock_zh_valuation_baidu PE 600519",
        _ak(lambda: ak.stock_zh_valuation_baidu(symbol="600519", indicator="市盈率(TTM)", period="近十年")),
        timeout_sec,
    )
    pb_call = _call(
        calls,
        "stock_zh_valuation_baidu PB 600519",
        _ak(lambda: ak.stock_zh_valuation_baidu(symbol="600519", indicator="市净率", period="近十年")),
        timeout_sec,
    )
    if pe_call["ok"] and pb_call["ok"] and isinstance(pe_call["value"], pd.DataFrame) and isinstance(pb_call["value"], pd.DataFrame):
        pe_df = pe_call["value"]
        pb_df = pb_call["value"]
        raw_columns["G6.stock_zh_valuation_baidu.pe.600519"] = df_columns(pe_df)
        raw_columns["G6.stock_zh_valuation_baidu.pb.600519"] = df_columns(pb_df)
        pe_date = date_column(pe_df)
        pb_date = date_column(pb_df)
        pe_val = pe_column(pe_df) or _first_non_date_column(pe_df)
        pb_val = pb_column(pb_df) or _first_non_date_column(pb_df)
        evidence = {
            "pe_shape": df_shape(pe_df),
            "pb_shape": df_shape(pb_df),
            "pe_columns": df_columns(pe_df),
            "pb_columns": df_columns(pb_df),
            "pe_date_col": pe_date,
            "pb_date_col": pb_date,
            "pe_value_col": pe_val,
            "pb_value_col": pb_val,
            "pe_sample": safe_sample_records(pe_df, 1),
            "pb_sample": safe_sample_records(pb_df, 1),
        }
        has_needed = bool(pe_date and pb_date and pe_val and pb_val and df_shape(pe_df)[0] > 100 and df_shape(pb_df)[0] > 100)
        return gate(
            "GO" if has_needed else "WORKAROUND",
            "AkShare Baidu 估值接口可取历史 PE/PB。" if has_needed else "AkShare Baidu 估值接口返回，但字段/覆盖度证据不足。",
            evidence,
        )
    errors = [pe_call.get("error"), pb_call.get("error")]
    return gate_from_failure("AkShare Baidu 历史估值接口未取得可判定证据。", {"errors": errors})


def _first_non_date_column(df: pd.DataFrame) -> str | None:
    date_col = date_column(df)
    for col in df_columns(df):
        if col != date_col:
            return col
    return None


def _probe_g5_throughput(calls: list[dict[str, Any]]) -> dict[str, Any]:
    result = throughput_gate(calls, "AkShare 接口")
    if result["status"] in {"GO", "WORKAROUND"}:
        result["risks"].append("估时是 Phase A 小样本粗估；全量需限频、断点续跑和缓存。")
    return result
