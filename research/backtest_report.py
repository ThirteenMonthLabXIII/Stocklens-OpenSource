"""Phase C report runner with one-shot holdout discipline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.backtest_engine import BacktestConfig, coverage_status, run_backtest
from research.metrics_robustness import (
    deflated_sharpe_ratio,
    pbo_cscv,
    period_sharpe_ratio,
    return_summary,
)
from research.panel_db import OUT_DIR, open_panel
from research.scoring_offline import WEIGHTS_PATH, CandidateWeights, load_preregistered_weights

LEDGER_PATH = Path(__file__).resolve().parent / "holdout_ledger.json"
METHODOLOGY_FILES = (
    "research/weights_preregister.json",
    "research/scoring_offline.py",
    "research/backtest_engine.py",
    "research/backtest_report.py",
    "research/metrics_robustness.py",
    "research/pit_join.py",
    "research/panel_db.py",
    "research/metric_metadata.py",
)


def generate_report(
    out_md: Path | None = None,
    out_json: Path | None = None,
    *,
    run_holdout: bool = False,
    ledger_path: Path = LEDGER_PATH,
    holdout_gate: Callable[[Path], dict] | None = None,
) -> dict:
    out_md = out_md or (OUT_DIR / "backtest_report.md")
    out_json = out_json or (OUT_DIR / "backtest_report.json")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    with open_panel() as db:
        coverage = coverage_status(db)
        candidates = load_preregistered_weights()
        ledger = _read_ledger(ledger_path)
        payload: dict = {
            "generated_at": _now(),
            "status": "BLOCKED_FULL_PANEL_REQUIRED",
            "coverage": coverage,
            "hs300_status": coverage.get("hs300_status", "MISSING_DATA"),
            "candidates": [{"id": c.id, "reason": c.reason, "weights": c.weights} for c in candidates],
            "holdout_run_count": _holdout_run_count(ledger),
            "holdout_ledger": ledger,
            "compliance": {
                "production_untouched_by_phase_c": True,
                "d_weight_policy_locked": True,
                "marketing_claims": "none",
            },
        }
        if coverage["full_panel_ready"]:
            payload.update(
                _run_real_experiment(
                    db,
                    candidates,
                    run_holdout=run_holdout,
                    ledger_path=ledger_path,
                    ledger=ledger,
                    holdout_gate=holdout_gate,
                )
            )
        elif run_holdout:
            payload["status"] = "BLOCKED_FULL_PANEL_REQUIRED"
            payload["holdout_block_reason"] = "full panel coverage is required before holdout"
        out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        out_md.write_text(_render_markdown(payload), encoding="utf-8")
        return payload


def _run_real_experiment(
    db,
    candidates: list[CandidateWeights],
    *,
    run_holdout: bool,
    ledger_path: Path,
    ledger: dict | None,
    holdout_gate: Callable[[Path], dict] | None = None,
) -> dict:
    windows = {
        "train": ("2019-01-01", "2023-12-31"),
        "validation": ("2024-01-01", "2024-12-31"),
        "holdout": ("2025-01-01", "2026-06-16"),
    }
    train_validation_excess: dict[str, pd.Series] = {}
    validation_excess: dict[str, pd.Series] = {}
    summaries: list[dict] = []

    for candidate in candidates:
        by_window: dict[str, dict] = {}
        for window_name in ("train", "validation"):
            start, end = windows[window_name]
            bt = run_backtest(candidate.weights, db, config=BacktestConfig(start_date=start, end_date=end))
            excess = pd.to_numeric(bt.get("excess_return"), errors="coerce")
            by_window[window_name] = {
                "summary": return_summary(excess),
                "period_rows": bt.to_dict("records"),
            }
            if window_name == "validation":
                validation_excess[candidate.id] = excess
            train_validation_excess.setdefault(candidate.id, pd.Series(dtype="float64"))
            train_validation_excess[candidate.id] = pd.concat(
                [train_validation_excess[candidate.id], excess],
                ignore_index=True,
            )
        summaries.append(
            {
                "candidate_id": candidate.id,
                "train": by_window["train"]["summary"],
                "validation": by_window["validation"]["summary"],
            }
        )

    validation_sharpes = pd.Series(
        {candidate_id: period_sharpe_ratio(excess) for candidate_id, excess in validation_excess.items()},
        dtype="float64",
    )
    for summary in summaries:
        candidate_id = summary["candidate_id"]
        summary["validation"].update(
            deflated_sharpe_ratio(
                validation_excess.get(candidate_id, pd.Series(dtype="float64")),
                sharpe_estimates=validation_sharpes,
            )
        )

    winner_id = _select_winner(summaries)
    matrix = pd.DataFrame(train_validation_excess)
    robust = pbo_cscv(matrix)
    ranked = sorted(summaries, key=_ranking_key)
    result = {
        "status": "TRAIN_VALIDATION_EVALUATED",
        "winner": winner_id,
        "holdout_run_count": _holdout_run_count(ledger),
        "results": ranked,
        "pbo_cscv": robust,
    }

    if ledger:
        result["holdout_ledger"] = ledger
        _attach_ledger_holdout(result["results"], ledger)
        return result
    if not run_holdout:
        return result

    gate_info = (holdout_gate or _assert_holdout_gate)(ledger_path)
    winner = next(c for c in candidates if c.id == winner_id)
    start, end = windows["holdout"]
    holdout_bt = run_backtest(winner.weights, db, config=BacktestConfig(start_date=start, end_date=end))
    holdout_excess = pd.to_numeric(holdout_bt.get("excess_return"), errors="coerce")
    holdout_summary = return_summary(holdout_excess)
    holdout_summary.update(deflated_sharpe_ratio(holdout_excess, sharpe_estimates=[period_sharpe_ratio(holdout_excess)]))
    new_ledger = {
        "schema_version": 1,
        "run_count": 1,
        "winner": winner_id,
        "weights_file": gate_info.get("weights_file"),
        "methodology_commit": gate_info.get("methodology_commit"),
        "methodology_files": list(METHODOLOGY_FILES),
        "data_fingerprint": {
            "universe": db.table_fingerprint("universe"),
            "prices_daily": db.table_fingerprint("prices_daily"),
            "fundamentals_pit": db.table_fingerprint("fundamentals_pit"),
            "industry_map": db.table_fingerprint("industry_map"),
        },
        "holdout_window": [start, end],
        "ran_at": _now(),
        "result_summary": holdout_summary,
        "period_rows": holdout_bt.to_dict("records"),
    }
    _write_ledger(ledger_path, new_ledger)
    result["status"] = "HOLDOUT_EVALUATED_ONCE"
    result["holdout_run_count"] = 1
    result["holdout_ledger"] = new_ledger
    _attach_ledger_holdout(result["results"], new_ledger)
    return result


def _select_winner(summaries: list[dict]) -> str:
    if not summaries:
        raise ValueError("no candidate summaries")
    return sorted(summaries, key=_ranking_key)[0]["candidate_id"]


def _ranking_key(row: dict) -> tuple:
    validation = row.get("validation", {})
    dsr = validation.get("dsr")
    mean = validation.get("mean")
    return (
        -(float(dsr) if dsr is not None else -1.0),
        -(float(mean) if mean is not None else -999.0),
        str(row.get("candidate_id", "")),
    )


def _read_ledger(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_ledger(path: Path, payload: dict) -> None:
    if path.exists():
        raise RuntimeError(f"holdout ledger already exists: {path}")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _holdout_run_count(ledger: dict | None) -> int:
    if not ledger:
        return 0
    return int(ledger.get("run_count") or 1)


def _attach_ledger_holdout(results: list[dict], ledger: dict) -> None:
    winner = str(ledger.get("winner", ""))
    for row in results:
        if row.get("candidate_id") == winner:
            row["holdout"] = ledger.get("result_summary")


def _assert_holdout_gate(ledger_path: Path) -> dict:
    if ledger_path.exists():
        raise RuntimeError(f"holdout ledger already exists: {ledger_path}")
    missing = [path for path in METHODOLOGY_FILES if not _git_ok(["ls-files", "--error-unmatch", path])]
    if missing:
        raise RuntimeError(f"methodology files must be tracked before holdout: {missing}")
    dirty = [path for path in METHODOLOGY_FILES if not _git_ok(["diff", "--quiet", "HEAD", "--", path])]
    if dirty:
        raise RuntimeError(f"methodology files must be clean relative to HEAD before holdout: {dirty}")
    return {
        "methodology_commit": _git_out(["rev-parse", "HEAD"]),
        "weights_file": {
            "path": str(WEIGHTS_PATH.relative_to(ROOT)),
            "head_blob": _git_out(["rev-parse", f"HEAD:{WEIGHTS_PATH.relative_to(ROOT).as_posix()}"]),
            "head_commit": _git_out(["log", "-n", "1", "--format=%H", "--", str(WEIGHTS_PATH.relative_to(ROOT))]),
        },
    }


def _git_ok(args: list[str]) -> bool:
    return subprocess.run(["git", *args], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _git_out(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _render_markdown(payload: dict) -> str:
    lines = [
        "# Phase C 预注册权重稳健性报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 状态：**{payload['status']}**",
        "- 合规：仅 research 实验；不修改生产 D-weight-policy；不输出营销式胜率/准确率话术。",
        f"- 留出运行次数：{payload.get('holdout_run_count', 0)}",
        f"- 沪深300参考基准：{payload.get('hs300_status', 'MISSING_DATA')}",
        "",
        "## 预注册候选",
    ]
    for c in payload["candidates"]:
        lines.append(f"- `{c['id']}`：{c['reason']}；weights={json.dumps(c['weights'], ensure_ascii=False)}")
    lines.extend(["", "## 数据覆盖"])
    lines.append(f"- prices: {json.dumps(payload['coverage']['prices'], ensure_ascii=False)}")
    lines.append(f"- fundamentals: {json.dumps(payload['coverage']['fundamentals'], ensure_ascii=False)}")
    if payload["status"] == "BLOCKED_FULL_PANEL_REQUIRED":
        lines.extend(
            [
                "",
                "## 阻断说明",
                "",
                "当前 `research/data/panel.sqlite3` 不是 Phase C 真实回测输入：价格/财务未达到全A 2018-2026 覆盖。",
                "因此本报告只锁定预注册候选与执行闸门，不编造 train/val/holdout 排名。",
            ]
        )
    else:
        lines.extend(["", "## 稳健性排序"])
        for row in payload["results"]:
            holdout = row.get("holdout") or {}
            holdout_text = ""
            if holdout:
                holdout_text = (
                    f", holdout_mean_excess={holdout.get('mean')}, "
                    f"holdout_DSR={holdout.get('dsr')}"
                )
            lines.append(
                f"- `{row['candidate_id']}`：validation_mean_excess={row['validation']['mean']}, "
                f"validation_period_sharpe={row['validation']['period_sharpe']}, "
                f"validation_DSR={row['validation']['dsr']}{holdout_text}"
            )
        lines.append(f"- Winner: `{payload['winner']}`")
        lines.append(f"- PBO/CSCV: {json.dumps(payload['pbo_cscv'], ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-holdout", action="store_true", help="Run the one-shot holdout after all gates pass.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    print(json.dumps(generate_report(run_holdout=args.run_holdout), ensure_ascii=False, indent=2))
