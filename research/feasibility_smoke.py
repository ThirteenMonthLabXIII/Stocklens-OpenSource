from __future__ import annotations

import argparse
import importlib.metadata
import json
import multiprocessing as mp
import queue
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.sample_universe import all_sample
from research.sources import ak_probe, baostock_probe, tushare_probe
from research.sources.common import json_safe, now_iso, unavailable_gates


OUT_DIR = ROOT / "research" / "out"
PROBES = {
    "akshare": ak_probe.run_probe,
    "baostock": baostock_probe.run_probe,
    "tushare": tushare_probe.run_probe,
}
GATE_NAMES = {
    "G1": "复权价",
    "G2": "点时财务",
    "G3": "点时成分",
    "G4": "行业",
    "G5": "吞吐",
    "G6": "历史估值",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase A feasibility smoke probes.")
    parser.add_argument("--source", choices=[*PROBES.keys(), "all"], default="all")
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    args = parser.parse_args()

    payload = run_smoke(source=args.source, timeout_sec=args.timeout_sec, out_dir=OUT_DIR, argv=sys.argv[1:])
    print(f"wrote {OUT_DIR / 'feasibility_report.md'}")
    print(f"phase_b_gate={payload['phase_b_gate']['status']}")
    print(f"commercial_gate={payload['commercial_gate']['status']}")
    return 0


def run_smoke(
    source: str = "all",
    timeout_sec: float = 30.0,
    out_dir: Path | None = None,
    argv: list[str] | None = None,
) -> dict[str, Any]:
    out_dir = out_dir or OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    db_before = _production_db_snapshot()
    sample = all_sample()
    selected = PROBES if source == "all" else {source: PROBES[source]}

    results = []
    for name, runner in selected.items():
        results.append(_run_probe(name, runner, sample, timeout_sec))

    db_after = _production_db_snapshot()
    db_unchanged = db_before == db_after
    payload = {
        "generated_at": now_iso(),
        "root": str(ROOT),
        "environment": _environment_snapshot(source, timeout_sec, argv),
        "sample_size": len(sample),
        "sources": json_safe(results),
        "gate_summary": _summarize_gates(results),
        "phase_b_gate": None,
        "commercial_gate": None,
        "production_db_snapshot_unchanged": db_unchanged,
        "production_db_files_observed": sorted(db_after.keys()),
    }
    payload["phase_b_gate"] = _phase_b_gate(payload["gate_summary"])
    payload["commercial_gate"] = _commercial_gate(payload["gate_summary"])

    raw_columns = {
        result["source"]: result.get("raw_columns", {})
        for result in results
    }

    (out_dir / "feasibility_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "raw_columns_dump.json").write_text(json.dumps(json_safe(raw_columns), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "feasibility_report.md").write_text(_render_report(payload), encoding="utf-8")
    return payload


def _environment_snapshot(source: str, timeout_sec: float, argv: list[str] | None) -> dict[str, Any]:
    packages = {}
    for name in ("python", "pandas", "akshare", "baostock", "tushare", "requests"):
        if name == "python":
            packages[name] = sys.version.split()[0]
            continue
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "not installed"
    return {
        "python_executable": sys.executable,
        "python_version": sys.version.replace("\n", " "),
        "argv": argv or [],
        "params": {"source": source, "timeout_sec": timeout_sec},
        "package_versions": packages,
        "proxy_bypass": "AkShare/公开 HTTP probe 会临时设置 NO_PROXY=*；baostock/Tushare 使用各自 SDK 默认连接。",
    }


def _probe_worker(source: str, sample: list[Any], timeout_sec: float, out: mp.Queue) -> None:
    try:
        out.put(("ok", PROBES[source](sample=sample, timeout_sec=timeout_sec)))
    except BaseException as exc:  # noqa: BLE001 - probe workers must report all failures
        out.put(("error", str(exc)))


def _run_probe(name: str, runner, sample: list[Any], timeout_sec: float) -> dict[str, Any]:
    if name != "baostock":
        return runner(sample=sample, timeout_sec=timeout_sec)
    ctx = mp.get_context("spawn")
    out: mp.Queue = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_probe_worker, args=(name, sample, timeout_sec, out))
    proc.start()
    proc.join(max(5.0, timeout_sec))
    if proc.is_alive():
        proc.terminate()
        proc.join(3)
        return _probe_unreachable(name, f"baostock probe watchdog timeout after {timeout_sec:.1f}s")
    try:
        kind, payload = out.get_nowait()
    except queue.Empty:
        return _probe_unreachable(name, "baostock probe returned no result")
    if kind == "error":
        return _probe_unreachable(name, str(payload))
    return payload


def _probe_unreachable(source: str, reason: str) -> dict[str, Any]:
    return {
        "source": source,
        "available": True,
        "gates": unavailable_gates("SOURCE_UNREACHABLE", reason),
        "calls": [
            {
                "ok": False,
                "label": "probe watchdog",
                "duration_sec": 0,
                "error": reason,
                "error_class": "SOURCE_UNREACHABLE",
                "status": "SOURCE_UNREACHABLE",
            }
        ],
        "raw_columns": {},
    }


def _production_db_snapshot() -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for pattern in ("*.sqlite", "*.sqlite3", "*.db"):
        for path in ROOT.rglob(pattern):
            if "research" in path.relative_to(ROOT).parts:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files[str(path)] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
    return files


def _summarize_gates(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for gate_id in GATE_NAMES:
        source_rows = []
        for result in results:
            gate_result = result.get("gates", {}).get(gate_id, {})
            source_rows.append(
                {
                    "source": result.get("source"),
                    "status": gate_result.get("status", "UNTESTED"),
                    "summary": gate_result.get("summary", ""),
                }
            )
        status = _combine_status([row["status"] for row in source_rows])
        recommended = [
            row["source"]
            for row in source_rows
            if row["status"] == ("GO" if status == "GO" else "WORKAROUND")
        ]
        summary[gate_id] = {
            "name": GATE_NAMES[gate_id],
            "status": status,
            "recommended_sources": recommended,
            "sources": source_rows,
        }
    return summary


def _combine_status(statuses: list[str]) -> str:
    if "GO" in statuses:
        return "GO"
    if "WORKAROUND" in statuses:
        return "WORKAROUND"
    if any(status == "NO-GO" for status in statuses):
        return "NO-GO"
    if any(status == "SOURCE_UNREACHABLE" for status in statuses):
        return "SOURCE_UNREACHABLE"
    if any(status == "ENV_UNAVAILABLE" for status in statuses):
        return "ENV_UNAVAILABLE"
    return "UNTESTED"


def _phase_b_gate(gate_summary: dict[str, Any]) -> dict[str, str]:
    required = ("G1", "G2", "G3", "G6")
    bad = [gate_id for gate_id in required if gate_summary[gate_id]["status"] not in {"GO", "WORKAROUND"}]
    if bad:
        status = _blocked_gate_status(gate_summary, bad)
        if status == "NO-GO":
            summary = "Phase B 暂不启动；G1/G2/G3/G6 存在已连通但字段/覆盖率不满足的门控：" + ",".join(bad)
        elif status == "SOURCE_UNREACHABLE":
            summary = "Phase B 暂不启动；当前网络或上游源不可达，不能把该结果解释为数据不可行：" + ",".join(bad)
        elif status == "ENV_UNAVAILABLE":
            summary = "Phase B 暂不启动；本机依赖或 token 不完整，不能把该结果解释为数据不可行：" + ",".join(bad)
        else:
            summary = "Phase B 暂不启动；缺少可判定证据：" + ",".join(bad)
        return {
            "status": status,
            "summary": summary,
        }
    if any(gate_summary[gate_id]["status"] == "WORKAROUND" for gate_id in required):
        return {
            "status": "WORKAROUND",
            "summary": "Phase B 可做小样本建库验证，但必须先落实 WORKAROUND 的数据口径。",
        }
    return {"status": "GO", "summary": "Phase B 可启动点时面板建库。"}


def _commercial_gate(gate_summary: dict[str, Any]) -> dict[str, str]:
    required = ("G1", "G2", "G3", "G4", "G5", "G6")
    bad = [gate_id for gate_id in required if gate_summary[gate_id]["status"] not in {"GO", "WORKAROUND"}]
    if bad:
        status = _blocked_gate_status(gate_summary, bad)
        return {
            "status": status,
            "summary": "商业全量门控尚未通过；需处理：" + ",".join(bad),
        }
    if any(gate_summary[gate_id]["status"] == "WORKAROUND" for gate_id in required):
        return {
            "status": "WORKAROUND",
            "summary": "商业全量可进入工程化建库，但 G4/G5 等 workaround 必须通过覆盖率报告、缓存、增量与失败重试闭环。",
        }
    return {"status": "GO", "summary": "G1-G6 全部达到 GO，可进入商业全量验收。"}


def _blocked_gate_status(gate_summary: dict[str, Any], gate_ids: list[str]) -> str:
    statuses = [gate_summary[gate_id]["status"] for gate_id in gate_ids]
    if "NO-GO" in statuses:
        return "NO-GO"
    if "SOURCE_UNREACHABLE" in statuses:
        return "SOURCE_UNREACHABLE"
    if "ENV_UNAVAILABLE" in statuses:
        return "ENV_UNAVAILABLE"
    return "UNTESTED"


def _render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase A 数据可行性冒烟报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 解释器：`{payload['environment']['python_executable']}`",
        f"- 包版本：{', '.join(f'{k}={v}' for k, v in payload['environment']['package_versions'].items())}",
        f"- 命令参数：`{payload['environment']['params']}`",
        f"- 代理处理：{payload['environment']['proxy_bypass']}",
        f"- 样本数：{payload['sample_size']}",
        f"- 生产库快照未变化：{payload['production_db_snapshot_unchanged']}",
        f"- Phase B 门控：**{payload['phase_b_gate']['status']}** — {payload['phase_b_gate']['summary']}",
        f"- 商业全量门控：**{payload['commercial_gate']['status']}** — {payload['commercial_gate']['summary']}",
        "",
        "## 门控结论",
        "",
        "| 门 | 名称 | 结论 | 推荐源 |",
        "|---|---|---|---|",
    ]
    for gate_id, item in payload["gate_summary"].items():
        recommended = ", ".join(item["recommended_sources"]) if item["recommended_sources"] else "-"
        lines.append(f"| {gate_id} | {item['name']} | {item['status']} | {recommended} |")

    lines.extend(["", "## 分数据源证据", ""])
    for result in payload["sources"]:
        lines.append(f"### {result['source']}")
        if not result.get("available", True):
            lines.append("- 可用性：未安装/不可用")
        for gate_id in GATE_NAMES:
            item = result.get("gates", {}).get(gate_id, {})
            lines.append(f"- {gate_id} {GATE_NAMES[gate_id]}：**{item.get('status', 'UNTESTED')}** — {item.get('summary', '')}")
            evidence = item.get("evidence") or {}
            if evidence:
                lines.append(f"  证据样本：`{json.dumps(json_safe(evidence), ensure_ascii=False)[:900]}`")
            risks = item.get("risks") or []
            if risks:
                lines.append(f"  风险：{'；'.join(risks)}")
        calls = result.get("calls", [])
        if calls:
            ok = sum(1 for call in calls if call.get("ok"))
            failed = len(calls) - ok
            lines.append(f"- 接口调用：{len(calls)} 次，成功 {ok}，失败 {failed}")
        lines.append("")

    lines.extend(
        [
            "## 约束声明",
            "",
            "- 本阶段未做任何权重、回测或胜率判断。",
            "- 研究代码与输出均隔离在 `research/`；冒烟运行输出只写 `research/out/`，不写生产库；上方生产库快照用于复核。",
            "- `UNTESTED` 不等于失败，表示缺依赖、缺 token 或未获得可执行接口证据。",
            "",
            f"_generated by research/feasibility_smoke.py at {datetime.now().isoformat(timespec='seconds')}_",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
