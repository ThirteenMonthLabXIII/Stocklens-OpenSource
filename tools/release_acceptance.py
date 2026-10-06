from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "docs" / "acceptance_reports"
BLOCK_TIMEOUT_SECONDS = 300


OFFLINE_BLOCKS = [
    (
        "离线稳定回归验收",
        [
            sys.executable,
            "tools/test_all_blocks.py",
            "--full",
            "--timeout-seconds",
            str(BLOCK_TIMEOUT_SECONDS),
        ],
    ),
]

LIVE_BLOCK = ("实时小样本", [sys.executable, "tools/test_fetch_success_rate.py", "--limit", "3", "--days", "30"])


def _normalize_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _join_output(stdout: object, stderr: object) -> str:
    output = _normalize_output(stdout)
    error_output = _normalize_output(stderr)
    if error_output:
        output += ("\n" if output else "") + error_output
    return output.strip()


def _run_block(name: str, command: list[str]) -> tuple[bool, float, str]:
    start = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    elapsed = time.perf_counter() - start
    output = _join_output(completed.stdout, completed.stderr)
    return completed.returncode == 0, elapsed, output


def _write_report(lines: list[str], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json_report(payload: dict, report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="商业版发版前验收。默认只跑离线验收；--live 单独开启实时小样本。")
    parser.add_argument("--live", action="store_true", help="额外运行真实行情源小样本测试，受网络和公开接口影响。")
    parser.add_argument("--report", default="", help="自定义验收报告输出路径。")
    parser.add_argument("--json-report", default="", help="自定义 JSON 验收报告输出路径。")
    args = parser.parse_args()

    blocks = list(OFFLINE_BLOCKS)
    if args.live:
        blocks.append(LIVE_BLOCK)

    started_at = datetime.now().isoformat(timespec="seconds")
    report_path = Path(args.report) if args.report else REPORT_DIR / f"release_acceptance_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    json_report_path = (
        Path(args.json_report)
        if args.json_report
        else REPORT_DIR / f"release_acceptance_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    lines = [
        "Stocklens 离线回归验收报告",
        f"开始时间：{started_at}",
        f"实时小样本：{'开启' if args.live else '未开启'}",
        "验收口径：离线稳定回归验收，调用 python tools/test_all_blocks.py --full，包含第四阶段轻量研究台隔离验收。",
        f"超时口径：test_all_blocks.py 内部按单区块 {BLOCK_TIMEOUT_SECONDS}s 超时；release_acceptance.py 不设置整套总超时。",
        "口径说明：离线回归通过不代表允许外发；外发仍受候选哈希、P7、合同和最终闸门阻断。",
        "Live 规则：实时小样本失败只记录风险，不阻断离线发版。",
        "",
    ]
    json_payload = {
        "title": "Stocklens 离线回归验收报告",
        "started_at": started_at,
        "finished_at": "",
        "live_enabled": bool(args.live),
        "live_status": "not_run",
        "acceptance_scope": "offline_stable_regression_with_research_workbench_gate",
        "block_timeout_seconds": BLOCK_TIMEOUT_SECONDS,
        "failed": False,
        "conclusion": "",
        "txt_report": str(report_path),
        "json_report": str(json_report_path),
        "blocks": [],
    }

    failed = False
    total_elapsed = 0.0
    for name, command in blocks:
        print(f"\n========== {name} ==========")
        print(" ".join(command))
        ok, elapsed, output = _run_block(name, command)
        is_live_block = name == LIVE_BLOCK[0]
        status = "PASS" if ok else ("RISK" if is_live_block else "FAIL")
        total_elapsed += elapsed
        print(output)
        print(f"[{status}] {name} elapsed={elapsed:.3f}s")
        lines.extend(
            [
                f"========== {name} ==========",
                "命令：" + " ".join(command),
                f"结果：{status}",
                f"耗时：{elapsed:.3f}s",
                output,
                "",
            ]
        )
        json_payload["blocks"].append(
            {
                "name": name,
                "command": command,
                "status": status,
                "elapsed_seconds": round(elapsed, 6),
                "output_tail": output[-4000:],
            }
        )
        if is_live_block and not ok:
            json_payload["live_status"] = "risk"
            continue
        if is_live_block and ok:
            json_payload["live_status"] = "pass"
        if not ok:
            failed = True
            break

    finished_at = datetime.now().isoformat(timespec="seconds")
    lines.append(f"结束时间：{finished_at}")
    lines.append("结论：" + ("离线回归失败，不允许进入候选打包" if failed else "离线回归通过，不代表允许外发"))
    json_payload["finished_at"] = finished_at
    json_payload["failed"] = failed
    json_payload["conclusion"] = "离线回归失败，不允许进入候选打包" if failed else "离线回归通过，不代表允许外发"
    if not args.live:
        json_payload["live_status"] = "not_run"
    json_payload["elapsed_seconds"] = round(total_elapsed, 6)
    _write_report(lines, report_path)
    _write_json_report(json_payload, json_report_path)
    print(f"\n验收报告：{report_path}")
    print(f"JSON验收报告：{json_report_path}")
    if failed:
        print("[FAIL] 发版验收未通过")
        return 1
    print("[OK] 离线回归通过；不代表允许外发")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
