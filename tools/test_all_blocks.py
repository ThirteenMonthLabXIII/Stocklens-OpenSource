from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BLOCK_TIMEOUT_SECONDS = 300
@dataclass(frozen=True)
class BlockResult:
    name: str
    command: list[str]
    elapsed_seconds: float


QUICK_BLOCKS = [
    ("核心基础区块", [sys.executable, "tools/test_core_blocks.py"]),
    ("阶段0证据契约区块", [sys.executable, "tools/test_phase0_contracts.py"]),
    ("阶段0.5选股规则区块", [sys.executable, "tools/test_phase0_5_selection_policy.py"]),
    ("阶段1确定性候选层区块", [sys.executable, "tools/test_phase1_candidate_layer.py"]),
    ("阶段2证据包与事实引用区块", [sys.executable, "tools/test_phase2_evidence_package.py"]),
    ("阶段3基本面 Agent 区块", [sys.executable, "tools/test_phase3_fundamental_agent.py"]),
    ("阶段4五 Agent 编排区块", [sys.executable, "tools/test_phase4_multi_agent.py"]),
    ("阶段5证据汇总与候选状态区块", [sys.executable, "tools/test_phase5_evidence_summary.py"]),
    ("阶段7评价离线验收区块", [sys.executable, "tools/test_phase7_evaluation.py"]),
    ("不可变数据快照契约区块", [sys.executable, "tools/test_data_snapshot.py"]),
    ("AI研究讨论安全区块", [sys.executable, "tools/test_research_discussion.py"]),
    ("Qt研究AI边界区块", [sys.executable, "tools/test_qt_research_ai_boundary.py"]),
    ("正式筛选凭证区块", [sys.executable, "tools/test_formal_screening_certificate.py"]),
    ("事实级质量聚合区块", [sys.executable, "tools/test_fact_quality_aggregation.py"]),
    ("自选优先数据刷新区块", [sys.executable, "tools/test_data_refresh_coordinator.py"]),
    ("功能数据就绪区块", [sys.executable, "tools/test_data_readiness.py"]),
    ("评分契约区块", [sys.executable, "tools/test_professional_scoring_contract.py"]),
    ("候选画像区块", [sys.executable, "tools/test_selection_profile.py"]),
    ("闭环MVP区块", [sys.executable, "tools/test_performance_loop.py"]),
    ("PIT历史筛选适配器区块", [sys.executable, "tools/test_pit_screener_adapter.py"]),
    ("研究实验身份区块", [sys.executable, "-m", "research.test_v2_boundary"]),
    ("实验清单身份区块", [sys.executable, "-m", "research.test_experiment_manifest"]),
    ("自适应多因子基线区块", [sys.executable, "-m", "research.test_adaptive_baseline"]),
    ("事件语义回测区块", [sys.executable, "-m", "research.test_event_backtest"]),
    ("执行账本区块", [sys.executable, "-m", "research.test_execution_ledger"]),
    ("研究严谨度闸门区块", [sys.executable, "-m", "research.test_research_rigor_gate"]),
    ("独立研究严谨度验收", [sys.executable, "tools/research_rigor_acceptance.py"]),
    ("核心工作流一致性区块", [sys.executable, "tools/test_frontend_consistency.py"]),
    ("P1共享工作流闸门", [sys.executable, "tools/test_frontend_p1_gate.py"]),
    ("前端功能合同区块", [sys.executable, "tools/test_frontend_parity_contract.py"]),
    ("模板重叠与参数敏感性区块", [sys.executable, "-m", "research.test_template_diagnostics"]),
    ("Qt迁移P0验收区块", [sys.executable, "tools/test_qt_p0_acceptance.py"]),
    ("价格预警区块", [sys.executable, "tools/test_price_alert.py"]),
    ("共享预警服务区块", [sys.executable, "tools/test_alert_service.py"]),
    ("免费行情准实时预警区块", [sys.executable, "tools/test_alert_monitor.py"]),
    ("共享复盘服务区块", [sys.executable, "tools/test_review_workflow.py"]),
    ("本地自动复盘区块", [sys.executable, "tools/test_review_automation.py"]),
    ("Qt预警真实点击区块", [sys.executable, "tools/test_qt_alert_interactions.py"]),
    ("Qt筛选真实点击区块", [sys.executable, "tools/test_qt_screening_interactions.py"]),
    ("P2研究筛选策略闸门", [sys.executable, "tools/test_frontend_p2_gate.py"]),
    ("P3复盘与公式治理闸门", [sys.executable, "tools/test_frontend_p3_gate.py"]),
    ("P4设置数据导出验收闸门", [sys.executable, "tools/test_frontend_p4_gate.py"]),
    ("P5默认Qt启动闸门", [sys.executable, "tools/test_frontend_p5_gate.py"]),
    ("P6正式Qt构建静态闸门", [sys.executable, "tools/test_frontend_p6_gate.py"]),
    ("Qt正式导航表面区块", [sys.executable, "tools/test_qt_default_surface.py"]),
    ("Qt情报栏布局区块", [sys.executable, "tools/test_qt_intelligence_layout.py"]),
    ("Qt观察复盘真实点击区块", [sys.executable, "tools/test_qt_tracking_review.py"]),
    ("Qt阶段7系统评价区块", [sys.executable, "tools/test_qt_evaluation_review.py"]),
    ("Qt Dashboard区块", [sys.executable, "tools/test_qt_dashboard.py"]),
    ("Qt P4设置数据与 PIT 验收区块", [sys.executable, "tools/test_qt_p4_acceptance.py"]),
    ("Qt工作台Smoke区块", [sys.executable, "tools/test_qt_workbench_smoke.py"]),
    ("编译导入区块", [sys.executable, "-m", "compileall", "stock_tool", "tools"]),
]


FULL_BLOCKS = [
    ("MVP闭集生产链路", [sys.executable, "tools/test_mvp_closed_set.py"]),
    ("阶段0证据契约区块", [sys.executable, "tools/test_phase0_contracts.py"]),
    ("阶段0.5选股规则区块", [sys.executable, "tools/test_phase0_5_selection_policy.py"]),
    ("阶段1确定性候选层区块", [sys.executable, "tools/test_phase1_candidate_layer.py"]),
    ("阶段2证据包与事实引用区块", [sys.executable, "tools/test_phase2_evidence_package.py"]),
    ("阶段3基本面 Agent 区块", [sys.executable, "tools/test_phase3_fundamental_agent.py"]),
    ("阶段4五 Agent 编排区块", [sys.executable, "tools/test_phase4_multi_agent.py"]),
    ("阶段5证据汇总与候选状态区块", [sys.executable, "tools/test_phase5_evidence_summary.py"]),
    ("阶段7评价离线验收区块", [sys.executable, "tools/test_phase7_evaluation.py"]),
    ("不可变数据快照契约区块", [sys.executable, "tools/test_data_snapshot.py"]),
    ("AI研究讨论安全区块", [sys.executable, "tools/test_research_discussion.py"]),
    ("Qt研究AI边界区块", [sys.executable, "tools/test_qt_research_ai_boundary.py"]),
    ("正式筛选凭证区块", [sys.executable, "tools/test_formal_screening_certificate.py"]),
    ("事实级质量聚合区块", [sys.executable, "tools/test_fact_quality_aggregation.py"]),
    ("核心基础区块", [sys.executable, "tools/test_core_blocks.py"]),
    ("自选优先数据刷新区块", [sys.executable, "tools/test_data_refresh_coordinator.py"]),
    ("功能数据就绪区块", [sys.executable, "tools/test_data_readiness.py"]),
    ("个股情报工作台图表区块", [sys.executable, "tools/test_chart_workbench.py"]),
    ("自然语言策略区块", [sys.executable, "tools/test_nl_query.py"]),
    ("技术因子筛选区块", [sys.executable, "tools/test_screener_technical_filters.py"]),
    ("筛选预设卡片区块", [sys.executable, "tools/test_screener_presets.py"]),
    ("模板重叠与参数敏感性区块", [sys.executable, "-m", "research.test_template_diagnostics"]),
    ("筛选资金流降级区块", [sys.executable, "tools/test_screener_money_flow.py"]),
    ("评分契约区块", [sys.executable, "tools/test_professional_scoring_contract.py"]),
    ("候选画像区块", [sys.executable, "tools/test_selection_profile.py"]),
    ("东方财富网关抗限流区块", [sys.executable, "tools/test_eastmoney_gateway.py"]),
    ("逐只主力资金多日窗口区块", [sys.executable, "tools/test_money_flow_window.py"]),
    ("PIT点时数据管线区块", [sys.executable, "tools/test_pit_pipeline.py"]),
    ("PIT历史筛选适配器区块", [sys.executable, "tools/test_pit_screener_adapter.py"]),
    ("研究实验身份区块", [sys.executable, "-m", "research.test_v2_boundary"]),
    ("实验清单身份区块", [sys.executable, "-m", "research.test_experiment_manifest"]),
    ("自适应多因子基线区块", [sys.executable, "-m", "research.test_adaptive_baseline"]),
    ("事件语义回测区块", [sys.executable, "-m", "research.test_event_backtest"]),
    ("执行账本区块", [sys.executable, "-m", "research.test_execution_ledger"]),
    ("研究严谨度闸门区块", [sys.executable, "-m", "research.test_research_rigor_gate"]),
    ("独立研究严谨度验收", [sys.executable, "tools/research_rigor_acceptance.py"]),
    ("V1因子库区块", [sys.executable, "tools/test_v1_factor_store.py"]),
    ("V1策略保存区块", [sys.executable, "tools/test_strategy_store.py"]),
    ("闭环MVP区块", [sys.executable, "tools/test_performance_loop.py"]),
    ("核心工作流一致性区块", [sys.executable, "tools/test_frontend_consistency.py"]),
    ("前端功能合同区块", [sys.executable, "tools/test_frontend_parity_contract.py"]),
    ("Qt迁移P0验收区块", [sys.executable, "tools/test_qt_p0_acceptance.py"]),
    ("价格预警区块", [sys.executable, "tools/test_price_alert.py"]),
    ("共享预警服务区块", [sys.executable, "tools/test_alert_service.py"]),
    ("免费行情准实时预警区块", [sys.executable, "tools/test_alert_monitor.py"]),
    ("共享复盘服务区块", [sys.executable, "tools/test_review_workflow.py"]),
    ("本地自动复盘区块", [sys.executable, "tools/test_review_automation.py"]),
    ("Qt预警真实点击区块", [sys.executable, "tools/test_qt_alert_interactions.py"]),
    ("Qt筛选真实点击区块", [sys.executable, "tools/test_qt_screening_interactions.py"]),
    ("P2研究筛选策略闸门", [sys.executable, "tools/test_frontend_p2_gate.py"]),
    ("P3复盘与公式治理闸门", [sys.executable, "tools/test_frontend_p3_gate.py"]),
    ("P4设置数据导出验收闸门", [sys.executable, "tools/test_frontend_p4_gate.py"]),
    ("P5默认Qt启动闸门", [sys.executable, "tools/test_frontend_p5_gate.py"]),
    ("P6正式Qt构建静态闸门", [sys.executable, "tools/test_frontend_p6_gate.py"]),
    ("Qt正式导航表面区块", [sys.executable, "tools/test_qt_default_surface.py"]),
    ("Qt情报栏布局区块", [sys.executable, "tools/test_qt_intelligence_layout.py"]),
    ("Qt观察复盘真实点击区块", [sys.executable, "tools/test_qt_tracking_review.py"]),
    ("Qt阶段7系统评价区块", [sys.executable, "tools/test_qt_evaluation_review.py"]),
    ("Qt P4设置数据与 PIT 验收区块", [sys.executable, "tools/test_qt_p4_acceptance.py"]),
    ("第七阶段复盘中心区块", [sys.executable, "tools/test_phase7_replay.py"]),
    ("第八阶段Polish区块", [sys.executable, "tools/test_phase8_polish.py"]),
    ("第九阶段交付区块", [sys.executable, "tools/test_phase9_delivery.py"]),
    ("V3 Nuitka交付区块", [sys.executable, "tools/test_v3_delivery.py"]),
    ("V3发布证据Schema区块", [sys.executable, "tools/test_release_schema.py"]),
    ("V3发布安全工具区块", [sys.executable, "tools/test_release_security_tools.py"]),
    ("V3发布签名区块", [sys.executable, "tools/test_release_signing.py"]),
    ("第四阶段轻量研究台区块", [sys.executable, "tools/test_research_workbench.py"]),
    ("通知推送区块", [sys.executable, "tools/test_notify_push.py"]),
    ("第六阶段商业工作台区块", [sys.executable, "tools/test_phase6_workbench.py"]),
    ("规模筛选区块", [sys.executable, "tools/test_scale_screener.py", "--sizes", "300,1000,5000"]),
    ("策略压力区块", [sys.executable, "tools/test_strategy_stress.py", "--presets", "300", "--runs", "1000"]),
    ("AI异常外部服务区块", [sys.executable, "tools/test_ai_failure_modes.py"]),
    ("长期运行循环区块", [sys.executable, "tools/test_soak_runner.py", "--cycles", "20"]),
    ("Qt Dashboard区块", [sys.executable, "tools/test_qt_dashboard.py"]),
    ("Qt工作台Smoke区块", [sys.executable, "tools/test_qt_workbench_smoke.py"]),
    ("编译导入区块", [sys.executable, "-m", "compileall", "stock_tool", "tools"]),
]


LIVE_BLOCK = ("实时抓取小样本区块", [sys.executable, "tools/test_fetch_success_rate.py", "--limit", "3", "--days", "30"])


def run_block(name: str, command: list[str], timeout_seconds: int) -> BlockResult:
    print(f"\n========== {name} ==========", flush=True)
    print(" ".join(command), flush=True)
    start = time.perf_counter()
    try:
        subprocess.run(command, cwd=ROOT, check=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start
        print(f"[TIMEOUT] {name} elapsed={elapsed:.3f}s limit={timeout_seconds}s", flush=True)
        raise
    except subprocess.CalledProcessError as exc:
        elapsed = time.perf_counter() - start
        print(f"[FAIL] {name} exit={exc.returncode} elapsed={elapsed:.3f}s", flush=True)
        raise
    elapsed = time.perf_counter() - start
    print(f"[PASS] {name} elapsed={elapsed:.3f}s", flush=True)
    return BlockResult(name=name, command=command, elapsed_seconds=elapsed)


def print_timing_summary(results: list[BlockResult]) -> None:
    if not results:
        return
    total_elapsed = sum(result.elapsed_seconds for result in results)
    print("\n========== 区块耗时统计 ==========", flush=True)
    print(f"总耗时：{total_elapsed:.3f}s", flush=True)
    print("耗时倒序：", flush=True)
    for index, result in enumerate(sorted(results, key=lambda item: item.elapsed_seconds, reverse=True), start=1):
        print(f"{index}. {result.name}: {result.elapsed_seconds:.3f}s", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="按区块运行 A股选股仪 测试。")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--quick", action="store_true", help="运行日常核心离线闭环。")
    mode_group.add_argument("--full", action="store_true", help="运行完整离线验收；默认值。")
    parser.add_argument(
        "--live",
        action="store_true",
        help="运行真实行情源小样本风险检查；与 --quick/--full 组合时追加到离线验收后。",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_BLOCK_TIMEOUT_SECONDS,
        help=f"单个区块外部进程超时秒数，默认 {DEFAULT_BLOCK_TIMEOUT_SECONDS}s。",
    )
    args = parser.parse_args()

    live_only = args.live and not args.quick and not args.full
    if live_only:
        blocks = [LIVE_BLOCK]
    else:
        blocks = list(QUICK_BLOCKS if args.quick else FULL_BLOCKS)
    if args.live and not live_only:
        blocks.append(LIVE_BLOCK)

    mode = "live" if live_only else ("quick" if args.quick else "full")
    print(f"验收模式：{mode}", flush=True)
    print(f"单区块外部进程超时：{args.timeout_seconds}s", flush=True)
    if args.live:
        print("Live 规则：实时小样本需显式开启，受网络和公开接口影响。", flush=True)

    results: list[BlockResult] = []
    try:
        for name, command in blocks:
            results.append(run_block(name, command, args.timeout_seconds))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print_timing_summary(results)
        print("\n[FAIL] 区块测试未通过", flush=True)
        return 1

    print_timing_summary(results)
    print("\n[OK] 所有区块测试通过", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
