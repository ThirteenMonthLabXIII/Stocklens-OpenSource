from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.research_workbench import build_workbench_plan, commercial_dependency_gate, render_markdown


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_plan_contract() -> None:
    plan = build_workbench_plan(ROOT)
    _assert(plan["scope"] == "research_only", "research workbench must stay research-only")
    _assert(plan["commercial_chain"]["trade_execution"] is False, "research workbench must not expose trading")
    _assert(plan["commercial_chain"]["ai_direct_activation"] is False, "research workbench must not activate AI weights")
    _assert(plan["risk_gate"]["passed"], f"commercial dependency gate failed: {plan['risk_gate']}")
    _assert(len(plan["stages"]) >= 4, "phase 4 needs data/factor/backtest/shadow stages")
    stage_ids = {stage["id"] for stage in plan["stages"]}
    for expected in {"data_layer", "factor_diagnostics", "backtest_analysis", "ml_candidate_shadow"}:
        _assert(expected in stage_ids, f"missing stage {expected}")
    for stage in plan["stages"]:
        for command in stage["commands"]:
            _assert(command.startswith("python research/"), f"non-research command leaked: {command}")
        for output in stage["outputs"]:
            _assert(output.startswith("research/"), f"non-research output leaked: {output}")
    for ref in plan["reference_architectures"]:
        _assert(ref["adoption"].startswith("reference_only"), f"external reference not isolated: {ref}")


def test_commercial_dependency_gate_scans_main_manifests() -> None:
    gate = commercial_dependency_gate(ROOT)
    scanned = set(gate["scanned"])
    _assert("requirements.txt" in scanned, "main requirements must be scanned")
    _assert("build.spec" in scanned, "default pyinstaller spec must be scanned")
    _assert("research/requirements-research.txt" == gate["research_requirements"], "research deps must remain isolated")
    _assert(not gate["forbidden_main_dependencies"], f"forbidden deps found: {gate['forbidden_main_dependencies']}")


def test_render_and_cli_json() -> None:
    plan = build_workbench_plan(ROOT)
    text = render_markdown(plan)
    _assert("轻量研究台" in text, "markdown title missing")
    _assert("商业依赖闸门" in text, "risk gate summary missing")
    proc = subprocess.run(
        [sys.executable, "research/research_workbench.py", "--format", "json", "--strict"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    payload = json.loads(proc.stdout)
    _assert(payload["scope"] == "research_only", "cli json scope mismatch")
    _assert(payload["risk_gate"]["passed"], "cli strict should pass")


def test_cli_can_persist_research_artifact() -> None:
    out_path = ROOT / "research" / "out" / "_test_research_workbench.md"
    if out_path.exists():
        out_path.unlink()
    try:
        subprocess.run(
            [
                sys.executable,
                "research/research_workbench.py",
                "--output",
                str(out_path.relative_to(ROOT)),
                "--strict",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        _assert(out_path.exists(), "research workbench artifact was not written")
        text = out_path.read_text(encoding="utf-8")
        _assert("Stocklens Phase 4" in text, "persisted artifact title missing")
        _assert("商业依赖闸门：通过" in text, "persisted artifact gate summary missing")
    finally:
        if out_path.exists():
            out_path.unlink()


def test_cli_rejects_non_research_output() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "research/research_workbench.py",
            "--output",
            "docs/_bad_research_workbench.md",
            "--strict",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _assert(completed.returncode != 0, "cli must reject outputs outside research/")
    _assert("output must stay under" in completed.stderr, "rejection reason missing")


def run_tests() -> None:
    test_plan_contract()
    test_commercial_dependency_gate_scans_main_manifests()
    test_render_and_cli_json()
    test_cli_can_persist_research_artifact()
    test_cli_rejects_non_research_output()
    print("test_research_workbench passed")


if __name__ == "__main__":
    run_tests()
