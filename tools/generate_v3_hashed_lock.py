from __future__ import annotations

"""Regenerate requirements-v3.lock hashes from an explicitly downloaded wheelhouse."""

import argparse
import hashlib
import re
from pathlib import Path

from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "requirements-v3.lock"
REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")


def _artifact_identity(path: Path) -> tuple[str, str]:
    if path.suffix == ".whl":
        name, version, _build, _tags = parse_wheel_filename(path.name)
    else:
        name, version = parse_sdist_filename(path.name)
    return canonicalize_name(name), str(version)


def generate(wheelhouse: Path, lock_path: Path = LOCK_PATH) -> None:
    source_lines = lock_path.read_text(encoding="utf-8").splitlines()
    comments = [line for line in source_lines if line.startswith("#") and "Install with:" not in line]
    requirements: list[tuple[str, str]] = []
    for line in source_lines:
        match = REQUIREMENT_RE.match(line.strip())
        if match:
            requirements.append((match.group(1), match.group(2)))
    artifacts: dict[tuple[str, str], list[Path]] = {}
    for path in wheelhouse.iterdir():
        if not path.is_file() or not (path.name.endswith(".whl") or path.name.endswith((".tar.gz", ".zip"))):
            continue
        artifacts.setdefault(_artifact_identity(path), []).append(path)
    output = comments + ["# Install with: python -m pip install --require-hashes -r requirements-v3.lock"]
    for name, version in requirements:
        matched = sorted(artifacts.get((canonicalize_name(name), version), []), key=lambda item: item.name)
        if not matched:
            raise RuntimeError(f"wheelhouse 缺少锁定 artifact：{name}=={version}")
        output.append(f"{name}=={version} \\")
        for index, artifact in enumerate(matched):
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            suffix = " \\" if index < len(matched) - 1 else ""
            output.append(f"    --hash=sha256:{digest}{suffix}")
    lock_path.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheelhouse", default=str(ROOT / ".toolcache" / "wheels"))
    args = parser.parse_args()
    generate(Path(args.wheelhouse).resolve())
    print(f"[OK] 已生成 hash-checking lock：{LOCK_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
