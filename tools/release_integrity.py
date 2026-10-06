from __future__ import annotations

"""Content-addressed release artifacts shared by every V3 release gate."""

import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DRIVE_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class ArtifactRecord:
    path: str
    role: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True)
class ArtifactInventory:
    records: tuple[ArtifactRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"files": [record.to_dict() for record in self.records]}


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    detail: str

    def __str__(self) -> str:
        suffix = f"：{self.path}" if self.path else ""
        return f"{self.code}{suffix}（{self.detail}）"


def _sha256_stream(stream: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _normalized_relative_path(value: str, *, reject_zip: bool = False) -> str:
    text = value.replace("\\", "/")
    if not text or text.endswith("/"):
        raise ValueError("路径为空或不是文件路径")
    if text.startswith("/") or DRIVE_RE.match(text):
        raise ValueError("拒绝绝对路径或盘符")
    parts = PurePosixPath(text).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("拒绝空段、当前目录或路径穿越")
    normalized = "/".join(parts)
    if reject_zip and normalized.lower().endswith(".zip"):
        raise ValueError("拒绝嵌套 ZIP")
    return normalized


def file_record(path: Path, *, root: Path | None = None, role: str = "artifact") -> ArtifactRecord:
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"artifact 必须是普通文件：{source}")
    relative = source.name if root is None else source.resolve().relative_to(Path(root).resolve()).as_posix()
    normalized = _normalized_relative_path(relative)
    with source.open("rb") as stream:
        digest, size = _sha256_stream(stream)
    return ArtifactRecord(path=normalized, role=role, sha256=digest, size_bytes=size)


def _ensure_unique(records: Iterable[ArtifactRecord]) -> tuple[ArtifactRecord, ...]:
    result = sorted(records, key=lambda item: item.path.encode("utf-8"))
    seen: dict[str, str] = {}
    for record in result:
        key = record.path.casefold()
        previous = seen.get(key)
        if previous is not None:
            raise ValueError(f"路径重复或大小写折叠冲突：{previous} / {record.path}")
        seen[key] = record.path
    return tuple(result)


def directory_inventory(root: Path, *, role: str = "package_file") -> ArtifactInventory:
    directory = Path(root)
    if not directory.is_dir():
        raise ValueError(f"inventory 根目录不存在：{directory}")
    records: list[ArtifactRecord] = []
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"拒绝符号链接：{path}")
        if path.is_file():
            if path.suffix.lower() == ".zip":
                raise ValueError(f"拒绝嵌套 ZIP：{path}")
            records.append(file_record(path, root=directory, role=role))
    return ArtifactInventory(_ensure_unique(records))


def _zip_member_path(info: zipfile.ZipInfo) -> str:
    raw = info.filename
    if "\\" in raw:
        raise ValueError(f"ZIP 条目不得使用反斜杠：{raw}")
    return _normalized_relative_path(raw, reject_zip=True)


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


def validate_zip_safety(path: Path) -> list[Issue]:
    issues: list[Issue] = []
    try:
        with zipfile.ZipFile(path) as archive:
            seen_exact: set[str] = set()
            seen_folded: dict[str, str] = {}
            for info in archive.infolist():
                if info.is_dir():
                    continue
                try:
                    normalized = _zip_member_path(info)
                except ValueError as exc:
                    issues.append(Issue("unsafe_zip_path", info.filename, str(exc)))
                    continue
                if normalized in seen_exact:
                    issues.append(Issue("duplicate_zip_entry", normalized, "ZIP 含重复条目"))
                seen_exact.add(normalized)
                folded = normalized.casefold()
                previous = seen_folded.get(folded)
                if previous is not None and previous != normalized:
                    issues.append(Issue("casefold_collision", normalized, f"与 {previous} 大小写折叠冲突"))
                seen_folded[folded] = normalized
                if _zip_is_symlink(info):
                    issues.append(Issue("zip_symlink", normalized, "ZIP 含符号链接"))
                if info.flag_bits & 0x1:
                    issues.append(Issue("encrypted_zip_entry", normalized, "ZIP 条目已加密，无法稳定审计"))
            bad_member = archive.testzip()
            if bad_member:
                issues.append(Issue("corrupt_zip_entry", bad_member, "CRC 校验失败"))
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        issues.append(Issue("invalid_zip", str(path), type(exc).__name__))
    return issues


def zip_inventory(path: Path, *, role: str = "package_file") -> ArtifactInventory:
    issues = validate_zip_safety(path)
    if issues:
        raise ValueError("; ".join(str(issue) for issue in issues))
    records: list[ArtifactRecord] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            normalized = _zip_member_path(info)
            with archive.open(info, "r") as stream:
                digest, size = _sha256_stream(stream)
            if size != info.file_size:
                raise ValueError(f"ZIP 条目解码大小不一致：{normalized}")
            records.append(ArtifactRecord(normalized, role, digest, size))
    return ArtifactInventory(_ensure_unique(records))


def compare_inventories(expected: ArtifactInventory, actual: ArtifactInventory) -> list[Issue]:
    expected_map = {record.path.casefold(): record for record in expected.records}
    actual_map = {record.path.casefold(): record for record in actual.records}
    issues: list[Issue] = []
    for key in sorted(expected_map.keys() - actual_map.keys()):
        record = expected_map[key]
        issues.append(Issue("missing_file", record.path, "actual inventory 缺少文件"))
    for key in sorted(actual_map.keys() - expected_map.keys()):
        record = actual_map[key]
        issues.append(Issue("unexpected_file", record.path, "actual inventory 多出文件"))
    for key in sorted(expected_map.keys() & actual_map.keys()):
        wanted = expected_map[key]
        found = actual_map[key]
        if wanted.path != found.path:
            issues.append(Issue("path_case_mismatch", found.path, f"预期路径为 {wanted.path}"))
        if wanted.sha256 != found.sha256 or wanted.size_bytes != found.size_bytes:
            issues.append(Issue("content_mismatch", wanted.path, "SHA256 或大小不一致"))
    return issues


def canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
