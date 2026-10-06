from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_ROOT = ROOT / "release"
REPORT_DIR = ROOT / "docs" / "acceptance_reports"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.prepare_phase9_release import (  # noqa: E402
    COMMERCIAL_NAME,
    COMMERCIAL_PROTECTION_MODES,
    MINIMUM_OBFUSCATED_TARGETS,
    SENSITIVE_NAME_PARTS,
    SENSITIVE_SUFFIXES,
    SOURCE_NAME,
    commercial_allowed_files,
    is_installer_file_name,
    normalize_protection_mode,
    protection_targets,
    sha256_file,
    validate_package,
)
from stock_tool.config.release_identity import APP_VERSION  # noqa: E402
from tools.release_integrity import atomic_write_json  # noqa: E402
from tools.release_schema import require_schema  # noqa: E402
from tools.verify_release_signing import sign_release  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="最终商业外发闸门：检查 release 是否满足外发条件。")
    parser.add_argument("--release-dir", default="", help="指定 release 下的交付目录；为空则自动选择含 RELEASE_MANIFEST.json 的最新目录。")
    parser.add_argument("--report-dir", default="", help="闸门报告输出目录，默认 docs/acceptance_reports。")
    parser.add_argument("--allow-manual-pending", action="store_true", help="允许人工 UI 验收 pending，仅用于内部预检。")
    parser.add_argument("--allow-live-risk", action="store_true", help="允许 live 小样本 risk。")
    parser.add_argument("--allow-live-not-run", action="store_true", help="允许未运行 live 小样本。")
    parser.add_argument("--allow-missing-offline", action="store_true", help="允许缺少离线验收结果，主要用于脚本测试。")
    parser.add_argument("--internal-precheck", action="store_true", help="内部预检模式；只有该模式可使用 allow-* 放宽参数。")
    return parser.parse_args()


def has_relaxation_flags(args: argparse.Namespace) -> bool:
    return any(
        bool(getattr(args, name))
        for name in ("allow_manual_pending", "allow_live_risk", "allow_live_not_run", "allow_missing_offline")
    )


def is_sensitive_zip_name(name: str) -> bool:
    lower = name.lower()
    filename = Path(lower).name
    return (
        filename == ".env"
        or filename.startswith(".env.")
        or filename.endswith(".private.json")
        or lower.endswith(tuple(SENSITIVE_SUFFIXES | {".zip"}))
        or any(part in filename for part in SENSITIVE_NAME_PARTS)
    )


def latest_release_dir() -> Path:
    candidates = [path for path in RELEASE_ROOT.iterdir() if path.is_dir() and (path / "RELEASE_MANIFEST.json").is_file()]
    if not candidates:
        raise FileNotFoundError("未找到 release 下含 RELEASE_MANIFEST.json 的交付目录。")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_manifest(release_dir: Path) -> dict:
    manifest_path = release_dir / "RELEASE_MANIFEST.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"RELEASE_MANIFEST.json 不存在：{manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("RELEASE_MANIFEST.json 格式无效")
    return payload


def _is_inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def resolve_release_path(path_text: object, release_dir: Path, default_rel: str) -> Path:
    fallback = release_dir / default_rel
    text = str(path_text or "")
    if not text:
        return fallback
    path = Path(text)
    if not path.is_absolute():
        return release_dir / path
    if _is_inside(path, release_dir):
        return path
    if fallback.exists():
        return fallback
    return path


def resolve_manual_report_path(report_path_text: str, release_dir: Path) -> Path | None:
    if not report_path_text:
        return None
    path = Path(report_path_text)
    candidates: list[Path]
    if path.is_absolute():
        candidates = [path] if _is_inside(path, release_dir) else [release_dir / "manual_ui_acceptance.json", release_dir / path.name, path]
    else:
        candidates = [release_dir / path]
    if path.name != "manual_ui_acceptance.json":
        candidates.append(release_dir / "manual_ui_acceptance.json")
    seen: set[str] = set()
    unique_candidates = []
    for candidate in candidates:
        key = str(candidate.resolve())
        if key not in seen:
            seen.add(key)
            unique_candidates.append(candidate)
    for candidate in unique_candidates:
        if candidate.exists():
            return candidate.resolve()
    return unique_candidates[0].resolve() if unique_candidates else None


def _rel(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve())).replace("\\", "/")
    except ValueError:
        return path.name


def check_hashes(release_dir: Path, manifest: dict) -> list[str]:
    errors: list[str] = []
    sha_map = manifest.get("sha256")
    if not isinstance(sha_map, dict) or not sha_map:
        return ["manifest 缺少 sha256"]
    for rel, expected in sha_map.items():
        path = release_dir / rel
        if not path.exists():
            errors.append(f"sha256 文件不存在：{rel}")
            continue
        actual = sha256_file(path)
        if actual != expected:
            errors.append(f"sha256 不匹配：{rel}")
    return errors


def check_zip(zip_path: Path, *, package_name: str, commercial: bool, allowed_files: set[str] | None = None) -> list[str]:
    errors: list[str] = []
    if not zip_path.exists():
        return [f"zip 不存在：{zip_path}"]
    with zipfile.ZipFile(zip_path) as zf:
        names = [name for name in zf.namelist() if not name.endswith("/")]
    prefix = package_name + "/"
    if not all(name.startswith(prefix) for name in names):
        errors.append(f"zip 内路径前缀异常：{zip_path.name}")
    rel_names = {name[len(prefix):] for name in names if name.startswith(prefix)}
    forbidden = [
        name
        for name in rel_names
        if is_sensitive_zip_name(name)
        or (commercial and name.lower().endswith(".py"))
        or "private_keys/" in name
        or name.endswith("license_private_key.json")
        or name == "data_location.txt"
        or name.startswith(("my_data/", "build/", "dist/", "release/"))
        or (commercial and (name.startswith(("stock_tool/", "tools/", "_internal/")) or "/_internal/" in name))
    ]
    if forbidden:
        errors.append(f"zip 包含禁入项：{', '.join(sorted(forbidden)[:20])}")
    if commercial and allowed_files is not None and rel_names != allowed_files:
        errors.append("商业 zip 文件白名单不一致：" + "、".join(sorted(rel_names)))
    return errors


def _is_allowed_release_report(path: Path) -> bool:
    name = path.name
    return name in {
        "phase9_offline_acceptance.txt",
        "phase9_offline_acceptance.json",
        "manual_ui_acceptance.txt",
        "manual_ui_acceptance.json",
    }


def _is_forbidden_release_root_item(path: Path) -> bool:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if path.is_dir():
        return name in {"private_keys"}
    return (
        name == "data_location.txt"
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith(".private.json")
        or suffix in SENSITIVE_SUFFIXES
        or suffix == ".zip"
        or any(part in name for part in SENSITIVE_NAME_PARTS)
    )


def check_release_root_forbidden_items(
    release_dir: Path,
    *,
    commercial_dir: Path,
    source_dir: Path,
    commercial_zip: Path,
    source_zip: Path,
) -> list[str]:
    errors: list[str] = []
    allowed_files = {
        (release_dir / "RELEASE_MANIFEST.json").resolve(),
        commercial_zip.resolve(),
        source_zip.resolve(),
    }
    allowed_dirs = {commercial_dir.resolve(), source_dir.resolve()}
    for path in release_dir.rglob("*"):
        resolved = path.resolve()
        if any(_is_inside(resolved, allowed_dir) for allowed_dir in allowed_dirs):
            continue
        if path.is_file() and (resolved in allowed_files or _is_allowed_release_report(path)):
            continue
        if any(part.lower() == "private_keys" for part in path.relative_to(release_dir).parts):
            errors.append(f"release 根目录包含禁入 private_keys 路径：{_rel(path, release_dir)}")
            continue
        if _is_forbidden_release_root_item(path):
            item_type = "目录" if path.is_dir() else "文件"
            errors.append(f"release 根目录包含禁入{item_type}：{_rel(path, release_dir)}")
    return sorted(set(errors))


def check_manual_report_binding(release_dir: Path, manifest: dict, manual: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    sha_map = manifest.get("sha256") if isinstance(manifest.get("sha256"), dict) else {}
    commercial_zip = resolve_release_path(manifest.get("commercial_zip"), release_dir, f"{COMMERCIAL_NAME}.zip")
    source_zip = resolve_release_path(manifest.get("source_zip"), release_dir, f"{SOURCE_NAME}.zip")
    current_commercial_sha = sha_map.get(_rel(commercial_zip, release_dir)) or (sha256_file(commercial_zip) if commercial_zip.exists() else "")
    current_source_sha = sha_map.get(_rel(source_zip, release_dir)) or (sha256_file(source_zip) if source_zip.exists() else "")

    embedded_evidence = manual.get("release_evidence") if isinstance(manual.get("release_evidence"), dict) else {}
    if not embedded_evidence.get("available"):
        errors.append("人工 UI 验收缺少 manifest 内嵌 release 包证据")

    def add_unique(message: str) -> None:
        if message not in errors:
            errors.append(message)

    def check_evidence(evidence: dict) -> None:
        if not isinstance(evidence, dict) or not evidence.get("available"):
            add_unique("人工 UI 验收报告缺少 release 包证据")
            return
        if evidence.get("commercial_zip_sha256") != current_commercial_sha:
            add_unique("人工 UI 验收报告与当前商业 zip SHA256 不一致")
        if evidence.get("source_zip_sha256") != current_source_sha:
            add_unique("人工 UI 验收报告与当前源码 zip SHA256 不一致")

    check_evidence(embedded_evidence)
    if int(manual.get("failed") or 0) != 0:
        errors.append("manifest 人工 UI 验收存在未通过项")
    if int(manual.get("required_not_applicable") or 0) != 0:
        errors.append("manifest 人工 UI 验收 required 项存在不适用，不能外发")

    report_path_text = str(manual.get("json_report") or "")
    report_path = resolve_manual_report_path(report_path_text, release_dir)
    if not report_path_text:
        warnings.append("人工 UI 验收缺少 JSON 报告路径，已使用 manifest 内嵌验收证据")
        return errors, warnings
    if not report_path or not report_path.exists():
        warnings.append(f"人工 UI 验收 JSON 报告不可读，已使用 manifest 内嵌验收证据：{report_path}")
        return errors, warnings

    if not _is_inside(report_path, release_dir):
        warnings.append("人工 UI 验收 JSON 报告不在当前 release 目录，建议重新运行记录工具固化留档")
    expected_report_sha = str(manual.get("json_report_sha256") or "")
    if expected_report_sha and sha256_file(report_path) != expected_report_sha:
        errors.append("人工 UI 验收 JSON 报告 SHA256 与 manifest 不一致")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = report.get("summary") if isinstance(report, dict) else {}
    if not isinstance(summary, dict) or not summary.get("accepted"):
        errors.append("人工 UI 验收报告本身未 accepted")
    if isinstance(summary, dict) and int(summary.get("required_not_applicable") or 0) != 0:
        errors.append("人工 UI 验收报告 required 项存在不适用，不能外发")
    items = report.get("items") if isinstance(report, dict) else []
    if isinstance(items, list) and any(isinstance(item, dict) and item.get("required") and item.get("status") == "na" for item in items):
        errors.append("人工 UI 验收报告明细包含 required 不适用项")
    evidence = report.get("release_evidence") if isinstance(report, dict) else {}
    check_evidence(evidence)
    if isinstance(evidence, dict) and isinstance(embedded_evidence, dict):
        for key in ("commercial_zip_sha256", "source_zip_sha256"):
            if evidence.get(key) and embedded_evidence.get(key) and evidence.get(key) != embedded_evidence.get(key):
                errors.append("人工 UI 验收报告与 manifest 内嵌包证据不一致")
                break
    report_release_dir = str(report.get("release_dir") or evidence.get("release_dir") or "")
    if report_release_dir and Path(report_release_dir).resolve() != release_dir.resolve():
        warnings.append("人工 UI 验收报告 release_dir 与当前 release 目录不同，但 zip hash 将作为绑定依据")
    return errors, warnings


def _installer_name_from_manifest(manifest: dict) -> str:
    protection = manifest.get("protection") if isinstance(manifest.get("protection"), dict) else {}
    installer_name = str(protection.get("installer_name") or "")
    if installer_name:
        return installer_name
    commercial_files = manifest.get("commercial_files") if isinstance(manifest.get("commercial_files"), list) else []
    installers = [str(name) for name in commercial_files if is_installer_file_name(str(name))]
    return installers[0] if len(installers) == 1 else ""


def expected_commercial_files(manifest: dict) -> set[str]:
    installer_name = _installer_name_from_manifest(manifest)
    return commercial_allowed_files(installer_name) if installer_name else set()


def check_protection(release_dir: Path, manifest: dict, commercial_dir: Path) -> list[str]:
    errors: list[str] = []
    protection = manifest.get("protection")
    if not isinstance(protection, dict):
        return ["manifest 缺少 protection 区块"]

    mode = normalize_protection_mode(protection)
    if not bool(protection.get("required")):
        errors.append("protection.required 必须为 true")
    if mode not in COMMERCIAL_PROTECTION_MODES:
        errors.append(f"protection.mode 不允许商业外发：{mode}")
    if not bool(protection.get("pyarmor_enabled")):
        errors.append("protection.pyarmor_enabled 必须为 true")
    if mode == "free-pyarmor":
        if bool(protection.get("bcc_enabled")):
            errors.append("free-pyarmor 模式下 protection.bcc_enabled 必须为 false")
        if not bool(protection.get("trial_build")):
            errors.append("free-pyarmor 模式必须标记 protection.trial_build=true")
        if str(protection.get("protection_level") or "") != "trial_obfuscation":
            errors.append("free-pyarmor 模式必须标记 protection.protection_level=trial_obfuscation")
    if mode == "bcc":
        if not bool(protection.get("bcc_enabled")):
            errors.append("bcc 模式下 protection.bcc_enabled 必须为 true")
        if not bool(protection.get("bcc_verified")):
            errors.append("bcc 模式下 protection.bcc_verified 必须为 true")

    installer_name = str(protection.get("installer_name") or "")
    if not installer_name or not is_installer_file_name(installer_name):
        errors.append("protection.installer_name 缺失或不符合安装包命名规则")
    else:
        installer_path = commercial_dir / installer_name
        if not installer_path.exists():
            errors.append(f"商业目录缺少 protection 指向的安装包：{installer_name}")
        else:
            actual_sha = sha256_file(installer_path)
            expected_sha = str(protection.get("installer_sha256") or "")
            if not expected_sha:
                errors.append("protection 缺少 installer_sha256")
            elif actual_sha != expected_sha:
                errors.append("protection.installer_sha256 与商业目录安装包不一致")
            sha_map = manifest.get("sha256") if isinstance(manifest.get("sha256"), dict) else {}
            rel = f"{COMMERCIAL_NAME}/{installer_name}"
            if sha_map.get(rel) and sha_map.get(rel) != actual_sha:
                errors.append("manifest sha256 中的安装包 hash 与实际文件不一致")

    bcc_targets = protection.get("bcc_targets")
    if mode == "bcc" and (not isinstance(bcc_targets, dict) or not bcc_targets):
        errors.append("bcc 模式 protection 缺少 BCC 保护目标清单")
    protected_targets = set(protection_targets(protection))
    missing_targets = sorted(MINIMUM_OBFUSCATED_TARGETS - protected_targets)
    if missing_targets:
        errors.append("protection 保护目标缺少核心白名单：" + "、".join(missing_targets))

    build_manifest_sha = str(protection.get("build_manifest_sha256") or "")
    if not build_manifest_sha:
        errors.append("protection 缺少 build_manifest_sha256")
    elif len(build_manifest_sha) != 64:
        errors.append("protection.build_manifest_sha256 格式异常")
    return errors


def evaluate_release(release_dir: Path, args: argparse.Namespace) -> tuple[list[str], list[str], dict]:
    manifest = load_manifest(release_dir)
    if manifest.get("product_version") == APP_VERSION and manifest.get("schema_version") != 5:
        return ["V3 最终闸门只接受 schema 5 候选；V3 release manifest schema 3/4 均已淘汰"], [], {"release_dir": str(release_dir), "version": APP_VERSION, "manual_ui_status": "unknown"}
    if manifest.get("schema_version") == 5:
        from tools.v3_release_gate import evaluate_v3_release

        if has_relaxation_flags(args):
            return ["V3 最终闸门不允许 allow-* 放宽参数"], [], {"release_dir": str(release_dir), "version": APP_VERSION, "manual_ui_status": "unknown"}
        return evaluate_v3_release(release_dir, verify_repository=True)
    errors: list[str] = []
    warnings: list[str] = []
    if has_relaxation_flags(args) and not args.internal_precheck:
        errors.append("allow-* 放宽参数只能与 --internal-precheck 一起使用，避免外发脚本误绕过验收")

    commercial_dir = resolve_release_path(manifest.get("commercial_dir"), release_dir, COMMERCIAL_NAME)
    source_dir = resolve_release_path(manifest.get("source_dir"), release_dir, SOURCE_NAME)
    commercial_zip = resolve_release_path(manifest.get("commercial_zip"), release_dir, f"{COMMERCIAL_NAME}.zip")
    source_zip = resolve_release_path(manifest.get("source_zip"), release_dir, f"{SOURCE_NAME}.zip")
    allowed_commercial_files = expected_commercial_files(manifest)

    errors.extend(validate_package(commercial_dir, package_type="commercial"))
    errors.extend(validate_package(source_dir, package_type="source"))
    errors.extend(check_protection(release_dir, manifest, commercial_dir))
    errors.extend(check_zip(commercial_zip, package_name=COMMERCIAL_NAME, commercial=True, allowed_files=allowed_commercial_files))
    errors.extend(check_zip(source_zip, package_name=SOURCE_NAME, commercial=False))
    errors.extend(check_hashes(release_dir, manifest))
    release_root_forbidden_errors = check_release_root_forbidden_items(
        release_dir,
        commercial_dir=commercial_dir,
        source_dir=source_dir,
        commercial_zip=commercial_zip,
        source_zip=source_zip,
    )
    errors.extend(release_root_forbidden_errors)

    offline = manifest.get("offline_acceptance")
    if not offline:
        (warnings if args.allow_missing_offline else errors).append("缺少离线验收结果")
    elif not bool(offline.get("ok")):
        errors.append("离线验收未通过")

    live_status = str(manifest.get("live_status") or "not_run")
    if live_status == "risk" and not args.allow_live_risk:
        errors.append("live 小样本为 risk")
    if live_status == "not_run" and not args.allow_live_not_run:
        errors.append("live 小样本未运行")

    manual = manifest.get("manual_ui_acceptance")
    manual_accepted = isinstance(manual, dict) and bool(manual.get("accepted")) and manual.get("status") == "accepted"
    if not manual_accepted:
        message = "人工 UI 验收未 accepted"
        (warnings if args.allow_manual_pending else errors).append(message)
    elif isinstance(manual, dict):
        manual_errors, manual_warnings = check_manual_report_binding(release_dir, manifest, manual)
        errors.extend(manual_errors)
        warnings.extend(manual_warnings)

    commercial_files = manifest.get("commercial_files")
    if not allowed_commercial_files:
        errors.append("无法从 manifest protection 解析商业安装包白名单")
    elif set(commercial_files or []) != allowed_commercial_files:
        errors.append("manifest commercial_files 与白名单不一致")
    if not (release_dir / "phase9_offline_acceptance.txt").exists() and not args.allow_missing_offline:
        errors.append("缺少 phase9_offline_acceptance.txt")
    if not (release_dir / "phase9_offline_acceptance.json").exists() and not args.allow_missing_offline:
        errors.append("缺少 phase9_offline_acceptance.json")

    summary = {
        "release_dir": str(release_dir),
        "version": manifest.get("version", ""),
        "live_status": live_status,
        "manual_ui_status": manual.get("status") if isinstance(manual, dict) else "missing",
        "commercial_zip": str(commercial_zip),
        "source_zip": str(source_zip),
        "release_root_forbidden_errors": release_root_forbidden_errors,
    }
    return errors, warnings, summary


def write_report(release_dir: Path, errors: list[str], warnings: list[str], summary: dict, report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = report_dir / f"final_release_gate_{stamp}.json"
    txt_path = report_dir / f"final_release_gate_{stamp}.txt"
    payload = {
        "title": "A股选股仪 最终商业外发闸门报告",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "release_root_forbidden_errors": summary.get("release_root_forbidden_errors", []),
        "summary": summary,
    }
    atomic_write_json(json_path, payload)
    lines = [
        "A股选股仪 最终商业外发闸门报告",
        f"状态：{'PASS' if not errors else 'FAIL'}",
        f"Release：{release_dir}",
        f"版本：{summary.get('version') or '-'}",
        f"Live：{summary.get('live_status')}",
        f"人工 UI：{summary.get('manual_ui_status')}",
        "",
        "错误：",
    ]
    lines.extend([f"- {item}" for item in errors] or ["- 无"])
    lines.append("")
    lines.append("警告：")
    lines.extend([f"- {item}" for item in warnings] or ["- 无"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, json_path


def write_final_decision(release_dir: Path, warnings: list[str]) -> Path:
    manifest_path = release_dir / "RELEASE_MANIFEST.json"
    p7_path = release_dir / "V3_P7_人工验收报告.json"
    manifest = load_manifest(release_dir)
    promotion = manifest.get("promotion_record")
    if not isinstance(promotion, dict):
        raise RuntimeError("RC1 manifest 缺少 promotion record，不能生成最终决策。")
    promotion_path = ROOT / str(promotion.get("path") or "")
    if not p7_path.is_file() or not promotion_path.is_file():
        raise RuntimeError("最终决策引用的 RC1 P7 或 promotion 文件缺失。")
    decision = {
        "schema_version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "allowed_for_external_release": True,
        "rc1_manifest_sha256": sha256_file(manifest_path),
        "rc1_p7_sha256": sha256_file(p7_path),
        "promotion_sha256": sha256_file(promotion_path),
        "sections": {
            "automated": "完整离线自动化与默认 Qt 闸门通过。",
            "manual_acceptance": "RC1 P7 固定路径报告通过并绑定当前候选。",
            "supply_chain_integrity": "候选 subject、inventory、依赖锁与签名由发布安全流程复核。",
            "known_risks": "未进行 Windows 发布者代码签名，客户安装时可能显示未知发布者。" + ("；" + "；".join(warnings) if warnings else ""),
            "external_release": "允许外发；产品仅用于研究和复盘辅助，不构成投资建议。",
        },
    }
    require_schema("final_decision", decision)
    path = release_dir / "FINAL_RELEASE_DECISION.json"
    atomic_write_json(path, decision)
    return path


def main() -> int:
    args = parse_args()
    release_dir = Path(args.release_dir).resolve() if args.release_dir else latest_release_dir().resolve()
    errors, warnings, summary = evaluate_release(release_dir, args)
    decision_path: Path | None = None
    signature_index: Path | None = None
    manifest = load_manifest(release_dir)
    is_v3_schema5 = manifest.get("schema_version") == 5
    if not errors and is_v3_schema5:
        try:
            decision_path = write_final_decision(release_dir, warnings)
            signature_index = sign_release(release_dir)
        except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            errors.append(f"最终决策签名闭环失败：{exc}")
            if decision_path is not None:
                decision_path.unlink(missing_ok=True)
    txt_path, json_path = write_report(release_dir, errors, warnings, summary, Path(args.report_dir) if args.report_dir else REPORT_DIR)
    print(f"最终闸门 TXT：{txt_path}")
    print(f"最终闸门 JSON：{json_path}")
    if warnings:
        print("[WARN]")
        for item in warnings:
            print(item)
    if errors:
        print("[FAIL] 最终商业外发闸门未通过")
        for item in errors:
            print(item)
        return 1
    if is_v3_schema5:
        print(f"最终决策：{decision_path}")
        print(f"签名索引：{signature_index}")
    print("[OK] 最终商业外发闸门通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
