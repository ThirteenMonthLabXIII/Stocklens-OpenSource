from __future__ import annotations

"""Bind externally collected V3 P7 evidence to exactly one release candidate."""

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

try:  # Works both as ``python tools/...`` and imported by delivery tests.
    from tools.prepare_v3_release import MANUAL_CASES, P7_SESSION_NAME, RELEASE_MANIFEST_NAME, sha256_file
    from tools.release_integrity import atomic_write_json
    from tools.release_schema import require_schema
except ModuleNotFoundError:  # pragma: no cover - direct script compatibility
    from prepare_v3_release import MANUAL_CASES, P7_SESSION_NAME, RELEASE_MANIFEST_NAME, sha256_file
    from release_integrity import atomic_write_json
    from release_schema import require_schema


P7_SCHEMA_VERSION = 3
VALID_STATUSES = {"PASS", "FAIL", "NA"}
CASE_OS_LABELS = {
    "win11_25h2_full": "win11_25h2",
    "win11_24h2_core": "win11_24h2",
    "win10_22h2_compat": "win10_22h2",
}
OPTIONAL_DEVICE_CASES = {"win10_22h2_compat"}
OS_LABEL_RE = re.compile(r"^[a-z0-9_]+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="记录 V3 P7 人工验收；不支持 --mark-all-pass。")
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--evidence-json", required=True, help="包含设备矩阵、候选哈希、逐项状态和截图路径的 JSON。")
    parser.add_argument("--operator", required=True)
    return parser.parse_args()


def _required_candidate(manifest: dict[str, Any]) -> dict[str, str]:
    artifacts = manifest["artifacts"]
    return {
        "candidate_id": str(manifest["candidate_id"]),
        "git_commit": str(manifest["git_commit"]),
        "installer_sha256": str(artifacts["installer"]["sha256"]),
        "commercial_zip_sha256": str(artifacts["commercial_zip"]["sha256"]),
        "source_zip_sha256": str(artifacts["source_zip"]["sha256"]),
    }


def image_metadata(path: Path) -> dict[str, str | int]:
    try:
        with Image.open(path) as image:
            image_format = str(image.format or "").lower()
            size = image.size
            image.verify()
        with Image.open(path) as decoded:
            decoded.load()
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError) as exc:
        raise RuntimeError(f"截图不是可完整解码且尺寸非零的 PNG/JPEG/WebP 图片：{path}") from exc
    if image_format == "jpg":
        image_format = "jpeg"
    if image_format not in {"png", "jpeg", "webp"} or size[0] <= 0 or size[1] <= 0:
        raise RuntimeError(f"截图不是可完整解码且尺寸非零的 PNG/JPEG/WebP 图片：{path}")
    return {"format": image_format, "width": size[0], "height": size[1], "size_bytes": path.stat().st_size}


def image_fingerprint(path: Path) -> dict[str, str | int]:
    metadata = image_metadata(path)
    with Image.open(path) as decoded:
        canonical = decoded.convert("RGBA")
        width, height = canonical.size
        pixel_digest = hashlib.sha256()
        pixel_digest.update(f"{width}x{height}:RGBA\0".encode("ascii"))
        pixel_digest.update(canonical.tobytes())
        grayscale = canonical.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(grayscale.getdata())
    bits = 0
    for row in range(8):
        for column in range(8):
            bits = (bits << 1) | int(pixels[row * 9 + column] > pixels[row * 9 + column + 1])
    return {
        **metadata,
        "sha256": sha256_file(path),
        "pixel_sha256": pixel_digest.hexdigest(),
        "dhash64": f"{bits:016x}",
        "pixel_mode": "RGBA",
    }


def _hamming_distance(first: str, second: str) -> int:
    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except (TypeError, ValueError):
        return 64


def _session_candidate(
    release_dir: Path,
    manifest: dict[str, Any],
    *,
    verify_manifest_hash: bool = True,
) -> dict[str, str]:
    candidate = _required_candidate(manifest)
    session_path = release_dir / P7_SESSION_NAME
    if not session_path.is_file():
        return candidate
    session = json.loads(session_path.read_text(encoding="utf-8"))
    manifest_path = release_dir / RELEASE_MANIFEST_NAME
    expected = {
        "candidate_id": candidate["candidate_id"],
        "git_commit": candidate["git_commit"],
        "candidate_stage": str(manifest.get("candidate_stage") or ""),
        "installer_sha256": candidate["installer_sha256"],
        "commercial_zip_sha256": candidate["commercial_zip_sha256"],
        "source_zip_sha256": candidate["source_zip_sha256"],
    }
    for key, value in expected.items():
        if session.get(key) != value:
            raise RuntimeError(f"P7 session 与候选不一致：{key}")
    manifest_sha256 = sha256_file(manifest_path)
    if verify_manifest_hash and session.get("candidate_manifest_sha256") != manifest_sha256:
        raise RuntimeError("P7 session 绑定的 candidate manifest SHA256 不一致。")
    challenge_id = str(session.get("challenge_id") or "")
    stage = str(manifest.get("candidate_stage") or "").upper()
    if not re.fullmatch(rf"{re.escape(stage)}-[0-9a-f]{{32}}", challenge_id):
        raise RuntimeError("P7 session challenge 格式或候选阶段不正确。")
    candidate.update({"candidate_manifest_sha256": str(session.get("candidate_manifest_sha256") or ""), "challenge_id": challenge_id})
    return candidate


def _devices(evidence: dict[str, Any]) -> dict[str, dict[str, str]]:
    raw = evidence.get("devices")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("P7 证据必须提供至少一个设备 ID、Windows 版本和 os_label。")
    devices: dict[str, dict[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("P7 devices 必须为对象数组。")
        device_id = str(item.get("device_id") or "").strip()
        windows_version = str(item.get("windows_version") or "").strip()
        os_label = str(item.get("os_label") or "").strip().lower()
        if not device_id or not windows_version or not OS_LABEL_RE.fullmatch(os_label):
            raise RuntimeError("P7 设备必须包含非空 device_id、windows_version 与合法 os_label。")
        if device_id in devices:
            raise RuntimeError(f"P7 设备 ID 重复：{device_id}")
        devices[device_id] = {"device_id": device_id, "windows_version": windows_version, "os_label": os_label}
    return devices


def _expected_items(evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_items = evidence.get("items")
    if not isinstance(raw_items, list):
        raise RuntimeError("P7 items 必须为数组。")
    items = {str(item.get("id")): item for item in raw_items if isinstance(item, dict)}
    expected = {key: (title, required) for key, title, required in MANUAL_CASES}
    if set(items) != set(expected) or len(items) != len(raw_items):
        raise RuntimeError("人工证据项目集合与 P7 清单不一致。")
    return items


def _recorded_screenshots(
    *,
    release_dir: Path,
    archive: Path,
    candidate_id: str,
    item_id: str,
    os_label: str,
    screenshot_paths: list[Any],
) -> list[dict[str, str | int]]:
    records: list[dict[str, str | int]] = []
    for index, screenshot in enumerate(screenshot_paths, start=1):
        source = Path(str(screenshot)).resolve()
        if not source.is_file():
            raise RuntimeError(f"截图不存在：{source}")
        metadata = image_fingerprint(source)
        suffix = ".jpg" if metadata["format"] == "jpeg" else f".{metadata['format']}"
        target = archive / f"{candidate_id}_{os_label}_{item_id}_{index:02d}{suffix}"
        shutil.copy2(source, target)
        records.append(
            {
                "path": str(target.relative_to(release_dir)).replace("\\", "/"),
                **metadata,
            }
        )
    return records


def _shared_screenshot_errors(items: list[dict[str, Any]]) -> list[str]:
    consumers: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        for screenshot in item.get("screenshots", []):
            consumers.setdefault(str(screenshot["sha256"]), []).append(item)
    errors = []
    for digest, linked_items in consumers.items():
        if len(linked_items) > 1 and not all(str(item.get("shared_evidence_reason") or "").strip() for item in linked_items):
            errors.append(f"截图 SHA256 被多个 P7 项共用但未声明 shared_evidence_reason：{digest}")
    return errors


def _preflight_evidence_images(items: dict[str, dict[str, Any]]) -> None:
    consumers: dict[str, list[dict[str, Any]]] = {}
    for item in items.values():
        screenshots = item.get("screenshots", [])
        if not isinstance(screenshots, list):
            raise RuntimeError(f"P7 screenshots 必须为数组：{item.get('id')}")
        for screenshot in screenshots:
            source = Path(str(screenshot)).resolve()
            if not source.is_file():
                raise RuntimeError(f"截图不存在：{source}")
            image_metadata(source)
            consumers.setdefault(sha256_file(source), []).append(item)
    for digest, linked_items in consumers.items():
        if len(linked_items) > 1 and not all(str(item.get("shared_evidence_reason") or "").strip() for item in linked_items):
            raise RuntimeError(f"截图 SHA256 被多个 P7 项共用但未声明 shared_evidence_reason：{digest}")


def validate_p7_report(
    report: dict[str, Any],
    release_dir: Path,
    expected_candidate: dict[str, str],
    *,
    prior_screenshot_fingerprints: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if report.get("schema_version") != P7_SCHEMA_VERSION:
        errors.append("V3 P7 evidence schema_version 必须为 3")
    if report.get("candidate") != expected_candidate:
        errors.append("V3 P7 人工报告候选哈希、commit 或 zip 绑定不一致")
    if not str(report.get("operator") or "").strip() or not str(report.get("recorded_at") or "").strip():
        errors.append("V3 P7 人工报告缺少验收人或记录时间")
    devices = report.get("devices")
    if not isinstance(devices, list) or not devices:
        errors.append("V3 P7 人工报告缺少设备矩阵")
        device_map: dict[str, dict[str, Any]] = {}
    else:
        device_map = {str(device.get("device_id")): device for device in devices if isinstance(device, dict)}
        if len(device_map) != len(devices):
            errors.append("V3 P7 人工报告设备 ID 重复或格式无效")
    expected = {key: required for key, _title, required in MANUAL_CASES}
    rows = {str(row.get("id")): row for row in report.get("items", []) if isinstance(row, dict)}
    if set(rows) != set(expected):
        errors.append("V3 P7 人工报告项目集合不完整")
        return errors, warnings
    for key, required in expected.items():
        row = rows[key]
        status = str(row.get("status", "")).upper()
        if status not in VALID_STATUSES:
            errors.append(f"人工项状态非法：{key}")
        if required and status != "PASS":
            errors.append(f"必需人工项不是 PASS：{key}")
        if not required and status == "FAIL":
            warnings.append(f"非阻断兼容项失败，需撤销相应兼容声明：{key}")
        device_id = str(row.get("device_id") or "")
        device = device_map.get(device_id)
        device_optional = key in OPTIONAL_DEVICE_CASES and status == "NA"
        if not device and not device_optional:
            errors.append(f"人工项缺少有效设备 ID：{key}")
        elif device and key in CASE_OS_LABELS and str(device.get("os_label") or "") != CASE_OS_LABELS[key]:
            errors.append(f"人工项设备系统不符合矩阵：{key}")
        screenshots = row.get("screenshots")
        if not isinstance(screenshots, list):
            errors.append(f"人工项截图列表格式错误：{key}")
            continue
        if required and not screenshots:
            errors.append(f"必需人工项缺少截图：{key}")
        for screenshot in screenshots:
            if not isinstance(screenshot, dict):
                errors.append(f"截图记录格式错误：{key}")
                continue
            screenshot_path = release_dir / str(screenshot.get("path") or "")
            if not screenshot_path.is_file() or sha256_file(screenshot_path) != screenshot.get("sha256"):
                errors.append(f"截图缺失或 SHA256 不一致：{key}")
                continue
            try:
                metadata = image_fingerprint(screenshot_path)
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            if any(screenshot.get(name) != value for name, value in metadata.items()):
                errors.append(f"截图元数据不一致：{key}")
            for prior in prior_screenshot_fingerprints or []:
                if screenshot.get("sha256") == prior.get("sha256") or screenshot.get("pixel_sha256") == prior.get("pixel_sha256"):
                    errors.append(f"RC1 截图复用了 RC0 字节或像素相同证据：{key}")
                    continue
                if _hamming_distance(str(screenshot.get("dhash64")), str(prior.get("dhash64"))) <= 6:
                    review = row.get("near_duplicate_review")
                    reviewer = str(review.get("reviewer") or "").strip() if isinstance(review, dict) else ""
                    reason = str(review.get("reason") or "").strip() if isinstance(review, dict) else ""
                    if not reviewer or not reason:
                        errors.append(f"RC1/RC0 近似截图需 REVIEW_REQUIRED 并填写复核人和具体理由：{key}")
        if not str(row.get("note") or "").strip():
            errors.append(f"人工项缺少观察备注：{key}")
    errors.extend(_shared_screenshot_errors(list(rows.values())))
    accepted = all(str(rows[key].get("status", "")).upper() == "PASS" for key, required in expected.items() if required)
    if bool(report.get("accepted")) != accepted:
        errors.append("P7 报告 accepted 与逐项实际状态不一致")
    return errors, warnings


def record(release_dir: Path, evidence: dict[str, Any], operator: str) -> Path:
    manifest_path = release_dir / RELEASE_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 5:
        raise RuntimeError("仅支持 schema 5 V3 release manifest。")
    operator = operator.strip()
    if not operator:
        raise RuntimeError("验收人不能为空。")
    supplied_operator = str(evidence.get("operator") or "").strip()
    if supplied_operator and supplied_operator != operator:
        raise RuntimeError("evidence-json 验收人与 --operator 不一致。")
    required_candidate = _session_candidate(release_dir, manifest)
    if evidence.get("candidate") != required_candidate:
        raise RuntimeError("人工证据候选哈希或 commit 与 RC manifest 不一致。")
    devices = _devices(evidence)
    provided = _expected_items(evidence)
    _preflight_evidence_images(provided)
    archive = release_dir / "release_local" / "screenshots"
    archive.mkdir(parents=True, exist_ok=True)
    expected = {key: (title, required) for key, title, required in MANUAL_CASES}
    recorded_items = []
    for key, (title, required) in expected.items():
        item = provided[key]
        status = str(item.get("status", "")).upper()
        if status not in VALID_STATUSES:
            raise RuntimeError(f"P7 状态必须是 PASS/FAIL/NA：{key}")
        device_id = str(item.get("device_id") or "").strip()
        device_optional = key in OPTIONAL_DEVICE_CASES and status == "NA"
        if device_id not in devices and not (device_optional and not device_id):
            raise RuntimeError(f"P7 项目引用了不存在的设备 ID：{key}")
        device = devices.get(device_id)
        if device and key in CASE_OS_LABELS and device["os_label"] != CASE_OS_LABELS[key]:
            raise RuntimeError(f"P7 系统矩阵不匹配：{key}")
        raw_screenshots = item.get("screenshots", [])
        if not isinstance(raw_screenshots, list):
            raise RuntimeError(f"P7 screenshots 必须为数组：{key}")
        if required and not raw_screenshots:
            raise RuntimeError(f"必需人工项缺少截图：{key}")
        if not device and raw_screenshots:
            raise RuntimeError(f"NA 项目未绑定设备时不得提供截图：{key}")
        note = str(item.get("note") or "").strip()
        if not note:
            raise RuntimeError(f"P7 项目缺少观察备注：{key}")
        screenshots = (
            _recorded_screenshots(
                release_dir=release_dir,
                archive=archive,
                candidate_id=required_candidate["candidate_id"],
                item_id=key,
                os_label=device["os_label"],
                screenshot_paths=raw_screenshots,
            )
            if device
            else []
        )
        recorded_items.append(
            {
                "id": key,
                "title": title,
                "required": required,
                "status": status,
                "device_id": device_id,
                "screenshots": screenshots,
                "shared_evidence_reason": str(item.get("shared_evidence_reason") or "").strip(),
                "near_duplicate_review": item.get("near_duplicate_review"),
                "note": note,
            }
        )
    shared_errors = _shared_screenshot_errors(recorded_items)
    if shared_errors:
        raise RuntimeError("\n".join(shared_errors))
    accepted = all(item["status"] == "PASS" for item in recorded_items if item["required"])
    report = {
        "schema_version": P7_SCHEMA_VERSION,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "operator": operator,
        "candidate": required_candidate,
        "devices": list(devices.values()),
        "items": recorded_items,
        "screenshot_list": [shot for item in recorded_items for shot in item["screenshots"]],
        "accepted": accepted,
        "mode": "evidence-json-with-validated-image-hashes",
    }
    report_json = release_dir / "V3_P7_人工验收报告.json"
    report_txt = release_dir / "V3_P7_人工验收报告.txt"
    require_schema("p7", report)
    atomic_write_json(report_json, report)
    lines = [
        "Stocklens V3 P7 人工验收报告",
        f"候选：{required_candidate['candidate_id']}",
        f"验收人：{operator}",
        f"记录时间：{report['recorded_at']}",
        f"结论：{'PASS' if accepted else 'FAIL'}",
        "",
    ]
    lines.extend(f"- [{item['status']}] {item['title']}：{item['note']}" for item in recorded_items)
    report_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_json


def main() -> int:
    args = parse_args()
    report = record(Path(args.release_dir).resolve(), json.loads(Path(args.evidence_json).read_text(encoding="utf-8")), args.operator)
    print(f"[OK] 已生成 V3 P7 人工验收报告：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
