"""Research-only lightweight workbench for phase 4.

The workbench is a thin map over existing research scripts. It does not import
``stock_tool`` runtime modules, does not execute trades, and does not add any
commercial-package dependency. External quant projects are documented as
architecture references only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / "research"
OUT_DIR = RESEARCH_DIR / "out"
DATA_DIR = RESEARCH_DIR / "data"

FORBIDDEN_COMMERCIAL_DEPENDENCIES = {
    "openbb": "AGPLv3, reference only",
    "vectorbt": "Commons Clause, reference only",
    "backtesting": "AGPL, reference only",
    "backtesting.py": "AGPL, reference only",
}

REFERENCE_ARCHITECTURES = [
    {
        "name": "Qlib",
        "use": "data-model-backtest-analysis workflow shape",
        "adoption": "reference_only",
    },
    {
        "name": "Lean",
        "use": "event-driven and pluggable module boundaries",
        "adoption": "reference_only_no_execution",
    },
    {
        "name": "FinRL-X",
        "use": "layered thinking: ML selection, timing, and risk control",
        "adoption": "reference_only_no_rl_trading",
    },
    {
        "name": "OpenBB",
        "use": "connect once, consume everywhere data-layer idea",
        "adoption": "reference_only_license_risk",
    },
    {
        "name": "vectorbt/backtesting.py",
        "use": "fast batch research ergonomics",
        "adoption": "reference_only_license_risk",
    },
]


@dataclass(frozen=True)
class ResearchStage:
    id: str
    title: str
    purpose: str
    commands: list[str]
    outputs: list[str]
    status: str
    notes: list[str]


def build_workbench_plan(root: Path = ROOT) -> dict:
    """Return a serializable phase-4 research workbench plan."""
    research_dir = root / "research"
    stages = [
        _stage(
            root,
            stage_id="data_layer",
            title="数据处理",
            purpose="构建 research/data/panel.sqlite3，保持 PIT 和生产库隔离。",
            commands=[
                "python research/feasibility_smoke.py",
                "python research/etl_universe.py",
                "python research/etl_calendar.py",
                "python research/etl_prices.py",
                "python research/etl_fundamentals.py",
                "python research/etl_index.py",
                "python research/data_quality_report.py",
            ],
            outputs=[
                "research/data/panel.sqlite3",
                "research/out/phase_b_quality.md",
                "research/out/phase_b_quality.json",
            ],
            notes=[
                "只写 research/data 与 research/out。",
                "live 数据源风险只记录为研究风险，不作为商业包逻辑失败。",
            ],
        ),
        _stage(
            root,
            stage_id="factor_diagnostics",
            title="因子诊断",
            purpose="按 horizon、行业/规模中性化和 IC 衰减检查候选因子。",
            commands=[
                "python research/factor_ic.py",
                "python research/hs300_relative_report.py",
            ],
            outputs=[
                "research/out/factor_ic.md",
                "research/out/factor_ic.json",
                "research/out/hs300_relative.md",
            ],
            notes=[
                "诊断结论是 exploratory，不支持 alpha 或胜率营销表述。",
            ],
        ),
        _stage(
            root,
            stage_id="template_diagnostics",
            title="模板重叠与参数敏感性",
            purpose="保留八个模板候选池的两两重叠，并完整记录参数尝试；holdout 与选择过程隔离。",
            commands=[
                "python research/template_diagnostics.py <input.json>",
            ],
            outputs=[
                "research/out/template_diagnostics.json",
                "research/out/template_diagnostics.md",
            ],
            notes=[
                "输入必须包含 candidate_pools、attempts、holdout_attempt_ids；不得从 holdout 选择参数。",
                "结果只用于研究比较，不会改写 Qt active 公式或模板契约。",
            ],
        ),
        _stage(
            root,
            stage_id="backtest_analysis",
            title="回测分析",
            purpose="使用预注册权重做批量研究、HS300/等权对照和归因复盘。",
            commands=[
                "python research/backtest_report.py",
                "python research/attribution_postmortem.py",
            ],
            outputs=[
                "research/out/backtest_report.md",
                "research/out/backtest_report.json",
                "research/out/attribution_postmortem.md",
            ],
            notes=[
                "默认不运行 one-shot holdout；holdout 需要显式 --run-holdout。",
            ],
        ),
        _stage(
            root,
            stage_id="ml_candidate_shadow",
            title="ML 候选与 shadow",
            purpose="在研究侧生成 v2 诊断和稳健性报告，不能直接改 active 专业公式。",
            commands=[
                "python research/v2_experiment.py --mode train",
                "python research/v2_experiment.py --mode validate",
                "python research/rigor_battery.py",
            ],
            outputs=[
                "research/out/v2_train_diagnostic.md",
                "research/out/v2_validate_diagnostic.md",
                "research/out/v2_development_acceptance.md",
                "research/out/v2_shrunk_default.json",
            ],
            notes=[
                "训练/验证最大日期由 v2_experiment 边界强制控制。",
                "输出只能作为 shadow 或解释性排序研究材料。",
            ],
        ),
        _stage(
            root,
            stage_id="oracle_crosschecks",
            title="可选外部 oracle 对标",
            purpose="在研究侧用可选包做交叉核验，缺包时应标记 SKIPPED。",
            commands=[
                "python research/_alphalens_crosscheck.py",
                "python research/_skfolio_cpcv_crosscheck.py",
                "python research/_arch_bootstrap_crosscheck.py",
            ],
            outputs=[
                "research/out/alphalens_crosscheck.md",
                "research/out/skfolio_cpcv_crosscheck.json",
                "research/out/arch_bootstrap_crosscheck.json",
            ],
            notes=[
                "oracle 是开发交叉检查，不是商业主链路依赖。",
            ],
        ),
    ]
    gate = commercial_dependency_gate(root)
    return {
        "schema_version": 1,
        "generated_at": _now(),
        "scope": "research_only",
        "research_dir": str(research_dir.relative_to(root)),
        "commercial_chain": {
            "imports_stock_tool_runtime": False,
            "package_inclusion": "excluded_from_default_pyinstaller_specs",
            "trade_execution": False,
            "ai_direct_activation": False,
        },
        "reference_architectures": REFERENCE_ARCHITECTURES,
        "risk_gate": gate,
        "stages": [asdict(stage) for stage in stages],
        "next_actions": _next_actions(stages, gate),
    }


def commercial_dependency_gate(root: Path = ROOT) -> dict:
    """Check that high-risk reference dependencies are not in main manifests."""
    scanned = [
        root / "requirements.txt",
        root / "requirements-optional.txt",
        root / "build.spec",
        root / "build_qt.spec",
    ]
    findings: list[dict] = []
    for path in scanned:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for package, reason in FORBIDDEN_COMMERCIAL_DEPENDENCIES.items():
            pattern = rf"(^|[^a-zA-Z0-9_.-]){re.escape(package)}([^a-zA-Z0-9_.-]|$)"
            if re.search(pattern, text, flags=re.IGNORECASE):
                findings.append(
                    {
                        "path": str(path.relative_to(root)),
                        "dependency": package,
                        "reason": reason,
                    }
                )
    return {
        "passed": not findings,
        "forbidden_main_dependencies": findings,
        "scanned": [str(path.relative_to(root)) for path in scanned if path.exists()],
        "research_requirements": "research/requirements-research.txt",
    }


def render_markdown(plan: dict) -> str:
    lines = [
        "# Stocklens Phase 4 轻量研究台",
        "",
        f"- 生成时间：{plan['generated_at']}",
        f"- 范围：`{plan['scope']}`",
        "- 定位：研究候选、观察验证和复盘辅助；不输出交易执行或收益承诺。",
        f"- 商业依赖闸门：{'通过' if plan['risk_gate']['passed'] else '阻断'}",
        "",
        "## 外部参照边界",
    ]
    for item in plan["reference_architectures"]:
        lines.append(f"- {item['name']}：{item['use']}；{item['adoption']}")
    lines.extend(["", "## 研究阶段"])
    for stage in plan["stages"]:
        lines.extend(
            [
                "",
                f"### {stage['title']}",
                f"- id：`{stage['id']}`",
                f"- 状态：`{stage['status']}`",
                f"- 目的：{stage['purpose']}",
                f"- 命令：{'; '.join(f'`{cmd}`' for cmd in stage['commands'])}",
                f"- 产物：{'; '.join(f'`{out}`' for out in stage['outputs'])}",
            ]
        )
        for note in stage["notes"]:
            lines.append(f"- 说明：{note}")
    lines.extend(["", "## 下一步"])
    for action in plan["next_actions"]:
        lines.append(f"- {action}")
    return "\n".join(lines) + "\n"


def _stage(
    root: Path,
    *,
    stage_id: str,
    title: str,
    purpose: str,
    commands: list[str],
    outputs: list[str],
    notes: list[str],
) -> ResearchStage:
    missing_scripts = [
        command.split()[1]
        for command in commands
        if len(command.split()) >= 2 and not (root / command.split()[1]).exists()
    ]
    existing_outputs = [output for output in outputs if (root / output).exists()]
    if missing_scripts:
        status = "blocked_missing_scripts"
    elif len(existing_outputs) == len(outputs):
        status = "ready_with_artifacts"
    elif existing_outputs:
        status = "ready_partial_artifacts"
    else:
        status = "ready_no_artifacts"
    return ResearchStage(
        id=stage_id,
        title=title,
        purpose=purpose,
        commands=commands,
        outputs=outputs,
        status=status,
        notes=notes,
    )


def _next_actions(stages: list[ResearchStage], gate: dict) -> list[str]:
    if not gate["passed"]:
        return ["先移除商业主链路中的高风险研究依赖，再运行研究台。"]
    missing = [stage.title for stage in stages if stage.status == "ready_no_artifacts"]
    if missing:
        return [f"优先补齐研究产物：{', '.join(missing)}。"]
    partial = [stage.title for stage in stages if stage.status == "ready_partial_artifacts"]
    if partial:
        return [f"继续补齐部分研究产物：{', '.join(partial)}。"]
    return ["所有研究阶段已有产物，可按需刷新因子诊断、回测分析或 v2 shadow 报告。"]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", default="", help="Write the rendered workbench plan to this path.")
    parser.add_argument("--strict", action="store_true", help="Fail when the commercial dependency gate fails.")
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    plan = build_workbench_plan()
    if args.format == "json":
        rendered = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    else:
        rendered = render_markdown(plan)
    if args.output:
        output_path = _resolve_research_output(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if args.strict and not plan["risk_gate"]["passed"]:
        return 2
    return 0


def _resolve_research_output(value: str) -> Path:
    output_path = Path(value)
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    resolved = output_path.resolve()
    research_root = RESEARCH_DIR.resolve()
    if resolved != research_root and research_root not in resolved.parents:
        raise ValueError(f"research workbench output must stay under {research_root}")
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
