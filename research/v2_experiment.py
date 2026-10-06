"""Research-only v2 diagnostic wrapper with machine-enforced boundaries.

The v2 preregistration may use data through 2026-06-16 for training and
diagnostics. It must not emit confirmatory claims from that data, and every
training/diagnostic evaluation is appended to an audit log for future DSR trial
counting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass
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
from research.factor_ic import _attach_derived_price_features
from research.experiment_identity import data_generation_fingerprint
from research.metrics_robustness import return_summary
from research.panel_db import OUT_DIR, ResearchPanelDB, open_panel
from research.pit_join import as_of
from research.scoring_offline import _percentile

TRAIN_VALID_MAX_DATE = "2026-06-16"
FORWARD_START_EXCLUSIVE = "2026-06-16"

V2_REGISTRY_PATH = Path(__file__).resolve().parent / "weights_preregister_v2.json"
V2_TRIAL_LOG_PATH = OUT_DIR / "v2_trial_log.jsonl"
V2_FORWARD_LEDGER_PATH = Path(__file__).resolve().parent / "forward_ledger.json"

TRAINING_MODES = {"train", "validate"}
ALL_MODES = TRAINING_MODES | {"forward_confirm"}
TRAINING_STATUS = "TRAINING_DIAGNOSTIC_NOT_CONFIRMATION"
VALIDATION_STATUS = "VALIDATION_DIAGNOSTIC_NOT_CONFIRMATION"
FORWARD_STATUS = "FORWARD_CONFIRMATION_FROM_LEDGER_ONLY"


@dataclass(frozen=True)
class V2DiagnosticConfig:
    mode: str = "train"
    start_date: str = "2019-01-01"
    end_date: str = TRAIN_VALID_MAX_DATE
    holding_days: int = 20
    top_n: int = 50
    commission_bps: float = 3.0
    stamp_tax_sell_bps: float = 5.0
    slippage_bps: float = 5.0
    limit_lock_threshold: float = 0.095


def assert_within_training_boundary(asof_date: str) -> None:
    """Reject training/diagnostic reads past the preregistered boundary."""
    if pd.Timestamp(asof_date) > pd.Timestamp(TRAIN_VALID_MAX_DATE):
        raise AssertionError(
            f"train/diagnostic mode cannot read {asof_date} > {TRAIN_VALID_MAX_DATE}"
        )


def assert_training_window(start_date: str, end_date: str) -> None:
    assert_within_training_boundary(start_date)
    assert_within_training_boundary(end_date)


def assert_mode(mode: str) -> None:
    if mode not in ALL_MODES:
        raise ValueError(f"unknown v2 mode {mode!r}; expected one of {sorted(ALL_MODES)}")


def assert_forward_confirmation_ready(
    start_date: str,
    *,
    ledger_path: Path = V2_FORWARD_LEDGER_PATH,
) -> None:
    """Forward confirmation must be strictly after the training boundary and ledger-backed."""
    if pd.Timestamp(start_date) <= pd.Timestamp(FORWARD_START_EXCLUSIVE):
        raise AssertionError(
            f"forward_confirm mode must start after {FORWARD_START_EXCLUSIVE}; got {start_date}"
        )
    if not ledger_path.exists():
        raise RuntimeError(f"forward_confirm mode requires one-shot forward ledger: {ledger_path}")


def load_v2_registry(path: Path = V2_REGISTRY_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def make_v2_result_payload(
    config: V2DiagnosticConfig,
    *,
    registry: Mapping[str, Any] | None = None,
    metrics: Mapping[str, Any] | None = None,
    period_rows: list[dict] | None = None,
    data_fingerprint: Mapping[str, Any] | None = None,
    forward_ledger_path: Path = V2_FORWARD_LEDGER_PATH,
) -> dict:
    """Build a mode-stamped result payload.

    Training/validation payloads are explicitly non-confirmatory and contain no
    adoption, winner, or production-ready decision keys.
    """
    assert_mode(config.mode)
    if config.mode in TRAINING_MODES:
        assert_training_window(config.start_date, config.end_date)
        confirmatory = False
        status = TRAINING_STATUS if config.mode == "train" else VALIDATION_STATUS
    else:
        assert_forward_confirmation_ready(config.start_date, ledger_path=forward_ledger_path)
        confirmatory = True
        status = FORWARD_STATUS

    registry = dict(registry or load_v2_registry())
    data_fingerprint = dict(data_fingerprint or {})
    runtime_config = _runtime_config_payload(config, registry)
    payload = {
        "generated_at": _now(),
        "schema_version": 2,
        "mode": config.mode,
        "status": status,
        "confirmatory": confirmatory,
        "claim_scope": (
            "diagnostic_only_no_adoption_or_production_claims"
            if not confirmatory
            else "ledger_backed_forward_confirmation"
        ),
        "training_boundary": {
            "train_valid_max_date_inclusive": TRAIN_VALID_MAX_DATE,
            "forward_start_exclusive": FORWARD_START_EXCLUSIVE,
        },
        "window": {"start_date": config.start_date, "end_date": config.end_date},
        "candidate_id": registry.get("candidate", {}).get("id"),
        "config_hash": stable_hash(runtime_config),
        "data_fingerprint": data_fingerprint,
        "data_fingerprint_hash": stable_hash(data_fingerprint) if data_fingerprint else None,
        "metrics": dict(metrics or {}),
        "period_rows": period_rows or [],
    }
    return payload


def score_v2_cross_section(panel: pd.DataFrame, registry: Mapping[str, Any] | None = None) -> pd.DataFrame:
    """Score one cross-section using the preregistered v2 structural components."""
    registry = registry or load_v2_registry()
    candidate = registry["candidate"]
    if panel.empty:
        return pd.DataFrame(columns=["code", "score"])
    out = panel.copy()
    if "code" not in out.columns:
        raise ValueError("panel must contain code")
    result = pd.DataFrame({"code": out["code"].astype(str)})
    if "industry" in out.columns:
        result["industry"] = out["industry"].fillna("__missing__").astype(str)

    total = pd.Series(0.0, index=out.index)
    for component in candidate.get("score_components", []):
        component_score = pd.Series(0.0, index=out.index)
        component_weight = float(component["weight"])
        subweight_total = 0.0
        for item in component.get("inputs", []):
            factor = str(item["factor"])
            direction = str(item["direction"])
            subweight = float(item["subweight"])
            if factor in out.columns:
                values = pd.to_numeric(out[factor], errors="coerce")
            else:
                values = pd.Series(math.nan, index=out.index, dtype="float64")
            component_score = component_score + _percentile(values, direction).fillna(0.0) * subweight
            subweight_total += subweight
        if subweight_total > 0:
            component_score = component_score / subweight_total
        result[f"{component['name']}_score"] = component_score
        total = total + component_score * component_weight
    result["score"] = total
    return result.sort_values(["score", "code"], ascending=[False, True]).reset_index(drop=True)


def run_v2_backtest(
    db: ResearchPanelDB,
    *,
    config: V2DiagnosticConfig = V2DiagnosticConfig(),
    registry: Mapping[str, Any] | None = None,
) -> pd.DataFrame:
    """Run v2 in research mode. Training/validation dates are hard-guarded."""
    assert_mode(config.mode)
    if config.mode in TRAINING_MODES:
        assert_training_window(config.start_date, config.end_date)
    else:
        assert_forward_confirmation_ready(config.start_date)

    registry = registry or load_v2_registry()
    dates = _trading_dates(db, config.start_date, config.end_date)
    rebalances = _month_start_dates(dates)
    rows: list[dict] = []
    for rebalance_date in rebalances:
        if config.mode in TRAINING_MODES:
            assert_within_training_boundary(rebalance_date)
        exit_date = _exit_date(dates, rebalance_date, config.holding_days)
        if exit_date is None:
            continue
        if config.mode in TRAINING_MODES:
            assert_within_training_boundary(exit_date)

        panel = as_of(rebalance_date, db)
        if panel.empty:
            continue
        panel = _attach_derived_price_features(panel, db, rebalance_date)
        eligible_raw = _eligible_at_entry(panel, config.limit_lock_threshold)
        if eligible_raw.empty:
            continue
        eligible = _apply_liquidity_floor(eligible_raw)
        scored = score_v2_cross_section(eligible, registry)
        selected = _select_with_caps(scored, registry, config.top_n)
        picks = selected["code"].astype(str).tolist()
        if not picks:
            continue

        entry = eligible.set_index("code")
        future = db.query_df(
            f"SELECT code, close FROM prices_daily WHERE date=? AND code IN ({','.join('?' for _ in picks)})",
            (exit_date, *picks),
        ).set_index("code")
        pick_returns = []
        for code in picks:
            if code not in future.index:
                continue
            entry_close = float(entry.loc[code, "close"])
            exit_close = float(future.loc[code, "close"])
            if entry_close > 0 and exit_close > 0:
                pick_returns.append(exit_close / entry_close - 1.0)
        if not pick_returns:
            continue
        portfolio_return = float(pd.Series(pick_returns).mean()) - _round_trip_cost(config)
        benchmark_return = _benchmark_equal_weight_return(db, eligible_raw["code"].astype(str).tolist(), rebalance_date, exit_date)
        rows.append(
            {
                "mode": config.mode,
                "confirmatory": False if config.mode in TRAINING_MODES else True,
                "rebalance_date": rebalance_date,
                "exit_date": exit_date,
                "n_eligible": int(len(eligible_raw)),
                "n_after_liquidity_floor": int(len(eligible)),
                "n_selected": int(len(pick_returns)),
                "portfolio_return": portfolio_return,
                "benchmark_return": benchmark_return,
                "excess_return": portfolio_return - benchmark_return,
                "selected_codes": ",".join(picks),
            }
        )
    return pd.DataFrame(rows)


def generate_v2_diagnostic(
    out_json: Path | None = None,
    out_md: Path | None = None,
    *,
    config: V2DiagnosticConfig = V2DiagnosticConfig(),
    trial_log_path: Path = V2_TRIAL_LOG_PATH,
) -> dict:
    """Run and write a v2 training/validation diagnostic report."""
    registry = load_v2_registry()
    with open_panel() as db:
        data_fingerprint = data_generation_fingerprint(db)
        bt = run_v2_backtest(db, config=config, registry=registry)
    metrics = return_summary(pd.to_numeric(bt.get("excess_return"), errors="coerce"))
    payload = make_v2_result_payload(
        config,
        registry=registry,
        metrics=metrics,
        period_rows=bt.to_dict("records"),
        data_fingerprint=data_fingerprint,
    )
    if config.mode in TRAINING_MODES:
        append_trial_log(payload, trial_log_path)

    out_json = out_json or (OUT_DIR / f"v2_{config.mode}_diagnostic.json")
    out_md = out_md or (OUT_DIR / f"v2_{config.mode}_diagnostic.md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_render_markdown(payload), encoding="utf-8")
    return payload


def append_trial_log(payload: Mapping[str, Any], log_path: Path = V2_TRIAL_LOG_PATH) -> None:
    """Append one training/diagnostic evaluation to the JSONL audit log."""
    if payload.get("confirmatory"):
        raise AssertionError("confirmatory payloads must use forward ledger, not v2 trial log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "logged_at": _now(),
        "mode": payload.get("mode"),
        "status": payload.get("status"),
        "confirmatory": False,
        "config_hash": payload.get("config_hash"),
        "window": payload.get("window"),
        "candidate_id": payload.get("candidate_id"),
        "data_fingerprint": payload.get("data_fingerprint", {}),
        "data_fingerprint_hash": payload.get("data_fingerprint_hash"),
        "metrics": payload.get("metrics", {}),
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def unique_trial_count(log_path: Path = V2_TRIAL_LOG_PATH) -> int:
    if not log_path.exists():
        return 0
    hashes: set[str] = set()
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("config_hash"):
            hashes.add(str(row["config_hash"]))
    return len(hashes)


def stable_hash(payload: Mapping[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _runtime_config_payload(config: V2DiagnosticConfig, registry: Mapping[str, Any]) -> dict:
    return {
        "config": asdict(config),
        "candidate": registry.get("candidate"),
        "discipline": registry.get("discipline"),
    }


def _apply_liquidity_floor(panel: pd.DataFrame) -> pd.DataFrame:
    if "liquidity_amount_20d" not in panel.columns:
        return panel.copy()
    liquidity = pd.to_numeric(panel["liquidity_amount_20d"], errors="coerce")
    valid = liquidity.dropna()
    if valid.empty:
        return panel.copy()
    floor = float(valid.quantile(0.10))
    return panel.loc[liquidity >= floor].copy()


def _select_with_caps(scored: pd.DataFrame, registry: Mapping[str, Any], top_n: int) -> pd.DataFrame:
    if scored.empty or top_n <= 0:
        return scored.iloc[0:0].copy()
    risk_guards = registry.get("candidate", {}).get("risk_guards", {})
    industry_cap = _cap_count(risk_guards.get("industry_cap"), top_n, default_share=0.20)
    board_cap = _cap_count(risk_guards.get("board_cap"), top_n, default_share=0.40)
    selected_rows: list[pd.Series] = []
    industry_counts: dict[str, int] = {}
    growth_board_count = 0
    for _, row in scored.iterrows():
        code = str(row["code"])
        industry = str(row.get("industry") or "__missing__")
        is_growth_board = _is_growth_board(code)
        if industry_counts.get(industry, 0) >= industry_cap:
            continue
        if is_growth_board and growth_board_count >= board_cap:
            continue
        selected_rows.append(row)
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        if is_growth_board:
            growth_board_count += 1
        if len(selected_rows) >= top_n:
            break
    if not selected_rows:
        return scored.iloc[0:0].copy()
    return pd.DataFrame(selected_rows).reset_index(drop=True)


def _cap_count(text: object, top_n: int, *, default_share: float) -> int:
    if isinstance(text, str):
        import re

        match = re.search(r"(\d+(?:\.\d+)?)%", text)
        if match:
            return max(1, math.floor(top_n * float(match.group(1)) / 100.0))
    return max(1, math.floor(top_n * default_share))


def _is_growth_board(code: str) -> bool:
    return code.startswith("sh.688") or code.startswith("sz.300") or code.startswith("sz.301")


def _render_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# V2 训练诊断",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- mode：`{payload['mode']}`",
        f"- status：**{payload['status']}**",
        f"- confirmatory：`{payload['confirmatory']}`",
        f"- 窗口：{payload['window']['start_date']} 至 {payload['window']['end_date']}",
        f"- config_hash：`{payload['config_hash']}`",
        "",
        "## 指标",
        "",
    ]
    for key, value in payload.get("metrics", {}).items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## 纪律",
            "",
            "- 该报告只能作为训练/诊断材料，不能作为采纳、上线、确认或营销依据。",
            f"- 训练/验证最大日期：{TRAIN_VALID_MAX_DATE}（含）。",
            f"- 前推确认必须严格晚于：{FORWARD_START_EXCLUSIVE}。",
        ]
    )
    return "\n".join(lines) + "\n"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=sorted(ALL_MODES), default="train")
    parser.add_argument("--start-date", default="2019-01-01")
    parser.add_argument("--end-date", default=TRAIN_VALID_MAX_DATE)
    return parser.parse_args(list(argv))


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    payload = generate_v2_diagnostic(
        config=V2DiagnosticConfig(mode=args.mode, start_date=args.start_date, end_date=args.end_date),
    )
    print(json.dumps({"status": payload["status"], "confirmatory": payload["confirmatory"]}, ensure_ascii=False))
