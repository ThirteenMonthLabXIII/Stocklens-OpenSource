from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "docs" / "acceptance_reports"
RELEASE_MANUAL_JSON = "manual_ui_acceptance.json"
RELEASE_MANUAL_TXT = "manual_ui_acceptance.txt"


@dataclass(frozen=True)
class CheckItem:
    key: str
    title: str
    group: str
    required: bool = True


CHECK_ITEMS = [
    CheckItem("source_start", "源码环境可启动 python main.py", "源码环境"),
    CheckItem("first_data_dir", "首次启动会要求选择数据目录", "源码环境"),
    CheckItem("settings_api_key_validation", "设置页可保存 API Key 并自动验证状态", "源码环境"),
    CheckItem("stock_search", "个股搜索覆盖代码、简称、空输入", "核心功能"),
    CheckItem("stock_detail", "个股详情展示行情、财务、走势图、技术画像、事件", "核心功能"),
    CheckItem("watchlist_persist", "自选股添加、删除、重启后仍存在", "核心功能"),
    CheckItem("nl_local_rule", "自然语言本地规则解析不消耗 API", "策略筛选"),
    CheckItem("nl_clarify", "模糊表达出现澄清提示，不直接执行", "策略筛选"),
    CheckItem("strategy_store", "策略保存、加载、覆盖、删除正常", "策略筛选"),
    CheckItem("strategy_import_export", "策略 JSON 导出、导入、预览、取消、确认正常", "策略筛选"),
    CheckItem("history_compare_export", "执行历史、对比上次、CSV 导出正常", "复盘导出"),
    CheckItem("health_center", "数据源健康、缓存年龄、缺失字段、失败记录可读", "验收中心"),
    CheckItem("acceptance_center", "验收中心可刷新、打开 TXT/JSON/报告目录", "验收中心"),
    CheckItem("restart_persist", "退出并重启后数据仍在用户目录", "稳定性"),
    CheckItem("commercial_clean_machine", "商业包在另一台 Windows 或干净虚拟机解压", "商业包冷启动"),
    CheckItem("commercial_no_data_location", "商业包目录不存在 data_location.txt", "商业包冷启动"),
    CheckItem("commercial_first_data_dir", "商业包首次启动要求选择客户数据目录", "商业包冷启动"),
    CheckItem("commercial_basic_flow", "商业包可搜索股票、运行本地规则筛选、打开设置页", "商业包冷启动"),
    CheckItem("commercial_restart", "商业包退出重启后不再要求重新选择数据目录", "商业包冷启动"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="记录第九阶段人工 UI 验收结果，生成可留档报告。")
    parser.add_argument("--release-dir", default="", help="本次验收对应的 release/V1_商业交付_* 目录。")
    parser.add_argument("--operator", default="", help="验收人。")
    parser.add_argument("--machine", default="", help="验收机器标识，默认当前主机名。")
    parser.add_argument("--notes", default="", help="总体备注。")
    parser.add_argument("--report-dir", default="", help="报告输出目录，默认 docs/acceptance_reports。")
    parser.add_argument("--mark-all-pass", action="store_true", help="非交互模式：全部标记为通过，主要用于自动化测试或已线下逐项确认后补录。")
    parser.add_argument("--fail", action="append", default=[], help="非交互模式：指定未通过项目 key，可重复。")
    parser.add_argument("--na", action="append", default=[], help="非交互模式：指定不适用项目 key，可重复。")
    return parser.parse_args()


def _prompt_status(item: CheckItem) -> tuple[str, str]:
    while True:
        raw = input(f"[{item.group}] {item.title} (y/n/na)：").strip().lower()
        if raw in {"y", "yes", "是", "pass"}:
            return "pass", ""
        if raw in {"n", "no", "否", "fail"}:
            note = input("  失败说明：").strip()
            return "fail", note
        if raw in {"na", "n/a", "skip"}:
            note = input("  不适用说明：").strip()
            return "na", note
        print("请输入 y、n 或 na。")


def build_results(args: argparse.Namespace) -> list[dict[str, str | bool]]:
    failed_keys = set(args.fail or [])
    na_keys = set(args.na or [])
    known_keys = {item.key for item in CHECK_ITEMS}
    unknown = sorted((failed_keys | na_keys) - known_keys)
    if unknown:
        raise SystemExit("未知验收项 key：" + "、".join(unknown))

    results: list[dict[str, str | bool]] = []
    interactive = not args.mark_all_pass and not failed_keys and not na_keys
    for item in CHECK_ITEMS:
        if interactive:
            status, note = _prompt_status(item)
        elif item.key in failed_keys:
            status, note = "fail", "命令行标记未通过"
        elif item.key in na_keys:
            status, note = "na", "命令行标记不适用"
        else:
            status, note = "pass", ""
        results.append(
            {
                "key": item.key,
                "group": item.group,
                "title": item.title,
                "required": item.required,
                "status": status,
                "note": note,
            }
        )
    return results


def summarize(results: list[dict[str, str | bool]]) -> dict[str, int | str | bool]:
    required_results = [item for item in results if item["required"]]
    failed = [item for item in required_results if item["status"] == "fail"]
    required_na = [item for item in required_results if item["status"] == "na"]
    pending = [item for item in required_results if item["status"] not in {"pass", "na", "fail"}]
    accepted = not failed and not pending and not required_na
    return {
        "status": "accepted" if accepted else "failed",
        "accepted": accepted,
        "total": len(results),
        "passed": sum(1 for item in results if item["status"] == "pass"),
        "failed": len(failed),
        "not_applicable": sum(1 for item in results if item["status"] == "na"),
        "required_not_applicable": len(required_na),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_evidence(release_dir_text: str) -> dict[str, str | bool]:
    if not release_dir_text:
        return {"available": False, "message": "未填写 release-dir"}
    release_dir = Path(release_dir_text).resolve()
    manifest_path = release_dir / "RELEASE_MANIFEST.json"
    if not manifest_path.exists():
        return {"available": False, "release_dir": str(release_dir), "message": "release manifest 不存在"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    commercial_zip = Path(str(manifest.get("commercial_zip") or ""))
    source_zip = Path(str(manifest.get("source_zip") or ""))
    evidence: dict[str, str | bool] = {
        "available": True,
        "release_dir": str(release_dir),
        "manifest": str(manifest_path),
        "version": str(manifest.get("version") or ""),
        "commercial_zip": str(commercial_zip),
        "source_zip": str(source_zip),
        "commercial_zip_sha256": "",
        "source_zip_sha256": "",
    }
    if commercial_zip.exists():
        evidence["commercial_zip_sha256"] = sha256_file(commercial_zip)
    if source_zip.exists():
        evidence["source_zip_sha256"] = sha256_file(source_zip)
    return evidence


def write_reports(payload: dict, report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = report_dir / f"manual_ui_acceptance_{stamp}.json"
    txt_path = report_dir / f"manual_ui_acceptance_{stamp}.txt"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "A股选股仪 第九阶段人工 UI 验收报告",
        f"状态：{payload['summary']['status']}",
        f"验收人：{payload['operator']}",
        f"机器：{payload['machine']}",
        f"Release：{payload['release_dir'] or '-'}",
        f"包证据：{'已记录' if payload['release_evidence'].get('available') else '未记录'}",
        f"时间：{payload['created_at']}",
        f"备注：{payload['notes'] or '-'}",
        "",
    ]
    current_group = ""
    for item in payload["items"]:
        if item["group"] != current_group:
            current_group = item["group"]
            lines.append(f"## {current_group}")
        line = f"- [{item['status']}] {item['title']}"
        if item["note"]:
            line += f"：{item['note']}"
        lines.append(line)
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, json_path


def _manifest_path_text(path: Path, release_dir: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(release_dir.resolve())).replace("\\", "/")
    except ValueError:
        return str(resolved)


def _copy_report_to_release(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest


def persist_reports_in_release(payload: dict, txt_path: Path, json_path: Path) -> tuple[Path, Path] | None:
    evidence = payload.get("release_evidence")
    if not isinstance(evidence, dict) or not evidence.get("available"):
        return None
    manifest_path = Path(str(evidence.get("manifest") or ""))
    if not manifest_path.exists():
        return None
    release_dir = manifest_path.parent
    release_txt = _copy_report_to_release(txt_path, release_dir / RELEASE_MANUAL_TXT)
    release_json = _copy_report_to_release(json_path, release_dir / RELEASE_MANUAL_JSON)
    return release_txt, release_json


def manifest_manual_payload(
    payload: dict,
    json_path: Path,
    txt_path: Path | None = None,
    *,
    release_dir: Path | None = None,
    original_json_path: Path | None = None,
    original_txt_path: Path | None = None,
) -> dict:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    release_base = release_dir or json_path.parent
    manifest_payload = {
        "status": str(summary.get("status") or "unknown"),
        "accepted": bool(summary.get("accepted")),
        "json_report": _manifest_path_text(json_path, release_base),
        "txt_report": _manifest_path_text(txt_path, release_base) if txt_path else "",
        "operator": str(payload.get("operator") or ""),
        "machine": str(payload.get("machine") or ""),
        "created_at": str(payload.get("created_at") or ""),
        "release_dir": str(payload.get("release_dir") or ""),
        "release_evidence": payload.get("release_evidence") if isinstance(payload.get("release_evidence"), dict) else {},
        "summary": summary,
        "items": payload.get("items") if isinstance(payload.get("items"), list) else [],
        "notes": str(payload.get("notes") or ""),
        "failed": int(summary.get("failed") or 0),
        "passed": int(summary.get("passed") or 0),
        "not_applicable": int(summary.get("not_applicable") or 0),
        "required_not_applicable": int(summary.get("required_not_applicable") or 0),
    }
    if original_json_path and original_json_path.resolve() != json_path.resolve():
        manifest_payload["original_json_report"] = str(original_json_path.resolve())
    if original_txt_path and txt_path and original_txt_path.resolve() != txt_path.resolve():
        manifest_payload["original_txt_report"] = str(original_txt_path.resolve())
    if json_path.exists():
        manifest_payload["json_report_sha256"] = sha256_file(json_path)
    if txt_path and txt_path.exists():
        manifest_payload["txt_report_sha256"] = sha256_file(txt_path)
    return manifest_payload


def update_release_manifest(payload: dict, txt_path: Path, json_path: Path) -> Path | None:
    evidence = payload.get("release_evidence")
    if not isinstance(evidence, dict) or not evidence.get("available"):
        return None
    manifest_path = Path(str(evidence.get("manifest") or ""))
    if not manifest_path.exists():
        return None
    release_dir = manifest_path.parent
    persisted = persist_reports_in_release(payload, txt_path, json_path)
    manifest_txt_path, manifest_json_path = persisted or (txt_path, json_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manual_ui_acceptance"] = manifest_manual_payload(
        payload,
        manifest_json_path,
        manifest_txt_path,
        release_dir=release_dir,
        original_json_path=json_path,
        original_txt_path=txt_path,
    )
    sha_map = manifest.get("sha256")
    if not isinstance(sha_map, dict):
        sha_map = {}
    for report_path in (manifest_txt_path, manifest_json_path):
        if report_path.exists():
            try:
                rel = str(report_path.resolve().relative_to(release_dir.resolve())).replace("\\", "/")
            except ValueError:
                continue
            sha_map[rel] = sha256_file(report_path)
    manifest["sha256"] = sha_map
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def main() -> int:
    args = parse_args()
    results = build_results(args)
    summary = summarize(results)
    payload = {
        "title": "A股选股仪 第九阶段人工 UI 验收报告",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "operator": args.operator.strip() or "未填写",
        "machine": args.machine.strip() or socket.gethostname(),
        "release_dir": str(Path(args.release_dir).resolve()) if args.release_dir else "",
        "release_evidence": release_evidence(args.release_dir),
        "notes": args.notes.strip(),
        "summary": summary,
        "items": results,
    }
    txt_path, json_path = write_reports(payload, Path(args.report_dir) if args.report_dir else REPORT_DIR)
    manifest_path = update_release_manifest(payload, txt_path, json_path)
    print(f"人工 UI 验收 TXT：{txt_path}")
    print(f"人工 UI 验收 JSON：{json_path}")
    if manifest_path:
        print(f"已更新 release manifest：{manifest_path}")
    if not summary["accepted"]:
        print("[FAIL] 人工 UI 验收存在未通过项")
        return 1
    print("[OK] 人工 UI 验收已记录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
