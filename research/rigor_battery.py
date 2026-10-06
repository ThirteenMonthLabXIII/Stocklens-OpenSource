"""Development-set rigor battery for v2 research conclusions.

All outputs are non-confirmatory. This module may use data through
2026-06-16 only, and it does not alter production scoring.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.backtest_engine import (
    _benchmark_equal_weight_return,
    _eligible_at_entry,
    _exit_date,
    _month_start_dates,
    _round_trip_cost,
    _trading_dates,
)
from research.factor_ic import _attach_derived_price_features, ic_decay_from_summary
from research.metrics_robustness import (
    deflated_sharpe_ratio,
    pbo_cscv,
    period_sharpe_ratio,
    return_summary,
    stationary_bootstrap_ci,
)
from research.panel_db import OUT_DIR, ResearchPanelDB, open_panel
from research.pit_join import as_of
from research.scoring_offline import score_cross_section
from research.v2_experiment import (
    TRAIN_VALID_MAX_DATE,
    V2DiagnosticConfig,
    _apply_liquidity_floor,
    _select_with_caps,
    assert_training_window,
    load_v2_registry,
    score_v2_cross_section,
    unique_trial_count,
)

PHASE_C_WEIGHTS_PATH = Path(__file__).resolve().parent / "weights_preregister.json"
V2_TRAIN_JSON = OUT_DIR / "v2_train_diagnostic.json"
V2_VALIDATE_JSON = OUT_DIR / "v2_validate_diagnostic.json"
V2_PROBE_LOG = OUT_DIR / "v2_robustness_probes.jsonl"
V2_BATTERY_JSON = OUT_DIR / "v2_rigor_battery.json"
V2_ACCEPTANCE_MD = OUT_DIR / "v2_development_acceptance.md"
V2_SHRUNK_DEFAULT_JSON = OUT_DIR / "v2_shrunk_default.json"
ARCH_BOOTSTRAP_JSON = OUT_DIR / "arch_bootstrap_crosscheck.json"
SKFOLIO_CPCV_JSON = OUT_DIR / "skfolio_cpcv_crosscheck.json"

DEVELOPMENT_STATUS = "DEVELOPMENT_ESTIMATE_NOT_CONFIRMATION"
V2_EVIDENCE_FACTORS = (
    "pb_mrq",
    "ps_ttm",
    "volatility_20d",
    "turnover_20d",
    "liquidity_amount_20d",
    "momentum_20d",
    "momentum_60d",
    "momentum_120d",
    "dimension_valuation",
)

DSR_RAMP = (0.05, 0.65)
PROB_RAMP = (0.50, 0.75)


def generate_rigor_battery(
    *,
    start_date: str = "2019-01-01",
    end_date: str = "2024-12-31",
    out_json: Path = V2_BATTERY_JSON,
    out_md: Path = V2_ACCEPTANCE_MD,
    shrunk_json: Path = V2_SHRUNK_DEFAULT_JSON,
    probe_log: Path = V2_PROBE_LOG,
) -> dict:
    """Run the A-F development-set battery and write report artifacts."""
    assert_training_window(start_date, end_date)
    registry = load_v2_registry()
    trials_n = int(registry["discipline"]["dsr_trials_n"])
    train_payload = _load_json(V2_TRAIN_JSON)
    validate_payload = _load_json(V2_VALIDATE_JSON)
    train_excess = _excess_series(train_payload)
    validate_excess = _excess_series(validate_payload)
    combined_excess = pd.concat([train_excess, validate_excess], ignore_index=True)

    dsr = {
        "train": _dsr_block(train_excess, trials_n),
        "validate": _dsr_block(validate_excess, trials_n),
        "combined": _dsr_block(combined_excess, trials_n),
    }
    bootstrap = {
        "train": _bootstrap_block(train_excess),
        "validate": _bootstrap_block(validate_excess),
        "combined": _bootstrap_block(combined_excess),
    }

    variants = component_weight_variants(registry)
    with open_panel() as db:
        family = run_v2_variant_family(db, registry, variants, config=V2DiagnosticConfig(mode="train", start_date=start_date, end_date=end_date))
        regime = regime_summaries(db, train_payload.get("period_rows", []) + validate_payload.get("period_rows", []))
    write_probe_log(family["variant_summaries"], probe_log)

    strategy_matrix = family["strategy_matrix"]
    pbo = pbo_cscv(strategy_matrix, n_splits=6, embargo=1)
    decay = _ic_decay()
    baseline = _contract_baseline_weights()
    dev_best = _development_value_tilt_prior()
    combined = dsr["combined"]
    combined_boot = bootstrap["combined"]
    shrink_gate = development_shrink_gate(
        pbo.get("pbo"),
        combined.get("dsr"),
        combined_boot["mean"],
        combined_boot["period_sharpe"],
    )
    shrunk = shrink_product_weights(
        baseline,
        dev_best,
        pbo.get("pbo"),
        dsr=combined.get("dsr"),
        mean_ci=combined_boot["mean"],
        sharpe_ci=combined_boot["period_sharpe"],
    )
    shrunk_payload = {
        "generated_at": _now(),
        "status": DEVELOPMENT_STATUS,
        "confirmatory": False,
        "purpose": (
            "Product-layer explainable ranking default only; non-confirmatory development estimate, "
            "not measured alpha, and not valid for win-rate, accuracy, or alpha claims."
        ),
        "baseline": baseline,
        "dev_best_prior": dev_best,
        "pbo": pbo.get("pbo"),
        "shrink_gate": shrink_gate,
        "lambda": shrunk["lambda"],
        "weights": shrunk["weights"],
        "forbidden_claims": ["alpha confirmed", "win-rate claim", "accuracy claim", "production investability claim"],
    }
    shrunk_json.parent.mkdir(parents=True, exist_ok=True)
    shrunk_json.write_text(json.dumps(shrunk_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    payload = {
        "generated_at": _now(),
        "status": DEVELOPMENT_STATUS,
        "confirmatory": False,
        "window": {"start_date": start_date, "end_date": end_date},
        "boundary": {"train_valid_max_date_inclusive": TRAIN_VALID_MAX_DATE},
        "trial_budget": {
            "pre_registered_dsr_trials_n": trials_n,
            "v2_trial_log_unique_count": unique_trial_count(),
            "robustness_probe_log": str(probe_log.relative_to(ROOT)),
            "robustness_probes_are_selection": False,
        },
        "dsr_n180": dsr,
        "bootstrap_ci": bootstrap,
        "purged_pbo": pbo,
        "robustness": {
            "variant_count": len(family["variant_summaries"]),
            "variant_summaries": family["variant_summaries"],
            "dispersion": family["dispersion"],
        },
        "regime": regime,
        "ic_decay": decay,
        "shrunk_default": shrunk_payload,
        "oracle_status": oracle_statuses(),
        "conclusion": {
            "strategy_effectiveness": "NOT_SUPPORTED_AS_ALPHA",
            "production_default": "SHRUNK_TO_BASELINE_FOR_EXPLAINABLE_RANKING_ONLY",
            "forward_confirmation": "DEFERRED_UNTIL_REAL_DATA_AFTER_2026_06_16",
        },
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_render_acceptance(payload), encoding="utf-8")
    return payload


def component_weight_variants(registry: Mapping[str, Any], shocks: tuple[float, ...] = (-0.20, -0.10, 0.10, 0.20)) -> list[dict]:
    components = registry["candidate"]["score_components"]
    base_weights = {str(c["name"]): float(c["weight"]) for c in components}
    variants = [{"id": "v2_base", "weights": base_weights, "registry": copy.deepcopy(registry)}]
    for component_name in base_weights:
        for shock in shocks:
            weights = dict(base_weights)
            weights[component_name] = max(0.0, weights[component_name] * (1.0 + shock))
            total = sum(weights.values())
            weights = {key: value / total for key, value in weights.items()}
            variant_registry = copy.deepcopy(registry)
            for component in variant_registry["candidate"]["score_components"]:
                component["weight"] = weights[str(component["name"])]
            variants.append(
                {
                    "id": f"{component_name}_{shock:+.0%}",
                    "weights": weights,
                    "registry": variant_registry,
                    "shock_component": component_name,
                    "shock": shock,
                }
            )
    return variants


def run_v2_variant_family(
    db: ResearchPanelDB,
    registry: Mapping[str, Any],
    variants: list[dict],
    *,
    config: V2DiagnosticConfig,
) -> dict:
    assert_training_window(config.start_date, config.end_date)
    dates = _trading_dates(db, config.start_date, config.end_date)
    rebalances = _month_start_dates(dates)
    returns_by_variant: dict[str, list[dict]] = {str(v["id"]): [] for v in variants}
    baseline_rows: list[dict] = []
    baseline_weights = _contract_baseline_weights()

    for rebalance_date in rebalances:
        exit_date = _exit_date(dates, rebalance_date, config.holding_days)
        if exit_date is None:
            continue
        assert_training_window(rebalance_date, exit_date)
        panel = as_of(rebalance_date, db)
        if panel.empty:
            continue
        panel = _attach_derived_price_features(panel, db, rebalance_date)
        eligible_raw = _eligible_at_entry(panel, config.limit_lock_threshold)
        if eligible_raw.empty:
            continue
        eligible_raw = eligible_raw.copy()
        eligible_raw["trend_20d"] = eligible_raw.get("momentum_20d")
        eligible_raw["trend_60d"] = eligible_raw.get("momentum_60d")
        eligible = _apply_liquidity_floor(eligible_raw)
        if eligible.empty:
            continue

        all_codes = eligible_raw["code"].astype(str).tolist()
        future_all = db.query_df(
            f"SELECT code, close AS exit_close FROM prices_daily WHERE date=? AND code IN ({','.join('?' for _ in all_codes)})",
            (exit_date, *all_codes),
        )
        ret_frame = eligible_raw[["code", "close"]].merge(future_all, on="code", how="inner")
        ret_frame["close"] = pd.to_numeric(ret_frame["close"], errors="coerce")
        ret_frame["exit_close"] = pd.to_numeric(ret_frame["exit_close"], errors="coerce")
        ret_frame = ret_frame[(ret_frame["close"] > 0) & (ret_frame["exit_close"] > 0)].copy()
        ret_frame["holding_return"] = ret_frame["exit_close"] / ret_frame["close"] - 1.0
        return_map = ret_frame.set_index("code")["holding_return"]
        benchmark_return = _benchmark_equal_weight_return(db, all_codes, rebalance_date, exit_date)

        for variant in variants:
            scored = score_v2_cross_section(eligible, variant["registry"])
            selected = _select_with_caps(scored, variant["registry"], config.top_n)
            picks = selected["code"].astype(str).tolist()
            excess = _portfolio_excess(return_map, picks, benchmark_return, config)
            if excess is not None:
                returns_by_variant[str(variant["id"])].append({"rebalance_date": rebalance_date, "excess_return": excess})

        baseline_scored = score_cross_section(eligible_raw, baseline_weights)
        baseline_picks = baseline_scored.head(config.top_n)["code"].astype(str).tolist()
        baseline_excess = _portfolio_excess(return_map, baseline_picks, benchmark_return, config)
        if baseline_excess is not None:
            baseline_rows.append({"rebalance_date": rebalance_date, "excess_return": baseline_excess})

    matrix = pd.DataFrame(index=sorted({row["rebalance_date"] for rows in returns_by_variant.values() for row in rows}))
    for variant_id, rows in returns_by_variant.items():
        series = pd.Series({row["rebalance_date"]: row["excess_return"] for row in rows}, dtype="float64")
        matrix[variant_id] = series
    baseline_series = pd.Series({row["rebalance_date"]: row["excess_return"] for row in baseline_rows}, dtype="float64")
    matrix["contract_baseline"] = baseline_series
    matrix = matrix.sort_index()
    summaries = [
        {"id": col, **return_summary(pd.to_numeric(matrix[col], errors="coerce"))}
        for col in matrix.columns
    ]
    sharpes = [row.get("period_sharpe") for row in summaries if row.get("period_sharpe") is not None]
    means = [row.get("mean") for row in summaries if row.get("mean") is not None]
    return {
        "strategy_matrix": matrix,
        "variant_summaries": summaries,
        "dispersion": {
            "mean_min": min(means) if means else None,
            "mean_max": max(means) if means else None,
            "mean_range": (max(means) - min(means)) if means else None,
            "period_sharpe_min": min(sharpes) if sharpes else None,
            "period_sharpe_max": max(sharpes) if sharpes else None,
            "period_sharpe_range": (max(sharpes) - min(sharpes)) if sharpes else None,
        },
    }


def development_shrink_lambda(
    pbo: float | None,
    dsr: float | None,
    mean_ci: Mapping[str, Any] | None,
    sharpe_ci: Mapping[str, Any] | None,
) -> float:
    gate = development_shrink_gate(pbo, dsr, mean_ci, sharpe_ci)
    return float(gate["lambda"])


def development_shrink_gate(
    pbo: float | None,
    dsr: float | None,
    mean_ci: Mapping[str, Any] | None,
    sharpe_ci: Mapping[str, Any] | None,
) -> dict:
    dsr_value = _float_or_none(dsr)
    pbo_value = _float_or_none(pbo)
    mean_stat = _ci_stat(mean_ci)
    sharpe_stat = _ci_stat(sharpe_ci)
    mean_prob = _prob_gt_threshold(mean_ci)
    sharpe_prob = _prob_gt_threshold(sharpe_ci)
    reasons: list[str] = []

    if dsr_value is None:
        reasons.append("DSR missing or non-finite")
    if pbo_value is None:
        reasons.append("PBO missing or non-finite")
    if mean_stat is None:
        reasons.append("mean stat missing or non-finite")
    if sharpe_stat is None:
        reasons.append("period Sharpe stat missing or non-finite")
    if mean_prob is None:
        reasons.append("P(mean > 0) missing or non-finite")
    if sharpe_prob is None:
        reasons.append("P(sharpe > 0) missing or non-finite")

    direction_gate = {
        "rule": "strict mean_stat > 0 and period_sharpe_stat > 0",
        "mean_stat": mean_stat,
        "period_sharpe_stat": sharpe_stat,
        "passed": bool(mean_stat is not None and sharpe_stat is not None and mean_stat > 0.0 and sharpe_stat > 0.0),
    }
    if mean_stat is not None and mean_stat <= 0.0:
        reasons.append("mean_stat <= 0")
    if sharpe_stat is not None and sharpe_stat <= 0.0:
        reasons.append("period_sharpe_stat <= 0")

    legacy_lam = _pbo_discount(pbo_value) if pbo_value is not None else 0.0
    components = {
        "dsr": smoothstep_ramp(dsr_value, *DSR_RAMP) if dsr_value is not None else 0.0,
        "prob_mean_gt_0": smoothstep_ramp(mean_prob, *PROB_RAMP) if mean_prob is not None else 0.0,
        "prob_sharpe_gt_0": smoothstep_ramp(sharpe_prob, *PROB_RAMP) if sharpe_prob is not None else 0.0,
    }
    evidence = _geometric_mean(components.values()) if not reasons else 0.0
    lam = legacy_lam * evidence if not reasons else 0.0
    passed = not reasons and lam > 0.0
    return {
        "gate_type": "direction_soft_ramp_v1",
        "rule": "negative direction returns lambda=0; positive direction gets evidence-weighted exploratory shrinkage with PBO as discount cap",
        "ramp_policy": {
            "smoothstep": "t*t*(3-2*t) after clipping t=(value-lower)/(upper-lower)",
            "dsr_ramp": {"lower": DSR_RAMP[0], "upper": DSR_RAMP[1]},
            "probability_ramp": {"lower": PROB_RAMP[0], "upper": PROB_RAMP[1]},
            "evidence": "geometric_mean(dsr_ramp, prob_mean_gt_0_ramp, prob_sharpe_gt_0_ramp)",
            "pbo": "lambda = clip(1-PBO,0,1) * evidence; PBO is only a discount cap",
        },
        "direction_gate": direction_gate,
        "dsr": dsr_value,
        "mean_ci": dict(mean_ci or {}),
        "period_sharpe_ci": dict(sharpe_ci or {}),
        "p_mean_gt_0": mean_prob,
        "p_period_sharpe_gt_0": sharpe_prob,
        "pbo": pbo_value,
        "legacy_pbo_only_lambda": legacy_lam,
        "evidence": evidence,
        "evidence_components": components,
        "passed": passed,
        "emits_tilt": lam > 0.0,
        "failed_reasons": reasons,
        "lambda": lam,
    }


def shrink_product_weights(
    baseline: Mapping[str, float],
    dev_best: Mapping[str, float],
    pbo: float | None,
    *,
    dsr: float | None = None,
    mean_ci: Mapping[str, Any] | None = None,
    sharpe_ci: Mapping[str, Any] | None = None,
) -> dict:
    lam = development_shrink_lambda(pbo, dsr, mean_ci, sharpe_ci)
    keys = list(baseline.keys())
    weights = {key: lam * float(dev_best[key]) + (1.0 - lam) * float(baseline[key]) for key in keys}
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("shrunk weights sum to zero")
    weights = {key: value / total for key, value in weights.items()}
    return {"lambda": lam, "weights": weights}


def smoothstep_ramp(value: float | None, lower: float, upper: float) -> float:
    value = _float_or_none(value)
    lower = float(lower)
    upper = float(upper)
    if value is None:
        return 0.0
    if upper <= lower:
        raise ValueError("upper must be greater than lower")
    t = min(max((value - lower) / (upper - lower), 0.0), 1.0)
    return float(t * t * (3.0 - 2.0 * t))


def oracle_statuses() -> dict:
    return {
        "arch_bootstrap_crosscheck": _oracle_status(ARCH_BOOTSTRAP_JSON),
        "skfolio_cpcv_crosscheck": _oracle_status(SKFOLIO_CPCV_JSON),
    }


def write_probe_log(summaries: list[dict], path: Path = V2_PROBE_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in summaries:
            fh.write(
                json.dumps(
                    {
                        "logged_at": _now(),
                        "kind": "robustness_probe",
                        "confirmatory": False,
                        "selection_trial": False,
                        "summary": row,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )


def regime_summaries(db: ResearchPanelDB, period_rows: list[dict]) -> dict:
    if not period_rows:
        return {}
    rows = pd.DataFrame(period_rows)
    rows["excess_return"] = pd.to_numeric(rows["excess_return"], errors="coerce")
    out: list[dict] = []
    hs300_returns: list[float] = []
    for _, row in rows.iterrows():
        entry = str(row["rebalance_date"])
        exit_date = str(row["exit_date"])
        hs_ret = _hs300_return(db, entry, exit_date)
        hs300_returns.append(hs_ret)
    rows["hs300_period_return"] = hs300_returns
    rows["regime"] = rows["hs300_period_return"].map(lambda x: "hs300_up" if x is not None and x >= 0 else "hs300_down")
    rows["abs_hs300_return"] = pd.to_numeric(rows["hs300_period_return"], errors="coerce").abs()
    q = rows["abs_hs300_return"].quantile(0.67)
    rows["vol_regime"] = rows["abs_hs300_return"].map(lambda x: "high_abs_move" if pd.notna(x) and x >= q else "normal_abs_move")
    for key in ("regime", "vol_regime"):
        for regime_name, group in rows.groupby(key):
            summary = return_summary(group["excess_return"])
            summary[key] = regime_name
            out.append(summary)
    return {"rows": out}


def _portfolio_excess(return_map: pd.Series, picks: list[str], benchmark_return: float, config: V2DiagnosticConfig) -> float | None:
    returns = pd.to_numeric(return_map.reindex(picks), errors="coerce").dropna()
    if returns.empty:
        return None
    return float(returns.mean()) - _round_trip_cost(config) - benchmark_return


def _dsr_block(returns: pd.Series, trials_n: int) -> dict:
    summary = return_summary(returns)
    summary.update(deflated_sharpe_ratio(returns, trials=trials_n))
    return summary


def _bootstrap_block(returns: pd.Series) -> dict:
    return {
        "mean": stationary_bootstrap_ci(returns, lambda s: float(s.mean()), n_boot=2000, expected_block=3, seed=101),
        "period_sharpe": stationary_bootstrap_ci(returns, period_sharpe_ratio, n_boot=2000, expected_block=3, seed=202),
    }


def _excess_series(payload: Mapping[str, Any]) -> pd.Series:
    rows = pd.DataFrame(payload.get("period_rows", []))
    if rows.empty or "excess_return" not in rows.columns:
        return pd.Series(dtype="float64")
    return pd.to_numeric(rows["excess_return"], errors="coerce").dropna().reset_index(drop=True)


def _contract_baseline_weights() -> dict[str, float]:
    payload = _load_json(PHASE_C_WEIGHTS_PATH)
    for candidate in payload["candidates"]:
        if candidate["id"] == "contract_baseline":
            return {str(k): float(v) for k, v in candidate["weights"].items()}
    raise KeyError("contract_baseline")


def _development_value_tilt_prior() -> dict[str, float]:
    return {
        "fundamental": 0.28,
        "growth": 0.18,
        "safety": 0.20,
        "valuation": 0.26,
        "trend": 0.08,
    }


def _ic_decay() -> list[dict]:
    path = OUT_DIR / "factor_ic.json"
    if not path.exists():
        return []
    payload = _load_json(path)
    return ic_decay_from_summary(payload.get("summary", []), factors=V2_EVIDENCE_FACTORS, ic_type="raw")


def _oracle_status(path: Path) -> dict:
    if not path.exists():
        return {
            "status": "SKIPPED",
            "confirmatory": False,
            "reason": f"artifact missing: {path.relative_to(ROOT)}",
        }
    try:
        payload = _load_json(path)
    except json.JSONDecodeError as exc:
        return {
            "status": "FAIL",
            "confirmatory": False,
            "reason": f"invalid JSON: {exc}",
        }
    status = str(payload.get("status", "FAIL"))
    out = {
        "status": status,
        "confirmatory": bool(payload.get("confirmatory", False)),
        "artifact": str(path.relative_to(ROOT)),
    }
    reason = payload.get("details", {}).get("reason") if isinstance(payload.get("details"), dict) else None
    if reason:
        out["reason"] = reason
    return out


def _hs300_return(db: ResearchPanelDB, entry: str, exit_date: str) -> float | None:
    frame = db.query_df(
        "SELECT date, close FROM hs300_daily WHERE date IN (?,?)",
        (entry, exit_date),
    )
    if frame.empty or len(frame) < 2:
        return None
    prices = {str(row["date"]): float(row["close"]) for _, row in frame.iterrows()}
    start = prices.get(entry)
    end = prices.get(exit_date)
    if not start or not end or start <= 0:
        return None
    return end / start - 1.0


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _float_or_none(value: object) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _ci_stat(ci: Mapping[str, Any] | None) -> float | None:
    if ci is None:
        return None
    return _float_or_none(ci.get("stat"))


def _prob_gt_threshold(ci: Mapping[str, Any] | None) -> float | None:
    if ci is None:
        return None
    prob = _float_or_none(ci.get("prob_gt_threshold"))
    if prob is None or prob < 0.0 or prob > 1.0:
        return None
    return prob


def _pbo_discount(pbo: float | None) -> float:
    if pbo is None:
        return 0.0
    return min(max(1.0 - float(pbo), 0.0), 1.0)


def _geometric_mean(values: Iterable[float]) -> float:
    vals = [min(max(float(value), 0.0), 1.0) for value in values]
    if not vals or any(value <= 0.0 for value in vals):
        return 0.0
    return float(math.prod(vals) ** (1.0 / len(vals)))


def _render_acceptance(payload: Mapping[str, Any]) -> str:
    pbo = payload["purged_pbo"]
    shrunk = payload["shrunk_default"]
    combined = payload["dsr_n180"]["combined"]
    boot = payload["bootstrap_ci"]["combined"]
    oracles = payload.get("oracle_status", {})
    lines = [
        "# V2 开发集严谨化验收",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 状态：**{payload['status']}**",
        f"- confirmatory：`{payload['confirmatory']}`",
        f"- 窗口：{payload['window']['start_date']} 至 {payload['window']['end_date']}（开发集，非确认）",
        f"- DSR trials N：{payload['trial_budget']['pre_registered_dsr_trials_n']}",
        "",
        "## 结论",
        "",
        "- 当前 v2 与 5 维默认配置均未获得可称为 alpha 的开发集支持。",
        "- 允许产出的只是产品层收缩默认配置：可解释排序，非实测 alpha。",
        "- 前推确认仍 deferred；2026-06-16 之后真实数据到位前不得采纳确认性结论。",
        "",
        "## DSR 与 CI",
        "",
        f"- combined mean：{combined.get('mean')}",
        f"- combined period_sharpe：{combined.get('period_sharpe')}",
        f"- combined DSR(N=180)：{combined.get('dsr')}",
        f"- expected max Sharpe hurdle：{combined.get('expected_max_sharpe')}",
        f"- mean 95% bootstrap CI：[{boot['mean']['lower']}, {boot['mean']['upper']}]",
        f"- P(mean > 0) bootstrap：{boot['mean'].get('prob_gt_threshold')}",
        f"- period Sharpe 95% bootstrap CI：[{boot['period_sharpe']['lower']}, {boot['period_sharpe']['upper']}]",
        f"- P(period Sharpe > 0) bootstrap：{boot['period_sharpe'].get('prob_gt_threshold')}",
        "",
        "## Oracle 状态",
        "",
        f"- arch_bootstrap_crosscheck：{oracles.get('arch_bootstrap_crosscheck', {}).get('status', 'SKIPPED')}",
        f"- skfolio_cpcv_crosscheck：{oracles.get('skfolio_cpcv_crosscheck', {}).get('status', 'SKIPPED')}",
        "- 若状态为 SKIPPED，表示外部 oracle parity 未闭合；不得写成已通过。",
        "",
        "## Purged PBO 与收缩",
        "",
        f"- purged PBO：{pbo.get('pbo')}",
        "- 负方向（mean_stat <= 0 或 period_sharpe_stat <= 0）直接 `lambda=0`。",
        "- 正方向只允许按 DSR 与 bootstrap 概率的连续证据坡道给 exploratory shrinkage；PBO 只作 `clip(1-PBO,0,1)` 折扣上限。",
        "- `lambda > 0` 的含义仅为 `evidence-weighted exploratory shrinkage`，不是 alpha confirmation。",
        f"- legacy PBO-only lambda：{shrunk.get('shrink_gate', {}).get('legacy_pbo_only_lambda')}",
        f"- evidence：{shrunk.get('shrink_gate', {}).get('evidence')}",
        f"- evidence components：{json.dumps(shrunk.get('shrink_gate', {}).get('evidence_components', {}), ensure_ascii=False)}",
        f"- shrink gate passed：{shrunk.get('shrink_gate', {}).get('passed')}",
        f"- shrink gate failed reasons：{json.dumps(shrunk.get('shrink_gate', {}).get('failed_reasons', []), ensure_ascii=False)}",
        f"- embargo：{pbo.get('embargo')}",
        f"- purged train rows：{pbo.get('purged_train_rows')}",
        f"- shrink lambda：{shrunk.get('lambda')}",
        f"- shrunk weights：{json.dumps(shrunk.get('weights'), ensure_ascii=False)}",
        "",
        "## 稳健性",
        "",
        f"- variant_count：{payload['robustness']['variant_count']}",
        f"- mean_range：{payload['robustness']['dispersion']['mean_range']}",
        f"- period_sharpe_range：{payload['robustness']['dispersion']['period_sharpe_range']}",
        "",
        "## 标签",
        "",
        "- `DEVELOPMENT_ESTIMATE_NOT_CONFIRMATION`",
        "- `NOT_SUPPORTED_AS_ALPHA`",
        "- `confirmatory=false`",
        "- `not alpha confirmation`",
        "- 禁止胜率、准确率、alpha、可投入生产等确认性话术。",
    ]
    return "\n".join(lines) + "\n"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2019-01-01")
    parser.add_argument("--end-date", default="2024-12-31")
    return parser.parse_args(list(argv))


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    result = generate_rigor_battery(start_date=args.start_date, end_date=args.end_date)
    print(json.dumps({"status": result["status"], "confirmatory": result["confirmatory"]}, ensure_ascii=False))
