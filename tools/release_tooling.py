from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "stock_tool" / "config" / "release_tool_lock.json"
TOOLCACHE = ROOT / ".toolcache"
BIN_DIR = TOOLCACHE / "bin"
DOWNLOAD_DIR = TOOLCACHE / "downloads"
EXECUTABLES = {"cosign": "cosign.exe", "gitleaks": "gitleaks.exe", "syft": "syft.exe"}
RELEASE_PYTHON = TOOLCACHE / "release-venv" / "Scripts" / "python.exe"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tool_lock() -> dict[str, Any]:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def executable(tool: str) -> Path:
    return BIN_DIR / EXECUTABLES[tool]


def verify_cached_tools() -> list[str]:
    errors: list[str] = []
    lock = tool_lock()["tools"]
    for name in EXECUTABLES:
        record = lock[name]
        asset = DOWNLOAD_DIR / record["asset"]
        if not asset.is_file():
            errors.append(f"{name} 固定 asset 尚未下载")
            continue
        if sha256_file(asset) != record["sha256"]:
            errors.append(f"{name} asset SHA256 与受控工具锁不一致")
    return errors


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Stocklens-G1.3"})
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def install_locked_tools() -> None:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    lock = tool_lock()["tools"]
    for name in EXECUTABLES:
        record = lock[name]
        asset = DOWNLOAD_DIR / record["asset"]
        if not asset.is_file() or sha256_file(asset) != record["sha256"]:
            _download(record["asset_url"], asset)
        if sha256_file(asset) != record["sha256"]:
            raise RuntimeError(f"{name} 下载文件 SHA256 与受控工具锁不一致，拒绝执行。")
        checksum_url = record["asset_url"].rsplit("/", 1)[0] + "/" + record["checksum_asset"]
        checksum = DOWNLOAD_DIR / record["checksum_asset"]
        if not checksum.is_file() or sha256_file(checksum) != record["checksum_sha256"]:
            _download(checksum_url, checksum)
        if sha256_file(checksum) != record["checksum_sha256"]:
            raise RuntimeError(f"{name} 官方 checksum 文件 SHA256 不一致。")
        expected_line = next((line for line in checksum.read_text(encoding="utf-8").splitlines() if line.rstrip().endswith(record["asset"])), "")
        if not expected_line or expected_line.split()[0].lower() != record["sha256"]:
            raise RuntimeError(f"{name} asset 未被官方 checksum 正确绑定。")
        target = executable(name)
        if asset.suffix.lower() == ".zip":
            with zipfile.ZipFile(asset) as archive:
                member = next((item for item in archive.infolist() if Path(item.filename).name.lower() == target.name.lower()), None)
                if member is None or ".." in Path(member.filename).parts:
                    raise RuntimeError(f"{name} ZIP 不含预期可执行文件。")
                target.write_bytes(archive.read(member))
        else:
            shutil.copy2(asset, target)
    expected_versions = {"cosign": "v3.0.6", "gitleaks": "8.28.0", "syft": "1.44.0"}
    for name, expected in expected_versions.items():
        completed = subprocess.run([str(executable(name)), "version"], text=True, capture_output=True, timeout=30)
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0 or expected not in output:
            raise RuntimeError(f"{name} 版本自检失败；期望 {expected}。")
    python_tools = {name: lock[name]["version"] for name in ("jsonschema", "pip-audit")}
    if not RELEASE_PYTHON.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(RELEASE_PYTHON.parents[1])], check=True, cwd=ROOT)
    probe = subprocess.run(
        [str(RELEASE_PYTHON), "-c", "import importlib.metadata as m; print(m.version('jsonschema')); print(m.version('pip-audit'))"],
        text=True,
        capture_output=True,
        cwd=ROOT,
    )
    expected_probe = f"{python_tools['jsonschema']}\n{python_tools['pip-audit']}"
    if probe.returncode != 0 or probe.stdout.strip() != expected_probe:
        subprocess.run(
            [str(RELEASE_PYTHON), "-m", "pip", "install", "--disable-pip-version-check", *(f"{name}=={version}" for name, version in python_tools.items())],
            check=True,
            cwd=ROOT,
        )
        verify = subprocess.run(
            [str(RELEASE_PYTHON), "-c", "import importlib.metadata as m; print(m.version('jsonschema')); print(m.version('pip-audit'))"],
            text=True,
            capture_output=True,
            cwd=ROOT,
            check=True,
        )
        if verify.stdout.strip() != expected_probe:
            raise RuntimeError("隔离 release venv 的 Python 工具版本自检失败。")


if __name__ == "__main__":
    install_locked_tools()
    print("[OK] 固定发布安全工具已校验并安装到 .toolcache/bin")
